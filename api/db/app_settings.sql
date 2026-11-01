-- 全局应用设置表（键值结构，存放缓存复用策略等运行时可调参数）
CREATE TABLE IF NOT EXISTS app_settings (
    key         VARCHAR(64)  PRIMARY KEY,
    value       TEXT         NOT NULL,
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE  app_settings        IS '全局应用设置（键值），用于缓存复用策略等运行时可调参数';
COMMENT ON COLUMN app_settings.key    IS '设置项键名，如 cache_enabled / cache_ttl_seconds / cache_refresh_on_hit';
COMMENT ON COLUMN app_settings.value  IS '设置项值（统一以字符串存储，应用层按类型解析）';

-- 初始化缓存复用策略默认值（已存在则保留用户当前配置）
INSERT INTO app_settings (key, value) VALUES
    ('cache_enabled',        'true'),
    ('cache_ttl_seconds',    '28800'),
    ('cache_refresh_on_hit', 'true')
ON CONFLICT (key) DO NOTHING;
