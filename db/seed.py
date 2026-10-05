"""建表 + 生成模拟数据。

幂等：可反复执行，跑多少次库里都是同样这批数据（时间戳除外）。
用法：python -m db.seed
"""

import random
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from db.connection import get_db_conn

INIT_SQL_PATH = Path(__file__).resolve().parent / "init.sql"

# 固定随机种子：每次生成的数据一致，出问题能稳定复现
RANDOM_SEED = 42

# 各表目标行数
COUNTS = {
    "members": 20,
    "coaches": 5,
    "courses": 10,
    "schedules": 50,
    "bookings": 30,
    "transactions": 40,
    "policies": 10,
    "knowledge_docs": 20,
}

# TRUNCATE 用的表名。取自本文件常量，拼进 SQL 无注入风险
TABLES = tuple(COUNTS)

SURNAMES = "王李张刘陈杨黄赵周吴徐孙马朱胡林郭何高罗"
GIVEN_NAMES = [
    "伟", "芳", "娜", "秀英", "敏", "静", "丽", "强", "磊", "洋",
    "艳", "勇", "军", "杰", "娟", "涛", "明", "超", "平", "刚",
    "霞", "桂英", "鑫", "晨", "宇", "倩", "鹏", "琳",
]

ROOMS = ("团操房A", "团操房B", "瑜伽室", "单车房", "力量区", "拳击台")

# (姓名, 性别, 专长, 简介, 课时费)
COACHES = (
    ("张伟", "male", "增肌/力量训练", "国家健身教练职业资格认证，8 年力量训练教学经验，擅长自由重量技术打磨。", 300),
    ("李娜", "female", "瑜伽/普拉提", "RYT-500 认证瑜伽导师，5 年教学经验，主授哈他瑜伽与普拉提塑形。", 260),
    ("王强", "male", "拳击/搏击", "省级拳击队退役运动员，擅长零基础拳击入门与减脂搏击操。", 280),
    ("陈静", "female", "减脂/有氧", "运动营养师，专注体重管理与体态改善，累计服务学员超 400 人。", 240),
    ("刘洋", "male", "体能/综合训练", "CrossFit L1 认证教练，12 年体能训练经验，擅长运动表现提升。", 320),
)

# (课程名, 分类, 简介, 时长分钟, 人数上限, 难度)
COURSES = (
    ("燃脂单车", "cardio", "跟随音乐节奏进行高强度间歇骑行，单节课消耗 400-600 千卡。", 45, 20, "beginner"),
    ("核心力量训练", "strength", "针对腹横肌与下背部的稳定性训练，改善久坐导致的腰背不适。", 60, 15, "intermediate"),
    ("哈他瑜伽", "yoga", "以体式与呼吸配合为主的基础瑜伽，适合零基础学员放松与柔韧提升。", 60, 20, "beginner"),
    ("普拉提塑形", "pilates", "借助小器械强化深层肌肉，改善体态与线条，对腰部友好。", 50, 12, "intermediate"),
    ("拳击基础", "boxing", "步伐、闪避与出拳组合的基础训练，兼顾心肺与协调性。", 60, 10, "beginner"),
    ("动感尊巴", "dance", "拉丁风格有氧舞蹈，全程跟跳，氛围轻松，适合减脂入门。", 45, 25, "beginner"),
    ("杠铃深蹲进阶", "strength", "大重量深蹲技术专项课，含保护架使用与呼吸法教学。", 75, 8, "advanced"),
    ("流瑜伽", "yoga", "体式串联流畅、强度中等偏上，强调核心控制与呼吸节奏。", 60, 18, "intermediate"),
    ("HIIT 高强度间歇", "cardio", "短时高强度爆发交替低强度恢复，课耗高，建议有一定基础。", 30, 16, "advanced"),
    ("战绳训练", "strength", "以战绳为主的全身体能训练，提升爆发力与心肺耐力。", 40, 12, "intermediate"),
)

