-- migration for long-text-chunking-ner
-- version: 001
-- applies to: task_results
--
-- 该脚本可在生产数据库上安全反复执行(使用 IF NOT EXISTS),不会触发
-- api/db.py::_sync_schema 的 DROP + REBUILD,因此不会丢失历史任务数据。
--
-- 部署步骤:
--   1. 先在目标数据库手动执行本脚本
--   2. 再部署新版代码,_sync_schema 检测到列已齐全会跳过重建
--
-- 历史任务的 chunk_metadata 列留空 (NULL),对应 "未分块 / 历史任务" 语义,
-- 由 API 层和前端按占位渲染。

ALTER TABLE task_results
    ADD COLUMN IF NOT EXISTS chunk_metadata JSONB;

-- 表达式索引:按 chunk_count 过滤长文本任务时用到,只对已分块的记录建索引
CREATE INDEX IF NOT EXISTS idx_task_results_chunk_count
    ON task_results (((chunk_metadata->>'chunk_count')::int))
    WHERE chunk_metadata IS NOT NULL;

COMMENT ON COLUMN task_results.chunk_metadata IS
    '分块元数据 JSON, 结构参考 api/schemas.py::ChunkMetadata pydantic 模型。历史任务为 NULL';
