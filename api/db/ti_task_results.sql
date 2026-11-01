-- TI 联网情报任务结果表：平台原生内容（区块 + 原始 JSON），不做业务加工
CREATE TABLE IF NOT EXISTS ti_task_results (
    task_id     UUID            PRIMARY KEY REFERENCES ti_tasks(task_id) ON DELETE CASCADE,
    sections    JSONB           NOT NULL DEFAULT '[]'::jsonb,
    raw         JSONB,
    diagnostics JSONB,
    created_at  TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE  ti_task_results             IS 'TI 联网情报任务结果表';
COMMENT ON COLUMN ti_task_results.sections    IS '平台页面原生区块：[{name, format: kv/table/text, ...}]，按页面顺序';
COMMENT ON COLUMN ti_task_results.raw         IS '响应拦截得到的平台原始 JSON（完整保真）';
COMMENT ON COLUMN ti_task_results.diagnostics IS '采集诊断信息（通道/耗时/selector 版本/留证路径等）';