# (标题, 分类, 正文, 生效日期)
POLICIES = (
    ("会员入会须知", "会员卡",
     "入会需持本人身份证实名登记，签署会员服务协议并完成缴费。入会当日须完成体成分检测与运动风险筛查，"
     "有心血管疾病、骨关节伤病等既往病史的，须提供医生许可证明。未满 16 周岁者需监护人陪同并签署知情同意书。",
     date(2024, 1, 1)),
    ("会员卡使用规则", "会员卡",
     "会员卡仅限本人使用，入场须刷脸或出示会员码核验，不得转借他人。会籍权益包含器械区与团操课不限次使用，"
     "不含私教课、体测复测及付费小班。会籍到期后未续费者，入场权限自动冻结。",
     date(2024, 1, 1)),
    ("退卡退款政策", "退费",
     "开卡 7 天内且未使用任何服务的，可申请全额退款。超过 7 天或已使用服务的，按剩余天数折算退款并扣除 20% 手续费；"
     "年卡退款需另行扣除已赠课时与体测的等值金额。退款自审核通过之日起 15 个工作日内退回原支付渠道。"
     "促销期间的折扣卡按实际支付金额折算。",
     date(2024, 6, 1)),
    ("会员卡冻结与延期规定", "会员卡",
     "年卡会员每自然年可申请冻结 2 次、每次不超过 30 天；季卡会员可申请 1 次、不超过 15 天；月卡不支持冻结。"
     "冻结须提前 3 个自然日在前台办理，冻结期间会籍有效期顺延。因伤病申请冻结的，凭三甲医院证明免收手续费，"
     "每年累计不超过 90 天。",
     date(2024, 6, 1)),
    ("团课预约与取消规则", "约课",
     "团操课须提前预约，系统每日 20:00 开放次日起 7 天内的场次。课程开始前 4 小时以上可免费取消；"
     "不足 4 小时取消或未到场的，计为一次爽约。每自然月累计爽约 3 次，自次月起 7 天内暂停预约权限。"
     "满员课程开放候补，递补通知须在 1 小时内确认，逾期视为放弃。",
     date(2025, 1, 1)),
    ("私教课程管理规定", "私教",
     "私教课按课时包销售，有效期为自购买之日起 12 个月。未开课的课时包可在 7 天内无理由全额退款；"
     "已开课的按剩余课时折算退款，扣除已上课时费用及 10% 违约金。单次私教课一经预约开课不予退款。"
     "因伤病或长期出差需延期的，凭证明可申请延长，最长 6 个月。私教课不得转让他人。",
     date(2025, 1, 1)),
    ("场馆安全与器械使用规定", "场馆",
     "使用器械前须确认配重片卡紧，使用后须将配重归位并擦拭器械表面。深蹲、卧推等大重量动作必须使用保护架"
     "或由同伴保护。禁止在器械区穿拖鞋、赤膊或饮食。训练中出现胸闷、头晕、关节剧痛须立即停止并示意工作人员。"
     "场馆配备急救箱与 AED，全体教练持急救证书。",
     date(2024, 1, 1)),
    ("更衣室与储物柜管理规定", "场馆",
     "更衣室与淋浴间免费开放，开放时间与营业时间一致。临时储物柜免费使用，需自备挂锁或在前台租用密码锁"
     "（押金 50 元，归还即退）。长期储物柜按月 100 元、按年 1000 元租用。柜内禁止存放易燃易爆物品，"
     "馆方每周一次例行清柜，逾期存放物品将统一保管 30 天后处理。",
     date(2024, 6, 1)),
    ("会员行为守则", "场馆",
     "请尊重其他会员与工作人员，禁止在场馆内大声喧哗、拍摄他人、推销商品或从事与健身无关的经营活动。"
     "器械使用请遵循先来后到，高峰时段单台器械连续占用不超过 20 分钟。损坏器械或设施须照价赔偿。"
     "违反守则且劝阻无效的，馆方有权暂停或终止其会籍，已缴费用不予退还。",
     date(2024, 1, 1)),
    ("会员信息与隐私保护政策", "合规",
     "本馆收集的会员姓名、身份证号、联系方式、体测与消费记录，仅用于会籍管理、课程预约、健康风险提示"
     "与法定税务申报，不向第三方出售或提供。会员可随时申请查询、更正或删除个人信息；"
     "删除后将会影响会籍与预约功能。人脸识别数据仅用于入场核验，本地加密存储，不对外传输。",
     date(2025, 1, 1)),
)

