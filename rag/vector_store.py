"""Milvus 向量库客户端封装。"""

from pymilvus import MilvusClient

from config import require_env


def get_milvus_client(db_name: str | None = None) -> MilvusClient:
    """新建一个 MilvusClient，URI 从 .env 的 MILVUS_URI 读。

    db_name 不传时走 default 库（保持原有行为）；知识库集合在 rag_fitagent 库里，
    读 .env 的 DB_NAME 传进来即可。
    """
    return MilvusClient(uri=require_env("MILVUS_URI"), db_name=db_name or "")
