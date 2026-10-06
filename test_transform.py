"""rag/transform.py 的单测：LLM 全部打桩，只验策略逻辑与策略解析。"""

import pytest

from rag import transform as tf


@pytest.fixture
def stub_chat(monkeypatch):
    """把策略层的 chat 换成固定返回值，并记录被调次数。"""

    def _install(reply: str):
        calls = []

        def fake(*args, **kwargs):
            calls.append(kwargs.get("messages") or args[0])
            return reply

        monkeypatch.setattr(tf, "chat", fake)
        return calls

    return _install


@pytest.fixture
def stub_chat_json(monkeypatch):
    """同理，桩掉 chat_json。"""

    def _install(payload):
        calls = []

        def fake(*args, **kwargs):
            calls.append(kwargs.get("messages") or args[0])
            return payload

        monkeypatch.setattr(tf, "chat_json", fake)
        return calls

    return _install


# --------------------------------------------------------------- 三个策略
def test_rewrite_query_returns_single_query(stub_chat):
    calls = stub_chat("  场馆的营业时间是几点到几点  ")

    result = tf.rewrite_query("场馆几点开门")

    assert len(calls) == 1
    assert result == ["场馆的营业时间是几点到几点"]  # 顺手验证 strip


def test_rewrite_query_falls_back_to_original_when_model_returns_blank(stub_chat):
    stub_chat("   ")

    assert tf.rewrite_query("场馆几点开门") == ["场馆几点开门"]


def test_multi_query_dedups_and_drops_blanks(stub_chat_json):
    stub_chat_json({"queries": ["营业时间", "营业时间 ", "场馆几点开门", "", "   ", "开放时间"]})

    assert tf.multi_query("场馆几点开门") == ["营业时间", "场馆几点开门", "开放时间"]


def test_multi_query_caps_at_n(stub_chat_json):
    stub_chat_json({"queries": ["a", "b", "c", "d", "e"]})

    assert tf.multi_query("q", n=3) == ["a", "b", "c"]


def test_multi_query_falls_back_when_model_returns_nothing(stub_chat_json):
    stub_chat_json({"queries": []})

    assert tf.multi_query("场馆几点开门") == ["场馆几点开门"]


def test_multi_query_survives_missing_key(stub_chat_json):
    """模型不按 schema 来（少输出 queries 键）也不能崩。"""
    stub_chat_json({"result": "oops"})

    assert tf.multi_query("场馆几点开门") == ["场馆几点开门"]


def test_hyde_returns_one_hypothetical_document(stub_chat):
    stub_chat("本馆营业时间为周一至周五 06:00-23:00，周六周日 08:00-22:00。")

    result = tf.hyde("场馆几点开门")

    assert result == ["本馆营业时间为周一至周五 06:00-23:00，周六周日 08:00-22:00。"]


def test_hyde_falls_back_when_model_returns_blank(stub_chat):
    stub_chat("")

    assert tf.hyde("场馆几点开门") == ["场馆几点开门"]


# --------------------------------------------------------------- 策略注册表
def test_available_strategies_registers_all_three():
    assert set(tf.available_strategies()) == {"rewrite", "multi_query", "hyde"}


def test_at_least_two_strategies_so_retry_can_switch():
    """第 2 轮要求「换一个策略」，注册表少于 2 个就换不动了。"""
    assert len(tf.available_strategies()) >= 2


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("rewrite", "rewrite"),
        ("HyDE", "hyde"),
        (" hyde ", "hyde"),
        ("multi_query", "multi_query"),
        ("multi-query", "multi_query"),
        ("multi query", "multi_query"),
        ("MULTI-QUERY", "multi_query"),
        ("", tf.DEFAULT_STRATEGY),
        ("   ", tf.DEFAULT_STRATEGY),
        (None, tf.DEFAULT_STRATEGY),
        ("nonsense", tf.DEFAULT_STRATEGY),
    ],
)
def test_resolve_strategy_normalizes_and_falls_back(raw, expected):
    assert tf.resolve_strategy(raw) == expected


# --------------------------------------------------------------- transform 分发
def test_transform_dispatches_to_rewrite(monkeypatch):
    seen = []
    monkeypatch.setitem(tf.STRATEGIES, "rewrite", lambda q: seen.append(("rewrite", q)) or ["改写"])

    assert tf.transform("原问题", "rewrite") == ["改写"]
    assert seen == [("rewrite", "原问题")]


def test_transform_dispatches_to_hyde(monkeypatch):
    seen = []
    monkeypatch.setitem(tf.STRATEGIES, "hyde", lambda q: seen.append(("hyde", q)) or ["假想文档"])

    assert tf.transform("原问题", "HyDE") == ["假想文档"]
    assert seen == [("hyde", "原问题")]


def test_transform_falls_back_to_original_when_strategy_returns_empty(monkeypatch):
    monkeypatch.setitem(tf.STRATEGIES, "rewrite", lambda q: [])

    assert tf.transform("原问题", "rewrite") == ["原问题"]


def test_transform_unknown_strategy_uses_default(monkeypatch):
    seen = []
    monkeypatch.setitem(tf.STRATEGIES, "rewrite", lambda q: seen.append(q) or ["改写"])

    assert tf.transform("原问题", "完全不存在的策略") == ["改写"]
    assert seen == ["原问题"]


def test_transform_strips_and_drops_blanks(monkeypatch):
    monkeypatch.setitem(tf.STRATEGIES, "multi_query", lambda q: ["  a  ", "", "   ", "b"])

    assert tf.transform("原问题", "multi_query") == ["a", "b"]
