"""把 PostgreSQL 的 knowledge_docs 表向量化后写入 Milvus。

幂等：每次全量重建（drop + create），可反复执行。
用法：python -m rag.ingest
"""

from pymilvus import DataType

from config import require_env
from db.connection import get_db_conn
from rag.embedding import VECTOR_DIM, embed
from rag.vector_store import get_milvus_client

TITLE_MAX = 512
CONTENT_MAX = 8192
CATEGORY_MAX = 64
SOURCE_MAX = 128


def fetch_docs() -> list[dict]:
    """从 PostgreSQL 读全部知识文档。"""
    with get_db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "select id, title, content, category, source"
            "  from knowledge_docs order by id"
        )
        rows = cur.fetchall()

    return [
        {
            "id": row[0],
            "title": row[1],
            "content": row[2],
            "category": row[3],
            "source": row[4] or "",
        }
        for row in rows
    ]


def build_collection(client, name: str) -> None:
    """建集合。

    必须用 schema 模式：简化模式（create_collection(dimension=...)）只能建出
    id + vector 两个字段，存不下文本，检索结果就只剩个编号没法读。
    """
    schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field("id", DataType.INT64, is_primary=True)
    schema.add_field("vector", DataType.FLOAT_VECTOR, dim=VECTOR_DIM)
    schema.add_field("title", DataType.VARCHAR, max_length=TITLE_MAX)
    schema.add_field("content", DataType.VARCHAR, max_length=CONTENT_MAX)
    schema.add_field("category", DataType.VARCHAR, max_length=CATEGORY_MAX)
    schema.add_field("source", DataType.VARCHAR, max_length=SOURCE_MAX)

    index_params = client.prepare_index_params()
    # 向量在 Ollama 侧已做 L2 归一化（实测范数 = 1.0），余弦是自然选择
    index_params.add_index(
        field_name="vector", index_type="AUTOINDEX", metric_type="COSINE"
    )

    client.create_collection(
        collection_name=name, schema=schema, index_params=index_params
    )


def main() -> None:
    db_name = require_env("DB_NAME")
    collection = require_env("COLLECTION_NAME")
    client = get_milvus_client(db_name=db_name)

    docs = fetch_docs()
    if not docs:
        raise RuntimeError("knowledge_docs 表是空的，先跑一次 python -m db.seed")

    # 标题带主题信号、正文带细节，拼在一起编码，检索时更容易命中
    vectors = embed([f"{doc['title']}\n{doc['content']}" for doc in docs])

    # 幂等：全量重建，避免主键冲突或重复堆积
    if collection in client.list_collections():
        client.drop_collection(collection)
    build_collection(client, collection)

    rows = [
        {
            "id": doc["id"],
            "vector": vector,
            "title": doc["title"],
            "content": doc["content"],
            "category": doc["category"],
            "source": doc["source"],
        }
        for doc, vector in zip(docs, vectors)
    ]
    client.insert(collection_name=collection, data=rows)
    client.load_collection(collection)

    print(f"已写入 Milvus：{db_name}.{collection} 共 {len(rows)} 条")


if __name__ == "__main__":
    main()
