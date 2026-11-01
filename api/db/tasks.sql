-- NER 任务表
CREATE TABLE IF NOT EXISTS tasks (
    id            SERIAL          PRIMARY KEY,
    task_id       UUID            NOT NULL UNIQUE,
    task_hash     VARCHAR(32),
    model_name    VARCHAR(50)     NOT NULL,
    input_text    TEXT            NOT NULL,
    labels        JSONB           NOT NULL,
    label_mapping JSONB,
    information_json JSONB        NOT NULL DEFAULT '{}'::jsonb,
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
    cache_hit     BOOLEAN         NOT NULL DEFAULT FALSE,
    source        VARCHAR(20),
    routed_model  VARCHAR(120)
);

CREATE INDEX IF NOT EXISTS idx_tasks_task_id    ON tasks(task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_task_hash  ON tasks(task_hash);
CREATE INDEX IF NOT EXISTS idx_tasks_model_name ON tasks(model_name);
CREATE INDEX IF NOT EXISTS idx_tasks_status     ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_user_id    ON tasks(user_id);
CREATE INDEX IF NOT EXISTS idx_tasks_cache_expires_at ON tasks(cache_expires_at);

COMMENT ON TABLE  tasks            IS 'NER 实体提取任务表';
COMMENT ON COLUMN tasks.task_id    IS '任务唯一标识（UUID）';
COMMENT ON COLUMN tasks.task_hash  IS '任务内容哈希（文本+排序后标签的MD5），用于去重复用';
COMMENT ON COLUMN tasks.status     IS '任务状态：pending / running / done / failed';
COMMENT ON COLUMN tasks.label_mapping IS '标签映射字典，格式：{原始标签: 标准标签}';
COMMENT ON COLUMN tasks.information_json IS '用户自定义信息字段，用于消息绑定';
COMMENT ON COLUMN tasks.latency_ms    IS '模型推理耗时（毫秒）';
COMMENT ON COLUMN tasks.callback_config IS '回调配置（JSON）';
COMMENT ON COLUMN tasks.callback_status IS '回调状态：pending / success / failed';
COMMENT ON COLUMN tasks.callback_error IS '回调错误信息';
COMMENT ON COLUMN tasks.callback_finished_at IS '回调完成时间';
COMMENT ON COLUMN tasks.user_id     IS '提交任务的用户 ID';
COMMENT ON COLUMN tasks.cache_expires_at IS '缓存复用过期时间；NULL 表示该任务不参与缓存复用，超过此时间后不再作为 reuse_cache 命中来源';
COMMENT ON COLUMN tasks.cache_hit    IS '是否为缓存命中复用产生的任务（true=未实际推理，复用历史结果）';
COMMENT ON COLUMN tasks.source       IS '任务来源渠道：text / upload / eml；历史数据为 NULL';
COMMENT ON COLUMN tasks.routed_model IS 'auto/attack-auto 路由后实际子模型（多个逗号拼接）；直连模型为 NULL';
