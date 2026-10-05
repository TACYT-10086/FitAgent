"""最朴素的 RAG 检索：原问题不加工，直接编码后查 top-k。

这是基线，刻意不做查询改写、多路召回、重排。
"""

from config import require_env
from rag.embedding import embed
from rag.vector_store import get_milvus_client


def retrieve(query: str, top_k: int = 3) -> list[dict]:
    """检索与 query 最相关的 top_k 条知识文档。

    返回 [{"id","title","content","category","source","score"}, ...]，
    score 是余弦相似度，越大越相关。
    """
    db_name = require_env("DB_NAME")
    collection = require_env("COLLECTION_NAME")
    client = get_milvus_client(db_name=db_name)

    # 幂等；避免集合未加载时检索报错
    client.load_collection(collection)

    hits = client.search(
        collection_name=collection,
        data=[embed([query])[0]],
        limit=top_k,
        output_fields=["title", "content", "category", "source"],
    )

    return [
        {
            "id": hit["id"],
            "title": hit["entity"]["title"],
            "content": hit["entity"]["content"],
            "category": hit["entity"]["category"],
            "source": hit["entity"]["source"],
            "score": hit["distance"],
        }
        for hit in hits[0]
    ]
