"""手写 Agent Loop：意图判断 → 工具选择 → 硬权限校验 → 执行 → 多轮澄清 → 失败重试。

不依赖 LangChain（CLAUDE.md 明确要求 Agent Loop 必须手写）。由于 llm.py 只提供
chat / chat_json、没有 function calling，工具选择走「prompt 里列工具清单 → 模型用 JSON
返回决策」的路线，与 tools/search_policy.py::_choose_strategy 同构。

步数语义：一次循环 = 一次模型决策，工具执行发生在该步内部、不单独计步。
所以「最多 8 步」就是最多 8 次决策；chat_json 自带 retries=1，最坏情况下模型调用翻倍。

多轮澄清做成**无阻塞**的：需要追问时直接返回 status="clarify" 并带上待问的问题，
调用方把用户回答作为下一次 run_agent 的 user_message、把返回的 history 原样传回来即可。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from agent.permissions import ROLE_ADMIN, ROLE_COACH, ROLE_MEMBER, TOOL_ROLES, SessionContext
from agent.tools import ToolResult, call_tool, model_params, tool_names
from llm import LLMError, chat_json

MAX_STEPS = 8  # 逻辑步上限
MAX_TOOL_ERRORS = 3  # 工具失败重试上限，独立于步数预算
OBSERVATION_LIMIT = 4000  # 单条工具观察回填给模型的字符上限
HISTORY_LIMIT = 40  # 返回给调用方的历史消息条数上限，避免无限膨胀

_SCHEMA_HINT = (
    '{"action": "tool", "tool": "query_schedule", '
    '"params": {"date": "2026-10-15"}, "reason": "用户想查某天的课"}'
)

_ROLE_LABEL = {ROLE_MEMBER: "会员", ROLE_COACH: "教练", ROLE_ADMIN: "管理员"}

_TOOL_SUMMARY: dict[str, str] = {
    "query_schedule": "查询排课场次（开课时间、教练、场地、已约人数/容量）",
    "book_class": "为会员预约一个场次",
    "cancel_booking": "取消一个已有预约",
    "get_member_balance": "查询会员的会籍状态与消费概览",
    "recommend_course": "推荐未来可预约的场次",
    "generate_daily_report": "生成某一天的运营报表",
    "search_policy": "检索并回答场馆规章制度",
}

_PARAM_DOC: dict[str, str] = {
    "date": "date=YYYY-MM-DD（不传日期时默认只看今天及以后，查历史需显式指定）",
    "date_from": "date_from=YYYY-MM-DD",
    "date_to": "date_to=YYYY-MM-DD",
    "category": "category=yoga|strength|cardio|pilates|boxing|dance",
    "coach_id": "coach_id=整数",
    "keyword": "keyword=关键词",
    "only_available": "only_available=true|false",
    "limit": "limit=整数",
    "difficulty": "difficulty=beginner|intermediate|advanced",
    "schedule_id": "schedule_id=整数",
    "booking_id": "booking_id=整数",
    "member_id": "member_id=整数（管理员必填，用于指定目标会员）",
    "question": "question=用户原话",
    "top_k": "top_k=整数",
    "strategy": "strategy=rewrite|multi_query|hyde",
}


@dataclass
class AgentResponse:
    """一轮 run_agent 的结果。

    status：
      answered  —— 已有最终答复
      clarify   —— 需要向用户追问，pending_question 就是问题
      error     —— 调用模型失败或工具连续失败
      max_steps —— 用完了 MAX_STEPS 步仍未收敛
    history 刻意**不含** system 消息，方便下一轮原样回传（避免 system 重复叠加）。
    """

    status: str
    message: str
    session_id: str
    steps: int
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    pending_question: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)


def _describe_tools(role: str) -> str:
    """按角色列出可用工具。

    可用性直接读 permissions.TOOL_ROLES、参数直接读 tools.model_params，
    所以 prompt 里写的和实际能执行/能传的永远一致 —— 不会出现"prompt 说有、实际被拒"。
    """
    lines: list[str] = []
    for name in tool_names():
        if role not in TOOL_ROLES.get(name, frozenset()):
            continue
        params = sorted(model_params(name, role))
        shown = "、".join(_PARAM_DOC.get(p, p) for p in params) if params else "（无参数）"
        lines.append(f"{len(lines) + 1}. {name} —— {_TOOL_SUMMARY.get(name, '')}；参数：{shown}")
    return "\n".join(lines)


def _system_prompt(session: SessionContext) -> str:
    if session.user_id is None:
        identity = f"{_ROLE_LABEL[session.role]}（role={session.role}，数据库中没有对应实体）"
    else:
        identity = f"{_ROLE_LABEL[session.role]}（role={session.role}，user_id={session.user_id}）"

    return "\n".join(
        [
            "你是健身/游泳场馆的智能运营助手 FitAgent 的调度器。",
            f"当前会话身份：{identity}。身份由系统注入，你无法更改，也不需要复述。",
            "",
            "你可以调用下列工具访问真实数据；除此之外不得凭空编造场馆数据（排课、名额、金额尤其不能猜）：",
            _describe_tools(session.role),
            "",
            "严格按以下协议返回 json，每次只做一个动作：",
            '1. 需要查或改数据：{"action":"tool","tool":"<工具名>","params":{...},"reason":"一句话理由"}',
            '2. 信息不足或指代不明（例如没说哪一天、哪个场次）：{"action":"clarify","message":"要追问用户的问题"}',
            '3. 已经可以回答：{"action":"respond","message":"用中文组织好的最终答复"}',
            "",
            "规则：",
            "- 参数不全就 clarify 问清楚，不要瞎猜日期或场次。",
            "- 工具返回 ok=false 时，看清 error 再用正确参数重试，或换工具，或如实告诉用户做不到。",
            "- 如果提示权限不足，重试同一个工具不会有任何作用，请改为如实说明并给出可行建议。",
            "- 会员只能操作自己的数据，这由系统强制；不要试图代其他会员预约或查询。",
            "- 最终答复用中文、简洁自然，不要输出 json。",
        ]
    )


def _assistant_message(decision: dict[str, Any]) -> dict[str, Any]:
    """把模型的决策原样回填成 assistant 消息，保持对话协议自洽。"""
    return {"role": "assistant", "content": json.dumps(decision, ensure_ascii=False, default=str)}


def _nudge(text: str) -> dict[str, Any]:
    """协议用错时的纠正提示。这里用 user 而不是 system：system 已定稿，插在中间反而乱。"""
    return {"role": "user", "content": text}


def _observation(name: str, result: ToolResult) -> str:
    """把工具结果回填给模型。

    刻意不用 tool role —— 那需要 function calling 的 tool_call_id，llm.py 不支持。
    """
    payload = json.dumps(result.to_dict(), ensure_ascii=False, default=str)
    if len(payload) > OBSERVATION_LIMIT:
        payload = payload[:OBSERVATION_LIMIT] + "…（已截断）"

    if result.ok:
        return f"【{name} 的返回】{payload}"
    if result.error_kind == "permission":
        return (
            f"【{name} 被拒绝】{payload}\n"
            "（这是权限限制，重试同一个工具不会成功，请不要重试，改为如实告知用户并给出可行建议。）"
        )
    return f"【{name} 返回失败】{payload}\n（请根据 error 修正参数后重试，或改用其它工具。）"


def run_agent(
    user_message: str,
    session: SessionContext,
    *,
    history: list[dict[str, Any]] | None = None,
    session_id: str | None = None,
) -> AgentResponse:
    """跑一轮 Agent Loop，返回 AgentResponse。

    history 传入上一轮返回的 history 即可实现多轮澄清；system 消息每轮重新拼，
    所以不要把它塞进 history。
    """
    sid = session_id or f"s-{uuid.uuid4().hex[:8]}"
    messages: list[dict[str, Any]] = [{"role": "system", "content": _system_prompt(session)}]
    messages += list(history or [])
    messages.append({"role": "user", "content": str(user_message)})

    tool_calls: list[dict[str, Any]] = []
    tool_errors = 0
    steps = 0

    def reply(status: str, message: str, *, pending: str | None = None) -> AgentResponse:
        return AgentResponse(
            status=status,
            message=message,
            session_id=sid,
            steps=steps,
            tool_calls=tool_calls,
            pending_question=pending,
            history=messages[1:][-HISTORY_LIMIT:],
        )

    for step in range(1, MAX_STEPS + 1):
        steps = step

        try:
            decision = chat_json(list(messages), schema_hint=_SCHEMA_HINT)
        except LLMError as exc:
            return reply("error", f"调用模型失败，无法继续：{exc}")

        action = str(decision.get("action") or "").strip()

        if action == "clarify":
            question = str(decision.get("message") or "").strip()
            if question:
                return reply("clarify", question, pending=question)
            messages.append(_assistant_message(decision))
            messages.append(_nudge("clarify 动作必须带 message，写清你要向用户追问什么。"))
            continue

        if action == "respond":
            answer = str(decision.get("message") or "").strip()
            if answer:
                return reply("answered", answer)
            messages.append(_assistant_message(decision))
            messages.append(_nudge("respond 动作必须带 message，给出给用户的最终答复。"))
            continue

        if action != "tool":
            messages.append(_assistant_message(decision))
            messages.append(
                _nudge("action 只能是 tool / clarify / respond 三者之一，且必须是合法 json，请重新返回。")
            )
            continue

        name = str(decision.get("tool") or "").strip()
        raw_params = decision.get("params")
        if raw_params is not None and not isinstance(raw_params, dict):
            messages.append(_assistant_message(decision))
            messages.append(_nudge("params 必须是一个 json 对象（键值对），请重新返回。"))
            continue
        params: dict[str, Any] = raw_params or {}

        result = call_tool(name, session, params, session_id=sid, step=step)
        messages.append(_assistant_message(decision))
        messages.append({"role": "user", "content": _observation(name, result)})
        tool_calls.append(
            {
                "step": step,
                "tool": name,
                "params": params,
                "ok": result.ok,
                "error_kind": result.error_kind,
                "error": result.error,
            }
        )

        if result.ok:
            continue

        # 权限问题不是"参数写错了"，重试没有意义，也不该占用重试预算
        if result.error_kind == "permission":
            continue

        tool_errors += 1
        if tool_errors > MAX_TOOL_ERRORS:
            return reply(
                "error",
                f"连续 {tool_errors} 次工具调用都没成功，我先停下来。最后一次的问题：{result.error}",
            )

    return reply(
        "max_steps",
        f"抱歉，我尝试了 {MAX_STEPS} 步还是没能得到完整结果。"
        "你可以把需求说得更具体一些（比如具体日期、课程名），或者拆成几个问题分别问我。",
    )
