"""TI 联网情报的数据库访问层。

连接池复用 api.db._pool（延迟 import，与 callback_manager 同一模式，
避免模块 import 时固化 None 值）。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional

from psycopg2.extras import Json

logger = logging.getLogger(__name__)


def _get_pool():
    from ..db import _pool
    return _pool


# ─── 任务 ─────────────────────────────────────────────────────────────────────

def insert_ti_task(
    task_id: str,
    seed_type: str,
    seed_value: str,
    platform: str,
    channel: str = "web",
    callback_config: Optional[Dict] = None,
    information_json: Optional[Dict] = None,
    user_id: Optional[int] = None,
) -> None:
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO ti_tasks
                   (task_id, seed_type, seed_value, platform, channel,
                    callback_config, information_json, status, user_id)
                   VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, 'pending', %s)""",
                (task_id, seed_type, seed_value, platform, channel,
                 Json(callback_config) if callback_config else None,
                 Json(information_json) if information_json else None, user_id),
            )
            conn.commit()
    finally:
        pool.putconn(conn)


def update_ti_task_running(task_id: str) -> None:
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE ti_tasks SET status = 'running' WHERE task_id = %s::uuid",
                (task_id,),
            )
            conn.commit()
    finally:
        pool.putconn(conn)


def update_ti_task_done(
    task_id: str,
    sections: List[Dict],
    raw: Optional[Dict],
    diagnostics: Optional[Dict],
    latency_ms: float,
    cache_ttl_seconds: Optional[int] = None,
) -> None:
    """写入任务结果（平台原生区块 + 原始 JSON）。cache_ttl_seconds > 0 时进入缓存复用窗口。"""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            if cache_ttl_seconds is not None and cache_ttl_seconds > 0:
                cur.execute(
                    """UPDATE ti_tasks
                       SET status = 'done', latency_ms = %s, finished_at = NOW(),
                           cache_expires_at = NOW() + (%s || ' seconds')::interval
                       WHERE task_id = %s::uuid""",
                    (latency_ms, int(cache_ttl_seconds), task_id),
                )
            else:
                cur.execute(
                    """UPDATE ti_tasks
                       SET status = 'done', latency_ms = %s, finished_at = NOW(),
                           cache_expires_at = NULL
                       WHERE task_id = %s::uuid""",
                    (latency_ms, task_id),
                )
            cur.execute(
                """INSERT INTO ti_task_results (task_id, sections, raw, diagnostics)
                   VALUES (%s::uuid, %s, %s, %s)
                   ON CONFLICT (task_id) DO UPDATE
                   SET sections    = EXCLUDED.sections,
                       raw         = EXCLUDED.raw,
                       diagnostics = EXCLUDED.diagnostics""",
                (task_id, Json(sections),
                 Json(raw) if raw is not None else None,
                 Json(diagnostics) if diagnostics is not None else None),
            )
            conn.commit()
    finally:
        pool.putconn(conn)


def update_ti_task_failed(task_id: str, error_msg: str) -> None:
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE ti_tasks
                   SET status = 'failed', error_msg = %s, finished_at = NOW()
                   WHERE task_id = %s::uuid""",
                (error_msg, task_id),
            )
            conn.commit()
    finally:
        pool.putconn(conn)


def _row_to_task(row, with_result: bool = False) -> Dict:
    task = {
        "task_id": str(row[0]),
        "seed_type": row[1],
        "seed_value": row[2],
        "platform": row[3],
        "channel": row[4],
        "status": row[5],
        "error_msg": row[6],
        "created_at": row[7].isoformat() if row[7] else None,
        "finished_at": row[8].isoformat() if row[8] else None,
        "latency_ms": row[9],
        "callback_status": row[10],
        "user_id": row[11],
        "information_json": row[12] or {},
    }
    if with_result:
        task["sections"] = row[13] if len(row) > 13 and row[13] is not None else []
        task["raw"] = row[14] if len(row) > 14 else None
        task["diagnostics"] = row[15] if len(row) > 15 else None
    return task


_TASK_COLS = ("t.task_id, t.seed_type, t.seed_value, t.platform, t.channel, "
              "t.status, t.error_msg, t.created_at, t.finished_at, t.latency_ms, "
              "t.callback_status, t.user_id, t.information_json")


def get_ti_task(task_id: str) -> Optional[Dict]:
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT {_TASK_COLS}, r.sections, r.raw, r.diagnostics
                    FROM ti_tasks t
                    LEFT JOIN ti_task_results r ON r.task_id = t.task_id
                    WHERE t.task_id = %s::uuid""",
                (task_id,),
            )
            row = cur.fetchone()
            return _row_to_task(row, with_result=True) if row else None
    finally:
        pool.putconn(conn)


