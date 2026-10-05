"""项目级配置：从 .env 读环境变量。

用 dotenv_values() 而不是 load_dotenv()：前者返回 dict，可以对 key 和 value
统一 strip，这样 .env 里 "MILVUS_URI = http://..." 这种等号两侧带空格的写法
不会变成 os.getenv("MILVUS_URI ") 而取不到值。
"""

import os
from pathlib import Path

from dotenv import dotenv_values

ENV_PATH = Path(__file__).resolve().parent / ".env"

# .env 里读出来的原始值（key/value 都 strip）
_ENV = {k.strip(): (v or "").strip() for k, v in dotenv_values(ENV_PATH).items()}


def get_env(name: str, default: str | None = None) -> str | None:
    """取环境变量。真实环境变量优先于 .env；都没有则返回 default。"""
    if name in os.environ:
        return os.environ[name]
    return _ENV.get(name, default)


def require_env(name: str) -> str:
    """取必需的配置项，缺失就抛错，错误信息带上 .env 的路径方便排查。"""
    value = get_env(name)
    if not value:
        raise RuntimeError(f"缺少环境变量 {name}，请检查 {ENV_PATH}")
    return value