# (标题, 分类, 正文, 来源)
KNOWLEDGE_DOCS = (
    ("场馆营业时间", "场馆",
     "本馆营业时间为周一至周五 06:00-23:00，周六周日 08:00-22:00。法定节假日调整为 10:00-20:00，"
     "具体以门店公告为准。闭馆前 30 分钟停止入场，淋浴间提前 15 分钟关闭。", "场馆手册"),
    ("会员卡类型与价格", "会员卡",
     "我们提供三种会籍：月卡 299 元、季卡 799 元、年卡 2599 元。年卡额外赠送 2 次私教体验课和 1 次体成分检测。"
     "所有会籍均不限次数使用器械区与团操课，不含私教课。", "会员手册"),
    ("会员卡能否转让", "会员卡",
     "实名会员卡原则上不可转让。因搬家、长期出差等特殊原因确需转让的，可携带双方身份证到前台办理，"
     "每张卡在合约期内限转让一次，收取 200 元转让手续费，受让人须重新完成运动风险筛查。", "会员手册"),
    ("会员卡冻结规则", "会员卡",
     "年卡会员每自然年可申请冻结 2 次、每次不超过 30 天；季卡会员可冻结 1 次、不超过 15 天；月卡不支持冻结。"
     "冻结须提前 3 天在前台申请，冻结期间会籍有效期顺延。伤病冻结凭三甲医院证明可免手续费。", "会员手册"),
    ("退卡与退款政策", "退费",
     "开卡 7 天内且未使用任何服务可全额退款。超过 7 天或已使用服务的，按剩余天数折算退款并扣除 20% 手续费。"
     "年卡退款需扣除已赠课时的等值金额。审核通过后 15 个工作日内退回原支付渠道。", "会员手册"),
    ("如何预约团操课", "约课",
     "团操课需提前预约。可通过前台或小程序选择课程场次预约，系统每日 20:00 开放次日起 7 天内的课程。"
     "首次预约需完成入会并激活会籍。到场后请在前台扫码签到，迟到超过 10 分钟不得入场。", "约课指南"),
    ("取消预约的规则", "约课",
     "课程开始前 4 小时以上可免费取消预约；不足 4 小时取消视为爽约。每自然月累计爽约 3 次，"
     "自次月起 7 天内无法预约任何团操课。取消不收取费用，也不扣减课时。", "约课指南"),
    ("爽约与候补机制", "约课",
     "课程满员后可加入候补队列，一旦有人取消，系统按候补顺序自动递补并短信通知。收到递补通知后需在"
     "1 小时内确认，逾期视为放弃，名额顺延给下一位候补者。", "约课指南"),
    ("团课人数与开课标准", "约课",
     "每节团操课满 3 人开课，不足 3 人课程将取消并短信通知，不扣课时。各课程人数上限不同：瑜伽 20 人、"
     "普拉提 12 人、拳击 10 人、动感单车 20 人，以课程详情页显示为准。", "约课指南"),
    ("私教课收费标准", "私教",
     "私教课按课时包计费：10 节 2400 元、20 节 3600 元、50 节 7200 元。不同级别教练价格略有差异，"
     "以教练档期页报价为准。私教课不与其他折扣活动叠加使用。", "私教服务协议"),
    ("私教课可以退款吗", "私教",
     "未开课的私教课时包可在购买后 7 天内无理由全额退款。已开课的按剩余课时折算退款，扣除已上课时费用"
     "及 10% 违约金。单次私教课一经预约开课不予退款。", "私教服务协议"),
    ("私教课有效期", "私教",
     "私教课时包有效期为自购买之日起 12 个月。因伤病、长期出差等原因可申请延长，最长不超过 6 个月，"
     "需提供相应证明。到期未使用的课时不予退款，可在有效期内转让给同一会籍下的家庭成员。", "私教服务协议"),
    ("首次到店需要准备什么", "到店",
     "首次到店请携带本人身份证用于会籍实名登记。建议穿着运动服和室内运动鞋，场馆提供毛巾和储物柜钥匙，"
     "也可自带。有心脏病、高血压等既往病史请提前告知教练，以便调整训练强度。", "到店指引"),
    ("场馆器械区介绍", "器械",
     "自由力量区配备深蹲架、卧推架、龙门架及全套哑铃（2.5-50kg）；固定器械区有推胸、高位下拉、腿举等"
     "20 台器械；有氧区有跑步机 15 台、椭圆机 6 台、动感单车 20 台。所有器械对会员免费开放。", "场馆手册"),
    ("器械使用注意事项", "器械",
     "使用器械前请确认配重片已卡紧，使用后请将配重归位并擦拭器械。杠铃深蹲、卧推等大重量动作请使用"
     "保护架或找同伴保护。禁止在器械区穿拖鞋、赤膊或饮食。高峰时段单台器械连续占用不超过 20 分钟。",
     "安全须知"),
    ("更衣室与淋浴间", "场馆",
     "更衣室和淋浴间免费开放，开放时间与营业时间一致。淋浴间共 12 个隔间，提供洗发水和沐浴露。"
     "请自备拖鞋和浴巾，馆内不提供一次性拖鞋。贵重物品请寄存前台，馆方不承担遗失赔偿责任。", "场馆手册"),
    ("停车与交通指引", "场馆",
     "场馆地下停车场凭当日签到记录可免费停车 3 小时，超时部分每小时 5 元。地铁 2 号线体育中心站 B 口"
     "步行 400 米即到；公交 15 路、32 路、108 路在体育中心站下车。", "到店指引"),
    ("储物柜租用规则", "场馆",
     "临时储物柜免费使用，需自备挂锁或在前台租用密码锁（押金 50 元，归还即退）。长期储物柜按月 100 元、"
     "按年 1000 元租用。贵重物品请勿存放，遗失本馆不承担赔偿责任。", "场馆手册"),
    ("运动损伤与急救", "安全",
     "场馆配有急救箱和 AED，全体教练持有急救证书。训练中如出现胸闷、头晕、关节剧痛请立即停止并示意"
     "工作人员。建议首次训练前完成体测评估，避免运动强度过大导致肌肉拉伤。", "安全须知"),
    ("特殊人群锻炼须知", "安全",
     "孕妇、高血压、糖尿病、心脏病患者锻炼前须经医生评估并主动告知教练。孕期仅建议进行医生许可的"
     "低强度项目，且需家属陪同。55 岁以上会员首次训练建议先做体成分与心肺功能评估。", "安全须知"),
)


