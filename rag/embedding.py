"""文本向量化：调用本机 Ollama 的 bge-m3。

DeepSeek 没有 embeddings 接口，所以向量化不走它，走本机 Ollama。
OLLAMA_URL / MODEL 先作为模块常量；要改成可配置的再挪进 .env。
"""

import requests

OLLAMA_URL = "http://localhost:11434"
MODEL = "bge-m3"
VECTOR_DIM = 1024

# 首次调用需要把模型加载进内存，超时给足
TIMEOUT = 120


def embed(texts: list[str]) -> list[list[float]]:
    """批量把文本编码成 1024 维向量。

    用 /api/embed 而不是 /api/embeddings：前者支持一次传多条，
    20 条文档一个请求就够，不用循环 20 次。
    """
    if not texts:
        return []

    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/embed",
            json={"model": MODEL, "input": texts},
            timeout=TIMEOUT,
        )
    except requests.exceptions.ConnectionError as exc:
        raise RuntimeError(
            f"连不上 Ollama（{OLLAMA_URL}）。请确认 ollama 服务已启动、"
            f"且已拉取模型：ollama pull {MODEL}"
        ) from exc
    except requests.exceptions.Timeout as exc:
        raise RuntimeError(
            f"Ollama 编码超时（>{TIMEOUT}s），模型可能还在加载中"
        ) from exc

    if resp.status_code != 200:
        raise RuntimeError(f"Ollama 返回 HTTP {resp.status_code}: {resp.text[:200]}")

    vectors = resp.json().get("embeddings")
    if vectors is None:
        raise RuntimeError(f"Ollama 响应里没有 embeddings 字段: {resp.text[:200]}")

    if len(vectors) != len(texts):
        raise RuntimeError(
            f"Ollama 返回 {len(vectors)} 条向量，与输入的 {len(texts)} 条不一致"
        )

    # 维度对不上必须立刻报错，别等到写 Milvus 时才炸
    for i, vector in enumerate(vectors):
        if len(vector) != VECTOR_DIM:
            raise RuntimeError(
                f"第 {i} 条向量维度为 {len(vector)}，期望 {VECTOR_DIM}；"
                f"模型 {MODEL} 与预期不符？"
            )

    return vectors
