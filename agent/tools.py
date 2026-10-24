"""Agent 业务工具：用真实数据库连接完成预约、取消、查询与会籍管理。

设计约束（来自 CLAUDE.md）：
- 每个工具的**第一行**都是 require_role(...)，权限校验不依赖模型判断；
- session（role / user_id）由会话层注入，绝不作为模型可见参数传入；
- 工具统一返回 ToolResult，错误一律归一成中文可读信息，交给上层模型自纠。

业务规则不自己发明，直接对齐 db/seed.py 里 policies / knowledge_docs 的规章原文：
- 「团操课须提前预约，系统每日 20:00 开放次日起 7 天内的场次」（BOOKING_WINDOW_DAYS）
- 「课程开始前 4 小时以上可免费取消；不足 4 小时取消或未到场的，计为一次爽约」（FREE_CANCEL_HOURS）
- 「每自然月累计爽约 3 次，自次月起 7 天内暂停预约权限」（NO_SHOW_SUSPEND_*）
- 「会籍到期后未续费者，入场权限自动冻结」（members.status 与 expire_date 双查）

时间口径：库里全是 TIMESTAMPTZ，存的是 UTC instant；但「今天/某一天/自然月」这类业务
语义与返回给模型的时间字符串一律用北京时间（UTC+8）。任何"按天"的边界都在 Python 侧按
北京时间算好再传参，不用 ::date（那会依赖服务器/数据库时区，换个环境结果就漂）。
"""

from __future__ import annotations

import inspect
import json
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import psycopg

from agent.permissions import (
    ROLE_ADMIN,
    ROLE_COACH,
    ROLE_MEMBER,
    PermissionDenied,
    SessionContext,
    require_role,
    resolve_coach_id,
    resolve_member_id,
)
from db.connection import get_db_conn
from timeutil import BEIJING_TZ, iso_beijing, now_beijing, to_beijing
from tools.search_policy import search_policy as policy_search

# ---- 业务常量（对应场馆规章，改这里就等于改规则）--------------------------------

COURSE_CATEGORIES = frozenset({"yoga", "strength", "cardio", "pilates", "boxing", "dance"})
COURSE_DIFFICULTIES = frozenset({"beginner", "intermediate", "advanced"})

BOOKING_WINDOW_DAYS = 7  # 政策：只开放次日起 7 天内的场次
FREE_CANCEL_HOURS = 4  # 政策：开课前 4 小时以上可免费取消
NO_SHOW_SUSPEND_LIMIT = 3  # 政策：每自然月累计爽约 3 次
NO_SHOW_SUSPEND_DAYS = 7  # 政策：自次月起 7 天内暂停预约权限

DEFAULT_SCHEDULE_LIMIT = 20
MAX_SCHEDULE_LIMIT = 50
POLICY_DOC_CHARS = 600  # 单篇规章正文回传给模型的截断长度，避免撑爆上下文


# ---- 错误族与返回结构 -----------------------------------------------------------


class ToolError(RuntimeError):
    """工具业务错误基类。继承 RuntimeError，与项目其余模块的异常风格一致。"""


class ToolValidationError(ToolError):
    """参数非法或缺失。上层模型据此修正参数后重试。"""


class ToolNotFoundError(ToolError):
    """实体不存在（场次/会员/预约）。"""


class ToolConflictError(ToolError):
    """业务规则冲突：满员、重复预约、卡过期、状态不可取消等。"""


class ToolPermissionError(ToolError):
    """权限拒绝。由 PermissionDenied 归一而来，好让模型读懂而不是看到异常栈。"""


@dataclass
class ToolResult:
    """所有工具的统一返回。

    error_kind 让上层（loop）不必靠解析中文来区分错误性质：
    permission / validation / not_found / conflict / database / internal。
    """

    tool: str
    ok: bool
    data: dict[str, Any]
    error: str | None = None
    error_kind: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "ok": self.ok,
            "data": self.data,
            "error": self.error,
            "error_kind": self.error_kind,
        }


# ---- 模型可见参数白名单 ---------------------------------------------------------
# 这是安全边界的一部分：role / session 永远不会出现在这里；对 member 而言
# member_id 也不存在——模型就算生成了这个键，call_tool 也会直接拒绝执行。

_BASE_PARAMS: dict[str, frozenset[str]] = {
    "query_schedule": frozenset(
        {"date", "date_from", "date_to", "category", "coach_id", "keyword",
         "only_available", "limit"}
    ),
    "book_class": frozenset({"schedule_id"}),
    "cancel_booking": frozenset({"booking_id", "schedule_id"}),
    "get_member_balance": frozenset(),
    "recommend_course": frozenset({"category", "difficulty", "keyword", "limit"}),
    "generate_daily_report": frozenset({"date", "coach_id"}),
    "search_policy": frozenset({"question", "top_k", "strategy"}),
}

