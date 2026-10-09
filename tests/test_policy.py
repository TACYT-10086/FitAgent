"""grade_documents 与 search_policy 的真调用测试。

本文件只放真打 DeepSeek 的用例，不做任何打桩：评分质量、检索循环的
「选策略 -> 检索 -> 评分 -> 换策略重试」都得让模型真的判断才算数。
因此两个用例都带 llm 标记，默认被 conftest 跳过，设 RUN_LLM_TESTS=1 才跑
（花钱、结果不确定、依赖网络）。
"""

import pytest

from rag.retriever import grade_documents
from rag.transform import available_strategies
from tools import search_policy as sp


# --------------------------------------------------------------- 真打 DeepSeek（默认跳过）
@pytest.mark.llm
def test_llm_grade_documents_judges_real_pairs():
    relevant_doc = {
        "id": 1,
        "title": "场馆营业时间",
        "content": "本馆营业时间为周一至周五 06:00-23:00，周六周日 08:00-22:00。",
        "category": "场馆",
        "source": "场馆手册",
        "score": 0.9,
    }
    unrelated_doc = {
        "id": 2,
        "title": "储物柜租用规则",
        "content": "临时储物柜免费使用，需自备挂锁；长期储物柜按月 100 元租用。",
        "category": "场馆",
        "source": "场馆手册",
        "score": 0.9,
    }

    assert grade_documents("场馆几点开门，晚上几点关？", [relevant_doc])["sufficient"] is True
    assert grade_documents("场馆几点开门，晚上几点关？", [unrelated_doc])["sufficient"] is False


@pytest.mark.llm
def test_llm_search_policy_end_to_end():
    result = sp.search_policy("会员卡有几种，各自多少钱？", "member")

    assert set(result) >= {"question", "role", "strategy_used", "sufficient", "docs", "rounds"}
    assert result["rounds"][0]["strategy"] in available_strategies()
    assert 1 <= len(result["rounds"]) <= 2
