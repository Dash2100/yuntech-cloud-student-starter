"""Throwaway local PostgreSQL for offline tests (initdb + pg_ctl in a temp dir, Unix socket only).

No AWS, no TLS: production always connects with sslmode=verify-full; this only checks the SQL.
Tests are skipped when the PostgreSQL server binaries are not installed.
"""
import atexit
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

_cluster = None


def available():
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        return False
    return bool(shutil.which("initdb") and shutil.which("pg_ctl"))


def connect_kwargs():
    """Start the cluster once per process; returns psycopg2.connect kwargs for its `inspection` DB."""
    global _cluster
    if _cluster is None:
        base = Path(tempfile.mkdtemp(prefix="pg-"))
        data, sock = base / "data", base / "s"
        sock.mkdir()
        quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "check": True}
        subprocess.run(["initdb", "-D", str(data), "-U", "test", "--auth=trust", "-E", "UTF8"], **quiet)
        subprocess.run(["pg_ctl", "-D", str(data), "-w", "-l", str(base / "log"), "-o",
                        f"-k {sock} -c listen_addresses='' -p 54329", "start"], **quiet)
        atexit.register(lambda: (subprocess.run(["pg_ctl", "-D", str(data), "-m", "immediate", "stop"],
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
                                 shutil.rmtree(base, ignore_errors=True)))
        subprocess.run(["createdb", "-h", str(sock), "-p", "54329", "-U", "test", "inspection"], **quiet)
        _cluster = {"host": str(sock), "port": 54329, "dbname": "inspection", "user": "test"}
    return dict(_cluster)


def fresh_store(service):
    """A DbStore on an empty events table."""
    import psycopg2
    kwargs = connect_kwargs()
    with psycopg2.connect(**kwargs) as conn, conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS events")
    conn.close()
    return service.DbStore(kwargs)