# 只有管理员能指定目标会员：其余角色要么用本人 id（member），要么根本不该调这个工具
_ROLE_EXTRA_PARAMS: dict[str, dict[str, frozenset[str]]] = {
    ROLE_ADMIN: {
        "book_class": frozenset({"member_id"}),
        "cancel_booking": frozenset({"member_id"}),
        "get_member_balance": frozenset({"member_id"}),
    },
}

# 教练没法指定别人的课：coach_id 对教练不可见，由工具内部强制成本人
_ROLE_DENIED_PARAMS: dict[str, dict[str, frozenset[str]]] = {
    ROLE_COACH: {"generate_daily_report": frozenset({"coach_id"})},
}


def model_params(tool_name: str, role: str) -> frozenset[str]:
    """工具对某个角色开放的「模型可见参数」白名单。"""
    allowed = _BASE_PARAMS.get(tool_name, frozenset())
    allowed |= _ROLE_EXTRA_PARAMS.get(role, {}).get(tool_name, frozenset())
    allowed -= _ROLE_DENIED_PARAMS.get(role, {}).get(tool_name, frozenset())
    return allowed


# ---- trace ---------------------------------------------------------------------
# .gitignore 已含 trace/*.jsonl，路径刻意放在项目根的 trace/ 下。

TRACE_PATH = Path(__file__).resolve().parent.parent / "trace" / "trace.jsonl"

_TRACE_LOCK = threading.Lock()


