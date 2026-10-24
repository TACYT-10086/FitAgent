"""agent/tools.py 七个业务工具的真连接测试（真连 PostgreSQL，无打桩、无跳过）。

⚠ 真实 API 调用
   本文件里 `test_search_policy_end_to_end_real_api` 会**真的请求 DeepSeek**，并依赖本机
   Milvus 与 Ollama 在线，单次约 7 秒、会真的花钱。其余用例只连 PostgreSQL。
   这条用例刻意**不带 `llm` 标记**，因此不会被 tests/conftest.py 的跳过逻辑拦下 ——
   按约定，这类用例要么真跑、要么删掉，不留「环境变量不存在就跳过」的中间态。

写库用例一律 try/finally 自清理，跑完库里回到 seed 原状，可反复执行、不累积脏数据。

**刻意不硬编码场次/预约 id**：seed 的排课是按「距灌库当天的相对天数」生成的，重灌一次或隔几天
再跑，绝对日期与 id 就整体平移，硬编码会立刻失效。所有用例都在运行时用 SQL 定位符合条件的那一行，
定位不到就直接失败并说明原因 —— 不跳过、不静默放过。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from agent.permissions import ROLE_ADMIN, ROLE_COACH, ROLE_MEMBER, SessionContext
from agent.tools import (
    ToolConflictError,
    ToolNotFoundError,
    ToolValidationError,
    _booking_suspended,
    book_class,
    cancel_booking,
    generate_daily_report,
    get_member_balance,
    query_schedule,
    recommend_course,
    search_policy,
)
from db.connection import get_db_conn
from rag.transform import available_strategies
from timeutil import BEIJING_TZ

MEMBER = SessionContext(ROLE_MEMBER, 1)  # 查询类工具不限身份，固定用会员 1 即可


# ---- 直连辅助 --------------------------------------------------------------------
# 这些函数一律绕开被测代码：断言「库里真的有那一行」只能靠自己的 SQL。


def _fetchall(sql: str, args: tuple = ()) -> list[tuple]:
    with get_db_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def _scalar(sql: str, args: tuple = ()):
    with get_db_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchone()[0]


def _execute(sql: str, args: tuple = ()) -> None:
    """跑一条写语句并提交（psycopg3 在 with 块正常退出时提交）。"""
    with get_db_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, args)


# ---- 运行时定位测试素材 -------------------------------------------------------------


def _active_member_id() -> int:
    member_id = _scalar(
        """
        SELECT id FROM members
        WHERE status = 'active' AND expire_date >= CURRENT_DATE
        ORDER BY id LIMIT 1
        """
    )
    assert member_id is not None, "seed 数据里没有 active 且未过期的会员"
    return member_id


def _free_slot() -> tuple[int, int]:
    """一对「可约会员 x 可约场次」：场次在未来 7 天窗口内且未满，该会员在这场上也没有预约记录。"""
    rows = _fetchall(
        """
        SELECT m.id, s.id
        FROM members m
        CROSS JOIN schedules s
        WHERE m.status = 'active' AND m.expire_date >= CURRENT_DATE
          AND s.status = 'scheduled'
          AND s.start_time > now() AND s.start_time <= now() + interval '7 days'
          AND (SELECT count(*) FROM bookings b
               WHERE b.schedule_id = s.id AND b.status IN ('booked', 'attended')) < s.capacity
          AND NOT EXISTS (SELECT 1 FROM bookings b2
                          WHERE b2.member_id = m.id AND b2.schedule_id = s.id)
        ORDER BY m.id, s.start_time
        LIMIT 1
        """
    )
    assert rows, "seed 数据里找不到「可约会员 + 窗口内可约场次」的组合"
    return rows[0][0], rows[0][1]


def _busiest_day() -> tuple[str, int]:
    """场次最多的一天（按北京时间切天，与工具内部的 _day_bounds 口径一致），返回 (日期, 场次数)。"""
    rows = _fetchall(
        """
        SELECT (start_time AT TIME ZONE 'Asia/Shanghai')::date AS d, count(*) AS n
        FROM schedules GROUP BY d ORDER BY n DESC, d LIMIT 1
        """
    )
    assert rows, "schedules 表是空的"
    day, count = rows[0]
    return day.isoformat(), count


# ---- 造临时数据 -------------------------------------------------------------------


def _new_temp_schedule(
    *,
    capacity: int,
    starts_in: timedelta | None = None,
    starts_at: datetime | None = None,
    status: str = "scheduled",
) -> int:
    """造一个临时场次（借用 seed 里已有的课程与教练），调用方负责删除。

    starts_in 与 starts_at 二选一：前者相对现在（用来造「4 小时内开课」），
    后者给绝对时间（爽约规则要落在上一自然月）。
    """
    assert (starts_in is None) != (starts_at is None), "starts_in 与 starts_at 必须二选一"

    if starts_at is None:
        return _scalar(
            """
            INSERT INTO schedules (course_id, coach_id, start_time, end_time, room, capacity, status)
            VALUES ((SELECT min(id) FROM courses), (SELECT min(id) FROM coaches),
                    now() + %s, now() + %s, '测试场地', %s, %s)
            RETURNING id
            """,
            (starts_in, starts_in + timedelta(minutes=45), capacity, status),
        )

    return _scalar(
        """
        INSERT INTO schedules (course_id, coach_id, start_time, end_time, room, capacity, status)
        VALUES ((SELECT min(id) FROM courses), (SELECT min(id) FROM coaches),
                %s, %s, '测试场地', %s, %s)
        RETURNING id
        """,
        (starts_at, starts_at + timedelta(minutes=45), capacity, status),
    )


def _new_temp_member() -> int:
    """造一个专属测试会员（唯一手机号）。bookings / transactions 会随他一起级联删除。"""
    return _scalar(
        """
        INSERT INTO members (name, phone, gender, card_type, join_date, expire_date, status)
        VALUES ('测试会员', %s, 'male', 'yearly', CURRENT_DATE, CURRENT_DATE + 365, 'active')
        RETURNING id
        """,
        (f"1{uuid4().int % 10**10:010d}",),
    )


# ================================================================ query_schedule


def test_query_schedule_defaults_to_future_only():
    """不带任何时间条件时，只返回今天及以后的场次。

    这条钉死的是一个真实缺陷：早先没有默认下界，查询会把过去两周的历史场次一起捞出来，
    而模型极易把它们当成「还可预约」报给用户 —— 实测真的发生过。
    """
    result = query_schedule(MEMBER, limit=50)

    assert result.ok is True
    assert result.data["count"] > 0

    now = datetime.now(timezone.utc)
    for item in result.data["schedules"]:
        assert item["start_time"].endswith("+08:00"), "工具返回的时间不是北京时间"
        moment = datetime.fromisoformat(item["start_time"])
        assert moment > now, f"默认查询返回了历史场次：{item['start_time']} {item['course']}"


def test_query_schedule_explicit_past_date_returns_history():
    """显式传日期时历史照查不误。

    默认只看未来是「不替调用方猜」，不是「禁止查历史」—— 这两件事必须都成立。
    """
    day = _scalar(
        """
        SELECT (start_time AT TIME ZONE 'Asia/Shanghai')::date AS d
        FROM schedules WHERE start_time < now()
        GROUP BY d ORDER BY count(*) DESC, d LIMIT 1
        """
    )
    assert day is not None, "seed 数据里没有历史场次"

    start = datetime(day.year, day.month, day.day, tzinfo=BEIJING_TZ)
    end = start + timedelta(days=1)
    expected = _scalar(
        "SELECT count(*) FROM schedules WHERE start_time >= %s AND start_time < %s",
        (start, end),
    )

    result = query_schedule(MEMBER, date=day.isoformat(), limit=50)

    assert result.ok is True
    assert result.data["count"] == expected > 0
    for item in result.data["schedules"]:
        moment = datetime.fromisoformat(item["start_time"])
        assert start <= moment < end


def test_query_schedule_rejects_invalid_category():
    """类别是枚举，拼错要报错，而不是当成「没有这种课」返回空列表。"""
    with pytest.raises(ToolValidationError) as exc:
        query_schedule(MEMBER, category="kickboxing")

    assert "kickboxing" in str(exc.value)


# ================================================================ book_class


def test_book_class_really_writes_row():
    """成功路径必须真的落库。

    断言走**直连 SQL** 而不是工具的返回值：工具说自己 ok，和库里真有一行，是两件事。
    """
    member_id, schedule_id = _free_slot()
    assert (
        _scalar(
            "SELECT count(*) FROM bookings WHERE member_id = %s AND schedule_id = %s",
            (member_id, schedule_id),
        )
        == 0
    ), "选中的 (会员, 场次) 已经有预约记录，用例前提不成立"

    new_booking_id = None
    try:
        result = book_class(SessionContext(ROLE_MEMBER, member_id), schedule_id=schedule_id)

        assert result.ok is True
        new_booking_id = result.data["booking_id"]
        assert result.data["member_id"] == member_id
        assert result.data["schedule_id"] == schedule_id
        assert result.data["status"] == "booked"

        assert _fetchall(
            "SELECT member_id, schedule_id, status FROM bookings WHERE id = %s",
            (new_booking_id,),
        ) == [(member_id, schedule_id, "booked")]
    finally:
        if new_booking_id is not None:
            _execute("DELETE FROM bookings WHERE id = %s", (new_booking_id,))


def test_book_class_revives_cancelled_booking():
    """取消过的场次可以再约，走的是 UPDATE 复活而不是 INSERT。

    必须走复活路径：UNIQUE(member_id, schedule_id) 决定了同一个人在同一场上只可能有一行，
    直接 INSERT 会撞唯一约束。
    """
    rows = _fetchall(
        """
        SELECT b.id, b.member_id, b.schedule_id
        FROM bookings b
        JOIN schedules s ON s.id = b.schedule_id
        JOIN members m ON m.id = b.member_id
        WHERE b.status = 'cancelled'
          AND s.status = 'scheduled'
          AND s.start_time > now() AND s.start_time <= now() + interval '7 days'
          AND m.status = 'active' AND m.expire_date >= CURRENT_DATE
          AND (SELECT count(*) FROM bookings b2
               WHERE b2.schedule_id = s.id AND b2.status IN ('booked', 'attended')) < s.capacity
        ORDER BY b.id LIMIT 1
        """
    )
    assert rows, "seed 数据里找不到「已取消 + 场次仍在窗口内」的预约，无法验证复活路径"
    booking_id, member_id, schedule_id = rows[0]

    try:
        result = book_class(SessionContext(ROLE_MEMBER, member_id), schedule_id=schedule_id)

        assert result.ok is True
        assert result.data["revived"] is True
        assert result.data["booking_id"] == booking_id
        assert _scalar("SELECT status FROM bookings WHERE id = %s", (booking_id,)) == "booked"
    finally:
        _execute("UPDATE bookings SET status = 'cancelled' WHERE id = %s", (booking_id,))


def test_book_class_rejects_duplicate_booking():
    """重复预约同一场次要被拒。

    唯一约束其实也能兜住，但那会抛成数据库错误（500 味道），而不是模型能读懂的业务错误。
    """
    rows = _fetchall(
        """
        SELECT b.member_id, b.schedule_id
        FROM bookings b
        JOIN schedules s ON s.id = b.schedule_id
        JOIN members m ON m.id = b.member_id
        WHERE b.status = 'booked'
          AND s.status = 'scheduled'
          AND s.start_time > now() AND s.start_time <= now() + interval '7 days'
          AND m.status = 'active' AND m.expire_date >= CURRENT_DATE
        ORDER BY b.id LIMIT 1
        """
    )
    assert rows, "seed 数据里找不到「窗口内已预约」的记录"
    member_id, schedule_id = rows[0]

    with pytest.raises(ToolConflictError) as exc:
        book_class(SessionContext(ROLE_MEMBER, member_id), schedule_id=schedule_id)

    assert "已预约过" in str(exc.value)


def test_book_class_rejects_finished_schedule():
    schedule_id = _scalar(
        "SELECT id FROM schedules WHERE status = 'finished' ORDER BY start_time LIMIT 1"
    )
    assert schedule_id is not None, "seed 数据里没有已结束的场次"

    with pytest.raises(ToolConflictError) as exc:
        book_class(SessionContext(ROLE_MEMBER, _active_member_id()), schedule_id=schedule_id)

    assert "已结束" in str(exc.value)


def test_book_class_rejects_started_schedule():
    """状态还是 scheduled、但开课时间已过。

    这和「已结束」是两条不同的分支，只测一条会漏掉另一半。
    自己造场次而不是从 seed 里碰运气：种子数据只在特定时刻才恰好有「已开课但未结束」的课。
    """
    schedule_id = _new_temp_schedule(capacity=5, starts_in=timedelta(hours=-1))
    try:
        with pytest.raises(ToolConflictError) as exc:
            book_class(SessionContext(ROLE_MEMBER, _active_member_id()), schedule_id=schedule_id)

        assert "已经开课" in str(exc.value)
    finally:
        _execute("DELETE FROM schedules WHERE id = %s", (schedule_id,))


def test_book_class_rejects_beyond_booking_window():
    """超出 7 天窗口的场次尚未开放预约。

    政策原文：「系统每日 20:00 开放次日起 7 天内的场次」。
    """
    schedule_id = _scalar(
        """
        SELECT id FROM schedules
        WHERE status = 'scheduled' AND start_time > now() + interval '7 days'
        ORDER BY start_time LIMIT 1
        """
    )
    assert schedule_id is not None, "seed 数据里没有超出 7 天窗口的未来场次"

    with pytest.raises(ToolConflictError) as exc:
        book_class(SessionContext(ROLE_MEMBER, _active_member_id()), schedule_id=schedule_id)

    assert "7 天" in str(exc.value)


@pytest.mark.parametrize("status", ["expired", "frozen"])
def test_book_class_rejects_inactive_member(status):
    """会籍非 active 一律拦下。

    注意 seed 里 expired 会员的 status 就是 'expired'，所以命中的是「非 active」这条分支；
    「status 仍是 active、但 expire_date 已过」那条独立分支需要脏数据才能触发，本文件覆盖不到
    （见文件末尾说明）—— 所以这里断言的是错误信息里带着具体状态名，而不是「到期」二字。
    """
    member_id = _scalar(
        "SELECT id FROM members WHERE status = %s ORDER BY id LIMIT 1", (status,)
    )
    assert member_id is not None, f"seed 数据里没有状态为 {status} 的会员"

    _, schedule_id = _free_slot()

    with pytest.raises(ToolConflictError) as exc:
        book_class(SessionContext(ROLE_MEMBER, member_id), schedule_id=schedule_id)

    assert status in str(exc.value)
    assert "冻结" in str(exc.value)


def test_book_class_rejects_full_schedule():
    """满员拒绝。

    seed 里没有任何满员场次（最大占用只有 1 个名额），所以这里自己造一个 capacity=1 的临时场次，
    再让别人占掉那唯一一个名额。
    """
    temp_schedule = _new_temp_schedule(capacity=1, starts_in=timedelta(days=1))
    try:
        occupant = _active_member_id()
        _execute(
            "INSERT INTO bookings (member_id, schedule_id, status) VALUES (%s, %s, 'booked')",
            (occupant, temp_schedule),
        )

        booker = _scalar(
            """
            SELECT id FROM members
            WHERE status = 'active' AND expire_date >= CURRENT_DATE AND id <> %s
            ORDER BY id LIMIT 1
            """,
            (occupant,),
        )
        assert booker is not None, "seed 数据里没有第二个 active 会员"

        with pytest.raises(ToolConflictError) as exc:
            book_class(SessionContext(ROLE_MEMBER, booker), schedule_id=temp_schedule)

        assert "满员" in str(exc.value)
    finally:
        _execute("DELETE FROM schedules WHERE id = %s", (temp_schedule,))


def test_book_class_rejects_missing_schedule():
    with pytest.raises(ToolNotFoundError) as exc:
        book_class(SessionContext(ROLE_MEMBER, _active_member_id()), schedule_id=99999999)

    assert "不存在" in str(exc.value)


# ================================================================ cancel_booking


def test_cancel_booking_free_window():
    """距开课 4 小时以上 → 免费取消，状态记 cancelled，不计爽约。"""
    rows = _fetchall(
        """
        SELECT b.id, b.member_id
        FROM bookings b JOIN schedules s ON s.id = b.schedule_id
        WHERE b.status = 'booked' AND s.status = 'scheduled'
          AND s.start_time > now() + interval '4 hours'
        ORDER BY b.id LIMIT 1
        """
    )
    assert rows, "seed 数据里找不到「距开课超过 4 小时的 booked 预约」"
    booking_id, member_id = rows[0]

    try:
        result = cancel_booking(SessionContext(ROLE_MEMBER, member_id), booking_id=booking_id)

        assert result.ok is True
        assert result.data["status"] == "cancelled"
        assert "免费取消" in result.data["note"]
        assert _scalar("SELECT status FROM bookings WHERE id = %s", (booking_id,)) == "cancelled"
    finally:
        _execute("UPDATE bookings SET status = 'booked' WHERE id = %s", (booking_id,))


def test_cancel_booking_within_4h_counts_as_no_show():
    """距开课不足 4 小时取消 → 仍然取消，但按场馆规章记一次爽约（no_show）。

    seed 里没有 4 小时内开课的场次，所以造一个 now+2h 的临时场次。
    刻意不断言 no_show_this_month 的具体数值：它按自然月统计，跨月边界（月末 23 点 +2 小时）
    会把这条临时场次算进下个月，那样数值就不确定了 —— 规则本身由下面两条爽约用例独立覆盖。
    """
    temp_schedule = _new_temp_schedule(capacity=5, starts_in=timedelta(hours=2))
    try:
        member_id = _active_member_id()
        temp_booking = _scalar(
            """
            INSERT INTO bookings (member_id, schedule_id, status)
            VALUES (%s, %s, 'booked') RETURNING id
            """,
            (member_id, temp_schedule),
        )

        result = cancel_booking(SessionContext(ROLE_MEMBER, member_id), booking_id=temp_booking)

        assert result.ok is True
        assert result.data["status"] == "no_show"
        assert isinstance(result.data["no_show_this_month"], int)
        assert "爽约" in result.data["note"]
        assert _scalar("SELECT status FROM bookings WHERE id = %s", (temp_booking,)) == "no_show"
    finally:
        _execute("DELETE FROM schedules WHERE id = %s", (temp_schedule,))


def test_cancel_booking_rejects_already_cancelled():
    rows = _fetchall(
        "SELECT id, member_id FROM bookings WHERE status = 'cancelled' ORDER BY id LIMIT 1"
    )
    assert rows, "seed 数据里没有已取消的预约"
    booking_id, member_id = rows[0]

    with pytest.raises(ToolConflictError) as exc:
        cancel_booking(SessionContext(ROLE_MEMBER, member_id), booking_id=booking_id)

    assert "已经是取消状态" in str(exc.value)


def test_cancel_booking_rejects_attended():
    """已出席/已爽约的预约不能再取消 —— 只允许 booked → cancelled/no_show。"""
    rows = _fetchall(
        "SELECT id, member_id FROM bookings WHERE status = 'attended' ORDER BY id LIMIT 1"
    )
    assert rows, "seed 数据里没有已出席的预约"
    booking_id, member_id = rows[0]

    with pytest.raises(ToolConflictError) as exc:
        cancel_booking(SessionContext(ROLE_MEMBER, member_id), booking_id=booking_id)

    assert "不可取消" in str(exc.value)


# ================================================================ get_member_balance


def test_get_member_balance_derives_from_transactions():
    """消费概览必须真的从流水算出来。

    刻意走**与工具不同的代码路径**：把原始流水行取回来，在 Python 侧按政策口径自己算一遍。
    直接用同样的 SQL 聚合去比对工具结果，等于拿工具的 SQL 验证工具的 SQL，什么也证不了。
    """
    member_id = _scalar(
        """
        SELECT member_id FROM transactions
        GROUP BY member_id
        HAVING count(*) FILTER (WHERE status = 'paid' AND kind <> 'refund') > 0
           AND count(*) FILTER (WHERE kind = 'refund') > 0
        ORDER BY member_id LIMIT 1
        """
    )
    assert member_id is not None, "seed 数据里找不到同时有已付消费和退款的会员"

    raw = _fetchall(
        "SELECT amount, kind, status FROM transactions WHERE member_id = %s", (member_id,)
    )

    expected_paid = sum(
        float(amount) for amount, kind, status in raw if status == "paid" and kind != "refund"
    )
    expected_refund = sum(float(amount) for amount, kind, _ in raw if kind == "refund")
    expected_pending = sum(1 for _, _, status in raw if status == "pending")

    result = get_member_balance(SessionContext(ROLE_MEMBER, member_id))

    assert result.ok is True
    assert result.data["paid_total"] == pytest.approx(round(expected_paid, 2))
    assert result.data["refund_total"] == pytest.approx(round(expected_refund, 2))
    assert result.data["pending_transactions"] == expected_pending

    # 退款不能被算进消费额 —— 这是最容易写错的一处口径
    naive_total = round(sum(float(amount) for amount, _, _ in raw), 2)
    assert result.data["paid_total"] != naive_total, (
        "paid_total 等于全部流水求和，说明退款被当成收入算进去了"
    )


def test_get_member_balance_excludes_pending_from_paid():
    """待支付不算消费额。

    seed 里有会员名下只有一笔 pending 流水 —— status 过滤要是漏了，他会被算成「已消费 299 元」。
    """
    member_id = _scalar(
        """
        SELECT member_id FROM transactions
        GROUP BY member_id
        HAVING count(*) FILTER (WHERE status = 'paid') = 0
           AND count(*) FILTER (WHERE status = 'pending') > 0
        ORDER BY member_id LIMIT 1
        """
    )
    assert member_id is not None, "seed 数据里找不到只有待支付流水的会员"

    pending = _scalar(
        "SELECT count(*) FROM transactions WHERE member_id = %s AND status = 'pending'",
        (member_id,),
    )

    result = get_member_balance(SessionContext(ROLE_MEMBER, member_id))

    assert result.data["paid_total"] == 0.0
    assert result.data["pending_transactions"] == pending > 0


def test_get_member_balance_flags_expired_member():
    member_id = _scalar(
        "SELECT id FROM members WHERE expire_date < CURRENT_DATE ORDER BY expire_date LIMIT 1"
    )
    assert member_id is not None, "seed 数据里没有已过期的会员"

    result = get_member_balance(SessionContext(ROLE_MEMBER, member_id))

    assert result.data["expired"] is True
    assert result.data["remaining_days"] < 0


def test_get_member_balance_carries_no_balance_note():
    """返回必须自带「库里没有余额字段」的说明。

    库里确实没有 balance 列，这是防止模型顺着「余额」这个词编造具体金额的唯一护栏。
    """
    result = get_member_balance(SessionContext(ROLE_MEMBER, _active_member_id()))

    assert "未存储余额" in result.data["note"]


# ================================================================ generate_daily_report


def test_daily_report_admin_sees_whole_venue_and_revenue():
    """管理员看全场，并且能看到当日营收（按流水聚合）。"""
    day, expected_count = _busiest_day()

    result = generate_daily_report(SessionContext(ROLE_ADMIN), date=day)

    assert result.ok is True
    assert result.data["scope"] == "全场"
    assert result.data["schedule_count"] == expected_count
    assert "revenue" in result.data


def test_daily_report_totals_match_schedules():
    """汇总行必须等于明细逐条求和 —— 报表分两处各算一遍，是最容易对不上的地方。"""
    day, _ = _busiest_day()
    result = generate_daily_report(SessionContext(ROLE_ADMIN), date=day)

    items = result.data["schedules"]
    totals = result.data["totals"]

    assert items, f"{day} 没有任何场次，用例前提不成立"
    assert totals["capacity"] == sum(item["capacity"] for item in items)
    assert totals["booked"] == sum(item["booked_count"] for item in items)
    assert totals["attended"] == sum(item["attended_count"] for item in items)
    assert totals["no_show"] == sum(item["no_show_count"] for item in items)
    assert totals["cancelled"] == sum(item["cancelled_count"] for item in items)


def test_daily_report_coach_has_no_revenue():
    """营收不给教练：教练视角的返回里不该出现 revenue 这个键（而不是值为 0）。"""
    day, _ = _busiest_day()
    coach_id = _scalar(
        """
        SELECT coach_id FROM schedules
        WHERE (start_time AT TIME ZONE 'Asia/Shanghai')::date = %s
        GROUP BY coach_id ORDER BY count(*) DESC LIMIT 1
        """,
        (date.fromisoformat(day),),
    )
    assert coach_id is not None, f"{day} 当天没有任何教练的课"

    result = generate_daily_report(SessionContext(ROLE_COACH, coach_id), date=day)

    assert result.ok is True
    assert result.data["scope"] == f"教练 {coach_id}"
    assert "revenue" not in result.data


# ================================================================ recommend_course


def test_recommend_course_only_future_within_window():
    """推荐只给未来 7 天内的场次，且按「空位比例升序」排序（更容易成班的排前面）。"""
    result = recommend_course(SessionContext(ROLE_MEMBER, _active_member_id()), limit=20)

    assert result.ok is True
    assert result.data["count"] > 0

    now = datetime.now(timezone.utc)
    deadline = now + timedelta(days=7)
    for item in result.data["schedules"]:
        moment = datetime.fromisoformat(item["start_time"])
        assert now < moment <= deadline, f"推荐了窗口外的场次：{item['start_time']}"
        assert item["status"] == "scheduled"

    ratios = [
        item["booked_count"] / item["capacity"] for item in result.data["schedules"]
    ]
    assert ratios == sorted(ratios), "推荐结果没有按「空位比例升序」排序"


def test_recommend_course_excludes_members_own_bookings():
    """已经在同一场上约过的会员，不该再被推荐同一场次。"""
    rows = _fetchall(
        """
        SELECT b.member_id, b.schedule_id
        FROM bookings b JOIN schedules s ON s.id = b.schedule_id
        WHERE b.status IN ('booked', 'attended')
          AND s.status = 'scheduled'
          AND s.start_time > now() AND s.start_time <= now() + interval '7 days'
        ORDER BY b.id LIMIT 1
        """
    )
    assert rows, "seed 数据里找不到「会员已约 + 场次仍在窗口内」的记录"
    member_id, schedule_id = rows[0]

    result = recommend_course(SessionContext(ROLE_MEMBER, member_id), limit=50)

    assert schedule_id not in {
        item["schedule_id"] for item in result.data["schedules"]
    }


# ================================================================ search_policy


def test_search_policy_end_to_end_real_api():
    """真实 API 调用：会真的请求 DeepSeek，并依赖本机 Milvus 与 Ollama 在线（约 7 秒）。

    刻意不做任何打桩 —— 「选策略 → 检索 → 评分 → 不相关就换策略重试」这条 Agentic 链路的
    价值全在模型真的做判断上，桩掉之后测的只是我自己写的 if-else。
    也不带 llm 标记：跳过就等于没测，要么真跑要么删掉。
    """
    result = search_policy(
        SessionContext(ROLE_MEMBER, _active_member_id()),
        question="会员卡一共有几种，各自多少钱？",
    )

    assert result.ok is True

    data = result.data
    assert {"question", "strategy_used", "sufficient", "rounds", "docs"} <= set(data)
    assert data["strategy_used"] in available_strategies()
    assert isinstance(data["sufficient"], bool)
    assert 1 <= len(data["rounds"]) <= 2
    assert data["docs"], "检索结果为空，检查 Milvus 里 knowledge_docs 是否已入库"
    assert all(doc["title"] for doc in data["docs"])


# ================================================================ 爽约停约规则


@pytest.fixture
def make_no_show_member():
    """工厂 fixture：造一个「上一自然月爽约 n 次」的临时会员，测试结束统一清理。

    为什么需要它：`_booking_suspended` 只在「本月 1-7 号」才可能返回停约，
    而测试大概率不跑在月初；seed 数据里也凑不出一个会员 3 次爽约。
    所以自己造数据 + 由测试注入一个月初的时间点。
    """
    created: list[tuple[int, list[int]]] = []

    def _make(count: int) -> int:
        now = datetime.now(BEIJING_TZ)
        month_start = datetime(now.year, now.month, 1, tzinfo=BEIJING_TZ)
        prev_last = month_start - timedelta(days=1)

        member_id = _new_temp_member()
        schedules: list[int] = []
        created.append((member_id, schedules))

        for day in (1, 8, 15, 22):
            if len(schedules) >= count:
                break
            schedule_id = _new_temp_schedule(
                capacity=5,
                starts_at=datetime(
                    prev_last.year, prev_last.month, day, 10, 0, tzinfo=BEIJING_TZ
                ),
                status="finished",
            )
            schedules.append(schedule_id)
            _execute(
                """
                INSERT INTO bookings (member_id, schedule_id, status)
                VALUES (%s, %s, 'no_show')
                """,
                (member_id, schedule_id),
            )
        return member_id

    yield _make

    for member_id, schedules in created:
        _execute("DELETE FROM bookings WHERE member_id = %s", (member_id,))
        for schedule_id in schedules:
            _execute("DELETE FROM schedules WHERE id = %s", (schedule_id,))
        _execute("DELETE FROM members WHERE id = %s", (member_id,))


def _month_start() -> datetime:
    now = datetime.now(BEIJING_TZ)
    return datetime(now.year, now.month, 1, tzinfo=BEIJING_TZ)


def test_no_show_suspension_triggers_in_first_7_days(make_no_show_member):
    """每自然月累计爽约 3 次 → 自次月起 7 天内暂停预约权限。

    注入时间点而不是伪造时间：函数本身是纯规则判断，`moment` 就是它的入参，
    传一个真实的月初时间点进来，读的还是库里的真实数据。
    """
    member_id = make_no_show_member(3)

    with get_db_conn() as conn, conn.cursor() as cur:
        reason = _booking_suspended(cur, member_id, _month_start() + timedelta(days=2))

    assert reason is not None
    assert "爽约" in reason


def test_no_show_suspension_needs_three(make_no_show_member):
    """两次不该停 —— 界限是「累计 3 次」，把 >= 写成 >= 2 是很容易犯的错。"""
    member_id = make_no_show_member(2)

    with get_db_conn() as conn, conn.cursor() as cur:
        reason = _booking_suspended(cur, member_id, _month_start() + timedelta(days=2))

    assert reason is None


def test_no_show_suspension_lapses_after_day_7(make_no_show_member):
    """7 天期限真的在收敛：同一个人、同一批数据，到了月中就不该再被拦。"""
    member_id = make_no_show_member(3)

    with get_db_conn() as conn, conn.cursor() as cur:
        reason = _booking_suspended(cur, member_id, _month_start() + timedelta(days=14))

    assert reason is None


# ================================================================ 已知覆盖缺口
#
# 1. `book_class` 内部走 _now()，时间无法注入。所以「因爽约停约导致 book_class 拒绝」这条
#    完整链路覆盖不到，上面三条只验证到规则函数 `_booking_suspended` 这一层。
#    「测试跑在月初 1-7 号」时才会顺带走到，属于不确定的覆盖。
# 2. 「status 仍是 active、但 expire_date 已过」的会员需要脏数据才能造出来，
#    seed 不会产生这种组合，所以 book_class 里那条独立分支只被间接覆盖。
# 3. 防超卖的真并发（多线程同时抢最后一个名额）没有覆盖，本文件只验证了「满员拒绝」这条静态分支。
