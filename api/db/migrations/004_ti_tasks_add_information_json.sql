-- migration: ti_tasks 增加 information_json 自定义信息列
-- version: 004
-- applies to: ti_tasks
--
-- 该脚本可在生产数据库上安全反复执行(使用 IF NOT EXISTS),不会触发
-- api/db.py::_sync_schema 的 DROP + REBUILD,因此不会丢失历史任务数据。
--
-- 部署步骤:
--   1. 先在目标数据库手动执行本脚本
--   2. 再部署新版代码,_sync_schema 检测到列已齐全会跳过重建
--
-- 历史任务的 information_json 列留空 (NULL),API 层按 {} 返回。

ALTER TABLE ti_tasks
    ADD COLUMN IF NOT EXISTS information_json JSONB;

COMMENT ON COLUMN ti_tasks.information_json IS '用户自定义信息（随结果/回调原样返回，与 /extract 同构）';
