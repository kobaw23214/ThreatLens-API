"""TI 联网情报数据导出：把 ti_tasks / ti_task_results 生成可重复加载的 SQL 文件。

纯函数模块，不触 DB：
  - build_ti_export_sql(tasks, results, start_date, end_date) → str

生成的 SQL 自包含（CREATE TABLE IF NOT EXISTS + INSERT ... ON CONFLICT DO NOTHING），
可在空库或另一套同系统库中直接 `psql -f` 加载，重复执行不产生重复行。

缓存复用语义（内网导入场景）：
  - 传 cache_ttl_seconds 时，status='done' 任务行的 cache_expires_at 不写源库原值，
    改写为 SQL 表达式 NOW() + ttl（在目标库 psql 加载时求值）——
    即"导入即默认进入缓存复用窗口"，窗口从导入时刻起算；
  - ON CONFLICT DO NOTHING 保证只影响新插入行，目标库已有任务的缓存状态不被改写；
  - 非 done 行（failed/pending）本就不参与缓存复用，保持原值。

与 api/db/*.sql 的差异说明：
  - ti_tasks.user_id 保留源库用户 ID，但导出 DDL 不含 users 外键
    （避免跨模块依赖：目标库无 users 表也能建表加载）；
  - 不导出自增 id（目标库由 SERIAL 重新分配），去重靠 task_id UNIQUE。
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import List, Optional, Sequence, Tuple

# ─── 列定义（顺序即 SELECT / INSERT 列序，与 store.fetch_ti_export_rows 对应）────

TI_TASK_COLUMNS: Tuple[str, ...] = (
    "task_id", "seed_type", "seed_value", "platform", "channel", "status",
    "error_msg", "created_at", "finished_at", "latency_ms",
    "callback_config", "callback_status", "callback_error", "callback_finished_at",
    "user_id", "cache_expires_at", "information_json",
)
TI_RESULT_COLUMNS: Tuple[str, ...] = (
    "task_id", "sections", "raw", "diagnostics", "created_at",
)

_UUID_COLS = {"task_id"}
_JSONB_COLS = {"callback_config", "information_json", "sections", "raw", "diagnostics"}
_TS_COLS = {"created_at", "finished_at", "callback_finished_at", "cache_expires_at"}

# 建表语句：列与 api/db/ti_tasks.sql / ti_task_results.sql 保持一致，
# 差异仅在于 ti_tasks.user_id 不带 users 外键（见模块 docstring）。
_DDL = """\
CREATE TABLE IF NOT EXISTS ti_tasks (
    id            SERIAL          PRIMARY KEY,
    task_id       UUID            NOT NULL UNIQUE,
    seed_type     VARCHAR(20)     NOT NULL,
    seed_value    TEXT            NOT NULL,
    platform      VARCHAR(20)     NOT NULL,
    channel       VARCHAR(10)     NOT NULL DEFAULT 'web',
    status        VARCHAR(20)     NOT NULL DEFAULT 'pending',
    error_msg     TEXT,
    created_at    TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    finished_at   TIMESTAMPTZ,
    latency_ms    DOUBLE PRECISION,
    callback_config JSONB,
    callback_status VARCHAR(20),
    callback_error TEXT,
    callback_finished_at TIMESTAMPTZ,
    user_id       INTEGER,
    cache_expires_at TIMESTAMPTZ,
    information_json JSONB
);