def delete_ti_task(task_id: str) -> bool:
    """删除 TI 任务；ti_task_results 由外键 ON DELETE CASCADE 级联删除。"""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM ti_tasks WHERE task_id = %s::uuid",
                (task_id,),
            )
            deleted = cur.rowcount > 0
            conn.commit()
            return deleted
    finally:
        pool.putconn(conn)


def list_ti_tasks(
    page: int = 1,
    page_size: int = 20,
    platform: Optional[str] = None,
    status: Optional[str] = None,
    user_id: Optional[int] = None,
) -> Dict:
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            where, params = [], []
            if platform:
                where.append("platform = %s")
                params.append(platform)
            if status:
                where.append("status = %s")
                params.append(status)
            if user_id is not None:
                where.append("user_id = %s")
                params.append(user_id)
            where_sql = ("WHERE " + " AND ".join(where)) if where else ""

            cur.execute(f"SELECT COUNT(*) FROM ti_tasks {where_sql}", params)
            total = cur.fetchone()[0]

            cur.execute(
                f"""SELECT {_TASK_COLS.replace('t.', '')}
                    FROM ti_tasks {where_sql}
                    ORDER BY created_at DESC
                    LIMIT %s OFFSET %s""",
                params + [page_size, (page - 1) * page_size],
            )
            items = [_row_to_task(r) for r in cur.fetchall()]
            return {"total": total, "items": items}
    finally:
        pool.putconn(conn)


def find_cached_ti_task(seed_type: str, seed_value: str, platform: str,
                        cache_ttl_seconds: int) -> Optional[Dict]:
    """查找缓存复用命中：同种子+平台、done、未过缓存期的最近任务。

    命中时按滚动窗口刷新 cache_expires_at（与 NER 缓存同一语义）。
    """
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT task_id FROM ti_tasks
                   WHERE seed_type = %s AND seed_value = %s AND platform = %s
                     AND status = 'done'
                     AND cache_expires_at IS NOT NULL AND cache_expires_at > NOW()
                   ORDER BY finished_at DESC
                   LIMIT 1""",
                (seed_type, seed_value, platform),
            )
            row = cur.fetchone()
            if not row:
                return None
            task_id = str(row[0])
            if cache_ttl_seconds > 0:
                cur.execute(
                    """UPDATE ti_tasks
                       SET cache_expires_at = NOW() + (%s || ' seconds')::interval
                       WHERE task_id = %s::uuid""",
                    (int(cache_ttl_seconds), task_id),
                )
                conn.commit()
            return get_ti_task(task_id)
    finally:
        pool.putconn(conn)


# ─── 缓存管理（admin 缓存管理页「联网解析缓存」分类）────────────────────────────

def get_ti_cache_stats() -> Dict:
    """TI 缓存复用概览，与 db.get_cache_stats 同构（active/expired/最近最远过期时间）。"""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT
                       COUNT(*) FILTER (
                           WHERE cache_expires_at IS NOT NULL AND cache_expires_at > NOW()
                       ),
                       COUNT(*) FILTER (
                           WHERE cache_expires_at IS NOT NULL AND cache_expires_at <= NOW()
                       ),
                       MIN(cache_expires_at) FILTER (
                           WHERE cache_expires_at IS NOT NULL AND cache_expires_at > NOW()
                       ),
                       MAX(cache_expires_at) FILTER (
                           WHERE cache_expires_at IS NOT NULL AND cache_expires_at > NOW()
                       )
                   FROM ti_tasks"""
            )
            r = cur.fetchone()
            return {
                "active": int(r[0] or 0),
                "expired": int(r[1] or 0),
                "soonest_expire": r[2],
                "latest_expire": r[3],
            }
    finally:
        pool.putconn(conn)


