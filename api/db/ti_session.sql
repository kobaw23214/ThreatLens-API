-- TI 平台登录会话状态表
CREATE TABLE IF NOT EXISTS ti_session (
    platform      VARCHAR(20)     PRIMARY KEY,
    login_state   VARCHAR(20)     NOT NULL DEFAULT 'unknown',
    last_login_at TIMESTAMPTZ,
    last_check_at TIMESTAMPTZ,
    fail_reason   TEXT,
    proxy_used    TEXT,
    updated_at    TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE  ti_session             IS 'TI 平台登录会话状态表';
COMMENT ON COLUMN ti_session.login_state IS '会话状态：unknown / ok / logging_in / need_manual / auth_failed / expired';
COMMENT ON COLUMN ti_session.fail_reason IS '最近失败原因';
COMMENT ON COLUMN ti_session.proxy_used  IS '当前会话使用的代理地址';