def create_schema(conn) -> None:
    """执行 init.sql 建表（全部 CREATE ... IF NOT EXISTS，幂等）。"""
    # 必须显式 utf-8：Windows 上 read_text() 默认走 cp936，会把 SQL 里的中文注释读成乱码
    sql = INIT_SQL_PATH.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        # psycopg3 在无参数时走简单查询协议，整份多语句脚本可以一次执行
        cur.execute(sql)


def truncate_all(conn) -> None:
    """清空所有表并重置自增 id，保证反复执行结果一致。"""
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")


def _phone_pool(rng: random.Random, n: int) -> list[str]:
    """生成 n 个互不相同的 11 位手机号。

    用 list 保序而不是直接返回 set —— set 的迭代顺序受 str 哈希随机化影响，
    会破坏「固定种子 => 数据可复现」这个前提。
    """
    phones: list[str] = []
    seen: set[str] = set()
    while len(phones) < n:
        phone = "1" + rng.choice("3578") + "".join(rng.choice("0123456789") for _ in range(9))
        if phone not in seen:
            seen.add(phone)
            phones.append(phone)
    return phones


def seed_members(cur, rng: random.Random, n: int) -> None:
    today = date.today()
    card_days = {"monthly": 30, "quarterly": 90, "yearly": 365}
    phones = _phone_pool(rng, n)

    rows = []
    for i in range(n):
        name = rng.choice(SURNAMES) + rng.choice(GIVEN_NAMES)
        gender = rng.choice(("male", "female"))
        birth_date = today - timedelta(days=rng.randint(18 * 365, 55 * 365))
        card_type = rng.choice(("monthly", "quarterly", "yearly"))
        join_date = today - timedelta(days=rng.randint(0, 400))
        expire_date = join_date + timedelta(days=card_days[card_type])
        # 过期的再随机分成「已过期」和「已冻结」，让状态分布更真实
        status = rng.choice(("expired", "frozen")) if expire_date < today else "active"
        rows.append((name, phones[i], gender, birth_date, card_type,
                     join_date, expire_date, status))

    cur.executemany(
        """insert into members
               (name, phone, gender, birth_date, card_type, join_date, expire_date, status)
           values (%s, %s, %s, %s, %s, %s, %s, %s)""",
        rows,
    )


