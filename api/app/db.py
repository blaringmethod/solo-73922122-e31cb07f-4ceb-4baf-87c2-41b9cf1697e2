"""数据库连接池(启动时等待数据库就绪)."""
from __future__ import annotations

import os
import time

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://watershed:watershed@localhost:5432/watershed",
)

pool: ConnectionPool | None = None


def init_pool() -> None:
    global pool
    pool = ConnectionPool(
        DATABASE_URL,
        min_size=1,
        max_size=10,
        open=False,
        kwargs={"row_factory": dict_row},
    )
    deadline = time.monotonic() + 60
    while True:
        try:
            pool.open()
            with pool.connection() as conn:
                conn.execute("SELECT 1")
            return
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(1)


def close_pool() -> None:
    global pool
    if pool is not None:
        pool.close()
        pool = None


def get_pool() -> ConnectionPool:
    if pool is None:
        raise RuntimeError("connection pool is not initialized")
    return pool
