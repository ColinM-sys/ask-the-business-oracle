"""Oracle AI Database connection from env settings (.env). `python db.py` prints the server version."""
import os
import threading

import oracledb

_pool = None
_pool_lock = threading.Lock()


def _load_env(path=os.path.join(os.path.dirname(__file__), ".env")):
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k, v)


def connect():
    global _pool
    with _pool_lock:
        if _pool is None:
            _load_env()
            kw = dict(user=os.environ["ORACLE_USER"], password=os.environ["ORACLE_PASSWORD"], dsn=os.environ["ORACLE_DSN"])
            wallet = os.environ.get("ORACLE_WALLET_DIR")
            if wallet:
                kw.update(config_dir=wallet, wallet_location=wallet, wallet_password=os.environ.get("ORACLE_WALLET_PASSWORD"))
            _pool = oracledb.create_pool(**kw)
    # Connection.close()/context exit rolls back and returns the connection to the pool.
    return _pool.acquire()


if __name__ == "__main__":
    with connect() as con:
        print("connected:", con.version)
