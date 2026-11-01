-- 任务统计增强：为 tasks 表补充三个统计口径字段（幂等，可重复执行）
--   cache_hit    : 本行是否为缓存命中复用产生的任务
--   source       : 任务来源渠道（text=/extract, upload=/extract/upload, eml=/extract/eml）
--   routed_model : auto / attack-auto 路由后实际执行的子模型（多个时逗号拼接；非路由模型为 NULL）
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS cache_hit    BOOLEAN      NOT NULL DEFAULT FALSE;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS source       VARCHAR(20);
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS routed_model VARCHAR(120);

COMMENT ON COLUMN tasks.cache_hit    IS '是否为缓存命中复用产生的任务（true=未实际推理，复用历史结果）';
COMMENT ON COLUMN tasks.source       IS '任务来源渠道：text / upload / eml；历史数据为 NULL';
COMMENT ON COLUMN tasks.routed_model IS 'auto/attack-auto 路由后实际子模型（多个逗号拼接）；直连模型为 NULL';

CREATE INDEX IF NOT EXISTS idx_tasks_cache_hit ON tasks(cache_hit) WHERE cache_hit;
