-- FitAgent 数据层建表脚本
-- 幂等：全部使用 CREATE ... IF NOT EXISTS，可反复执行。
-- 配套 db/seed.py：先跑本脚本建表，再 TRUNCATE 清空并灌入模拟数据。

-- ---------------------------------------------------------------- 会员
CREATE TABLE IF NOT EXISTS members (
    id          BIGSERIAL   PRIMARY KEY,
    name        TEXT        NOT NULL,
    phone       TEXT        NOT NULL UNIQUE,
    gender      TEXT        NOT NULL CHECK (gender IN ('male', 'female')),
    birth_date  DATE,
    card_type   TEXT        NOT NULL CHECK (card_type IN ('monthly', 'quarterly', 'yearly')),
    join_date   DATE        NOT NULL,
    expire_date DATE        NOT NULL,
    status      TEXT        NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active', 'expired', 'frozen')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (expire_date >= join_date)
);

-- ---------------------------------------------------------------- 教练
CREATE TABLE IF NOT EXISTS coaches (
    id          BIGSERIAL     PRIMARY KEY,
    name        TEXT          NOT NULL,
    phone       TEXT          NOT NULL UNIQUE,
    gender      TEXT          NOT NULL CHECK (gender IN ('male', 'female')),
    specialty   TEXT          NOT NULL,
    bio         TEXT,
    hourly_rate NUMERIC(10,2) NOT NULL CHECK (hourly_rate >= 0),
    hire_date   DATE          NOT NULL,
    status      TEXT          NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active', 'leave')),
    created_at  TIMESTAMPTZ   NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- 课程目录
CREATE TABLE IF NOT EXISTS courses (
    id           BIGSERIAL   PRIMARY KEY,
    name         TEXT        NOT NULL UNIQUE,
    category     TEXT        NOT NULL
                             CHECK (category IN ('yoga', 'strength', 'cardio',
                                                 'pilates', 'boxing', 'dance')),
    description  TEXT,
    duration_min INT         NOT NULL CHECK (duration_min > 0),
    capacity     INT         NOT NULL CHECK (capacity > 0),
    difficulty   TEXT        NOT NULL
                             CHECK (difficulty IN ('beginner', 'intermediate', 'advanced')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- 排课（课程的具体场次）
CREATE TABLE IF NOT EXISTS schedules (
    id         BIGSERIAL   PRIMARY KEY,
    course_id  BIGINT      NOT NULL REFERENCES courses (id) ON DELETE CASCADE,
    coach_id   BIGINT      NOT NULL REFERENCES coaches (id) ON DELETE RESTRICT,
    start_time TIMESTAMPTZ NOT NULL,
    end_time   TIMESTAMPTZ NOT NULL,
    room       TEXT        NOT NULL,
    capacity   INT         NOT NULL CHECK (capacity > 0),
    status     TEXT        NOT NULL DEFAULT 'scheduled'
                           CHECK (status IN ('scheduled', 'finished', 'cancelled')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (end_time > start_time)
);

-- ---------------------------------------------------------------- 预约
-- UNIQUE(member_id, schedule_id)：同一会员不能重复约同一场次
CREATE TABLE IF NOT EXISTS bookings (
    id          BIGSERIAL   PRIMARY KEY,
    member_id   BIGINT      NOT NULL REFERENCES members (id) ON DELETE CASCADE,
    schedule_id BIGINT      NOT NULL REFERENCES schedules (id) ON DELETE CASCADE,
    booked_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    status      TEXT        NOT NULL DEFAULT 'booked'
                            CHECK (status IN ('booked', 'attended', 'cancelled', 'no_show')),
    UNIQUE (member_id, schedule_id)
);

-- ---------------------------------------------------------------- 消费流水
-- 列名用 kind 而不是 type，避免与「类型」概念混淆
CREATE TABLE IF NOT EXISTS transactions (
    id         BIGSERIAL     PRIMARY KEY,
    member_id  BIGINT        NOT NULL REFERENCES members (id) ON DELETE CASCADE,
    amount     NUMERIC(10,2) NOT NULL CHECK (amount >= 0),
    kind       TEXT          NOT NULL
                             CHECK (kind IN ('membership', 'course',
                                             'personal_training', 'refund')),
    method     TEXT          NOT NULL
                             CHECK (method IN ('wechat', 'alipay', 'cash', 'card')),
    status     TEXT          NOT NULL DEFAULT 'paid'
                             CHECK (status IN ('paid', 'pending', 'refunded')),
    note       TEXT,
    created_at TIMESTAMPTZ   NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- 规章制度
CREATE TABLE IF NOT EXISTS policies (
    id             BIGSERIAL   PRIMARY KEY,
    title          TEXT        NOT NULL UNIQUE,
    category       TEXT        NOT NULL,
    content        TEXT        NOT NULL,
    effective_date DATE        NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- RAG 知识语料
CREATE TABLE IF NOT EXISTS knowledge_docs (
    id         BIGSERIAL   PRIMARY KEY,
    title      TEXT        NOT NULL,
    content    TEXT        NOT NULL,
    category   TEXT        NOT NULL,
    source     TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- 索引
CREATE INDEX IF NOT EXISTS idx_schedules_start_time  ON schedules (start_time);
CREATE INDEX IF NOT EXISTS idx_bookings_member       ON bookings (member_id);
CREATE INDEX IF NOT EXISTS idx_transactions_member   ON transactions (member_id);
