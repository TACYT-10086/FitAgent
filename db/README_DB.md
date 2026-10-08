# FitAgent 数据库说明（init.sql）

本文件说明 `init.sql` 中定义的全部数据表、字段、约束与索引。数据库为 **PostgreSQL**，建表脚本全部使用 `CREATE TABLE IF NOT EXISTS` 和 `CREATE INDEX IF NOT EXISTS`，可重复执行。

## 一、表关系总览

共 **8 张表**，外键关系如下：

```
courses(课程) ──< schedules(排课场次) >── coaches(教练)
                      │
                      └──< bookings(预约) >── members(会员)
                                                │
members(会员) ──< transactions(消费流水) ────────┘

policies(规章制度)        独立表
knowledge_docs(知识语料)  独立表，供 RAG 检索
```

外键删除策略：

- `ON DELETE CASCADE`（级联删除）：删除主记录时，连带删除相关记录。
- `ON DELETE RESTRICT`（限制删除）：只要还存在相关记录，主记录就不允许删除。

## 二、通用约定

- 主键统一为 `id BIGSERIAL`（自增大整数）。
- 业务时间统一为 `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`（带时区，默认当前时间）。
- 所有枚举类字段用 `TEXT + CHECK` 限定取值。
- `NOT NULL` 表示必填；未标注的字段允许为空。

## 三、表与字段说明

### 1. members（会员表）

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | BIGSERIAL | PRIMARY KEY | 会员 ID，自增主键 |
| name | TEXT | NOT NULL | 会员姓名 |
| phone | TEXT | NOT NULL, UNIQUE | 手机号，全局唯一 |
| gender | TEXT | NOT NULL, CHECK | 性别：`male` / `female` |
| birth_date | DATE | 可空 | 出生日期 |
| card_type | TEXT | NOT NULL, CHECK | 卡类型：`monthly`(月卡) / `quarterly`(季卡) / `yearly`(年卡) |
| join_date | DATE | NOT NULL | 入会日期 |
| expire_date | DATE | NOT NULL | 会籍到期日期 |
| status | TEXT | NOT NULL, DEFAULT `active`, CHECK | 状态：`active`(正常) / `expired`(已过期) / `frozen`(已冻结) |
| created_at | TIMESTAMPTZ | NOT NULL, DEFAULT now() | 记录创建时间 |

表级约束：`CHECK (expire_date >= join_date)`，到期日期不能早于入会日期。

### 2. coaches（教练表）

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | BIGSERIAL | PRIMARY KEY | 教练 ID，自增主键 |
| name | TEXT | NOT NULL | 教练姓名 |
| phone | TEXT | NOT NULL, UNIQUE | 手机号，全局唯一 |
| gender | TEXT | NOT NULL, CHECK | 性别：`male` / `female` |
| specialty | TEXT | NOT NULL | 专长（如增肌、瑜伽、拳击） |
| bio | TEXT | 可空 | 个人简介 |
| hourly_rate | NUMERIC(10,2) | NOT NULL, CHECK ≥ 0 | 课时费（元），非负 |
| hire_date | DATE | NOT NULL | 入职日期 |
| status | TEXT | NOT NULL, DEFAULT `active`, CHECK | 状态：`active`(在职) / `leave`(休假) |
| created_at | TIMESTAMPTZ | NOT NULL, DEFAULT now() | 记录创建时间 |

### 3. courses（课程目录表）

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | BIGSERIAL | PRIMARY KEY | 课程 ID，自增主键 |
| name | TEXT | NOT NULL, UNIQUE | 课程名称，全局唯一 |
| category | TEXT | NOT NULL, CHECK | 分类：`yoga` / `strength` / `cardio` / `pilates` / `boxing` / `dance` |
| description | TEXT | 可空 | 课程简介 |
| duration_min | INT | NOT NULL, CHECK > 0 | 单节时长（分钟） |
| capacity | INT | NOT NULL, CHECK > 0 | 人数上限 |
| difficulty | TEXT | NOT NULL, CHECK | 难度：`beginner`(初级) / `intermediate`(中级) / `advanced`(高级) |
| created_at | TIMESTAMPTZ | NOT NULL, DEFAULT now() | 记录创建时间 |

### 4. schedules（排课表 / 课程的具体场次）

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | BIGSERIAL | PRIMARY KEY | 场次 ID，自增主键 |
| course_id | BIGINT | NOT NULL, FK → courses(id), ON DELETE CASCADE | 所属课程；删除课程时连带删除场次 |
| coach_id | BIGINT | NOT NULL, FK → coaches(id), ON DELETE RESTRICT | 授课教练；教练仍有排课时禁止删除 |
| start_time | TIMESTAMPTZ | NOT NULL | 场次开始时间 |
| end_time | TIMESTAMPTZ | NOT NULL | 场次结束时间 |
| room | TEXT | NOT NULL | 上课教室（如团操房 A、瑜伽室） |
| capacity | INT | NOT NULL, CHECK > 0 | 该场次人数上限 |
| status | TEXT | NOT NULL, DEFAULT `scheduled`, CHECK | 状态：`scheduled`(已排课) / `finished`(已结束) / `cancelled`(已取消) |
| created_at | TIMESTAMPTZ | NOT NULL, DEFAULT now() | 记录创建时间 |

