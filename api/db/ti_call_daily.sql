-- TI 外部平台调用统计表（按天 / 平台 / 通道）
CREATE TABLE IF NOT EXISTS ti_call_daily (
    id            SERIAL          PRIMARY KEY,
    day           INTEGER         NOT NULL,
    platform      VARCHAR(20)     NOT NULL,
    channel       VARCHAR(10)     NOT NULL,
    success_count INTEGER         NOT NULL DEFAULT 0,
    fail_count    INTEGER         NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_ti_call_daily_uniq ON ti_call_daily(day, platform, channel);

COMMENT ON TABLE  ti_call_daily         IS 'TI 外部平台调用统计（按天）';
COMMENT ON COLUMN ti_call_daily.day     IS '日期，格式 YYYYMMDD';
COMMENT ON COLUMN ti_call_daily.channel IS '采集通道：web / api';
