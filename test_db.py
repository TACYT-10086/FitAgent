"""真实连接 PostgreSQL 的冒烟测试 —— 不 mock，也不 skip。"""

from db.connection import get_db_conn


def test_get_db_conn_connects_to_postgres():
    # psycopg3 的 with 块退出时会关闭连接，所以直接用 with 管生命周期
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("select 1")
            assert cur.fetchone() == (1,)

        # 确认连的确实是 DATABASE_URL 里指定的那个库
        assert conn.info.dbname == "FitAgent"
