"""Constrained, read-only business SQL and Oracle vector policy retrieval."""
import json
from decimal import Decimal

import oracledb
import sqlglot
from sqlglot import exp

from db import connect
from policies import embed, ensure_policies

TABLES = {"SALES", "COSTS", "CUSTOMERS", "DISCOUNTS", "PRODUCTS", "REGIONS"}
FUNCTIONS = {"SUM", "MIN", "MAX", "AVG", "COUNT", "ROUND", "NULLIF", "COALESCE", "ABS", "AND", "OR"}


def _number_to_decimal(cursor, metadata):
    if metadata.type_code is oracledb.DB_TYPE_NUMBER:
        return cursor.var(Decimal, arraysize=cursor.arraysize)


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
    if any(tree.find_all(exp.Into, exp.Lock, exp.Command, exp.Insert, exp.Update,
                         exp.Delete, exp.Create, exp.Drop, exp.Alter, exp.With)):
        raise ValueError("SQL writes, locks and procedural statements are refused")
    for table in tree.find_all(exp.Table):
        name = table.name
        # Oracle preserves quoted case; only ASCII names can match these objects.
        resolved_name = name if table.this.args.get("quoted") else name.upper()
        if not name.isascii() or resolved_name not in TABLES or table.db or table.catalog:
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
        with con.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.outputtypehandler = _number_to_decimal
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
    for _ in range(3):
        backend, fingerprint = ensure_policies(include_fingerprint=True)
        with connect() as con, con.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            collection = cur.execute("SELECT DISTINCT backend, source_hash FROM policy_chunks").fetchall()
            if collection != [(backend, fingerprint)]:
                continue
            vector = embed(cur, text, backend)
            # Store the exact vector as JSON; SQL converts it back on execution/replay.
            params = {"query_vector": json.dumps(vector.tolist()), "k": k}
            sql = """SELECT id, doc, chunk, VECTOR_DISTANCE(embedding, TO_VECTOR(:query_vector), COSINE) AS distance
                FROM policy_chunks ORDER BY distance, id FETCH FIRST :k ROWS ONLY"""
            rows = cur.execute(sql, params).fetchall()
            return {"sql": sql, "params": params,
                    "rows": [dict(zip(("id", "doc", "chunk", "distance"), row)) for row in rows]}
    if not collection:
        raise RuntimeError("No policy chunks available")
    raise RuntimeError("Policy collection changed during retrieval")
