"""DeepSeek 对话封装（OpenAI SDK 兼容）。

Key 从 .env 的 DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL 读，模型固定 deepseek-chat。
客户端懒建（放在 _client() 里而不是模块级）：import 本模块不该依赖 .env 已就绪，
否则单测只要 import 就会炸。
"""

import json
from typing import Any

import openai

from config import get_env, require_env

DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_TIMEOUT = 60.0
# 中文 + JSON 输出留足余量；deepseek-chat 单次输出上限 8192
DEFAULT_MAX_TOKENS = 2048

# 错误信息里回显原文的上限。别把整坨响应塞进日志——既难读，也可能带敏感内容。
_PREVIEW = 200


class LLMError(RuntimeError):
    """llm.py 对外统一抛的异常。

    继承 RuntimeError，与 rag/embedding.py 的风格保持一致：调用方只需要接 RuntimeError。
    """


class _Retryable(LLMError):
    """可重试的失败：DeepSeek 偶发空内容、模型偶尔吐出不合法 JSON。"""


def _client() -> openai.OpenAI:
    return openai.OpenAI(
        api_key=require_env("DEEPSEEK_API_KEY"),
        base_url=get_env("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL),
        timeout=DEFAULT_TIMEOUT,
        # 重试策略由 chat_json 自己控制，别和 SDK 的内置重试叠两层
        max_retries=0,
    )


def _preview(text: str) -> str:
    return (text or "").strip().replace("\n", " ")[:_PREVIEW]


def _strip_fence(text: str) -> str:
    """剥掉 markdown 代码围栏（```json ... ```）。没有围栏就原样返回。"""
    text = (text or "").strip()
    if not text.startswith("```"):
        return text

    body = text[3:]
    # 干掉紧跟围栏的语言标记：```json / ```JSON（仅当它是单独一行时才敢删）
    newline = body.find("\n")
    if newline != -1:
        head = body[:newline].strip()
        if not head or head.isalpha():
            body = body[newline + 1 :]

    body = body.rstrip()
    if body.endswith("```"):
        body = body[:-3]
    return body.strip()


def _extract_json(text: str) -> dict[str, Any]:
    """从模型回复里抠出 JSON 对象。

    三层兜底：直接解析 → 剥 ```围栏 → 截取首个 { 到末个 } 的子串。
    全失败抛 _Retryable，错误信息带上原文前 200 字，方便排查又不会灌爆日志。
    """
    raw = text or ""

    candidates = [raw]
    stripped = _strip_fence(raw)
    if stripped != raw:
        candidates.append(stripped)

    first, last = raw.find("{"), raw.rfind("}")
    if 0 <= first < last:
        candidates.append(raw[first : last + 1])

    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed

    raise _Retryable(f"无法从模型回复中解析出 json 对象：{_preview(text)}")


def _map_error(exc: Exception, timeout: float) -> LLMError:
    # 注意顺序：APITimeoutError 是 APIConnectionError 的子类，
    # AuthenticationError / RateLimitError 是 APIStatusError 的子类，特例必须先判。
    if isinstance(exc, openai.AuthenticationError):
        return LLMError("DeepSeek 鉴权失败，检查 .env 的 DEEPSEEK_API_KEY")
    if isinstance(exc, openai.APITimeoutError):
        return LLMError(f"DeepSeek 超时（>{timeout}s）")
    if isinstance(exc, openai.APIConnectionError):
        return LLMError(f"连不上 DeepSeek：{exc}")
    if isinstance(exc, openai.RateLimitError):
        return LLMError("DeepSeek 限流，稍后重试")
    if isinstance(exc, openai.APIStatusError):
        # 400「prompt 必须包含 json 字样」这类错误会落到这里，原文对排查至关重要
        return LLMError(f"DeepSeek 返回 HTTP {exc.status_code}: {_preview(str(exc))}")
    return LLMError(f"调用 DeepSeek 失败：{exc}")


def _create(
    messages: list[dict],
    *,
    response_format: dict | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    timeout: float | None = None,
) -> str:
    kwargs: dict[str, Any] = {
        "model": DEFAULT_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens or DEFAULT_MAX_TOKENS,
    }
    if response_format is not None:
        kwargs["response_format"] = response_format

    client = _client()
    try:
        resp = client.chat.completions.create(**kwargs)
    except Exception as exc:
        raise _map_error(exc, timeout or DEFAULT_TIMEOUT) from exc

    choices = getattr(resp, "choices", None) or []
    if not choices:
        raise _Retryable(f"DeepSeek 返回里没有 choices：{_preview(str(resp))}")

    content = getattr(choices[0].message, "content", None)
    if not content or not content.strip():
        # DeepSeek 官方已知会偶发返回空串
        raise _Retryable("DeepSeek 返回了空内容")
    return content


def chat(
    messages: list[dict],
    *,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    timeout: float | None = None,
) -> str:
    """普通对话，返回模型回复的纯文本。"""
    return _create(
        messages, temperature=temperature, max_tokens=max_tokens, timeout=timeout
    )


# 这条 system 消息是必须的，不是为了「引导格式」：DeepSeek 的 json_object 模式
# 要求 prompt 里出现字面量 "json"，否则请求直接 400。
_JSON_GUARD = "你必须只输出一个合法的 json 对象，不要输出任何解释、前后缀或 markdown 围栏。"


def chat_json(
    messages: list[dict],
    *,
    schema_hint: str | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    timeout: float | None = None,
    retries: int = 1,
) -> dict[str, Any]:
    """强制模型输出 JSON 对象，返回解析好的 dict。

    统一在这里补一条含 "json" 字样的 system 消息兜底，调用方就不用各自操心
    DeepSeek 那个 400 了。解析失败/空内容会重试 retries 次；
    鉴权、限流、超时这类重试无意义的错误直接抛出，不浪费调用。
    """
    guard = _JSON_GUARD
    if schema_hint:
        guard = f"{guard}\n输出格式样例：\n{schema_hint}"
    payload = [{"role": "system", "content": guard}, *messages]

    last: _Retryable | None = None
    for attempt in range(retries + 1):
        if attempt == 0:
            attempt_messages = payload
        else:
            attempt_messages = [
                *payload,
                {"role": "user", "content": "上次输出不是合法的 json，请只返回一个 json 对象。"},
            ]

        try:
            text = _create(
                attempt_messages,
                response_format={"type": "json_object"},
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
            )
            return _extract_json(text)
        except _Retryable as exc:
            last = exc

    raise last
