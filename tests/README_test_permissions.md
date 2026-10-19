# test_permissions.py 用例说明

权限与数据归属的测试。**13 条用例，全部真连 PostgreSQL，无 mock、无打桩、无跳过。**

被测对象：`agent/permissions.py`（权限矩阵与归属校验）、`agent/tools.py`（工具第一行的硬校验）。

```powershell
pytest tests/test_permissions.py -v
```

## 为什么这些用例非要连库

权限校验发生在每个工具的**第一行**、碰库之前，光看「抛了异常」证明不了什么 —— 「拒绝了」和「数据本来就不存在」在异常层面长得一模一样。

所以每条用例都先用直连 SQL 确认目标数据真实存在，再断言工具拒绝；关键的几条还配**正面对照**（同一份数据换个身份就能拿到），用来证明拒绝源于角色，而不是数据缺失。

覆盖的核心约定：

- 权限对**未登记的工具**也 fail-closed —— 将来新增工具忘了登记，结果必须是被拒，而不是被放行
- 会话身份（role / user_id）由会话层注入，**绝不进入模型可见的工具参数**
- 越权一律**拒绝而非静默改写**：member 传别人的 `member_id` 是能被发现的事件，不该被悄悄「修正」成自己的
- 被拒的调用**零数据库副作用**

## 用例清单

### 一、member 越权（5 条）

| 用例 | 覆盖点 |
|---|---|
| `test_member_cannot_read_other_members_balance` | 会员查别人的会籍与消费概览 → `PermissionDenied`。**前置先用直连 SQL 证明会员 2 存在且名下有流水** |
| `test_admin_can_read_member_balance` | 〔正面对照〕管理员查**同一份**数据 → 成功。与上一条唯一的变量是身份 |
| `test_member_cannot_book_for_other_member` | 会员替别人约课 → `PermissionDenied`，且 `bookings` 行数不变 |
| `test_member_cannot_cancel_other_members_booking` | 会员取消别人的预约 → `ToolNotFoundError`，且那条预约状态一个字节没变 |
| `test_member_cannot_call_coach_tool` | 会员调 `generate_daily_report` → `PermissionDenied` |

### 二、coach 越权（3 条）

教练是员工不是会员，`members` 表里没有对应实体。

| 用例 | 覆盖点 |
|---|---|
| `test_coach_cannot_call_member_tools` | **逐一**验证 `book_class` / `cancel_booking` / `get_member_balance` 三个都被拒。三个工具的 `required_role` 是分别登记的，漏登记一个就是真的漏了 |
| `test_coach_cannot_read_other_coachs_report` | 教练传别人的 `coach_id` → `PermissionDenied`。**先确认那位教练当天确实有课**，排除「查不到数据」的干扰 |
| `test_coach_report_scoped_to_self` | 〔正面对照〕教练不传 `coach_id` 时，返回的每一条都是自己的课 |

### 三、admin 的边界（1 条）

| 用例 | 覆盖点 |
|---|---|
| `test_admin_must_specify_member_id` | 管理员在库里没有实体（没有 `users` / `admins` 表），推断不出「本人」，必须显式给 `member_id`。**拒绝而不是返回空数据** —— 后者会让上层以为「这个会员没有任何记录」 |

### 四、权限矩阵本身（4 条）

| 用例 | 覆盖点 |
|---|---|
| `test_unknown_tool_is_denied_fail_closed` | 未登记工具一律拒绝（fail-closed 的关键） |
| `test_call_tool_rejects_params_outside_whitelist` | 白名单外的参数在进函数前就被挡掉，且归成 `error_kind="permission"`。**回归点**：`ToolPermissionError` 一度被归成 `"tool"`，那样 loop 会白白重试 |
| `test_denied_calls_leave_no_db_side_effect` | 6 次越权调用跑完，`members` / `schedules` / `bookings` / `transactions` 行数完全不变 |
| `test_session_context_rejects_invalid_identity` | 非法角色、member 不带 `user_id` → `ValueError`；admin 不带则合法（别把合法的也一起拦了） |

## 前置条件

- PostgreSQL 在线，库内已灌入 seed 数据
- 不需要 Milvus / Ollama / DeepSeek —— 本文件零网络依赖（只连数据库）

**数据前提**：会员 1、2、6、8、16、18、19、20 活跃且未过期；会员 3 已过期、4 已冻结；教练 1–5。会员 id 与角色分布**不随重灌漂移**（seed 按「距今天的相对天数」生成会籍，绝对日期平移但相对关系不变），所以本文件可以硬编码会员 id。

## 副作用

**没有副作用。** 本文件全是只读用例，跑完数据库不会有任何变化，不需要清理。

## 异常类型说明

`PermissionDenied` 继承 `PermissionError`，所以：

```python
with pytest.raises(PermissionError):   # 契约层面：调用方只需 except PermissionError
```

也能兜住 `tools/search_policy.py::check_permission` 抛出的原生 `PermissionError`。需要精确断言具体类型时（`test_member_cannot_read_other_members_balance` 里做了一次 `isinstance(exc.value, PermissionDenied)`），再单独 import。
