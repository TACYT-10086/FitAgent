"""LangGraph 适配器：把第 3 周手写的 Agent Loop 接到 `langgraph dev` / Agent Chat UI。

这里只做状态转换，不含任何业务逻辑：
- `messages` 给网页端渲染；
- `agent_history` 保存 `run_agent` 需要的 loop 协议历史（assistant 的 JSON 决策 + 工具观察），
  刻意不放进 `messages`，免得网页端出现一屏 JSON。

业务代码（agent/、rag/）不在这里改写；身份与权限仍由 agent.permissions 负责。

持久化：`langgraph dev` 由 langgraph.json 的 `checkpointer` 钩子接管（见 checkpointer.py），
导出的 `graph` 刻意**不带** checkpointer —— local_dev 模式会拒绝自带 checkpointer 的已编译图。
`build_graph(checkpointer=...)` 只留给直接嵌入/FastAPI 场景使用。
"""

from __future__ import annotations

import json
import os
from typing import Annotated, Any, NotRequired, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from agent.loop import run_agent
from agent.permissions import ROLE_ADMIN, ROLE_MEMBER, SessionContext

DEFAULT_ROLE = ROLE_MEMBER
DEFAULT_USER_ID = 1


class AgentState(TypedDict):
    """图状态。

    messages 由 `add_messages` 累加，是 Agent Chat UI 看到的对话；
    agent_history 普通覆盖式字段，是 run_agent 的私有上下文。
    role / user_id 允许调用方（SDK 或 UI）显式注入；不传则用默认会员身份。
    """

    messages: Annotated[list[AnyMessage], add_messages]
    agent_history: NotRequired[list[dict[str, Any]]]
    role: NotRequired[str]
    user_id: NotRequired[int | None]


def _text(message: AnyMessage) -> str:
    """取出消息的纯文本。兼容字符串和 LangChain 1.x 的 content blocks。"""
    content = message.content
    if isinstance(content, str):
        return content

    parts: list[str] = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts)


def _last_user_index(messages: list[AnyMessage]) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        if isinstance(messages[index], HumanMessage):
            return index
    return None


def _history_from_messages(messages: list[AnyMessage]) -> list[dict[str, Any]]:
    """客户端一次性传入整段对话时的兜底：把 UI 消息翻译成 loop 协议历史。

    loop 约定 assistant 消息是 JSON 决策，所以把 UI 里的纯文本答复包成
    `{"action": "respond", ...}`，避免模型下一轮看到不符合协议的裸文本。
    """
    history: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, HumanMessage):
            history.append({"role": "user", "content": _text(message)})
        elif isinstance(message, AIMessage):
            decision = {"action": "respond", "message": _text(message)}
            history.append(
                {"role": "assistant", "content": json.dumps(decision, ensure_ascii=False)}
            )
    return history


def _identity(state: AgentState, config: RunnableConfig | None) -> SessionContext:
    """按 state → configurable → 环境变量 → 默认值的优先级组装会话身份。"""
    configurable = (config or {}).get("configurable") or {}

    role = (
        state.get("role")
        or configurable.get("role")
        or os.getenv("AGENT_CHAT_ROLE")
        or DEFAULT_ROLE
    )

    raw_user_id: Any = state.get("user_id")
    if raw_user_id in (None, ""):
        raw_user_id = configurable.get("user_id")
    if raw_user_id in (None, ""):
        raw_user_id = os.getenv("AGENT_CHAT_USER_ID")

    if raw_user_id in (None, ""):
        # admin 在数据库里没有实体，允许为空；其它角色默认会员 1。
        user_id = None if role == ROLE_ADMIN else DEFAULT_USER_ID
    else:
        user_id = int(raw_user_id)

    return SessionContext(role=role, user_id=user_id)


def agent_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """调用一次 run_agent，并把它需要的私有历史写回 state。"""
    messages = list(state.get("messages") or [])
    user_index = _last_user_index(messages)
    if user_index is None:
        return {"messages": [AIMessage(content="请先发送一条消息。")]}

    user_message = _text(messages[user_index])
    history = state.get("agent_history") or _history_from_messages(messages[:user_index])

    try:
        session = _identity(state, config)
    except (TypeError, ValueError) as exc:
        return {"messages": [AIMessage(content=f"会话身份配置无效：{exc}")]}

    configurable = (config or {}).get("configurable") or {}
    thread_id = configurable.get("thread_id")
    session_id = f"lg-{thread_id}" if thread_id else None

    response = run_agent(
        user_message,
        session,
        history=history,
        session_id=session_id,
    )

    # run_agent 在 clarify / answered 时直接返回，不会把本轮决策写进 history；
    # 这里补回去，否则下一轮模型看不到自己刚问过什么。
    action = "clarify" if response.status == "clarify" else "respond"
    new_history = list(response.history)
    new_history.append(
        {
            "role": "assistant",
            "content": json.dumps(
                {"action": action, "message": response.message}, ensure_ascii=False
            ),
        }
    )

    return {
        "messages": [AIMessage(content=response.message)],
        "agent_history": new_history,
    }


def build_graph(checkpointer: BaseCheckpointSaver | None = None):
    """组装并编译图。

    checkpointer 只在直接嵌入（自己管理生命周期）时传入；
    `langgraph dev` 下由平台通过 langgraph.json:checkpointer 注入，这里必须保持 None。
    """
    builder = StateGraph(AgentState)
    builder.add_node("agent", agent_node)
    builder.add_edge(START, "agent")
    builder.add_edge("agent", END)
    return builder.compile(checkpointer=checkpointer)


graph = build_graph()