def seed_coaches(cur, rng: random.Random) -> None:
    today = date.today()
    phones = _phone_pool(rng, len(COACHES))

    rows = []
    for (name, gender, specialty, bio, rate), phone in zip(COACHES, phones):
        hire_date = today - timedelta(days=rng.randint(90, 2000))
        status = rng.choices(("active", "leave"), weights=(9, 1))[0]
        rows.append((name, phone, gender, specialty, bio, rate, hire_date, status))

    cur.executemany(
        """insert into coaches
               (name, phone, gender, specialty, bio, hourly_rate, hire_date, status)
           values (%s, %s, %s, %s, %s, %s, %s, %s)""",
        rows,
    )


def seed_courses(cur) -> None:
    cur.executemany(
        """insert into courses
               (name, category, description, duration_min, capacity, difficulty)
           values (%s, %s, %s, %s, %s, %s)""",
        list(COURSES),
    )


def seed_schedules(cur, rng: random.Random, n: int) -> None:
    now = datetime.now(timezone.utc)

    cur.execute("select id, duration_min, capacity from courses order by id")
    courses = cur.fetchall()
    cur.execute("select id from coaches order by id")
    coach_ids = [row[0] for row in cur.fetchall()]

    rows = []
    for _ in range(n):
        course_id, duration_min, capacity = rng.choice(courses)
        # 排课跨过去 14 天 ~ 未来 14 天，让 Agent 查得到历史也查得到未来
        day_offset = rng.randint(-14, 14)
        start_time = (now + timedelta(days=day_offset)).replace(
            hour=rng.choice((7, 9, 10, 14, 15, 18, 19, 20, 21)),
            minute=0,
            second=0,
            microsecond=0,
        )
        end_time = start_time + timedelta(minutes=duration_min)
        status = "finished" if end_time < now else "scheduled"
        rows.append((course_id, rng.choice(coach_ids), start_time, end_time,
                     rng.choice(ROOMS), capacity, status))

    cur.executemany(
        """insert into schedules
               (course_id, coach_id, start_time, end_time, room, capacity, status)
           values (%s, %s, %s, %s, %s, %s, %s)""",
        rows,
    )


def seed_bookings(cur, rng: random.Random, n: int) -> None:
    now = datetime.now(timezone.utc)

    cur.execute("select id from members order by id")
    member_ids = [row[0] for row in cur.fetchall()]
    cur.execute("select id, start_time, end_time, capacity from schedules order by id")
    schedules = cur.fetchall()

    used_per_schedule: dict[int, int] = defaultdict(int)
    seen_pairs: set[tuple[int, int]] = set()

    rows = []
    attempts = 0
    max_attempts = n * 200
    while len(rows) < n:
        attempts += 1
        if attempts > max_attempts:
            raise RuntimeError(
                f"生成预约数据失败：目标 {n} 条，只凑出 {len(rows)} 条，"
                "可能是场次容量总和不够或循环条件有误"
            )

        member_id = rng.choice(member_ids)
        schedule_id, start_time, end_time, capacity = rng.choice(schedules)

        # 同一会员不能重复约同一场次（对应 UNIQUE(member_id, schedule_id)）
        if (member_id, schedule_id) in seen_pairs:
            continue
        # 也不能超过该场次的人数上限
        if used_per_schedule[schedule_id] >= capacity:
            continue

        seen_pairs.add((member_id, schedule_id))
        used_per_schedule[schedule_id] += 1

        if end_time < now:
            status = rng.choices(("attended", "no_show", "cancelled"),
                                 weights=(80, 10, 10))[0]
        else:
            status = rng.choices(("booked", "cancelled"), weights=(90, 10))[0]

        # 预约发生在开课前 1~7 天
        booked_at = start_time - timedelta(days=rng.randint(1, 7),
                                           hours=rng.randint(0, 23))
        rows.append((member_id, schedule_id, booked_at, status))

    cur.executemany(
        """insert into bookings (member_id, schedule_id, booked_at, status)
           values (%s, %s, %s, %s)""",
        rows,
    )