def list_ti_cache_entries(page: int = 1, page_size: int = 20,
                          only_active: bool = True) -> Dict:
    """分页列出参与缓存复用的 TI 任务（cache_expires_at 非 NULL）。返回 {total, items}。"""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            where = "WHERE cache_expires_at IS NOT NULL"
            if only_active:
                where += " AND cache_expires_at > NOW()"
            cur.execute(f"SELECT COUNT(*) FROM ti_tasks {where}")
            total = cur.fetchone()[0]
            cur.execute(
                f"""SELECT task_id, platform, seed_type, seed_value, status,
                           created_at, finished_at, cache_expires_at,
                           (cache_expires_at <= NOW())
                    FROM ti_tasks {where}
                    ORDER BY cache_expires_at DESC
                    LIMIT %s OFFSET %s""",
                (page_size, (page - 1) * page_size),
            )
            items = [
                {
                    "task_id": str(r[0]),
                    "platform": r[1],
                    "seed_type": r[2],
                    "seed_value": r[3],
                    "status": r[4],
                    "created_at": r[5],
                    "finished_at": r[6],
                    "cache_expires_at": r[7],
                    "expired": bool(r[8]),
                }
                for r in cur.fetchall()
            ]
            return {"total": int(total), "items": items}
    finally:
        pool.putconn(conn)


def invalidate_ti_cache_entry(task_id: str) -> bool:
    """让单条 TI 任务的缓存失效（cache_expires_at 置 NULL）。返回是否命中任务行。"""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE ti_tasks SET cache_expires_at = NULL WHERE task_id = %s::uuid",
                (task_id,),
            )
            affected = cur.rowcount
            conn.commit()
            return affected > 0
    finally:
        pool.putconn(conn)


def clear_ti_cache(expired_only: bool = False) -> int:
    """TI 缓存置为不可复用（不删除任务）。expired_only=True 仅处理已过期行。"""
    where = "WHERE cache_expires_at IS NOT NULL"
    if expired_only:
        where += " AND cache_expires_at <= NOW()"
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE ti_tasks SET cache_expires_at = NULL {where}")
            affected = cur.rowcount
            conn.commit()
            return affected
    finally:
        pool.putconn(conn)


def delete_expired_ti_cache_tasks() -> int:
    """物理删除已过期的 TI 缓存任务（后台定时清理用；结果行级联删除）。

    与 db.delete_expired_cache_tasks 同一语义：仅删 cache_expires_at 非 NULL
    且已过期的任务；NULL 行（未参与复用 / 已手动失效）不动。
    """
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """DELETE FROM ti_tasks
                   WHERE cache_expires_at IS NOT NULL AND cache_expires_at <= NOW()"""
            )
            affected = cur.rowcount
            conn.commit()
            return affected
    finally:
        pool.putconn(conn)


# ─── 数据导出（/ti/export，按创建日期范围取全量字段）────────────────────────────

