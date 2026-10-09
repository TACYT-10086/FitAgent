"""校验 rag/ingest.py 真的把知识文档灌进了 Milvus，且 retriever 检索得到。"""

import pytest

from config import require_env
from rag import ingest
from rag.retriever import retrieve, retrieve_many
from rag.vector_store import get_milvus_client

# 期望值硬编码，刻意不 import ingest 里的常量：
# 否则把常量调小，测试会跟着一起变绿，等于没测。
EXPECTED_DOCS = 20
COLLECTION = "knowledge_docs"


@pytest.fixture(scope="session", autouse=True)
def ingested():
    """整个测试会话开始前入库一次。ingest 幂等（drop + 重建），重复跑无副作用。"""
    ingest.main()


def test_collection_has_20_docs(ingested):
    client = get_milvus_client(db_name=require_env("DB_NAME"))
    client.flush(COLLECTION)  # 加这一行，强制落盘
    client.load_collection(COLLECTION)

    rows = client.query(COLLECTION, filter="", output_fields=["count(*)"])
    assert rows[0]["count(*)"] == EXPECTED_DOCS

    # 字段必须齐全：只存 id+vector 的话，检索出来只有个编号，没法用
    fields = {f["name"] for f in client.describe_collection(COLLECTION)["fields"]}
    assert {"id", "vector", "title", "content", "category", "source"} <= fields


def test_retriever_returns_results(ingested):
    results = retrieve("会员卡怎么退费", top_k=3)

    assert len(results) == 3
    for item in results:
        assert item["title"] and item["content"], f"检索结果字段为空: {item}"
        assert isinstance(item["score"], float)


def test_top_k_is_respected(ingested):
    assert len(retrieve("营业时间", top_k=1)) == 1


def test_retrieve_many_aligns_with_queries_and_matches_retrieve(ingested):
    """批量检索只是省掉重复的客户端构建/集合加载，结果必须与逐条 retrieve 一致。"""
    queries = ["场馆几点开门几点关门", "会员卡不想练了能退款吗"]

    batched = retrieve_many(queries, top_k=3)

    assert len(batched) == len(queries)
    for query, hits in zip(queries, batched):
        assert len(hits) == 3
        assert [h["id"] for h in hits] == [h["id"] for h in retrieve(query, top_k=3)]


def test_retrieve_many_empty_queries(ingested):
    assert retrieve_many([], top_k=3) == []


@pytest.mark.parametrize(
    "query,expected_title",
    [
        ("场馆几点开门几点关门", "场馆营业时间"),
        ("会员卡不想练了能退款吗", "退卡与退款政策"),
    ],
)
def test_retriever_finds_relevant_doc(ingested, query, expected_title):
    """语义相关性校验。

    只断言「返回了东西」是不够的 —— 向量插错、模型没生效、度量选错，这些情况下
    「返回了东西」依然成立（上一轮 knowledge_docs 列插反就是这么漏过去的）。
    """
    titles = [item["title"] for item in retrieve(query, top_k=3)]

    assert expected_title in titles, (
        f"query={query!r} 的 top-3 是 {titles}，其中没有 {expected_title!r}"
    )