# 按服务类型给一组真实感更强的价格档位，而不是随机数
PRICES = {
    "membership": (299, 799, 2599),
    "course": (199, 299, 499, 699),
    "personal_training": (2400, 3600, 4800, 7200),
    "refund": (299, 799, 2599),
}

TRANSACTION_NOTES = {
    "membership": "会籍续费",
    "course": "团课课时包购买",
    "personal_training": "私教课时包购买",
    "refund": "退卡退款",
}


def seed_transactions(cur, rng: random.Random, n: int) -> None:
    now = datetime.now(timezone.utc)

    cur.execute("select id from members order by id")
    member_ids = [row[0] for row in cur.fetchall()]

    rows = []
    for _ in range(n):
        # 会籍费最常见，退款最少
        kind = rng.choices(
            ("membership", "course", "personal_training", "refund"),
            weights=(5, 3, 2, 1),
        )[0]
        amount = rng.choice(PRICES[kind])
        status = "refunded" if kind == "refund" else \
            rng.choices(("paid", "pending"), weights=(9, 1))[0]
        created_at = now - timedelta(days=rng.randint(0, 400),
                                     hours=rng.randint(0, 23))
        rows.append((rng.choice(member_ids), amount, kind,
                     rng.choice(("wechat", "alipay", "cash", "card")),
                     status, TRANSACTION_NOTES[kind], created_at))

    cur.executemany(
        """insert into transactions
               (member_id, amount, kind, method, status, note, created_at)
           values (%s, %s, %s, %s, %s, %s, %s)""",
        rows,
    )


def seed_policies(cur) -> None:
    cur.executemany(
        """insert into policies (title, category, content, effective_date)
           values (%s, %s, %s, %s)""",
        list(POLICIES),
    )


def seed_knowledge_docs(cur) -> None:
    # 列顺序必须与 KNOWLEDGE_DOCS 的元组顺序 (标题, 分类, 正文, 来源) 一致
    cur.executemany(
        """insert into knowledge_docs (title, category, content, source)
           values (%s, %s, %s, %s)""",
        list(KNOWLEDGE_DOCS),
    )


def main() -> None:
    rng = random.Random(RANDOM_SEED)

    # 整批插入放在同一个事务里：中途出错全部回滚，不留半套数据
    with get_db_conn() as conn:
        create_schema(conn)
        truncate_all(conn)

        with conn.cursor() as cur:
            seed_members(cur, rng, COUNTS["members"])
            seed_coaches(cur, rng)
            seed_courses(cur)
            seed_schedules(cur, rng, COUNTS["schedules"])
            seed_bookings(cur, rng, COUNTS["bookings"])
            seed_transactions(cur, rng, COUNTS["transactions"])
            seed_policies(cur)
            seed_knowledge_docs(cur)

        with conn.cursor() as cur:
            counts = {}
            for table in TABLES:
                cur.execute(f"select count(*) from {table}")
                counts[table] = cur.fetchone()[0]

    print("seed 完成（幂等，可重复执行）：")
    for table in TABLES:
        print(f"  {table:<16} {counts[table]:>4} 行")


if __name__ == "__main__":
    main()
