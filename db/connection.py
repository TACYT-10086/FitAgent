"""PostgreSQL 连接封装。"""

import psycopg

from config import require_env

# 连不上要快速失败，不能让调用方（尤其 pytest）挂死
CONNECT_TIMEOUT = 5


def get_db_conn(dsn: str | None = None) -> psycopg.Connection:
    """新建一个到 FitAgent 库的 psycopg 连接。

    不传 dsn 时从 .env 的 DATABASE_URL 读。调用方负责 close()。
    """
    return psycopg.connect(
        dsn or require_env("DATABASE_URL"),
        connect_timeout=CONNECT_TIMEOUT,
    )
