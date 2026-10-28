"""Load data/oracle_load.sql into the database from .env. `python load.py`"""
import os

from db import connect

sql = open(os.path.join(os.path.dirname(__file__), "data", "oracle_load.sql"), encoding="utf-8").read()
with connect() as con:
    cur = con.cursor()
    for stmt in (s.strip() for s in sql.split(";\n")):
        if stmt and not stmt.startswith("--"):
            cur.execute(stmt.rstrip(";"))
    con.commit()
    for t in ("sales", "costs", "customers", "discounts", "products", "regions"):
        print(t, cur.execute(f"select count(*) from {t}").fetchone()[0])
