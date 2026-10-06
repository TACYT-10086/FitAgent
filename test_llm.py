"""llm.py 的纯单测：全程 monkeypatch 掉客户端，不发任何网络请求。"""

from types import SimpleNamespace

import httpx
import openai
import pytest

import llm
from llm import _extract_json

BASE = "https://api.deepseek.com/chat/completions"


# --------------------------------------------------------------- 测试替身
def _response(status_code: int) -> httpx.Response:
    return httpx.Response(
        status_code,
        request=httpx.Request("POST", BASE),
        json={"error": {"message": "boom"}},
    )


def _client_returning(content: str, captured: dict | None = None):
    """返回固定 content 的假客户端；传 captured 可回捞 create() 的实参。"""

    class _Completions:
        def create(self, **kwargs):
            if captured is not None:
                captured.update(kwargs)
            message = SimpleNamespace(content=content)
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    return SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))


def _client_raising(exc: Exception):
    class _Completions:
        def create(self, **kwargs):
            raise exc

    return SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))


# --------------------------------------------------------------- _extract_json
def test_extract_json_plain():
    assert _extract_json('{"a": 1}') == {"a": 1}


@pytest.mark.parametrize(
    "text",
    [
        '```json\n{"a": 1}\n```',
        '```\n{"a": 1}\n```',
        '```{"a": 1}```',
    ],
)
def test_extract_json_strips_markdown_fence(text):
    assert _extract_json(text) == {"a": 1}


def test_extract_json_tolerates_surrounding_prose():
    text = '好的，结果如下：\n{"grades": [{"index": 0, "relevant": true}]}\n希望有帮助。'
    assert _extract_json(text) == {"grades": [{"index": 0, "relevant": True}]}


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "完全不是 json",
        '{"a": 1, "b":',          # 截断
        "[1, 2, 3]",              # 是合法 JSON 但不是对象
    ],
)
def test_extract_json_raises_on_unusable(text):
    with pytest.raises(RuntimeError):
        _extract_json(text)


def test_extract_json_error_preview_is_truncated():
    """错误信息只回显前 200 字，别把整坨响应灌进日志。"""
    with pytest.raises(RuntimeError) as exc:
        _extract_json("x" * 1000)

    message = str(exc.value)
    assert "x" * 200 in message
    assert "x" * 201 not in message


# --------------------------------------------------------------- chat
def test_chat_returns_content(monkeypatch):
    monkeypatch.setattr(llm, "_client", lambda: _client_returning("你好"))
    assert llm.chat([{"role": "user", "content": "hi"}]) == "你好"


def test_chat_empty_content_raises(monkeypatch):
    """DeepSeek 偶发返回空串，必须当成失败而不是把空串当答案返回。"""
    monkeypatch.setattr(llm, "_client", lambda: _client_returning("   "))
    with pytest.raises(RuntimeError):
        llm.chat([{"role": "user", "content": "hi"}])


@pytest.mark.parametrize(
    "make_exc,expect_in_message",
    [
        (
            lambda: openai.AuthenticationError("bad key", response=_response(401), body=None),
            "DEEPSEEK_API_KEY",
        ),
        (
            lambda: openai.RateLimitError("slow down", response=_response(429), body=None),
            "限流",
        ),
        (
            lambda: openai.APITimeoutError(request=httpx.Request("POST", BASE)),
            "超时",
        ),
        (
            lambda: openai.APIConnectionError(request=httpx.Request("POST", BASE)),
            "连不上",
        ),
        (
            lambda: openai.BadRequestError("must contain json", response=_response(400), body=None),
            "HTTP 400",
        ),
    ],
)
def test_chat_maps_openai_errors_to_runtime_error(monkeypatch, make_exc, expect_in_message):
    monkeypatch.setattr(llm, "_client", lambda: _client_raising(make_exc()))

    with pytest.raises(RuntimeError) as exc:
        llm.chat([{"role": "user", "content": "hi"}])

    assert expect_in_message in str(exc.value)


# --------------------------------------------------------------- chat_json
def test_chat_json_enables_json_mode_and_prompt_mentions_json(monkeypatch):
    """DeepSeek 的 json_object 模式要求 prompt 里出现字面量 json，否则 400。"""
    captured = {}
    monkeypatch.setattr(llm, "_client", lambda: _client_returning('{"ok": true}', captured))

    result = llm.chat_json([{"role": "user", "content": "给我结果"}])

    assert result == {"ok": True}
    assert captured["response_format"] == {"type": "json_object"}
    joined = " ".join(m["content"] for m in captured["messages"])
    assert "json" in joined.lower()


def test_chat_json_includes_schema_hint(monkeypatch):
    captured = {}
    monkeypatch.setattr(llm, "_client", lambda: _client_returning('{"ok": 1}', captured))

    llm.chat_json(
        [{"role": "user", "content": "x"}],
        schema_hint='{"queries": ["..."]}',
    )

    joined = " ".join(m["content"] for m in captured["messages"])
    assert "queries" in joined


def test_chat_json_tolerates_fenced_output(monkeypatch):
    monkeypatch.setattr(llm, "_client", lambda: _client_returning('```json\n{"a": 1}\n```'))
    assert llm.chat_json([{"role": "user", "content": "x"}]) == {"a": 1}


def test_chat_json_retries_bad_json_then_raises(monkeypatch):
    calls = {"n": 0}

    class _Completions:
        def create(self, **kwargs):
            calls["n"] += 1
            message = SimpleNamespace(content="完全不是 json")
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(
        llm, "_client", lambda: SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    )

    with pytest.raises(RuntimeError):
        llm.chat_json([{"role": "user", "content": "x"}], retries=1)

    assert calls["n"] == 2  # 首次 + 1 次重试


def test_chat_json_does_not_retry_authentication_error(monkeypatch):
    """鉴权失败重试多少次都是白烧调用，必须一次就抛。"""
    calls = {"n": 0}

    class _Completions:
        def create(self, **kwargs):
            calls["n"] += 1
            raise openai.AuthenticationError("bad key", response=_response(401), body=None)

    monkeypatch.setattr(
        llm, "_client", lambda: SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    )

    with pytest.raises(RuntimeError):
        llm.chat_json([{"role": "user", "content": "x"}], retries=3)

    assert calls["n"] == 1
