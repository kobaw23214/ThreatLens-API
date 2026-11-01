-- TI 威胁情报扩线任务表（微步在线 / 奇安信）
CREATE TABLE IF NOT EXISTS ti_tasks (
    id            SERIAL          PRIMARY KEY,
    task_id       UUID            NOT NULL UNIQUE,
    seed_type     VARCHAR(20)     NOT NULL,
    seed_value    TEXT            NOT NULL,
    platform      VARCHAR(20)     NOT NULL,
    channel       VARCHAR(10)     NOT NULL DEFAULT 'web',
    status        VARCHAR(20)     NOT NULL DEFAULT 'pending',
    error_msg     TEXT,
    created_at    TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    finished_at   TIMESTAMPTZ,
    latency_ms    DOUBLE PRECISION,
    callback_config JSONB,
    callback_status VARCHAR(20),
    callback_error TEXT,
    callback_finished_at TIMESTAMPTZ,
    user_id       INTEGER         REFERENCES users(id) ON DELETE SET NULL,
    cache_expires_at TIMESTAMPTZ,
    information_json JSONB
);

CREATE INDEX IF NOT EXISTS idx_ti_tasks_task_id    ON ti_tasks(task_id);
CREATE INDEX IF NOT EXISTS idx_ti_tasks_seed       ON ti_tasks(seed_type, seed_value, platform);
CREATE INDEX IF NOT EXISTS idx_ti_tasks_status     ON ti_tasks(status);
CREATE INDEX IF NOT EXISTS idx_ti_tasks_created_at ON ti_tasks(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ti_tasks_user_id    ON ti_tasks(user_id);
CREATE INDEX IF NOT EXISTS idx_ti_tasks_cache_expires_at ON ti_tasks(cache_expires_at);

COMMENT ON TABLE  ti_tasks             IS 'TI 威胁情报扩线任务表（微步/奇安信）';
COMMENT ON COLUMN ti_tasks.seed_type   IS '种子类型：ip / domain / file';
COMMENT ON COLUMN ti_tasks.seed_value  IS '种子值（IP / 域名 / 样本哈希）';
COMMENT ON COLUMN ti_tasks.platform    IS '情报平台：threatbook / qianxin';
COMMENT ON COLUMN ti_tasks.channel     IS '采集通道：web（浏览器）/ api（预留）';
COMMENT ON COLUMN ti_tasks.status      IS '任务状态：pending / running / done / failed';
COMMENT ON COLUMN ti_tasks.cache_expires_at IS '缓存复用过期时间；NULL 表示不参与缓存复用';
COMMENT ON COLUMN ti_tasks.user_id     IS '提交任务的用户 ID';
COMMENT ON COLUMN ti_tasks.information_json IS '用户自定义信息（随结果/回调原样返回，与 /extract 同构）';
