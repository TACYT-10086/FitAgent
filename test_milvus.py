"""真实连接 Milvus 的冒烟测试 —— 不 mock，也不 skip。"""

from rag.vector_store import get_milvus_client


def test_get_milvus_client_connects():
    client = get_milvus_client()

    assert isinstance(client.list_collections(), list)

    # get_server_version 会真的往服务端跑一个来回，
    # 光有本地对象是拿不到版本号的
    assert client.get_server_version()
