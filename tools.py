"""Constrained, read-only business SQL and Oracle vector policy retrieval."""
import csv
import json
from pathlib import Path

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import ScopeType, traverse_scope

from db import connect
from policies import embed, ensure_policies

TABLES = {"sales", "costs", "customers", "discounts", "products", "regions"}
FUNCTIONS = {"SUM", "MIN", "MAX", "AVG", "COUNT", "ROUND", "NULLIF", "COALESCE", "ABS", "AND", "OR"}

# CSV headers describe the governed tables loaded into Oracle.
SCHEMA = {}
for table in TABLES:
    with (Path(__file__).parent / "data" / f"{table}.csv").open(encoding="utf-8-sig", newline="") as source:
        SCHEMA[table] = {column: "UNKNOWN" for column in next(csv.reader(source))}


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
    if tree.find(exp.Lateral) or any(table.args.get("joins") for table in tree.find_all(exp.Table)):
        raise ValueError("Lateral sources and nested joins are refused")

    # Oracle may interpret an unresolved qualified column as a package call,
    # even when its qualifier matches a table alias. Resolve against each scope
    # and the governed schema before allowing the database to parse the SQL.
    scoped_tree = normalize_identifiers(tree.copy(), dialect="oracle")
    for select in scoped_tree.find_all(exp.Select):
        joins = select.args.get("joins") or []
        for index, join in enumerate(joins):
            later = {item.this.alias_or_name for item in joins[index + 1:]}
            if any(column.table in later for column in join.find_all(exp.Column)):
                raise ValueError("Column qualifier is not in scope")
    for scope in traverse_scope(scoped_tree):
        for column in scope.find_all(exp.Column):
            if column.db or column.catalog:
                raise ValueError("Qualified object calls are refused")
            if not column.table:
                continue
            current = scope
            while current and column.table not in {name for name, _ in current.references}:
                if not current.can_be_correlated:
                    current = None
                elif current.scope_type == ScopeType.DERIVED_TABLE:
                    # A derived table cannot see its immediate parent's sources.
                    parent = current.parent
                    current = parent.parent if parent and parent.can_be_correlated else None
                else:
                    current = current.parent
            if current is None:
                raise ValueError("Column qualifier is not in scope")
    try:
        qualify(scoped_tree, dialect="oracle", schema=SCHEMA, quote_identifiers=False)
    except sqlglot.errors.OptimizeError as exc:
        raise ValueError("Column does not resolve to a governed table") from exc
    return tree.sql(dialect="oracle", comments=False)


def run_sql(select, params=None):
    executable = validate_select(select)
    with connect() as con:
        with con.cursor() as cur:
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
        row = cur.execute("SELECT backend FROM policy_chunks FETCH FIRST 1 ROW ONLY").fetchone()
        if row is None:
            raise RuntimeError("No policy chunks available")
        backend = row[0]
        vector = embed(cur, text, backend)
        # Store the exact vector as JSON; SQL converts it back on execution/replay.
        params = {"query_vector": json.dumps(vector.tolist()), "k": k}
        sql = """SELECT id, doc, chunk, VECTOR_DISTANCE(embedding, TO_VECTOR(:query_vector), COSINE) AS distance
            FROM policy_chunks ORDER BY distance, id FETCH FIRST :k ROWS ONLY"""
        rows = cur.execute(sql, params).fetchall()
        return {"sql": sql, "params": params,
                "rows": [dict(zip(("id", "doc", "chunk", "distance"), row)) for row in rows]}
