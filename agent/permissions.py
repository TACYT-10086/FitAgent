"""权限与身份模型：谁是谁、谁能用哪个工具、跨用户访问怎么拦。

设计原则（CLAUDE.md 的硬性要求：工具必须在执行前自己校验权限，不能依赖模型判断）：

- 校验发生在工具函数的**第一行**，没过门之前不碰数据库、不调 LLM；
- 会话身份（role / user_id）由会话层注入，**绝不放进模型可见的工具参数**——
  否则就成了「让模型自己声明我是谁」，等于没有权限校验；
- 一律 fail-closed：没登记的工具、没带主体的角色、越权的 member_id，全部拒绝。

本模块只依赖标准库，因此可以被任何层安全 import，不会引入循环依赖。
"""

from __future__ import annotations

from dataclasses import dataclass

ROLE_MEMBER = "member"
ROLE_COACH = "coach"
ROLE_ADMIN = "admin"

ALL_ROLES: frozenset[str] = frozenset({ROLE_MEMBER, ROLE_COACH, ROLE_ADMIN})

# 工具 → 允许执行的角色。这是权限的唯一事实来源。
# 没登记的工具一律拒绝（fail-closed）：新工具忘了登记，结果是被拒，而不是被放行。
TOOL_ROLES: dict[str, frozenset[str]] = {
    "query_schedule": ALL_ROLES,
    "book_class": frozenset({ROLE_MEMBER, ROLE_ADMIN}),
    "cancel_booking": frozenset({ROLE_MEMBER, ROLE_ADMIN}),
    "get_member_balance": frozenset({ROLE_MEMBER, ROLE_ADMIN}),
    "recommend_course": ALL_ROLES,
    "generate_daily_report": frozenset({ROLE_COACH, ROLE_ADMIN}),
    "search_policy": ALL_ROLES,
}


class PermissionDenied(PermissionError):
    """权限校验失败。

    继承 PermissionError，与 tools/search_policy.py::check_permission 的异常风格保持一致：
    上层即使不 import 本模块，也能用 except PermissionError 兜住。
    """


@dataclass(frozen=True)
class SessionContext:
    """一次会话已认证的身份。

    user_id：member 对应 members.id，coach 对应 coaches.id；
    admin 在数据库里没有实体（没有 users / admins 表），所以是 None。
    """

    role: str
    user_id: int | None = None
    session_id: str | None = None

    def __post_init__(self) -> None:
        if self.role not in ALL_ROLES:
            raise ValueError(f"未知角色 {self.role!r}，允许：{sorted(ALL_ROLES)}")
        # admin 没有实体，允许 user_id 为空；其余角色必须带主体 id，否则后面对不上记录
        if self.role != ROLE_ADMIN and self.user_id is None:
            raise ValueError(f"角色 {self.role} 的会话必须携带 user_id")


def require_role(tool_name: str, session: SessionContext) -> None:
    """工具的第一行调用：不通过就抛 PermissionDenied，不做任何后续动作。

    fail-closed 体现在两处：工具名没登记 → 拒绝；角色不在允许列表 → 拒绝。
    """
    allowed = TOOL_ROLES.get(tool_name)
    if allowed is None:
        raise PermissionDenied(f"未知工具 {tool_name!r}，拒绝执行")

    if session.role not in allowed:
        raise PermissionDenied(
            f"工具 {tool_name} 不允许角色 {session.role!r} 执行；允许的角色：{sorted(allowed)}"
        )


def resolve_member_id(session: SessionContext, requested: int | None) -> int:
    """把入参里的 member_id 归一到会话主体，防止越权访问他人数据。

    - member：不传就用本人 id；**传了别人的 id 直接拒绝**，而不是静默覆盖——
      越权尝试应该是一个能被发现的事件，不应该被悄悄"修正"掉。
    - admin ：数据库里没有 admin 实体，推断不出"本人"，所以必须显式指定目标会员。
    - coach ：会员维度的操作本就不允许 coach 执行，require_role 会更早拦下，这里兜底。
    """
    if session.role == ROLE_MEMBER:
        if requested is None:
            # SessionContext.__post_init__ 已保证 member 必然带 user_id
            assert session.user_id is not None
            return session.user_id
        if requested != session.user_id:
            raise PermissionDenied(
                f"会员只能操作自己的数据：会话主体是 {session.user_id}，请求的是 {requested}"
            )
        return requested

    if session.role == ROLE_ADMIN:
        if requested is None:
            raise PermissionDenied(
                "管理员没有对应的会员实体，必须在参数里显式指定 member_id 才能操作会员数据"
            )
        return requested

    raise PermissionDenied(f"角色 {session.role!r} 不允许操作会员维度的数据")


def resolve_coach_id(session: SessionContext, requested: int | None) -> int | None:
    """把入参里的 coach_id 归一到会话主体，用于「教练只能看自己带的课」。

    - coach：传了别的教练 → 拒绝；不传 → 用本人 id。
    - admin：不限制，None 表示看全场。
    """
    if session.role == ROLE_COACH:
        assert session.user_id is not None
        if requested is not None and requested != session.user_id:
            raise PermissionDenied(
                f"教练只能查看自己带的课程：会话主体是 {session.user_id}，请求的是 {requested}"
            )
        return session.user_id

    return requested
