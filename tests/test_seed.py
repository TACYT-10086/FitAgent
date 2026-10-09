"""校验 db/seed.py 真的把数据灌进了 PostgreSQL（默认执行，不跳过）。"""

import pytest

from db import seed
from db.connection import get_db_conn

# 期望值按需求硬编码，刻意不 import seed.COUNTS：
# 否则把 seed 里的常量调小，测试会跟着一起变绿，等于没测。
EXPECTED_COUNTS = {
    "members": 20,
    "coaches": 5,
    "courses": 10,
    "schedules": 50,
    "bookings": 30,
    "transactions": 40,
    "policies": 10,
    "knowledge_docs": 20,
}


@pytest.fixture(scope="session", autouse=True)
def seeded_db():
    """整个测试会话开始前灌一次数据。seed 是幂等的，重复跑无副作用。"""
    seed.main()


def test_members_table_returns_data(seeded_db):
    """members 表既要有数据，数据本身也要是像样的。"""
    with get_db_conn() as conn, conn.cursor() as cur:
        cur.execute("select count(*) from members")
        assert cur.fetchone()[0] == 20

        cur.execute("select name, phone from members order by id limit 3")
        rows = cur.fetchall()

    assert len(rows) == 3
    for name, phone in rows:
        assert name and name.strip(), "会员姓名为空"
        assert len(phone) == 11, f"手机号位数不对: {phone!r}"


@pytest.mark.parametrize("table,expected", EXPECTED_COUNTS.items())
def test_seed_counts_match_spec(seeded_db, table, expected):
    with get_db_conn() as conn, conn.cursor() as cur:
        cur.execute(f"select count(*) from {table}")
        actual = cur.fetchone()[0]

    assert actual == expected, f"{table}: 期望 {expected} 行，实际 {actual} 行"


@pytest.mark.parametrize("table", ("knowledge_docs", "policies"))
def test_category_and_content_are_not_swapped(seeded_db, table):
    """分类是短词、正文是长文本。

    只数行数的测试抓不到「INSERT 列顺序写反」这类错误 —— knowledge_docs 真的插反过一次，
    行数依然是对的。所以这里按内容形状再校验一遍。
    """
    with get_db_conn() as conn, conn.cursor() as cur:
        cur.execute(f"select title, category, content from {table}")
        rows = cur.fetchall()

    assert rows, f"{table} 表是空的"
    for title, category, content in rows:
        assert len(category) <= 6, f"{table} 的 {title!r} 中 category 不像分类词: {category!r}"
        assert len(content) > len(category), \
            f"{table} 的 {title!r} 中 content 比 category 还短，多半是插反了"