def append_trace(record: dict[str, Any]) -> None:
    """把一条工具调用记录追加到 trace/trace.jsonl（一行一个 JSON 对象）。

    trace 是旁路：写入失败一律吞掉，绝不能因为记日志失败而让工具调用失败。
    并发说明：threading.Lock 只保证**进程内**串行；多进程同时写同一文件时，
    Windows 上 open(..., "a") 的单行写多数情况够用，但不保证严格原子。
    """
    try:
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _TRACE_LOCK:
            TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
            # encoding=utf-8：Windows 默认 cp936，中文会乱码
            # newline="\n"：文本模式会把 \n 翻成 \r\n，破坏「一行一记录」
            with open(TRACE_PATH, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(line + "\n")
    except OSError:
        pass


# ---- 小工具函数 -----------------------------------------------------------------


def _iso(value: Any) -> str | None:
    """时间统一转北京时间再序列化；DATE 字段本身没有时区，原样输出。"""
    if isinstance(value, datetime):
        return iso_beijing(value)
    if isinstance(value, date):
        return value.isoformat()
    return None if value is None else str(value)


def _as_int(value: Any, field: str) -> int | None:
    """模型经常把数字写成字符串，这里统一收敛，别让 int("x") 抛裸 ValueError。"""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ToolValidationError(f"{field} 必须是整数，收到布尔值 {value!r}")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ToolValidationError(f"{field} 必须是整数，收到 {value!r}") from None


_TRUE_WORDS = frozenset({"true", "1", "yes", "y", "t", "是"})
_FALSE_WORDS = frozenset({"false", "0", "no", "n", "f", "", "否"})


def _as_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    raise ToolValidationError(f"{field} 必须是布尔值，收到 {value!r}")


def _day_start(day: str) -> datetime:
    """把 YYYY-MM-DD 解析成当日 00:00 的北京时间点（落库时等价于前一日 16:00 UTC）。"""
    try:
        parsed = date.fromisoformat(str(day).strip())
    except ValueError:
        raise ToolValidationError(f"日期格式必须是 YYYY-MM-DD，收到 {day!r}") from None
    return datetime(parsed.year, parsed.month, parsed.day, tzinfo=BEIJING_TZ)


def _day_bounds(day: str) -> tuple[datetime, datetime]:
    """返回北京时间某一天的 [00:00, 次日 00:00) 左闭右开区间。"""
    start = _day_start(day)
    return start, start + timedelta(days=1)


def _check_category(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text not in COURSE_CATEGORIES:
        raise ToolValidationError(
            f"课程类别必须是 {sorted(COURSE_CATEGORIES)} 之一，收到 {value!r}"
        )
    return text


def _check_difficulty(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text not in COURSE_DIFFICULTIES:
        raise ToolValidationError(
            f"难度必须是 {sorted(COURSE_DIFFICULTIES)} 之一，收到 {value!r}"
        )
    return text


def _clamp(value: Any, default: int, low: int, high: int, field: str) -> int:
    number = _as_int(value, field)
    if number is None:
        return default
    return max(low, min(number, high))


def _now() -> datetime:
    """当前北京时间。与库里 UTC instant 比较时是等价的绝对时间。"""
    return now_beijing()


def _month_start(moment: datetime) -> datetime:
    """moment 所在自然月的月初（按北京时间切月）。"""
    moment = to_beijing(moment)
    return datetime(moment.year, moment.month, 1, tzinfo=BEIJING_TZ)


def _shift_month(moment: datetime, months: int) -> datetime:
    """按自然月平移（只用于取月初，不用管天数溢出）。"""
    moment = to_beijing(moment)
    index = moment.year * 12 + (moment.month - 1) + months
    return datetime(index // 12, index % 12 + 1, 1, tzinfo=BEIJING_TZ)


_BOOKED_STATUSES = ("booked", "attended")


def _monthly_no_show_count(cur, member_id: int, moment: datetime) -> int:
    """统计某会员在当前自然月的爽约次数。"""
    cur.execute(
        """
        SELECT count(*)
        FROM bookings b JOIN schedules s ON s.id = b.schedule_id
        WHERE b.member_id = %s AND b.status = 'no_show'
          AND s.start_time >= %s AND s.start_time < %s
        """,
        (member_id, _month_start(moment), _shift_month(moment, 1)),
    )
    return int(cur.fetchone()[0])


def _booking_suspended(cur, member_id: int, moment: datetime) -> str | None:
    """判断会员是否处于「爽约停约」期，是则返回中文原因，否则 None。

    政策：「每自然月累计爽约 3 次，自次月起 7 天内暂停预约权限」。
    实现口径：看上一个自然月的累计爽约数，若 >= 3 且今天落在本月 1–7 号，即暂停。
    """
    if moment.day > NO_SHOW_SUSPEND_DAYS:
        return None

    prev_start = _shift_month(moment, -1)
    cur.execute(
        """
        SELECT count(*)
        FROM bookings b JOIN schedules s ON s.id = b.schedule_id
        WHERE b.member_id = %s AND b.status = 'no_show'
          AND s.start_time >= %s AND s.start_time < %s
        """,
        (member_id, prev_start, _month_start(moment)),
    )
    count = int(cur.fetchone()[0])
    if count < NO_SHOW_SUSPEND_LIMIT:
        return None

    return (
        f"上一个自然月（{prev_start:%Y-%m}）累计爽约 {count} 次，已达 {NO_SHOW_SUSPEND_LIMIT} 次上限，"
        f"按场馆规章本月 {NO_SHOW_SUSPEND_DAYS} 号前暂停预约权限"
    )


def _schedule_row(row: tuple) -> dict[str, Any]:
    """把 query_schedule / recommend_course 的行按位置映射成 dict（游标返回元组）。"""
    return {
        "schedule_id": row[0],
        "course": row[1],
        "category": row[2],
        "difficulty": row[3],
        "duration_min": row[4],
        "coach": row[5],
        "start_time": _iso(row[6]),
        "end_time": _iso(row[7]),
        "room": row[8],
        "capacity": row[9],
        "status": row[10],
        "booked_count": row[11],
        "available": max(row[9] - row[11], 0),
    }


# ---- 工具 1：query_schedule -----------------------------------------------------


def query_schedule(
    session: SessionContext,
    *,
    date: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    category: str | None = None,
    coach_id: Any = None,
    keyword: str | None = None,
    only_available: Any = False,
    limit: Any = None,
) -> ToolResult:
    """查询排课场次，附带**实时**已约人数（库里没有冗余计数字段，只能现算）。"""
    require_role("query_schedule", session)

    if date and (date_from or date_to):
        raise ToolValidationError("date 与 date_from/date_to 不能同时使用，请二选一")

    clauses: list[str] = []
    args: list[Any] = []

    if date:
        start, end = _day_bounds(str(date))
        clauses.append("s.start_time >= %s AND s.start_time < %s")
        args += [start, end]
    if date_from:
        clauses.append("s.start_time >= %s")
        args.append(_day_start(str(date_from)))
    if date_to:
        clauses.append("s.start_time < %s")
        args.append(_day_bounds(str(date_to))[1])

    # 没给任何时间条件时默认只看未来（含今天）。
    # 不加这个默认值的话，查询会把过去两周的历史场次一起捞出来，
    # 而模型极容易把它们当成「可预约」的场次报给用户（实测真的发生过）。
    # 想查历史就显式传 date 或 date_from —— 那是明确的意图，这里不替调用方猜。
    if not (date or date_from or date_to):
        clauses.append("s.start_time >= %s")
        args.append(_now())

    category = _check_category(category)
    if category:
        clauses.append("c.category = %s")
        args.append(category)

    coach = _as_int(coach_id, "coach_id")
    if coach is not None:
        clauses.append("s.coach_id = %s")
        args.append(coach)

    if keyword:
        clauses.append("(c.name ILIKE %s OR c.description ILIKE %s)")
        like = f"%{str(keyword).strip()}%"
        args += [like, like]

    if _as_bool(only_available, "only_available"):
        clauses.append("COALESCE(bk.n, 0) < s.capacity")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit = _clamp(limit, DEFAULT_SCHEDULE_LIMIT, 1, MAX_SCHEDULE_LIMIT, "limit")

    sql = f"""
        WITH booked AS (
            SELECT schedule_id,
                   count(*) FILTER (WHERE status IN ('booked', 'attended')) AS n
            FROM bookings
            GROUP BY schedule_id
        )
        SELECT s.id, c.name, c.category, c.difficulty, c.duration_min,
               co.name AS coach_name, s.start_time, s.end_time, s.room,
               s.capacity, s.status, COALESCE(bk.n, 0) AS booked_count
        FROM schedules s
        JOIN courses c ON c.id = s.course_id
        JOIN coaches co ON co.id = s.coach_id
        LEFT JOIN booked bk ON bk.schedule_id = s.id
        {where}
        ORDER BY s.start_time
        LIMIT %s
    """

    with get_db_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (*args, limit))
        rows = cur.fetchall()

    schedules = [_schedule_row(row) for row in rows]
    return ToolResult(
        "query_schedule",
        True,
        {
            "count": len(schedules),
            "schedules": schedules,
            "note": (
                "时间为北京时间（UTC+8）；booked_count 是实时统计的已约人数，available 是剩余名额。"
                "未指定日期时默认只返回今天及以后的场次；要查历史请显式传 date 或 date_from。"
            ),
        },
    )


# ---- 工具 2：book_class ---------------------------------------------------------


def book_class(
    session: SessionContext,
    *,
    schedule_id: Any,
    member_id: Any = None,
) -> ToolResult:
    """为会员预约一个团课场次。

    并发安全的关键在两步：先用 SELECT ... FOR UPDATE 锁住场次行让同一场次的预约串行化，
    **然后在锁内另起一条语句**统计已约人数。把 COUNT 写进上面那条加锁语句是个陷阱——
    READ COMMITTED 下它沿用加锁前的快照，看不到刚提交的预约，照样超卖。
    """
    require_role("book_class", session)
    target = resolve_member_id(session, _as_int(member_id, "member_id"))

    schedule = _as_int(schedule_id, "schedule_id")
    if schedule is None:
        raise ToolValidationError("schedule_id 必填")

    now = _now()
    try:
        with get_db_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.id, s.status, s.start_time, s.capacity, c.name
                FROM schedules s JOIN courses c ON c.id = s.course_id
                WHERE s.id = %s
                FOR UPDATE
                """,
                (schedule,),
            )
            row = cur.fetchone()
            if row is None:
                raise ToolNotFoundError(f"场次 {schedule} 不存在")

            _, sched_status, start_time, capacity, course_name = row
            if sched_status != "scheduled":
                reason = "已结束" if sched_status == "finished" else "已取消"
                raise ToolConflictError(f"该场次{reason}，无法预约")
            if start_time <= now:
                raise ToolConflictError(f"「{course_name}」已经开课，无法预约")
            if start_time > now + timedelta(days=BOOKING_WINDOW_DAYS):
                raise ToolConflictError(
                    f"「{course_name}」开课时间超过 {BOOKING_WINDOW_DAYS} 天，尚未开放预约"
                    f"（系统每日 20:00 开放次日起 {BOOKING_WINDOW_DAYS} 天内的场次）"
                )

            # 锁内单独一条 COUNT：READ COMMITTED 下这是新快照，能看到持锁事务刚提交的预约
            cur.execute(
                """
                SELECT count(*) FROM bookings
                WHERE schedule_id = %s AND status IN ('booked', 'attended')
                """,
                (schedule,),
            )
            booked = int(cur.fetchone()[0])
            if booked >= capacity:
                raise ToolConflictError(f"该场次已满员（{booked}/{capacity}），可留意候补")

            cur.execute("SELECT name, status, expire_date FROM members WHERE id = %s", (target,))
            member = cur.fetchone()
            if member is None:
                raise ToolNotFoundError(f"会员 {target} 不存在")

            _, m_status, expire_date = member
            if m_status != "active":
                raise ToolConflictError(
                    f"会员卡当前状态为 {m_status}（非 active），入场权限已冻结，请先到前台处理会籍"
                )
            if expire_date < now.date():
                raise ToolConflictError(f"会员卡已于 {expire_date} 到期，请先续费")

            suspended = _booking_suspended(cur, target, now)
            if suspended:
                raise ToolConflictError(suspended)

            cur.execute(
                "SELECT id, status FROM bookings WHERE member_id = %s AND schedule_id = %s",
                (target, schedule),
            )
            previous = cur.fetchone()

            if previous is not None and previous[1] in _BOOKED_STATUSES:
                raise ToolConflictError(f"已预约过该场次（预约 #{previous[0]}，状态 {previous[1]}）")

            if previous is not None:
                # 之前取消过/爽约过：直接复活，绕开 UNIQUE(member_id, schedule_id)
                cur.execute(
                    """
                    UPDATE bookings SET status = 'booked', booked_at = now()
                    WHERE id = %s RETURNING id
                    """,
                    (previous[0],),
                )
                booking_id, revived = int(cur.fetchone()[0]), True
            else:
                cur.execute(
                    """
                    INSERT INTO bookings (member_id, schedule_id, status)
                    VALUES (%s, %s, 'booked') RETURNING id
                    """,
                    (target, schedule),
                )
                booking_id, revived = int(cur.fetchone()[0]), False
    except psycopg.errors.UniqueViolation as exc:
        # 并发下同一会员抢同一场次会撞唯一约束，归一成业务错误而不是 500
        raise ToolConflictError("已预约过该场次（并发重复提交）") from exc

    return ToolResult(
        "book_class",
        True,
        {
            "booking_id": booking_id,
            "member_id": target,
            "schedule_id": schedule,
            "course": course_name,
            "start_time": _iso(start_time),
            "status": "booked",
            "revived": revived,
            "booked_count": booked + 1,
            "capacity": capacity,
            "note": f"开课前 {FREE_CANCEL_HOURS} 小时以上可免费取消；不足 {FREE_CANCEL_HOURS} 小时取消计为一次爽约。",
        },
    )


# ---- 工具 3：cancel_booking -----------------------------------------------------


def cancel_booking(
    session: SessionContext,
    *,
    booking_id: Any = None,
    schedule_id: Any = None,
    member_id: Any = None,
) -> ToolResult:
    """取消预约。

    取消是 UPDATE status，不是 DELETE——历史要留着，出勤与爽约统计都依赖它。
    开课前 4 小时以上免费取消；不足 4 小时**仍然取消，但按场馆规章记一次爽约**（no_show）。
    """
    require_role("cancel_booking", session)
    target = resolve_member_id(session, _as_int(member_id, "member_id"))

    booking = _as_int(booking_id, "booking_id")
    schedule = _as_int(schedule_id, "schedule_id")
    if booking is None and schedule is None:
        raise ToolValidationError("需要提供 booking_id 或 schedule_id 来定位要取消的预约")

    now = _now()
    with get_db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT b.id, b.status, s.id, s.start_time, s.status, c.name
            FROM bookings b
            JOIN schedules s ON s.id = b.schedule_id
            JOIN courses c ON c.id = s.course_id
            WHERE b.member_id = %s
              AND (%s::bigint IS NULL OR b.id = %s::bigint)
              AND (%s::bigint IS NULL OR b.schedule_id = %s::bigint)
            ORDER BY s.start_time DESC
            LIMIT 1
            """,
            (target, booking, booking, schedule, schedule),
        )
        row = cur.fetchone()
        if row is None:
            raise ToolNotFoundError(f"会员 {target} 名下找不到匹配的预约")

        b_id, b_status, s_id, start_time, sched_status, course_name = row
        if b_status == "cancelled":
            raise ToolConflictError(f"预约 #{b_id} 已经是取消状态")
        # 只有 booked 能取消；attended / no_show 已经发生过，先按状态拒绝，
        # 不要让下面的「已开课」把原因覆盖掉（否则报错会误导模型）。
        if b_status != "booked":
            raise ToolConflictError(f"预约 #{b_id} 当前状态是 {b_status}，不可取消")
        if sched_status == "cancelled":
            raise ToolConflictError(f"「{course_name}」场次本身已取消，无需再取消预约")
        if start_time <= now:
            raise ToolConflictError(f"「{course_name}」已经开课，无法取消")

        late = (start_time - now) < timedelta(hours=FREE_CANCEL_HOURS)
        new_status = "no_show" if late else "cancelled"
        cur.execute(
            "UPDATE bookings SET status = %s WHERE id = %s AND status = 'booked' RETURNING id",
            (new_status, b_id),
        )
        if cur.fetchone() is None:
            raise ToolConflictError("预约状态已被其他操作改变，请重新查询后再试")

        month_count = _monthly_no_show_count(cur, target, now) if late else None

    data: dict[str, Any] = {
        "booking_id": b_id,
        "member_id": target,
        "schedule_id": s_id,
        "course": course_name,
        "start_time": _iso(start_time),
        "status": new_status,
    }
    if late:
        data["no_show_this_month"] = month_count
        data["note"] = (
            f"距开课不足 {FREE_CANCEL_HOURS} 小时，按场馆规章本次取消计为一次爽约"
            f"（本月累计 {month_count} 次）；每自然月累计 {NO_SHOW_SUSPEND_LIMIT} 次将暂停预约权限。"
        )
    else:
        data["note"] = f"已免费取消（距开课 {FREE_CANCEL_HOURS} 小时以上）。"

    return ToolResult("cancel_booking", True, data)


# ---- 工具 4：get_member_balance ------------------------------------------------


def get_member_balance(session: SessionContext, *, member_id: Any = None) -> ToolResult:
    """查询会员的会籍状态与消费概览。

    注意：库里**没有余额/课时字段**（members 表没有 balance 列，也没有会员卡表），
    所以这里返回的是从 transactions 派生的累计指标，不是账户余额。
    返回里带 note 明示这一点，否则上层很容易顺着"余额"这个词编造出具体金额。
    """
    require_role("get_member_balance", session)
    target = resolve_member_id(session, _as_int(member_id, "member_id"))

    with get_db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, name, card_type, status, join_date, expire_date,
                   (expire_date - %s) AS remaining_days
            FROM members WHERE id = %s
            """,
            (_now().date(), target),
        )
        row = cur.fetchone()
        if row is None:
            raise ToolNotFoundError(f"会员 {target} 不存在")

        cur.execute(
            """
            SELECT count(*) FILTER (WHERE b.status IN ('booked', 'attended')
                                     AND s.end_time >= now()) AS upcoming,
                   count(*) FILTER (WHERE b.status = 'attended') AS attended,
                   count(*) FILTER (WHERE b.status = 'no_show') AS no_show
            FROM bookings b JOIN schedules s ON s.id = b.schedule_id
            WHERE b.member_id = %s
            """,
            (target,),
        )
        upcoming, attended, no_show = cur.fetchone()

        # refund 的 amount 也是正数，不能直接 sum(amount) 当消费额
        cur.execute(
            """
            SELECT COALESCE(sum(amount) FILTER (WHERE status = 'paid' AND kind <> 'refund'), 0),
                   COALESCE(sum(amount) FILTER (WHERE kind = 'refund'), 0),
                   count(*) FILTER (WHERE status = 'pending')
            FROM transactions WHERE member_id = %s
            """,
            (target,),
        )
        paid_total, refund_total, pending = cur.fetchone()

    remaining = int(row[6])
    return ToolResult(
        "get_member_balance",
        True,
        {
            "member_id": row[0],
            "name": row[1],
            "card_type": row[2],
            "status": row[3],
            "join_date": _iso(row[4]),
            "expire_date": _iso(row[5]),
            "remaining_days": remaining,
            "expired": remaining < 0,
            "upcoming_bookings": int(upcoming),
            "attended_count": int(attended),
            "no_show_count": int(no_show),
            "paid_total": round(float(paid_total), 2),
            "refund_total": round(float(refund_total), 2),
            "pending_transactions": int(pending),
            "note": (
                "本系统未存储余额/课时字段，paid_total 与 refund_total 是从消费流水派生的累计值，"
                "不是账户余额；会籍有效性请以 status 与 remaining_days 为准。"
            ),
        },
    )


# ---- 工具 5：recommend_course --------------------------------------------------


def recommend_course(
    session: SessionContext,
    *,
    category: Any = None,
    difficulty: Any = None,
    keyword: Any = None,
    limit: Any = None,
) -> ToolResult:
    """推荐未来可预约的场次。

    刻意用确定性 SQL 排序而不是再调一次 LLM：省一次调用、结果可复现、也便于解释。
    会员会排除自己已约的场次；管理员/教练没有会员主体，跳过该条件。
    """
    require_role("recommend_course", session)

    category = _check_category(category)
    difficulty = _check_difficulty(difficulty)
    viewer = session.user_id if session.role == ROLE_MEMBER else None
    limit = _clamp(limit, 5, 1, MAX_SCHEDULE_LIMIT, "limit")

    like = f"%{str(keyword).strip()}%" if keyword else None

    sql = """
        WITH booked AS (
            SELECT schedule_id,
                   count(*) FILTER (WHERE status IN ('booked', 'attended')) AS n
            FROM bookings
            GROUP BY schedule_id
        )
        SELECT s.id, c.name, c.category, c.difficulty, c.duration_min,
               co.name AS coach_name, s.start_time, s.end_time, s.room,
               s.capacity, s.status, COALESCE(bk.n, 0) AS booked_count
        FROM schedules s
        JOIN courses c ON c.id = s.course_id
        JOIN coaches co ON co.id = s.coach_id
        LEFT JOIN booked bk ON bk.schedule_id = s.id
        WHERE s.status = 'scheduled'
          AND s.start_time > now()
          AND s.start_time <= now() + %s::interval
          AND (%s::text IS NULL OR c.category = %s::text)
          AND (%s::text IS NULL OR c.difficulty = %s::text)
          AND (%s::text IS NULL OR c.name ILIKE %s::text OR c.description ILIKE %s::text)
          AND (%s::bigint IS NULL OR NOT EXISTS (
                SELECT 1 FROM bookings b
                WHERE b.schedule_id = s.id AND b.member_id = %s::bigint
                  AND b.status IN ('booked', 'attended')))
        ORDER BY (COALESCE(bk.n, 0)::float / NULLIF(s.capacity, 0)) ASC, s.start_time ASC
        LIMIT %s
    """
    args = (
        timedelta(days=BOOKING_WINDOW_DAYS),
        category, category,
        difficulty, difficulty,
        like, like, like,
        viewer, viewer,
        limit,
    )

    with get_db_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        rows = cur.fetchall()

    schedules = [_schedule_row(row) for row in rows]
    return ToolResult(
        "recommend_course",
        True,
        {
            "count": len(schedules),
            "schedules": schedules,
            "note": (
                f"只推荐未来 {BOOKING_WINDOW_DAYS} 天内已开放预约且未满的场次，"
                "按「空位比例低→开课时间近」排序。"
                + ("" if viewer is None else "已排除你自己已预约的场次。")
            ),
        },
    )


# ---- 工具 6：generate_daily_report ---------------------------------------------


def generate_daily_report(
    session: SessionContext,
    *,
    date: Any = None,
    coach_id: Any = None,
) -> ToolResult:
    """生成某一天的运营报表。

    归属校验在这里落地：教练的 coach_id 被强制成本人（传别人直接拒绝），
    所以教练只能看到自己带的课；管理员可看全场，也可用 coach_id 过滤。
    """
    require_role("generate_daily_report", session)
    effective_coach = resolve_coach_id(session, _as_int(coach_id, "coach_id"))

    day = str(date).strip() if date else _now().date().isoformat()
    start, end = _day_bounds(day)

    with get_db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH stats AS (
                SELECT b.schedule_id,
                       count(*) FILTER (WHERE b.status IN ('booked', 'attended')) AS booked,
                       count(*) FILTER (WHERE b.status = 'attended') AS attended,
                       count(*) FILTER (WHERE b.status = 'no_show') AS no_show,
                       count(*) FILTER (WHERE b.status = 'cancelled') AS cancelled
                FROM bookings b
                GROUP BY b.schedule_id
            )
            SELECT s.id, c.name, co.id, co.name, s.start_time, s.end_time, s.room,
                   s.status, s.capacity,
                   COALESCE(st.booked, 0), COALESCE(st.attended, 0),
                   COALESCE(st.no_show, 0), COALESCE(st.cancelled, 0)
            FROM schedules s
            JOIN courses c ON c.id = s.course_id
            JOIN coaches co ON co.id = s.coach_id
            LEFT JOIN stats st ON st.schedule_id = s.id
            WHERE s.start_time >= %s AND s.start_time < %s
              AND (%s::bigint IS NULL OR s.coach_id = %s::bigint)
            ORDER BY s.start_time
            """,
            (start, end, effective_coach, effective_coach),
        )
        rows = cur.fetchall()

        revenue: list[dict[str, Any]] | None = None
        if session.role == ROLE_ADMIN:
            cur.execute(
                """
                SELECT kind, count(*), COALESCE(sum(amount), 0)
                FROM transactions
                WHERE created_at >= %s AND created_at < %s AND status = 'paid'
                GROUP BY kind ORDER BY kind
                """,
                (start, end),
            )
            revenue = [
                {"kind": kind, "count": int(count), "amount": round(float(amount), 2)}
                for kind, count, amount in cur.fetchall()
            ]

    sessions = [
        {
            "schedule_id": row[0],
            "course": row[1],
            "coach_id": row[2],
            "coach": row[3],
            "start_time": _iso(row[4]),
            "end_time": _iso(row[5]),
            "room": row[6],
            "status": row[7],
            "capacity": row[8],
            "booked_count": row[9],
            "attended_count": row[10],
            "no_show_count": row[11],
            "cancelled_count": row[12],
            "attendance_rate": round(row[10] / row[9], 2) if row[9] else 0.0,
        }
        for row in rows
    ]

    data: dict[str, Any] = {
        "date": day,
        "scope": "全场" if effective_coach is None else f"教练 {effective_coach}",
        "schedule_count": len(sessions),
        "totals": {
            "capacity": sum(s["capacity"] for s in sessions),
            "booked": sum(s["booked_count"] for s in sessions),
            "attended": sum(s["attended_count"] for s in sessions),
            "no_show": sum(s["no_show_count"] for s in sessions),
            "cancelled": sum(s["cancelled_count"] for s in sessions),
        },
        "schedules": sessions,
    }
    if revenue is not None:
        data["revenue"] = revenue
    return ToolResult("generate_daily_report", True, data)


# ---- 工具 7：search_policy -----------------------------------------------------


def search_policy(
    session: SessionContext,
    *,
    question: Any,
    top_k: Any = None,
    strategy: Any = None,
) -> ToolResult:
    """检索并回答场馆规章制度，复用 tools/search_policy.py 的 Agentic RAG 实现。

    这是 loop 里最昂贵的一步：内部会打 DeepSeek + Milvus + Ollama，实测约 7 秒。
    返回里只保留必要字段，避免把整坨原文塞进对话历史撑爆上下文。
    """
    require_role("search_policy", session)

    text = str(question).strip() if question is not None else ""
    if not text:
        raise ToolValidationError("question 必填且不能为空")

    top_k = _clamp(top_k, 3, 1, 10, "top_k")
    name = str(strategy).strip() if strategy else None

    # 注意：这里必须传会话里的 role，不能传模型给的任何值
    raw = policy_search(text, session.role, top_k=top_k, strategy=name)

    docs = [
        {
            "id": doc.get("id"),
            "title": doc.get("title"),
            "content": (doc.get("content") or "")[:POLICY_DOC_CHARS],
            "category": doc.get("category"),
            "source": doc.get("source"),
            "score": doc.get("score"),
        }
        for doc in raw.get("docs", [])
    ]
    rounds = [
        {
            "strategy": item.get("strategy"),
            "sufficient": (item.get("grade") or {}).get("sufficient"),
        }
        for item in raw.get("rounds", [])
    ]

    return ToolResult(
        "search_policy",
        True,
        {
            "question": raw.get("question", text),
            "strategy_used": raw.get("strategy_used"),
            "sufficient": raw.get("sufficient"),
            "rounds": rounds,
            "docs": docs,
            "note": "docs 是场馆规章原文片段；sufficient=false 表示没检索到能回答该问题的规章。",
        },
    )


# ---- 统一入口 -------------------------------------------------------------------

_TOOLS: dict[str, Callable[..., ToolResult]] = {
    "query_schedule": query_schedule,
    "book_class": book_class,
    "cancel_booking": cancel_booking,
    "get_member_balance": get_member_balance,
    "recommend_course": recommend_course,
    "generate_daily_report": generate_daily_report,
    "search_policy": search_policy,
}

# 顺序有讲究：子类必须排在父类之前（ToolPermissionError 是 ToolError 的子类）
_ERROR_KINDS: tuple[tuple[type[BaseException], str], ...] = (
    (PermissionDenied, "permission"),
    (ToolPermissionError, "permission"),
    (ToolValidationError, "validation"),
    (ToolNotFoundError, "not_found"),
    (ToolConflictError, "conflict"),
    (ToolError, "tool"),
    (psycopg.Error, "database"),
)


def _error_kind(exc: BaseException) -> str:
    for klass, kind in _ERROR_KINDS:
        if isinstance(exc, klass):
            return kind
    return "internal"


def tool_names() -> list[str]:
    """已登记的工具名，供 loop 拼 prompt 用。"""
    return sorted(_TOOLS)


def call_tool(
    name: str,
    session: SessionContext,
    params: dict[str, Any] | None = None,
    *,
    session_id: str | None = None,
    step: int | None = None,
) -> ToolResult:
    """工具统一入口 —— agent loop 的唯一调用点，也是 trace 的唯一写入点。

    这里负责参数白名单过滤、异常归一、计时与落盘；**权限校验仍由每个工具的第一行负责**，
    这样即使有人绕过 call_tool 直接调工具函数，权限门也还在。

    白名单之外的参数一律**拒绝执行**（而不是静默丢弃）：模型试图给 member 塞 member_id
    属于越权尝试，应该被看见并留下痕迹。
    """
    params = dict(params or {})
    started = time.perf_counter()
    result: ToolResult

    try:
        allowed = model_params(name, session.role)
        unknown = sorted(set(params) - allowed)
        if unknown:
            raise ToolPermissionError(
                f"参数 {unknown} 不属于工具 {name} 对角色 {session.role!r} 的可用参数；"
                f"可用参数：{sorted(allowed) if allowed else '（该工具无参数）'}"
            )

        fn = _TOOLS.get(name)
        if fn is None:
            raise ToolValidationError(f"未知工具 {name!r}；可用工具：{tool_names()}")

        args = {key: value for key, value in params.items() if value is not None}
        try:
            # 先做签名绑定检查：参数名写错时给出清晰提示，也避免把函数内部的 TypeError 误判成参数问题
            inspect.signature(fn).bind(session, **args)
        except TypeError as exc:
            raise ToolValidationError(f"工具 {name} 的参数不合法：{exc}") from None

        result = fn(session, **args)
    except Exception as exc:  # noqa: BLE001 —— 这里就是要兜住一切，转成模型能读的错误
        result = ToolResult(
            tool=name,
            ok=False,
            data={},
            error=str(exc) or type(exc).__name__,
            error_kind=_error_kind(exc),
        )

    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    append_trace(
        {
            "ts": _now().isoformat(timespec="seconds"),
            "session_id": session_id,
            "step": step,
            "role": session.role,
            "user_id": session.user_id,
            "tool": name,
            "params": params,
            # 请求的与生效的 member_id 分开记：两者不一致就是一次越权尝试
            "member_id_requested": params.get("member_id"),
            "member_id_effective": None if not result.ok else result.data.get("member_id"),
            "ok": result.ok,
            "error_kind": result.error_kind,
            "elapsed_ms": elapsed_ms,
            "error": result.error,
        }
    )
    return result
