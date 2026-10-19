"""权限与数据归属的真连接测试（真连 PostgreSQL，无打桩、无跳过）。

为什么这些用例非要连库：权限校验发生在每个工具的**第一行**、碰库之前，光看「抛了异常」证明不了
什么 —— 「拒绝了」和「数据本来就不存在」在异常层面长得一模一样。所以每条用例都先用直连 SQL
确认目标数据真实存在，再断言工具拒绝；关键的几条还配了**正面对照**（同一份数据换个身份就能拿到），
用来证明拒绝源于角色而不是数据缺失。

数据事实（seed 用固定随机种子 RANDOM_SEED=42，可复现）：
  会员 1、2、6、8、16、18、19、20 活跃且未过期；会员 3 已过期、4 已冻结；教练 1-5。
  会员 id 与角色分布不随重灌漂移（seed 按「距今天的相对天数」生成会籍，绝对日期平移但相对关系不变）。
"""

import pytest

from agent.permissions import (
    ROLE_ADMIN,
    ROLE_COACH,
    ROLE_MEMBER,
    PermissionDenied,
    SessionContext,
    require_role,
)
from agent.tools import (
    ToolNotFoundError,
    book_class,
    call_tool,
    cancel_booking,
    generate_daily_report,
    get_member_balance,
)
from db.connection import get_db_conn

MEMBER_SELF = SessionContext(ROLE_MEMBER, 1)
MEMBER_OTHER_ID = 2  # 会员 1 想越权访问的对象


def _scalar(sql: str, args: tuple = ()):
    """跑一条单值查询。测试用它做前置/后置核对，**不经过被测代码**。"""
    with get_db_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchone()[0]


def _fetchall(sql: str, args: tuple = ()) -> list[tuple]:
    with get_db_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


_TRACKED_TABLES = ("members", "schedules", "bookings", "transactions")


def _table_counts() -> dict[str, int]:
    """几张会被越权操作波及的表当前行数，用于「零副作用」断言。"""
    counts: dict[str, int] = {}
    with get_db_conn() as conn, conn.cursor() as cur:
        for table in _TRACKED_TABLES:
            cur.execute(f"SELECT count(*) FROM {table}")
            counts[table] = cur.fetchone()[0]
    return counts


# ---- member 越权 -----------------------------------------------------------------


def test_member_cannot_read_other_members_balance():
    """会员不能查别人的会籍与消费概览。

    前置断言刻意做足：先证明会员 2 在库里真实存在、名下确实有流水。
    否则这条用例在「会员 2 被删了」的情况下也会通过，等于什么都没测。
    """
    assert _scalar("SELECT count(*) FROM members WHERE id = %s", (MEMBER_OTHER_ID,)) == 1
    assert _scalar(
        "SELECT count(*) FROM transactions WHERE member_id = %s", (MEMBER_OTHER_ID,)
    ) > 0

    with pytest.raises(PermissionError) as exc:
        get_member_balance(SessionContext(ROLE_MEMBER, 1), member_id=MEMBER_OTHER_ID)

    # 拒绝而不是静默改写：越权尝试应该是一个能被发现的事件
    assert isinstance(exc.value, PermissionDenied)
    assert "只能操作自己的数据" in str(exc.value)


def test_admin_can_read_member_balance():
    """正面对照：管理员显式指定 member_id 就能读到。

    这条和上一条用的是**同一份数据**，唯一变量是身份 —— 两条一起才构成完整的证明。
    """
    result = get_member_balance(SessionContext(ROLE_ADMIN), member_id=MEMBER_OTHER_ID)

    assert result.ok is True
    assert result.data["member_id"] == MEMBER_OTHER_ID


def test_member_cannot_book_for_other_member():
    """会员不能替别人约课，且这次越权不该在库里留下任何痕迹。"""
    before = _scalar("SELECT count(*) FROM bookings")

    with pytest.raises(PermissionError) as exc:
        book_class(MEMBER_SELF, schedule_id=1, member_id=MEMBER_OTHER_ID)

    assert "只能操作自己的数据" in str(exc.value)
    assert _scalar("SELECT count(*) FROM bookings") == before


def test_member_cannot_cancel_other_members_booking():
    """会员碰不到别人的预约。

    cancel_booking 的做法是**按会话主体把查询范围收窄**（WHERE b.member_id = 会话主体），
    所以别人的预约在他眼里根本不存在，报的是「找不到」而不是「无权限」——
    这同时避免了通过错误信息探测别人预约是否存在。真正要断言的是：那条预约一个字节都没变。
    """
    booking_id = _scalar(
        """
        SELECT b.id FROM bookings b
        WHERE b.member_id <> 1 AND b.status = 'booked'
        ORDER BY b.id LIMIT 1
        """
    )
    assert booking_id is not None, "seed 数据里找不到属于别人的 booked 预约"

    before_status = _scalar("SELECT status FROM bookings WHERE id = %s", (booking_id,))

    with pytest.raises(ToolNotFoundError) as exc:
        cancel_booking(MEMBER_SELF, booking_id=booking_id)

    assert "找不到" in str(exc.value)
    assert _scalar("SELECT status FROM bookings WHERE id = %s", (booking_id,)) == before_status


def test_member_cannot_call_coach_tool():
    """generate_daily_report 只开放给 coach / admin。

    不传 date：require_role 是第一行，会在任何参数被解读之前就拦下，日期值在这里没有意义。
    """
    with pytest.raises(PermissionError) as exc:
        generate_daily_report(MEMBER_SELF)

    assert "generate_daily_report" in str(exc.value)


