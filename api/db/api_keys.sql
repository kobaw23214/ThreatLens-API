-- API Key 表
CREATE TABLE IF NOT EXISTS api_keys (
    id            SERIAL        PRIMARY KEY,
    user_id       INTEGER       NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key_hash      VARCHAR(64)   NOT NULL UNIQUE,
    key_prefix    VARCHAR(12)   NOT NULL,
    name          VARCHAR(100)  NOT NULL DEFAULT '',
    expires_at    TIMESTAMPTZ   NOT NULL,
    is_active     BOOLEAN       NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_api_keys_key_hash ON api_keys(key_hash);
CREATE INDEX IF NOT EXISTS idx_api_keys_user_id  ON api_keys(user_id);

COMMENT ON TABLE  api_keys              IS 'API Key 表';
COMMENT ON COLUMN api_keys.user_id      IS '关联用户 ID';
COMMENT ON COLUMN api_keys.key_hash     IS 'Key 的 SHA-256 哈希（数据库仅存哈希）';
COMMENT ON COLUMN api_keys.key_prefix   IS 'Key 前缀（如 ner_a1b2），用于列表展示';
COMMENT ON COLUMN api_keys.name         IS 'Key 用途描述';
COMMENT ON COLUMN api_keys.expires_at   IS '过期时间';
COMMENT ON COLUMN api_keys.is_active    IS '是否启用';
