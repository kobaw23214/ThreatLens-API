-- migration: 为缓存复用增加 TTL 过期时间
-- version: 003
-- applies to: tasks
--
-- 该脚本可在生产数据库上安全反复执行(使用 IF NOT EXISTS),不会触发
-- api/db.py::_sync_schema 的 DROP + REBUILD,因此不会丢失历史任务数据。
--
-- cache_expires_at 语义:
--   - NULL      → 该任务不参与缓存复用(历史任务或提交时未启用复用)
--   - 未来时间   → 在此时间之前可作为 reuse_cache 命中来源
--   - 过去时间   → 已过期,不再被复用(但任务本身仍可正常查询)

ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS cache_expires_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_tasks_cache_expires_at ON tasks(cache_expires_at);

COMMENT ON COLUMN tasks.cache_expires_at IS
    '缓存复用过期时间；NULL 表示该任务不参与缓存复用，超过此时间后不再作为 reuse_cache 命中来源';
