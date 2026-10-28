"""Constrained, read-only business SQL and Oracle vector policy retrieval."""
import json

import sqlglot
from sqlglot import exp

from db import connect
from policies import embed, ensure_policies

TABLES = {"sales", "costs", "customers", "discounts", "products", "regions"}
FUNCTIONS = {"SUM", "MIN", "MAX", "AVG", "COUNT", "ROUND", "NULLIF", "COALESCE", "ABS", "AND", "OR"}


def validate_select(sql):
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("Only a single read-only SELECT is allowed")
    try:
        statements = sqlglot.parse(sql, read="oracle")
    except sqlglot.errors.SqlglotError as exc:
        raise ValueError("Invalid SELECT") from exc
    # SQLGlot represents comments after a terminal delimiter as a Semicolon node.
    if len(statements) == 2 and isinstance(statements[-1], exp.Semicolon):
        statements.pop()
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise ValueError("Only a single read-only SELECT is allowed")
    tree = statements[0]
    for join in tree.find_all(exp.Join):
        if join.kind == "CROSS" or not (join.args.get("on") or join.args.get("using")):
            raise ValueError("Cartesian joins are refused; use an explicit join predicate")
        if join.args.get("on"):
            # Require a connecting equality in an AND clause, not ON 1=1 or
            # an equality hidden behind OR TRUE. Resource limits bound other work.
            predicates = join.args["on"].flatten() if isinstance(join.args["on"], exp.And) else [join.args["on"]]
            if not any(isinstance(p, exp.EQ)
                       and isinstance(p.left, exp.Column) and isinstance(p.right, exp.Column)
                       and p.left.table and p.right.table and p.left.table != p.right.table
                       and join.this.alias_or_name in {p.left.table, p.right.table}
                       for p in predicates):
                raise ValueError("Cartesian joins are refused; use a connecting column equality")
    if any(tree.find_all(exp.Into, exp.Lock, exp.Command, exp.Insert, exp.Update,
                         exp.Delete, exp.Create, exp.Drop, exp.Alter, exp.With)):
        raise ValueError("SQL writes, locks and procedural statements are refused")
    for table in tree.find_all(exp.Table):
        if table.name.lower() not in TABLES or table.db or table.catalog:
            raise ValueError("Only governed business tables are allowed")
    for function in tree.find_all(exp.Func):
        # CASE is represented as a function by the parser, but performs no calls.
        if isinstance(function, (exp.Case, exp.If)):
            continue
        if function.sql_name() not in FUNCTIONS:
            raise ValueError("Function is not in the read-only allowlist")
    if tree.find(exp.Dot) or any(c.name.lower() in {"nextval", "currval"} for c in tree.find_all(exp.Column)):
        raise ValueError("Object calls and sequence access are refused")
    return tree.sql(dialect="oracle", comments=False)


def run_sql(select, params=None):
    executable = validate_select(select)
    with connect() as con:
        con.call_timeout = 15000  # milliseconds per database round trip, including fetches
        with con.cursor() as cur:
            # Fail closed if the DBA has not installed/enabled the resource profile.
            cur.execute("SELECT value FROM v$parameter WHERE name = 'resource_limit'")
            if cur.fetchone()[0].upper() != "TRUE":
                raise ValueError("Database resource limits must be enabled")
            cur.execute("SELECT resource_name, limit FROM user_resource_limits "
                        "WHERE resource_name IN ('CPU_PER_CALL', 'LOGICAL_READS_PER_CALL')")
            limits = dict(cur.fetchall())
            for name, maximum in (("CPU_PER_CALL", 1000), ("LOGICAL_READS_PER_CALL", 100000)):
                value = limits.get(name, "")
                if not str(value).isdigit() or not 0 < int(value) <= maximum:
                    raise ValueError("Database resource profile must bound CPU and logical reads")
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(executable, params or {})
            columns = [d[0].lower() for d in cur.description]
            rows = cur.fetchmany(1001)
            if len(rows) > 1000:
                raise ValueError("Evidence exceeds the row limit; aggregate the query")
            return {"sql": select, "params": params or {},
                    "rows": [dict(zip(columns, row)) for row in rows]}


def search_policies(text, k=5):
    if not isinstance(k, int) or not 1 <= k <= 20:
        raise ValueError("k must be between one and twenty")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Search text is required")
    ensure_policies()
    with connect() as con, con.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        backend = cur.execute("SELECT backend FROM policy_chunks FETCH FIRST 1 ROW ONLY").fetchone()[0]
        vector = embed(cur, text, backend)
        # Store the exact vector as JSON; SQL converts it back on execution/replay.
        params = {"query_vector": json.dumps(vector.tolist()), "k": k}
        sql = """SELECT id, doc, chunk, VECTOR_DISTANCE(embedding, TO_VECTOR(:query_vector), COSINE) AS distance
            FROM policy_chunks ORDER BY distance, id FETCH FIRST :k ROWS ONLY"""
        rows = cur.execute(sql, params).fetchall()
        return {"sql": sql, "params": params,
                "rows": [dict(zip(("id", "doc", "chunk", "distance"), row)) for row in rows]}
