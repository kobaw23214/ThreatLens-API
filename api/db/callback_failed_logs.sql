-- 回调失败记录表
CREATE TABLE IF NOT EXISTS callback_failed_logs (
    id SERIAL PRIMARY KEY,
    task_id UUID NOT NULL,
    callback_url TEXT NOT NULL,
    callback_method VARCHAR(10) DEFAULT 'POST',
    callback_headers JSONB,        -- 实际发送的完整请求头（含默认头）
    request_body JSONB,            -- 实际发送的请求体（回调内容）
    result_data JSONB,             -- 原始回调数据（兼容旧字段）
    error_message TEXT NOT NULL,
    http_status_code INTEGER,
    failed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    retry_count INTEGER DEFAULT 0,
    last_retry_at TIMESTAMPTZ,
    status VARCHAR(20) DEFAULT 'pending',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_callback_failed_logs_task_id ON callback_failed_logs(task_id);
CREATE INDEX IF NOT EXISTS idx_callback_failed_logs_status ON callback_failed_logs(status);
CREATE INDEX IF NOT EXISTS idx_callback_failed_logs_failed_at ON callback_failed_logs(failed_at DESC);

-- 新增字段（已有表的迁移）
ALTER TABLE callback_failed_logs ADD COLUMN IF NOT EXISTS request_body JSONB;
ALTER TABLE callback_failed_logs ALTER COLUMN result_data DROP NOT NULL;

-- 注释
COMMENT ON TABLE callback_failed_logs IS '回调失败记录表';
COMMENT ON COLUMN callback_failed_logs.id IS '主键';
COMMENT ON COLUMN callback_failed_logs.task_id IS '关联的任务 ID';
COMMENT ON COLUMN callback_failed_logs.callback_url IS '回调地址';
COMMENT ON COLUMN callback_failed_logs.callback_method IS 'HTTP 方法';
COMMENT ON COLUMN callback_failed_logs.callback_headers IS '请求头';
COMMENT ON COLUMN callback_failed_logs.result_data IS '要回调的数据';
COMMENT ON COLUMN callback_failed_logs.error_message IS '错误信息';
COMMENT ON COLUMN callback_failed_logs.http_status_code IS 'HTTP 状态码（如果有）';
COMMENT ON COLUMN callback_failed_logs.failed_at IS '失败时间';
COMMENT ON COLUMN callback_failed_logs.retry_count IS '重试次数';
COMMENT ON COLUMN callback_failed_logs.last_retry_at IS '最后重试时间';
COMMENT ON COLUMN callback_failed_logs.status IS '状态：pending/retrying/resolved';