CREATE INDEX IF NOT EXISTS idx_ti_tasks_task_id    ON ti_tasks(task_id);
CREATE INDEX IF NOT EXISTS idx_ti_tasks_seed       ON ti_tasks(seed_type, seed_value, platform);
CREATE INDEX IF NOT EXISTS idx_ti_tasks_status     ON ti_tasks(status);
CREATE INDEX IF NOT EXISTS idx_ti_tasks_created_at ON ti_tasks(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ti_tasks_user_id    ON ti_tasks(user_id);
CREATE INDEX IF NOT EXISTS idx_ti_tasks_cache_expires_at ON ti_tasks(cache_expires_at);

CREATE TABLE IF NOT EXISTS ti_task_results (
    task_id     UUID            PRIMARY KEY REFERENCES ti_tasks(task_id) ON DELETE CASCADE,
    sections    JSONB           NOT NULL DEFAULT '[]'::jsonb,
    raw         JSONB,
    diagnostics JSONB,
    created_at  TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);"""


# ─── SQL 字面量 ─────────────────────────────────────────────────────────────────

class _SqlExpr(str):
    """原样输出的 SQL 表达式（不作字面量转义/加引号），如 NOW() + interval。"""


def _sql_string(s: str) -> str:
    """标准字符串字面量（standard_conforming_strings=on：反斜杠原样，单引号双写）。"""
    return "'" + s.replace("'", "''") + "'"


def _sql_literal(col: str, value) -> str:
    if isinstance(value, _SqlExpr):
        return str(value)
    if value is None:
        return "NULL"
    if col in _JSONB_COLS:
        return _sql_string(json.dumps(value, ensure_ascii=False)) + "::jsonb"
    if col in _TS_COLS:
        ts = value.isoformat() if isinstance(value, datetime) else str(value)
        return _sql_string(ts) + "::timestamptz"
    if col in _UUID_COLS:
        return _sql_string(str(value)) + "::uuid"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    return _sql_string(str(value))


def _insert(table: str, columns: Sequence[str], row: Sequence) -> str:
    vals = ", ".join(_sql_literal(col, v) for col, v in zip(columns, row))
    return (f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({vals}) "
            f"ON CONFLICT (task_id) DO NOTHING;")


# ─── 导出文件构建 ───────────────────────────────────────────────────────────────

def build_ti_export_sql(
    tasks: List[Sequence],
    results: List[Sequence],
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    exported_at: Optional[datetime] = None,
    cache_ttl_seconds: Optional[int] = None,
) -> str:
    """生成 TI 数据导出 SQL 文本。

    Args:
        tasks:   ti_tasks 行（列序同 TI_TASK_COLUMNS）。
        results: ti_task_results 行（列序同 TI_RESULT_COLUMNS）。
        start_date / end_date: 导出的创建日期范围（含首尾，仅写入文件头注释）。
        exported_at: 导出时间，默认当前时间。
        cache_ttl_seconds: 缓存复用 TTL（秒）。>0 时 done 任务行的 cache_expires_at
            改写为 NOW() + ttl（导入时求值），让导入数据默认进入缓存复用窗口；
            None/0 时保持源库原值（纯备份语义）。
    """
    exported_at = exported_at or datetime.now()
    if start_date and end_date:
        range_desc = f"{start_date} ~ {end_date}（含首尾，按 ti_tasks.created_at）"
    elif start_date or end_date:
        range_desc = f"{start_date or '最早'} ~ {end_date or '最新'}（按 ti_tasks.created_at）"
    else:
        range_desc = "全部（未按日期过滤）"

    cache_note = "未启用（保持源库 cache_expires_at 原值）"
    if cache_ttl_seconds and cache_ttl_seconds > 0:
        idx_status = TI_TASK_COLUMNS.index("status")
        idx_cache = TI_TASK_COLUMNS.index("cache_expires_at")
        expr = _SqlExpr(f"NOW() + ({int(cache_ttl_seconds)} || ' seconds')::interval")
        tasks = [
            tuple(expr if i == idx_cache else v for i, v in enumerate(row))
            if row[idx_status] == "done" else row
            for row in tasks
        ]
        cache_note = f"done 任务导入即进缓存窗口（NOW() + {int(cache_ttl_seconds)} 秒，导入时求值）"

    lines: List[str] = [
        "-- ============================================================",
        "-- ThreatLens 联网情报（TI）数据导出",
        f"-- 导出时间: {exported_at.isoformat(timespec='seconds')}",
        f"-- 数据范围: {range_desc}",
        f"-- 任务行数: {len(tasks)}，结果行数: {len(results)}",
        "-- 加载方式: psql -d <目标库> -f <本文件>",
        "-- 幂等: 主键/唯一键冲突时跳过，可重复执行不产生重复行",
        f"-- 缓存复用: {cache_note}",
        "-- 注意: user_id 为源库用户 ID，仅作归属参考，未关联目标库 users 表",
        "-- ============================================================",
        "",
        "BEGIN;",
        "",
        _DDL,
        "",
    ]
    for row in tasks:
        lines.append(_insert("ti_tasks", TI_TASK_COLUMNS, row))
    if tasks:
        lines.append("")
    for row in results:
        lines.append(_insert("ti_task_results", TI_RESULT_COLUMNS, row))
    lines += ["", "COMMIT;", ""]
    return "\n".join(lines)