表级约束：`CHECK (end_time > start_time)`，结束时间必须晚于开始时间。

### 5. bookings（预约表）

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | BIGSERIAL | PRIMARY KEY | 预约 ID，自增主键 |
| member_id | BIGINT | NOT NULL, FK → members(id), ON DELETE CASCADE | 预约会员；删除会员时连带删除预约 |
| schedule_id | BIGINT | NOT NULL, FK → schedules(id), ON DELETE CASCADE | 预约场次；删除场次时连带删除预约 |
| booked_at | TIMESTAMPTZ | NOT NULL, DEFAULT now() | 预约时间 |
| status | TEXT | NOT NULL, DEFAULT `booked`, CHECK | 状态：`booked`(已预约) / `attended`(已出勤) / `cancelled`(已取消) / `no_show`(爽约) |

表级约束：`UNIQUE (member_id, schedule_id)`，同一会员对同一场次只能预约一次（该约束会自动生成一个复合唯一索引）。

### 6. transactions（消费流水表）

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | BIGSERIAL | PRIMARY KEY | 流水 ID，自增主键 |
| member_id | BIGINT | NOT NULL, FK → members(id), ON DELETE CASCADE | 所属会员；删除会员时连带删除流水 |
| amount | NUMERIC(10,2) | NOT NULL, CHECK ≥ 0 | 金额（元），非负 |
| kind | TEXT | NOT NULL, CHECK | 业务类型：`membership`(会籍) / `course`(团课课时包) / `personal_training`(私教) / `refund`(退款) |
| method | TEXT | NOT NULL, CHECK | 支付方式：`wechat` / `alipay` / `cash` / `card` |
| status | TEXT | NOT NULL, DEFAULT `paid`, CHECK | 状态：`paid`(已支付) / `pending`(待支付) / `refunded`(已退款) |
| note | TEXT | 可空 | 备注（如"会籍续费""私教课时包购买"） |
| created_at | TIMESTAMPTZ | NOT NULL, DEFAULT now() | 记录创建时间 |

### 7. policies（规章制度表）

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | BIGSERIAL | PRIMARY KEY | 制度 ID，自增主键 |
| title | TEXT | NOT NULL, UNIQUE | 制度标题，全局唯一 |
| category | TEXT | NOT NULL | 分类（如会员卡、退费、约课、私教、场馆、合规） |
| content | TEXT | NOT NULL | 制度正文 |
| effective_date | DATE | NOT NULL | 生效日期 |
| created_at | TIMESTAMPTZ | NOT NULL, DEFAULT now() | 记录创建时间 |

### 8. knowledge_docs（RAG 知识语料表）

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | BIGSERIAL | PRIMARY KEY | 文档 ID，自增主键 |
| title | TEXT | NOT NULL | 文档标题（常见问题） |
| content | TEXT | NOT NULL | 文档正文，供检索与生成使用 |
| category | TEXT | NOT NULL | 分类（如场馆、会员卡、约课、私教、到店、安全） |
| source | TEXT | 可空 | 来源（如会员手册、安全须知、到店指引） |
| created_at | TIMESTAMPTZ | NOT NULL, DEFAULT now() | 记录创建时间 |

## 四、索引

| 索引名 | 所在表（字段） | 作用 |
|---|---|---|
| idx_schedules_start_time | schedules (start_time) | 加速按时间范围查询场次，如"明天/未来一周的课" |
| idx_bookings_member | bookings (member_id) | 加速按会员查询预约，如"我约了哪些课" |
| idx_transactions_member | transactions (member_id) | 加速按会员查询流水，如"我的消费记录" |

> 主键、UNIQUE 字段会由数据库自动建立索引，无需手动创建。

## 五、枚举取值速查

| 表.字段 | 允许取值 |
|---|---|
| members.gender | male, female |
| members.card_type | monthly, quarterly, yearly |
| members.status | active, expired, frozen |
| coaches.gender | male, female |
| coaches.status | active, leave |
| courses.category | yoga, strength, cardio, pilates, box, dance |
| courses.difficulty | beginner, intermediate, advanced |
| schedules.status | scheduled, finished, cancelled |
| bookings.status | booked, attended, cancelled, no_show |
| transactions.kind | membership, course, personal_training, refund |
| transactions.method | wechat, alipay, cash, card |
| transactions.status | paid, pending, refunded |