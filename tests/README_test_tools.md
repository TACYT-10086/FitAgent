# test_tools.py 用例说明

`agent/tools.py` 七个业务工具的核心逻辑测试。**29 个测试函数 / 30 条用例，全部真连 PostgreSQL，无 mock、无打桩、无跳过。**

```powershell
pytest tests/test_tools.py -v
```

> ⚠ **真实 API 调用**：`test_search_policy_end_to_end_real_api` 会**真的请求 DeepSeek**，并依赖 Milvus 与 Ollama 在线，单次约 7 秒、会真的花钱。其余 29 条只连 PostgreSQL。
>
> 这条用例刻意**不带 `llm` 标记**，因此不会被 `tests/conftest.py` 的跳过逻辑拦下。按约定，这类用例要么真跑、要么删掉，不留「环境变量不存在就跳过」的中间态。

## 两条贯穿全文件的约定

### 1. 不硬编码场次 / 预约 id

seed 的排课是按「距灌库当天的相对天数」生成的，而且 `status` 还取决于**灌库那一刻的钟点**（同一场次，早上 6 点灌库是 `scheduled`，中午灌库就是 `finished`）。所以重灌一次或隔几天再跑，绝对日期与 id 就整体平移。

所有用例都在运行时用 SQL 定位符合条件的那一行，定位不到就带着原因**直接失败** —— 不跳过、不静默放过。

### 2. 写库用例一律 `try/finally` 自清理

跑完库里回到 seed 原状，可反复执行、不累积脏数据；断言失败也能还原。三类清理：

| 场景 | 清理方式 |
|---|---|
| 新建的预约 | `DELETE FROM bookings WHERE id = ...` |
| 还原既有行（复活 / 取消） | `UPDATE bookings SET status = '原状态' WHERE id = ...` |
| 临时会员 / 临时场次 | 先删 `bookings`，再删 `schedules`，最后删 `members` |

**验证没留脏数据**（应仍为 30 / 40）：

```powershell
python -c "from db.connection import get_db_conn; c=get_db_conn(); cur=c.cursor(); cur.execute('select count(*) from bookings'); print(cur.fetchone()); cur.execute('select count(*) from transactions'); print(cur.fetchone())"
```

## 用例清单

### 一、`query_schedule`（3 条）

| 用例 | 覆盖点 |
|---|---|
| `test_query_schedule_defaults_to_future_only` | 不带日期 → 每条 `start_time > now`。**钉死一个真实缺陷**：早先没有默认下界，会把过去两周的历史场次一起捞出来，而模型极易把它们当成「还可预约」报给用户（实测真的发生过） |
| `test_query_schedule_explicit_past_date_returns_history` | 显式传历史日期 → 正常返回，条数与独立 SQL 核对。默认只看未来是「不替调用方猜」，不是「禁止查历史」，两件事必须都成立 |
| `test_query_schedule_rejects_invalid_category` | 类别是枚举，拼错要报错，而不是当成「没有这种课」返回空列表 |

### 二、`book_class`（9 个函数 / 10 条）

| 用例 | 覆盖点 |
|---|---|
| `test_book_class_really_writes_row` | 成功路径必须真的落库。断言走**直连 SQL** 而非工具返回值 —— 工具说自己 ok，和库里真有一行，是两件事 |
| `test_book_class_revives_cancelled_booking` | 已取消的场次再约 → 走 `UPDATE` 复活（`revived=True`，复用原 `booking_id`）。必须走这条路径：`UNIQUE(member_id, schedule_id)` 决定了直接 `INSERT` 会撞唯一约束 |
| `test_book_class_rejects_duplicate_booking` | 重复预约 → `ToolConflictError`。唯一约束其实也兜得住，但那是数据库错误，不是模型能读懂的业务错误 |
| `test_book_class_rejects_finished_schedule` | 场次已结束 → 含「已结束」 |
| `test_book_class_rejects_started_schedule` | 状态仍是 `scheduled` 但开课时间已过 → 含「已经开课」。与上一条是**两条不同分支**，只测一条会漏掉另一半 |
| `test_book_class_rejects_beyond_booking_window` | 超出 7 天窗口 → 含「7 天」。政策原文：「系统每日 20:00 开放次日起 7 天内的场次」 |
| `test_book_class_rejects_inactive_member[expired]`<br>`test_book_class_rejects_inactive_member[frozen]` | 卡过期 / 卡冻结 → `ToolConflictError`（参数化 ×2） |
| `test_book_class_rejects_full_schedule` | **造临时数据**：capacity=1 的场次被别人占满 → 含「满员」。seed 里没有任何满员场次，必须自己造 |
| `test_book_class_rejects_missing_schedule` | `schedule_id` 不存在 → `ToolNotFoundError` |

### 三、`cancel_booking`（4 条）

| 用例 | 覆盖点 |
|---|---|
| `test_cancel_booking_free_window` | 距开课 >4 小时 → 状态记 `cancelled`，note 含「免费取消」 |
| `test_cancel_booking_within_4h_counts_as_no_show` | **造临时数据**：now+2h 开课的场次 → 仍然取消，但按规章记一次爽约（`no_show`），note 含「爽约」。政策原文：「不足 4 小时取消或未到场的，计为一次爽约」 |
| `test_cancel_booking_rejects_already_cancelled` | 已取消 → 含「已经是取消状态」 |
| `test_cancel_booking_rejects_attended` | 已出席 → 含「不可取消」。只允许 `booked → cancelled / no_show` |