# ---- coach 越权 ------------------------------------------------------------------


def test_coach_cannot_call_member_tools():
    """教练是员工不是会员，members 表里没有对应实体，三个会员维度工具一律拒绝。

    逐条断言而不是只测一个：三个工具的 required_role 是分别登记的，漏登记一个就是真的漏了。
    """
    coach = SessionContext(ROLE_COACH, 1)

    with pytest.raises(PermissionError):
        book_class(coach, schedule_id=1)

    with pytest.raises(PermissionError):
        cancel_booking(coach, booking_id=1)

    with pytest.raises(PermissionError):
        get_member_balance(coach)


def test_coach_cannot_read_other_coachs_report():
    """教练只能看自己带的课。

    先定位「别的教练确实有课」的那一天，这样拒绝就不可能是因为查不到数据。
    """
    rows = _fetchall(
        """
        SELECT (start_time AT TIME ZONE 'UTC')::date AS d, coach_id
        FROM schedules
        WHERE coach_id <> 1
        GROUP BY d, coach_id
        ORDER BY count(*) DESC, d
        LIMIT 1
        """
    )
    assert rows, "seed 数据里找不到教练 1 之外的课，无法验证归属隔离"
    day, other_coach = rows[0]

    with pytest.raises(PermissionError) as exc:
        generate_daily_report(
            SessionContext(ROLE_COACH, 1), date=day.isoformat(), coach_id=other_coach
        )

    assert "只能查看自己带的课程" in str(exc.value)


def test_coach_report_scoped_to_self():
    """正面对照：教练不传 coach_id 时，返回的必须全是自己的课。"""
    rows = _fetchall(
        """
        SELECT (start_time AT TIME ZONE 'UTC')::date AS d
        FROM schedules WHERE coach_id = 1
        GROUP BY d ORDER BY count(*) DESC, d LIMIT 1
        """
    )
    assert rows, "seed 数据里没有教练 1 的课"

    result = generate_daily_report(SessionContext(ROLE_COACH, 1), date=rows[0][0].isoformat())

    assert result.ok is True
    assert result.data["schedule_count"] > 0
    assert {item["coach_id"] for item in result.data["schedules"]} == {1}


# ---- admin 的边界 ----------------------------------------------------------------


def test_admin_must_specify_member_id():
    """管理员在库里没有实体（没有 users / admins 表），推断不出「本人」。

    这里必须拒绝，而不是返回空数据 —— 后者会让上层以为「这个会员没有任何记录」。
    """
    with pytest.raises(PermissionError) as exc:
        get_member_balance(SessionContext(ROLE_ADMIN))

    assert "member_id" in str(exc.value)


# ---- 权限矩阵本身 ------------------------------------------------------------------


def test_unknown_tool_is_denied_fail_closed():
    """没登记的工具一律拒绝。

    这是 fail-closed 的关键：将来新增工具忘了登记权限，结果必须是「被拒」，
    而不是「因为没查到限制所以放行」。
    """
    with pytest.raises(PermissionError) as exc:
        require_role("drop_all_tables", SessionContext(ROLE_ADMIN))

    assert "未知工具" in str(exc.value)


def test_call_tool_rejects_params_outside_whitelist():
    """白名单之外的参数在进函数前就被挡掉，且必须归成 permission。

    对 member 而言 member_id 根本不在可用参数里 —— 模型塞进来属于越权尝试，
    应该被拒绝并留痕，而不是静默丢弃后照常执行（回归 _ERROR_KINDS 的子类顺序问题：
    ToolPermissionError 一度被归成 "tool"，那样 loop 就会白白重试）。
    """
    result = call_tool("get_member_balance", MEMBER_SELF, {"member_id": MEMBER_OTHER_ID})

    assert result.ok is False
    assert result.error_kind == "permission"
    assert "member_id" in result.error
    assert result.data == {}


def test_denied_calls_leave_no_db_side_effect():
    """越权调用必须止步于权限门，一行数据都不许动。"""
    member = SessionContext(ROLE_MEMBER, 1)
    coach = SessionContext(ROLE_COACH, 1)
    admin = SessionContext(ROLE_ADMIN)

    before = _table_counts()

    attempts = (
        lambda: book_class(member, schedule_id=1, member_id=MEMBER_OTHER_ID),
        lambda: get_member_balance(member, member_id=MEMBER_OTHER_ID),
        lambda: book_class(coach, schedule_id=1),
        lambda: get_member_balance(coach),
        lambda: generate_daily_report(member),
        lambda: get_member_balance(admin),
    )
    for attempt in attempts:
        with pytest.raises(PermissionError):
            attempt()

    assert _table_counts() == before


def test_session_context_rejects_invalid_identity():
    """身份在会话构造期就该立住。

    角色不合法、或非管理员却不带主体 id，都不该被造出来 —— 一个 user_id 为 None 的 member
    会话会让下游的归属校验全部失去基准。
    """
    with pytest.raises(ValueError):
        SessionContext("superuser", 1)

    with pytest.raises(ValueError):
        SessionContext(ROLE_MEMBER, None)

    # 管理员没有实体，允许不带 user_id —— 别把合法的也一起拦了
    assert SessionContext(ROLE_ADMIN).user_id is None
