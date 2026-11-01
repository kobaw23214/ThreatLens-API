-- 调用记录表（用于限额计数）
CREATE TABLE IF NOT EXISTS usage_logs (
    id            SERIAL        PRIMARY KEY,
    user_id       INTEGER       NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    endpoint      VARCHAR(50)   NOT NULL DEFAULT '/extract',
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_usage_logs_user_id    ON usage_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_usage_logs_created_at ON usage_logs(created_at DESC);
-- 复合索引：按用户+时间范围高效查询限额
CREATE INDEX IF NOT EXISTS idx_usage_logs_user_time  ON usage_logs(user_id, created_at DESC);

COMMENT ON TABLE  usage_logs             IS '调用记录表，用于限额计数';
COMMENT ON COLUMN usage_logs.user_id     IS '调用者用户 ID';
COMMENT ON COLUMN usage_logs.endpoint    IS '调用端点';
COMMENT ON COLUMN usage_logs.created_at  IS '调用时间';
