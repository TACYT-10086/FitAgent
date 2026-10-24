"""LangGraph 持久化适配层：把会话状态（messages / agent_history）写进 FitAgent 的 PostgreSQL。

为什么不把 checkpointer 传进 `build_graph()`：
`langgraph dev` 的 local_dev 模式会在加载时拒绝自带 checkpointer 的已编译 graph
（平台自己负责持久化）。所以这里走官方钩子 —— 在 langgraph.json 的 `checkpointer.path`
里引用 `create_checkpointer`，由服务器在启动时接管，替换默认的 in-memory 后端。

必须用 Async 版本：langgraph-api 只会调用 aget / aget_tuple / aput / aput_writes / alist。
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from config import require_env

# checkpointer-postgres 的硬性要求：
# - autocommit：setup() 的 migration 不能包在事务里；
# - prepare_threshold=0：连接池会跨事件循环复用连接，prepared statement 不能缓存。
_POOL_KWARGS = {"autocommit": True, "prepare_threshold": 0}

# setup() 会建的表，用于启动/初始化后自检。
CHECKPOINT_TABLES = (
    "checkpoint_migrations",
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
)


def _ensure_compatible_loop() -> None:
    """Windows 的 ProactorEventLoop 不支持 psycopg 异步连接，提前报错而不是等 30 秒池超时。

    `langgraph dev` 默认带 reload，uvicorn 会选 SelectorEventLoop，所以服务器侧没问题；
    这个检查主要保护 `--no-reload` 和自建事件循环的调用方。
    """
    if sys.platform != "win32":
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if isinstance(loop, asyncio.ProactorEventLoop):
        raise RuntimeError(
            "当前事件循环是 ProactorEventLoop，psycopg 异步连接不支持。"
            "请使用默认的 `langgraph dev`（reload 模式选 SelectorEventLoop），"
            "或先调用 asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())。"
        )


def _make_pool() -> AsyncConnectionPool:
    """建连接池。open=False：由进入 async with 的那一方在运行中的事件循环里打开。"""
    _ensure_compatible_loop()
    return AsyncConnectionPool(
        conninfo=require_env("DATABASE_URL"),
        min_size=1,
        max_size=5,
        kwargs=_POOL_KWARGS,
        open=False,
    )


@asynccontextmanager
async def create_checkpointer() -> AsyncIterator[AsyncPostgresSaver]:
    """langgraph.json 引用的工厂：建池 → 幂等建表 → 交给服务器托管，退出时关池。"""
    async with _make_pool() as pool:
        saver = AsyncPostgresSaver(pool)
        await saver.setup()  # 已建过的表不受影响，只补跑未执行的 migration
        yield saver


async def setup_checkpointer() -> list[str]:
    """单独初始化 Checkpointer 表结构，返回自检到的表名。

    用法：`python checkpointer.py`。服务启动时 create_checkpointer 也会自动执行同样的 setup()。
    """
    async with _make_pool() as pool:
        await AsyncPostgresSaver(pool).setup()

        async with pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = current_schema() AND tablename::text = ANY(%s) "
                "ORDER BY tablename",
                (list(CHECKPOINT_TABLES),),
            )
            rows = await cursor.fetchall()

    return [row[0] for row in rows]


if __name__ == "__main__":
    # asyncio.run 在 Windows 上默认创建 ProactorEventLoop，显式换成 Selector。
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    tables = asyncio.run(setup_checkpointer())
    print(f"Checkpointer 表已就绪：{', '.join(tables) or '（未找到，请检查 DATABASE_URL）'}")