def fetch_ti_export_rows(start_date=None, end_date=None, batch_id=None):
    """按 ti_tasks.created_at 日期范围取导出行（含首尾两天；None 表示该侧不限制）。

    batch_id 非空时按任务组过滤（ti_tasks.information_json->>'batch_id'，
    批量提交的任务组在提交时写入该标记），与日期条件叠加。

    返回 (tasks, results)：
      - tasks   为 ti_tasks 全字段行（不含自增 id，目标库加载时重新分配），
                列序与 exporter.TI_TASK_COLUMNS 一致；
      - results 为对应 ti_task_results 行，列序与 exporter.TI_RESULT_COLUMNS 一致。
    均为按 created_at 升序的元组列表。
    """
    where, params = [], []
    if start_date is not None:
        where.append("t.created_at >= %s::date")
        params.append(start_date)
    if end_date is not None:
        where.append("t.created_at < %s::date + 1")
        params.append(end_date)
    if batch_id:
        where.append("t.information_json->>'batch_id' = %s")
        params.append(batch_id)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT t.task_id, t.seed_type, t.seed_value, t.platform, t.channel,
                           t.status, t.error_msg, t.created_at, t.finished_at, t.latency_ms,
                           t.callback_config, t.callback_status, t.callback_error,
                           t.callback_finished_at, t.user_id, t.cache_expires_at,
                           t.information_json
                    FROM ti_tasks t
                    {where_sql}
                    ORDER BY t.created_at, t.task_id""",
                params,
            )
            tasks = cur.fetchall()
            cur.execute(
                f"""SELECT r.task_id, r.sections, r.raw, r.diagnostics, r.created_at
                    FROM ti_task_results r
                    JOIN ti_tasks t ON t.task_id = r.task_id
                    {where_sql}
                    ORDER BY r.created_at, r.task_id""",
                params,
            )
            results = cur.fetchall()
            return tasks, results
    finally:
        pool.putconn(conn)


# ─── 调用统计 ─────────────────────────────────────────────────────────────────

def record_call(platform: str, channel: str, success: bool) -> None:
    """按天累计外部平台调用量（day 为 YYYYMMDD 整数）。"""
    day = int(datetime.now().strftime("%Y%m%d"))
    col = "success_count" if success else "fail_count"
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO ti_call_daily (day, platform, channel, {col})
                    VALUES (%s, %s, %s, 1)
                    ON CONFLICT (day, platform, channel)
                    DO UPDATE SET {col} = ti_call_daily.{col} + 1""",
                (day, platform, channel),
            )
            conn.commit()
    finally:
        pool.putconn(conn)


def get_today_call_count(platform: str) -> int:
    """该平台今日已发起的外部调用总数（成功+失败），用于日上限判定。

    缓存命中（channel='cache'）不触达平台，不计入日上限。
    """
    day = int(datetime.now().strftime("%Y%m%d"))
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT COALESCE(SUM(success_count + fail_count), 0)
                   FROM ti_call_daily
                   WHERE day = %s AND platform = %s AND channel <> 'cache'""",
                (day, platform),
            )
            return int(cur.fetchone()[0])
    finally:
        pool.putconn(conn)


def get_call_daily(days: int = 30) -> List[Dict]:
    """近 N 天调用明细（含 channel='cache' 的缓存命中行，命中记 success_count）。"""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT day, platform, channel, success_count, fail_count
                   FROM ti_call_daily
                   ORDER BY day DESC, platform
                   LIMIT %s""",
                (max(1, days) * 8,),
            )
            return [
                {"day": r[0], "platform": r[1], "channel": r[2],
                 "success_count": r[3], "fail_count": r[4]}
                for r in cur.fetchall()
            ]
    finally:
        pool.putconn(conn)