**取消是 UPDATE `status`，不是 DELETE** —— 历史要留着，出勤与爽约统计都依赖它。

### 四、`get_member_balance`（4 条）

| 用例 | 覆盖点 |
|---|---|
| `test_get_member_balance_derives_from_transactions` | **核心用例**。刻意走**与工具不同的代码路径**：把原始流水行取回来，在 Python 侧按政策口径自己算一遍再比对。直接用同样的 SQL 聚合去比，等于拿工具的 SQL 验证工具的 SQL。另断言 `paid_total ≠ 全部流水求和`，钉住「退款不能算进消费额」 |
| `test_get_member_balance_excludes_pending_from_paid` | 名下只有一笔 pending 流水的会员 → `paid_total == 0` 且 `pending_transactions > 0`。`status` 过滤要是漏了，他会被算成「已消费 299 元」 |
| `test_get_member_balance_flags_expired_member` | 已过期会员 → `expired=True`、`remaining_days < 0` |
| `test_get_member_balance_carries_no_balance_note` | 返回必带「未存储余额」说明。库里确实没有 `balance` 列，这是防止模型顺着「余额」这个词编造金额的唯一护栏 |

### 五、`generate_daily_report`（3 条）

| 用例 | 覆盖点 |
|---|---|
| `test_daily_report_admin_sees_whole_venue_and_revenue` | 管理员 → `scope == "全场"`、`schedule_count` 与独立 SQL 核对、返回带 `revenue` |
| `test_daily_report_totals_match_schedules` | `totals` 五个字段 == 明细逐条求和。报表分两处各算一遍，是最容易对不上的地方 |
| `test_daily_report_coach_has_no_revenue` | 教练视角 → **不含** `revenue` 键（而不是值为 0）。营收不给教练 |

### 六、`recommend_course`（2 条）

| 用例 | 覆盖点 |
|---|---|
| `test_recommend_course_only_future_within_window` | 全部落在 `(now, now+7d]` 且状态为 `scheduled`，并按「空位比例升序」排序（更容易成班的排前面） |
| `test_recommend_course_excludes_members_own_bookings` | 会员已在同一场上约过的，不再推荐给同一人 |

### 七、`search_policy`（1 条，真实 API 调用）

| 用例 | 覆盖点 |
|---|---|
| `test_search_policy_end_to_end_real_api` | **⚠ 真打 DeepSeek + Milvus + Ollama**。断言返回结构完整、`strategy_used` 属于注册表、`sufficient` 是 bool、`rounds` 在 1–2 之间、`docs` 非空 |

刻意不做任何打桩 —— 「选策略 → 检索 → 评分 → 不相关就换策略重试」这条 Agentic 链路的价值全在模型真的做判断上，桩掉之后测的只是自己写的 if-else。

### 八、爽约停约规则（3 条）

政策原文：「每自然月累计爽约 3 次，自次月起 7 天内暂停预约权限」。

| 用例 | 覆盖点 |
|---|---|
| `test_no_show_suspension_triggers_in_first_7_days` | **造临时数据** + 注入月初时间点 → 返回停约原因 |
| `test_no_show_suspension_needs_three` | 爽约 2 次 → 不拦。界限是「累计 3 次」，把 `>=` 写成 `>= 2` 是很容易犯的错 |
| `test_no_show_suspension_lapses_after_day_7` | 同一个人、同一批数据，时间推到月中 → 不再拦（7 天期限真的在收敛） |

辅助 fixture `make_no_show_member(count)`：造一个「上一自然月爽约 n 次」的临时会员，测试结束连同临时场次一起清掉。

## 前置条件

- PostgreSQL 在线，库内已灌入 seed 数据
- 仅 `test_search_policy_end_to_end_real_api` 需要 Milvus + Ollama + DeepSeek 在线，且 `knowledge_docs` 已入库

## 已知覆盖缺口

以下三处**测不到**，写在这里而不是藏着：

1. **`book_class` 因爽约停约而拒绝** —— 完整链路测不到。`book_class` 内部走 `_now()`，时间无法注入，而 `_booking_suspended` 只在「本月 1–7 号」才可能返回停约。所以只验证到规则函数 `_booking_suspended` 这一层；只有测试恰好跑在月初 1–7 号才会顺带走到完整链路，属于不确定的覆盖。
2. **「`status` 仍是 active、但 `expire_date` 已过」的会员** —— `book_class` 里这条独立分支需要脏数据才能触发，seed 不会产生这种组合。`test_book_class_rejects_inactive_member` 命中的是「非 active」那条分支（错误信息里带具体状态名，所以断言的是状态名而不是「到期」二字）。
3. **防超卖的真并发** —— 多线程同时抢最后一个名额没有覆盖，只验证了「满员拒绝」这条静态分支。工具内部用 `SELECT ... FOR UPDATE` 锁行 + 锁内另起一条 `COUNT` 来防超卖，这段并发正确性目前依赖代码审查而非测试。
