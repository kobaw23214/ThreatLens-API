-- 回调成功日志表
CREATE TABLE IF NOT EXISTS callback_success_logs (
    id SERIAL PRIMARY KEY,
    task_id UUID NOT NULL,
    callback_url TEXT NOT NULL,
    callback_method VARCHAR(10) DEFAULT 'POST',
    http_status_code INTEGER NOT NULL,
    response_time_ms FLOAT NOT NULL,
    request_headers JSONB,
    request_body JSONB,
    response_headers JSONB,
    response_body TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 新增字段（已有表的迁移）
ALTER TABLE callback_success_logs ADD COLUMN IF NOT EXISTS request_body JSONB;

CREATE INDEX IF NOT EXISTS idx_callback_success_logs_task_id ON callback_success_logs(task_id);
CREATE INDEX IF NOT EXISTS idx_callback_success_logs_created_at ON callback_success_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_callback_success_logs_callback_url ON callback_success_logs(callback_url);