def get_call_totals() -> Dict:
    """全量调用合计：外部调用成功/失败、缓存命中（channel='cache' 的 success_count）。"""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT COALESCE(SUM(success_count) FILTER (WHERE channel <> 'cache'), 0),
                          COALESCE(SUM(fail_count)    FILTER (WHERE channel <> 'cache'), 0),
                          COALESCE(SUM(success_count) FILTER (WHERE channel =  'cache'), 0)
                   FROM ti_call_daily"""
            )
            r = cur.fetchone()
            return {"external_success": int(r[0]), "external_fail": int(r[1]),
                    "cache_hits": int(r[2])}
    finally:
        pool.putconn(conn)


def get_task_stats() -> Dict:
    """ti_tasks 聚合：整体/按平台任务数与平均耗时、种子类型分布。

    注意：任务行可被用户删除，计数随之变化；latency_ms 仅 done 任务有值。
    """
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*),
                          COUNT(*) FILTER (WHERE status = 'done'),
                          COUNT(*) FILTER (WHERE status = 'failed'),
                          AVG(latency_ms) FILTER (WHERE status = 'done'
                                                    AND latency_ms IS NOT NULL)
                   FROM ti_tasks"""
            )
            r = cur.fetchone()
            overall = {
                "tasks": r[0], "tasks_done": r[1], "tasks_failed": r[2],
                "avg_latency_ms": round(float(r[3]), 1) if r[3] is not None else None,
            }
            cur.execute(
                """SELECT platform, COUNT(*),
                          COUNT(*) FILTER (WHERE status = 'done'),
                          COUNT(*) FILTER (WHERE status = 'failed'),
                          AVG(latency_ms) FILTER (WHERE status = 'done'
                                                    AND latency_ms IS NOT NULL)
                   FROM ti_tasks GROUP BY platform"""
            )
            platforms = {
                row[0]: {
                    "tasks": row[1], "tasks_done": row[2], "tasks_failed": row[3],
                    "avg_latency_ms": (round(float(row[4]), 1)
                                       if row[4] is not None else None),
                }
                for row in cur.fetchall()
            }
            cur.execute(
                """SELECT seed_type, COUNT(*) FROM ti_tasks
                   GROUP BY seed_type ORDER BY COUNT(*) DESC"""
            )
            seed_type_dist = [{"seed_type": row[0], "count": row[1]}
                              for row in cur.fetchall()]
            return {"overall": overall, "platforms": platforms,
                    "seed_type_dist": seed_type_dist}
    finally:
        pool.putconn(conn)


# ─── 会话状态 ─────────────────────────────────────────────────────────────────

def ensure_session_rows(platforms) -> None:
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            for p in platforms:
                cur.execute(
                    "INSERT INTO ti_session (platform) VALUES (%s) ON CONFLICT DO NOTHING",
                    (p,),
                )
            conn.commit()
    finally:
        pool.putconn(conn)


def upsert_session_state(
    platform: str,
    login_state: str,
    fail_reason: Optional[str] = None,
    proxy_used: Optional[str] = None,
    logged_in: bool = False,
) -> None:
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO ti_session (platform, login_state, fail_reason, proxy_used,
                                           last_login_at, last_check_at, updated_at)
                    VALUES (%s, %s, %s, %s,
                            {'NOW()' if logged_in else 'NULL'}, NOW(), NOW())
                    ON CONFLICT (platform) DO UPDATE
                    SET login_state   = EXCLUDED.login_state,
                        fail_reason   = EXCLUDED.fail_reason,
                        proxy_used    = EXCLUDED.proxy_used,
                        last_check_at = NOW(),
                        updated_at    = NOW()
                        {', last_login_at = NOW()' if logged_in else ''}""",
                (platform, login_state, fail_reason, proxy_used),
            )
            conn.commit()
    finally:
        pool.putconn(conn)


def get_sessions() -> List[Dict]:
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT platform, login_state, last_login_at, last_check_at,
                          fail_reason, proxy_used
                   FROM ti_session ORDER BY platform"""
            )
            return [
                {"platform": r[0], "login_state": r[1],
                 "last_login_at": r[2].isoformat() if r[2] else None,
                 "last_check_at": r[3].isoformat() if r[3] else None,
                 "fail_reason": r[4], "proxy_used": r[5]}
                for r in cur.fetchall()
            ]
    finally:
        pool.putconn(conn)
