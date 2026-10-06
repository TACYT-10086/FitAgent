"""pytest 全局配置。

标记 llm 的用例要真实打 DeepSeek：花钱、结果不确定、还依赖网络，
不该拖慢每次 pytest。所以默认跳过，显式设 RUN_LLM_TESTS=1 才放行。
"""

import os

import pytest

LLM_SKIP_REASON = (
    "需要真实调用 DeepSeek：设 RUN_LLM_TESTS=1 后再跑"
    "（PowerShell: $env:RUN_LLM_TESTS=1; pytest -m llm -v）"
)


def pytest_collection_modifyitems(config, items):
    if os.environ.get("RUN_LLM_TESTS") == "1":
        return

    skip = pytest.mark.skip(reason=LLM_SKIP_REASON)
    for item in items:
        if "llm" in item.keywords:
            item.add_marker(skip)
