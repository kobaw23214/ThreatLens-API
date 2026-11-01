-- 任务结果表（独立存储，与 tasks 表 1:1 关联）
CREATE TABLE IF NOT EXISTS task_results (
    id             SERIAL      PRIMARY KEY,
    task_id        UUID        NOT NULL UNIQUE,
    entities       JSONB       NOT NULL DEFAULT '[]',
    entity_stats   JSONB       NOT NULL DEFAULT '{}',
    chunk_metadata JSONB,                          -- long-text-chunking-ner: 允许 NULL,兼容历史任务
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_task_results_task_id ON task_results(task_id);

-- 表达式索引:按 chunk_count 过滤长文本任务(仅对已分块记录生效)
CREATE INDEX IF NOT EXISTS idx_task_results_chunk_count
    ON task_results (((chunk_metadata->>'chunk_count')::int))
    WHERE chunk_metadata IS NOT NULL;

COMMENT ON TABLE  task_results                IS '实体提取结果表，与 tasks 表 1:1 关联';
COMMENT ON COLUMN task_results.entities       IS '提取到的实体列表，每项：{text, label, start, end, score, chunk_index, chunk_char_start, chunk_char_end}';
COMMENT ON COLUMN task_results.entity_stats   IS '实体类型分布统计，格式：{label: count}';
COMMENT ON COLUMN task_results.chunk_metadata IS '分块元数据 JSON, 结构参考 api/schemas.py::ChunkMetadata pydantic 模型。历史任务为 NULL';
