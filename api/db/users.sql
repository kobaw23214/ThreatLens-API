-- 用户表
CREATE TABLE IF NOT EXISTS users (
    id            SERIAL        PRIMARY KEY,
    username      VARCHAR(50)   NOT NULL UNIQUE,
    password_hash VARCHAR(255)  NOT NULL,
    is_admin      BOOLEAN       NOT NULL DEFAULT FALSE,
    -- 调用限额配置（0 = 无限制）
    limit_5h      INTEGER       NOT NULL DEFAULT 2000,
    limit_weekly  INTEGER       NOT NULL DEFAULT 100000,
    is_active     BOOLEAN       NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);

COMMENT ON TABLE  users                IS '用户表';
COMMENT ON COLUMN users.username       IS '用户名（唯一）';
COMMENT ON COLUMN users.password_hash  IS '密码 bcrypt 哈希';
COMMENT ON COLUMN users.is_admin       IS '是否管理员';
COMMENT ON COLUMN users.limit_5h       IS '5小时调用次数限额（0=无限制）';
COMMENT ON COLUMN users.limit_weekly   IS '每周调用次数限额（0=无限制）';
COMMENT ON COLUMN users.is_active      IS '是否启用';
