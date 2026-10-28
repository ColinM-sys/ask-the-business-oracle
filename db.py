"""Oracle AI Database connection from env settings (.env). `python db.py` prints the server version."""
import ipaddress
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


def _local_host(host):
    if not isinstance(host, str):
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _values(value):
    return value if isinstance(value, list) else [value]


def connect():
    global _pool
    with _pool_lock:
        if _pool is None:
            _load_env()
            kw = dict(user=os.environ["ORACLE_USER"], password=os.environ["ORACLE_PASSWORD"])
            wallet = os.environ.get("ORACLE_WALLET_DIR")
            if wallet:
                kw.update(config_dir=wallet, wallet_location=wallet, wallet_password=os.environ.get("ORACLE_WALLET_PASSWORD"))
            params = oracledb.PoolParams(config_dir=wallet)
            params.parse_connect_string(os.environ["ORACLE_DSN"])
            hosts, protocols = _values(params.host), _values(params.protocol)
            if (len(hosts) != len(protocols) or
                    any(not host or (protocol != "tcps" and not _local_host(host))
                        for host, protocol in zip(hosts, protocols)) or
                    not all(match is True for match in _values(params.ssl_server_dn_match)) or
                    getattr(params, "use_eztls", False) or params.ssl_context is not None):
                raise ValueError("Oracle connections require TCPS and certificate validation for remote hosts")
            # Pass the parsed parameters so a DSN cannot override the checks above.
            _pool = oracledb.create_pool(params=params, **kw)
    # Connection.close()/context exit rolls back and returns the connection to the pool.
    return _pool.acquire()


if __name__ == "__main__":
    with connect() as con:
        print("connected:", con.version)
