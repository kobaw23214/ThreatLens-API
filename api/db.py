"""
PostgreSQL 操作层（同步，使用 psycopg2 线程安全连接池）

Schema 同步策略（参考 rag-web SchemaManager.java）：
  - 扫描 api/db/*.sql，解析 CREATE TABLE 语句
  - 表不存在 → 直接创建
  - 表存在但列不一致 → DROP CASCADE 后重建
  - 表存在且一致 → 跳过
"""
from __future__ import annotations

import re
import logging
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor, Json

logger = logging.getLogger(__name__)

_pool: Optional[pool.ThreadedConnectionPool] = None

# SQL 文件目录（与本文件同级的 db/ 子目录）
_SQL_DIR = Path(__file__).parent / "db"

# 有外键依赖时可在此指定顺序，其余文件按文件名排序追加
_ORDERED_FILES: List[str] = [
    "tasks.sql",
    "task_results.sql",
    "users.sql",
    "api_keys.sql",
    "usage_logs.sql",
    "app_settings.sql",
    # ti_task_results 外键依赖 ti_tasks，必须保持先后
    "ti_tasks.sql",
    "ti_task_results.sql",
    "ti_call_daily.sql",
    "ti_session.sql",
]

# 匹配 CREATE TABLE IF NOT EXISTS <name> (
_RE_CREATE = re.compile(
    r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)\s*\(",
    re.IGNORECASE,
)

# 匹配列定义行：<colname> <type> ...（排除 CONSTRAINT / FOREIGN KEY / PRIMARY KEY / CHECK 等）
_RE_COLUMN = re.compile(
    r"^\s+(\w+)\s+(?:SERIAL|BIGSERIAL|UUID|VARCHAR|TEXT|INTEGER|INT|BIGINT|BOOLEAN"
    r"|TIMESTAMPTZ|JSONB|FLOAT|DOUBLE|NUMERIC|SMALLINT|REAL)\b",
    re.IGNORECASE,
)


# ─── 连接池 ────────────────────────────────────────────────────────────────────

def init_db(host: str, port: int, dbname: str, user: str, password: str) -> None:
    global _pool
    _ensure_database(host, port, dbname, user, password)
    _pool = pool.ThreadedConnectionPool(
        minconn=1,
        maxconn=20,
        host=host,
        port=port,
        dbname=dbname,
        user=user,
        password=password,
    )
    _sync_schema()


def _ensure_database(host: str, port: int, dbname: str, user: str, password: str) -> None:
    """若目标数据库不存在则自动创建。"""
    conn = psycopg2.connect(
        host=host, port=port, dbname="postgres", user=user, password=password
    )
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)
            )
            if not cur.fetchone():
                cur.execute(f'CREATE DATABASE "{dbname}"')
                logger.info("数据库 %s 已自动创建", dbname)
            else:
                logger.info("数据库 %s 已存在", dbname)
    finally:
        conn.close()


# ─── Schema 同步 ───────────────────────────────────────────────────────────────

def _sync_schema() -> None:
    """扫描 db/*.sql，同步所有表结构。此外执行 db/migrations/*.sql 增量脚本。"""
    logger.info("开始 Schema 同步，扫描目录: %s", _SQL_DIR)

    ordered = list(_ORDERED_FILES)
    all_files = {f.name for f in _SQL_DIR.glob("*.sql")}
    # 追加未在 ORDERED_FILES 中的文件（按文件名排序）
    for name in sorted(all_files - set(ordered)):
        ordered.append(name)

    for filename in ordered:
        sql_path = _SQL_DIR / filename
        if not sql_path.exists():
            logger.warning("SQL 文件不存在: %s", sql_path)
            continue
        try:
            sql_text = sql_path.read_text(encoding="utf-8")
            _process_sql_file(filename, sql_text)
        except Exception as exc:
            logger.error("处理 SQL 文件 %s 失败: %s", filename, exc)

    # 执行 migrations/ 下的增量脚本(按文件名排序)。
    # 这些脚本全部使用 IF NOT EXISTS / ADD COLUMN IF NOT EXISTS 等幂等语句,
    # 可多次执行而不报错,用于补齐 _sync_schema 已同步的 schema 与生产数据的差异。
    migrations_dir = _SQL_DIR / "migrations"
    if migrations_dir.is_dir():
        for mig_path in sorted(migrations_dir.glob("*.sql")):
            try:
                _process_migration_file(mig_path)
            except Exception as exc:
                logger.error("执行 migration %s 失败: %s", mig_path.name, exc)

    logger.info("Schema 同步完成")


def _process_migration_file(mig_path: Path) -> None:
    """
    执行一个 migration SQL 文件。所有语句包裹在一个事务里,失败则回滚。
    出错时向上抛,由调用方记录日志。
    """
    sql_text = mig_path.read_text(encoding="utf-8")
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            for stmt in sql_text.split(";"):
                clean = "\n".join(
                    line for line in stmt.splitlines() if not line.strip().startswith("--")
                ).strip()
                if not clean:
                    continue
                cur.execute(clean)
        conn.commit()
        logger.info("[migration] 已应用: %s", mig_path.name)
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


def _process_sql_file(filename: str, sql_text: str) -> None:
    tables = _parse_create_tables(sql_text)
    conn = _pool.getconn()
    try:
        for table in tables:
            _sync_table(conn, table, filename)
        # 执行非 CREATE TABLE 语句（INDEX / COMMENT 等）
        _exec_non_create(conn, sql_text, tables)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


def _sync_table(conn, table: Dict, filename: str) -> None:
    table_name = table["name"]
    with conn.cursor() as cur:
        # 检查表是否存在
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = %s",
            (table_name,),
        )
        exists = cur.fetchone() is not None

        if not exists:
            logger.info("[%s] 表不存在，创建: %s", filename, table_name)
            cur.execute(table["create_sql"])
        else:
            db_cols = _get_db_columns(cur, table_name)
            sql_cols = table["columns"]
            if db_cols != sql_cols:
                missing = sql_cols - db_cols
                extra   = db_cols - sql_cols
                logger.info(
                    "[%s] 表 %s 结构不一致 — 缺少列: %s，多余列: %s，重建中...",
                    filename, table_name, missing, extra,
                )
                cur.execute(f"DROP TABLE IF EXISTS {table_name} CASCADE")
                cur.execute(table["create_sql"])
                logger.info("[%s] 表 %s 重建完成", filename, table_name)
            else:
                logger.info("[%s] 表已存在且一致: %s", filename, table_name)


def _get_db_columns(cur, table_name: str) -> Set[str]:
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s",
        (table_name,),
    )
    return {row[0] for row in cur.fetchall()}


def _parse_create_tables(sql_text: str) -> List[Dict]:
    """从 SQL 文本中解析所有 CREATE TABLE 块，返回 [{name, columns, create_sql}]。"""
    results = []
    pos = 0
    upper = sql_text.upper()

    while True:
        idx = upper.find("CREATE TABLE", pos)
        if idx < 0:
            break

        paren_start = sql_text.find("(", idx)
        if paren_start < 0:
            break

        # 括号匹配
        depth, paren_end = 0, -1
        for i in range(paren_start, len(sql_text)):
            if sql_text[i] == "(":
                depth += 1
            elif sql_text[i] == ")":
                depth -= 1
                if depth == 0:
                    paren_end = i
                    break
        if paren_end < 0:
            break

        semi = sql_text.find(";", paren_end)
        end  = semi + 1 if semi >= 0 else len(sql_text)
        create_sql = sql_text[idx:end].strip()

        m = _RE_CREATE.search(create_sql)
        if m:
            table_name = m.group(1).lower()
            body       = sql_text[paren_start + 1:paren_end]
            columns    = _parse_columns(body)
            results.append({"name": table_name, "columns": columns, "create_sql": create_sql})

        pos = end

    return results


def _parse_columns(body: str) -> Set[str]:
    cols: Set[str] = set()
    for line in body.splitlines():
        m = _RE_COLUMN.match(line)
        if m:
            cols.add(m.group(1).lower())
    return cols


def _exec_non_create(conn, sql_text: str, tables: List[Dict]) -> None:
    """执行 SQL 文件中非 CREATE TABLE 的语句（索引、COMMENT 等）。"""
    remaining = sql_text
    for t in tables:
        remaining = remaining.replace(t["create_sql"], "")

    with conn.cursor() as cur:
        for stmt in remaining.split(";"):
            clean = "\n".join(
                line for line in stmt.splitlines() if not line.strip().startswith("--")
            ).strip()
            if not clean:
                continue
            try:
                cur.execute(clean)
            except Exception as exc:
                msg = str(exc)
                if "already exists" not in msg:
                    logger.debug("执行语句失败（可忽略）: %s", msg)
                conn.rollback()  # psycopg2 出错后需回滚才能继续


def insert_task(
    task_id: str,
    model_name: str,
    input_text: str,
    labels: List[str],
    label_mapping: Optional[Dict] = None,
    task_hash: Optional[str] = None,
    information_json: Dict = None,
    callback_config: Optional[Dict] = None,
    user_id: Optional[int] = None,
    source: Optional[str] = None,
    cache_hit: bool = False,
) -> None:
    if information_json is None:
        information_json = {}
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO tasks (task_id, task_hash, model_name, input_text, labels, label_mapping, information_json, callback_config, status, user_id, source, cache_hit)
                   VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s)""",
                (task_id, task_hash, model_name, input_text, Json(labels), 
                 Json(label_mapping) if label_mapping else None,
                 Json(information_json),
                 Json(callback_config) if callback_config else None,
                 user_id, source, cache_hit),
            )
            conn.commit()
    finally:
        _pool.putconn(conn)


def update_task_running(task_id: str) -> None:
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET status = 'running' WHERE task_id = %s::uuid",
                (task_id,),
            )
            conn.commit()
    finally:
        _pool.putconn(conn)


def update_task_done(
    task_id: str,
    result: List[Dict],
    latency_ms: float,
    chunk_metadata: Optional[Dict] = None,
    cache_ttl_seconds: Optional[int] = None,
    routed_model: Optional[str] = None,
) -> None:
    """
    写入任务结果。

    Args:
        task_id: 任务 UUID
        result:  实体列表(每项含 text / label / start / end / score / chunk_index / ...)
        latency_ms: 推理耗时(毫秒)
        chunk_metadata: 分块元数据 dict;None 表示写入 SQL NULL(历史/未分块任务)
        cache_ttl_seconds: 缓存复用有效期(秒)。None 表示该任务不参与缓存复用
            (cache_expires_at 写 NULL);>0 则 cache_expires_at = NOW() + 间隔。
        routed_model: auto/attack-auto 路由后的实际子模型(多个逗号拼接);
            None 表示直连模型或无法判定(如无实体产出)。
    """
    # 统计实体类型分布
    stats: Dict[str, int] = {}
    for e in result:
        label = e.get("label", "UNKNOWN")
        stats[label] = stats.get(label, 0) + 1

    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            if cache_ttl_seconds is not None and cache_ttl_seconds > 0:
                cur.execute(
                    """UPDATE tasks
                       SET status = 'done', latency_ms = %s, finished_at = NOW(),
                           cache_expires_at = NOW() + (%s || ' seconds')::interval,
                           routed_model = %s
                       WHERE task_id = %s::uuid""",
                    (latency_ms, int(cache_ttl_seconds), routed_model, task_id),
                )
            else:
                cur.execute(
                    """UPDATE tasks
                       SET status = 'done', latency_ms = %s, finished_at = NOW(),
                           cache_expires_at = NULL, routed_model = %s
                       WHERE task_id = %s::uuid""",
                    (latency_ms, routed_model, task_id),
                )
            cur.execute(
                """INSERT INTO task_results (task_id, entities, entity_stats, chunk_metadata)
                   VALUES (%s::uuid, %s, %s, %s)
                   ON CONFLICT (task_id) DO UPDATE
                   SET entities       = EXCLUDED.entities,
                       entity_stats   = EXCLUDED.entity_stats,
                       chunk_metadata = EXCLUDED.chunk_metadata""",
                (
                    task_id,
                    Json(result),
                    Json(stats),
                    Json(chunk_metadata) if chunk_metadata is not None else None,
                ),
            )
            conn.commit()
    finally:
        _pool.putconn(conn)


def update_task_failed(task_id: str, error_msg: str) -> None:
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE tasks
                   SET status = 'failed', error_msg = %s, finished_at = NOW()
                   WHERE task_id = %s::uuid""",
                (error_msg, task_id),
            )
            conn.commit()
    finally:
        _pool.putconn(conn)





def list_tasks(page: int = 1, page_size: int = 20,
               model_name: Optional[str] = None,
               model_names: Optional[list] = None,
               status: Optional[str] = None,
               user_id: Optional[int] = None) -> Dict:
    """
    分页查询任务列表，返回 {total, items}。
    每条记录追加派生字段 chunk_count（从 task_results.chunk_metadata->>'chunk_count' 读取）,
    未分块 / 历史任务为 None。

    model_name:  精确匹配单个模型名（优先级高于 model_names）。
    model_names: 多模型名 IN 过滤，用于按类别（NER / ATT&CK）过滤全部模型任务。
    """
    offset = (page - 1) * page_size
    conditions = []
    params: list = []
    if model_name:
        conditions.append("t.model_name = %s")
        params.append(model_name)
    elif model_names:
        # 多模型名过滤（IN 子句），用于按类别过滤
        placeholders = ",".join(["%s"] * len(model_names))
        conditions.append(f"t.model_name IN ({placeholders})")
        params.extend(model_names)
    if status:
        conditions.append("t.status = %s")
        params.append(status)
    if user_id is not None:
        conditions.append("t.user_id = %s")
        params.append(user_id)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    conn = _pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f"SELECT COUNT(*) AS cnt FROM tasks t {where}", params)
            total = cur.fetchone()["cnt"]

            cur.execute(
                f"""SELECT t.task_id, t.model_name, t.status, t.error_msg,
                           t.created_at, t.finished_at, t.latency_ms,
                           t.labels, t.label_mapping,
                           LEFT(t.input_text, 100) AS input_text_preview,
                           (r.chunk_metadata->>'chunk_count')::int AS chunk_count
                    FROM tasks t
                    LEFT JOIN task_results r ON r.task_id = t.task_id
                    {where}
                    ORDER BY t.created_at DESC
                    LIMIT %s OFFSET %s""",
                params + [page_size, offset],
            )
            items = [dict(r) for r in cur.fetchall()]
        return {"total": int(total), "items": items}
    finally:
        _pool.putconn(conn)


def get_task(task_id: str) -> Optional[Dict]:

    conn = _pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT t.*, r.entities, r.entity_stats, r.chunk_metadata
                   FROM tasks t
                   LEFT JOIN task_results r ON r.task_id = t.task_id
                   WHERE t.task_id = %s::uuid""",
                (task_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        _pool.putconn(conn)


def get_task_result(task_id: str) -> Optional[Dict]:
    """只查结果表，适合前端单独获取结果和统计。"""
    conn = _pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM task_results WHERE task_id = %s::uuid",
                (task_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        _pool.putconn(conn)


def get_label_mapping(task_id: str) -> Optional[Dict]:
    """获取该任务的 label_mapping，用于前端展示时的标签反查。"""
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT label_mapping FROM tasks WHERE task_id = %s::uuid",
                (task_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        _pool.putconn(conn)


def get_stats(user_id: Optional[int] = None) -> List[Dict]:
    conn = _pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            query = """
                SELECT
                    model_name,
                    COUNT(*)                                        AS total_requests,
                    COUNT(*) FILTER (WHERE status = 'done')        AS successful_requests,
                    COUNT(*) FILTER (WHERE status = 'failed')      AS failed_requests,
                    AVG(latency_ms) FILTER (WHERE status = 'done') AS avg_latency_ms
                FROM tasks
            """
            params = []
            if user_id is not None:
                query += " WHERE user_id = %s"
                params.append(user_id)
            query += " GROUP BY model_name ORDER BY model_name"
            cur.execute(query, params)
            return [dict(row) for row in cur.fetchall()]
    finally:
        _pool.putconn(conn)


def get_model_overview(user_id: Optional[int] = None, days: int = 30) -> Dict:
    """
    任务统计增强聚合（实时 SQL，供 /stats/models/overview 使用）。

    返回 5 组原始行，按 model_name 关联，由调用方按模型类型分组：
      models : 每模型全量口径（总量/成败/缓存/今日/延迟均值/P95/平均实体数/平均输入长/最近调用）
      daily  : 近 days 天 × 模型 的成功(非缓存)/失败/缓存命中
      labels : 近 days 天 done 任务的 entity_stats 标签分布（model_name, label, count）
      sources: 每模型来源渠道分布（text/upload/eml/unknown）
      routed : auto 路由实际子模型分布（model_name, routed_model, count）
    """
    days = max(1, min(int(days), 365))
    where_user = "AND t.user_id = %s" if user_id is not None else ""
    conn = _pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # 1) 每模型全量口径
            cur.execute(
                f"""SELECT t.model_name,
                           COUNT(*)                                            AS total_requests,
                           COUNT(*) FILTER (WHERE t.status = 'done')          AS successful_requests,
                           COUNT(*) FILTER (WHERE t.status = 'failed')        AS failed_requests,
                           COUNT(*) FILTER (WHERE t.cache_hit)                AS cache_hits,
                           COUNT(*) FILTER (WHERE t.created_at::date = CURRENT_DATE)                        AS today_calls,
                           COUNT(*) FILTER (WHERE t.created_at::date = CURRENT_DATE
                                              AND t.status = 'done' AND NOT t.cache_hit)                   AS today_success,
                           COUNT(*) FILTER (WHERE t.created_at::date = CURRENT_DATE
                                              AND t.status = 'failed')                                      AS today_failed,
                           COUNT(*) FILTER (WHERE t.created_at::date = CURRENT_DATE
                                              AND t.cache_hit)                                              AS today_cache_hits,
                           AVG(t.latency_ms) FILTER (WHERE t.status = 'done') AS avg_latency_ms,
                           percentile_cont(0.95) WITHIN GROUP (ORDER BY t.latency_ms)
                               FILTER (WHERE t.status = 'done' AND t.latency_ms IS NOT NULL) AS p95_latency_ms,
                           AVG(LENGTH(t.input_text))                           AS avg_input_len,
                           AVG(ec.entity_count) FILTER (WHERE t.status = 'done') AS avg_entities,
                           MAX(t.created_at)                                   AS last_called_at
                    FROM tasks t
                    LEFT JOIN (
                        SELECT task_id, jsonb_array_length(entities) AS entity_count
                        FROM task_results
                    ) ec ON ec.task_id = t.task_id
                    WHERE 1=1 {where_user}
                    GROUP BY t.model_name ORDER BY t.model_name""",
                (user_id,) if user_id is not None else (),
            )
            models = [dict(r) for r in cur.fetchall()]

            # 2) 近 N 天 × 模型 趋势（缓存命中单列，成功不含缓存）
            params: List = [days]
            q = f"""SELECT t.created_at::date AS day, t.model_name,
                           COUNT(*) FILTER (WHERE t.status = 'done' AND NOT t.cache_hit) AS success,
                           COUNT(*) FILTER (WHERE t.status = 'failed')                   AS fail,
                           COUNT(*) FILTER (WHERE t.cache_hit)                           AS cache
                    FROM tasks t
                    WHERE t.created_at >= NOW() - (%s || ' days')::interval {where_user}
                    GROUP BY 1, 2 ORDER BY 1"""
            if user_id is not None:
                params.append(user_id)
            cur.execute(q, params)
            daily = [dict(r) for r in cur.fetchall()]
            for r in daily:
                r["day"] = r["day"].isoformat()

            # 3) 近 N 天标签/实体类型分布
            params = [days]
            q = f"""SELECT t.model_name, k.key AS label, SUM((k.value)::int) AS count
                    FROM tasks t
                    JOIN task_results r ON r.task_id = t.task_id
                    CROSS JOIN LATERAL jsonb_each_text(r.entity_stats) AS k
                    WHERE t.status = 'done'
                      AND t.created_at >= NOW() - (%s || ' days')::interval {where_user}
                    GROUP BY 1, 2"""
            if user_id is not None:
                params.append(user_id)
            cur.execute(q, params)
            labels = [dict(r) for r in cur.fetchall()]

            # 4) 来源渠道分布（全量）
            q = f"""SELECT t.model_name, COALESCE(t.source, 'unknown') AS source, COUNT(*) AS count
                    FROM tasks t
                    WHERE 1=1 {where_user}
                    GROUP BY 1, 2"""
            cur.execute(q, (user_id,) if user_id is not None else ())
            sources = [dict(r) for r in cur.fetchall()]

            # 5) auto 路由实际子模型分布（全量）
            q = f"""SELECT t.model_name, t.routed_model, COUNT(*) AS count
                    FROM tasks t
                    WHERE t.routed_model IS NOT NULL {where_user}
                    GROUP BY 1, 2"""
            cur.execute(q, (user_id,) if user_id is not None else ())
            routed = [dict(r) for r in cur.fetchall()]

            return {
                "models": models,
                "daily": daily,
                "labels": labels,
                "sources": sources,
                "routed": routed,
            }
    finally:
        _pool.putconn(conn)


def delete_task(task_id: str) -> bool:
    """删除任务及其结果，返回是否成功删除。"""
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            print(f"[DEBUG] 执行删除 SQL，task_id: {task_id}")  # 调试日志
            # 先删除结果表（有外键约束）
            cur.execute("DELETE FROM task_results WHERE task_id = %s::uuid", (task_id,))
            result_rows = cur.rowcount
            print(f"[DEBUG] 删除 task_results 行数: {result_rows}")  # 调试日志
            # 再删除任务表
            cur.execute("DELETE FROM tasks WHERE task_id = %s::uuid", (task_id,))
            task_rows = cur.rowcount
            print(f"[DEBUG] 删除 tasks 行数: {task_rows}")  # 调试日志
            deleted = task_rows > 0
            conn.commit()
            print(f"[DEBUG] 事务已提交，返回: {deleted}")  # 调试日志
            return deleted
    except Exception as e:
        print(f"[ERROR] 删除失败: {e}")  # 错误日志
        conn.rollback()
        return False
    finally:
        _pool.putconn(conn)


def close_db() -> None:
    if _pool:
        _pool.closeall()


def find_cached_task(task_hash: str, model_name: str) -> Optional[Dict]:
    """
    根据 task_hash 和 model_name 查找可复用的历史任务，返回实体数量最多的已完成任务。

    仅返回 cache_expires_at 在当前时间之后（未过期）的任务；过期或未启用缓存
    （cache_expires_at IS NULL）的任务不会被复用。

    Args:
        task_hash: 任务内容哈希
        model_name: 模型名称

    Returns:
        包含 task_id、实体数据、chunk_metadata 和 information_json 的字典;
        历史任务 chunk_metadata 可能为 None。无可复用任务时返回 None。
    """
    conn = _pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT t.task_id, t.latency_ms, t.finished_at, t.information_json,
                          r.entities, r.entity_stats, r.chunk_metadata,
                          jsonb_array_length(r.entities) as entity_count
                   FROM tasks t
                   INNER JOIN task_results r ON r.task_id = t.task_id
                   WHERE t.task_hash = %s 
                     AND t.model_name = %s 
                     AND t.status = 'done'
                     AND t.cache_expires_at IS NOT NULL
                     AND t.cache_expires_at > NOW()
                   ORDER BY entity_count DESC NULLS LAST, t.finished_at DESC
                   LIMIT 1""",
                (task_hash, model_name),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        _pool.putconn(conn)


def refresh_cache_expiry(task_id: str, cache_ttl_seconds: int) -> None:
    """缓存命中后刷新该任务的过期时间为 NOW() + ttl（滑动过期策略）。"""
    if not cache_ttl_seconds or cache_ttl_seconds <= 0:
        return
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE tasks
                   SET cache_expires_at = NOW() + (%s || ' seconds')::interval
                   WHERE task_id = %s::uuid""",
                (int(cache_ttl_seconds), task_id),
            )
            conn.commit()
    finally:
        _pool.putconn(conn)


# ─── 全局设置 (app_settings) ───────────────────────────────────────────────────

# 缓存复用策略默认值，库中无记录时回退到此
_CACHE_SETTINGS_DEFAULTS = {
    "cache_enabled": "true",
    "cache_ttl_seconds": "28800",       # 8 小时
    "cache_refresh_on_hit": "true",
}


def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """读取单个设置项的原始字符串值。"""
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
            row = cur.fetchone()
            return row[0] if row else default
    finally:
        _pool.putconn(conn)


def set_setting(key: str, value: str) -> None:
    """写入/更新单个设置项。"""
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO app_settings (key, value, updated_at)
                   VALUES (%s, %s, NOW())
                   ON CONFLICT (key) DO UPDATE
                   SET value = EXCLUDED.value, updated_at = NOW()""",
                (key, value),
            )
            conn.commit()
    finally:
        _pool.putconn(conn)


def get_cache_settings() -> Dict:
    """
    返回缓存复用策略，类型已解析:
        {enabled: bool, ttl_seconds: int, refresh_on_hit: bool}
    库中缺失的键回退到默认值。
    """
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT key, value FROM app_settings WHERE key = ANY(%s)",
                (list(_CACHE_SETTINGS_DEFAULTS.keys()),),
            )
            raw = {k: v for k, v in cur.fetchall()}
    finally:
        _pool.putconn(conn)

    merged = {**_CACHE_SETTINGS_DEFAULTS, **raw}
    try:
        ttl = int(merged["cache_ttl_seconds"])
    except (TypeError, ValueError):
        ttl = int(_CACHE_SETTINGS_DEFAULTS["cache_ttl_seconds"])
    return {
        "enabled": str(merged["cache_enabled"]).lower() == "true",
        "ttl_seconds": ttl,
        "refresh_on_hit": str(merged["cache_refresh_on_hit"]).lower() == "true",
    }


def update_cache_settings(
    enabled: Optional[bool] = None,
    ttl_seconds: Optional[int] = None,
    refresh_on_hit: Optional[bool] = None,
) -> Dict:
    """更新缓存复用策略，返回更新后的完整配置。仅更新非 None 的字段。"""
    if enabled is not None:
        set_setting("cache_enabled", "true" if enabled else "false")
    if ttl_seconds is not None:
        set_setting("cache_ttl_seconds", str(int(ttl_seconds)))
    if refresh_on_hit is not None:
        set_setting("cache_refresh_on_hit", "true" if refresh_on_hit else "false")
    return get_cache_settings()


# ─── 缓存管理（统计 / 清理）────────────────────────────────────────────────────

def get_cache_stats(model_names: Optional[List[str]] = None) -> Dict:
    """
    返回缓存复用概览统计:
        active   — 当前未过期、可复用的任务数
        expired  — 已过期（cache_expires_at 在过去）的任务数
        soonest_expire / latest_expire — 最近 / 最远的过期时间

    model_names: 仅统计这些模型的缓存（NER / ATT&CK 分类视图用）；
        None 表示不过滤。空列表匹配不到任何行，自然返回全零。
    """
    where = ""
    params: list = []
    if model_names is not None:
        where = "WHERE model_name = ANY(%s)"
        params.append(model_names)
    conn = _pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""SELECT
                       COUNT(*) FILTER (
                           WHERE cache_expires_at IS NOT NULL AND cache_expires_at > NOW()
                       ) AS active,
                       COUNT(*) FILTER (
                           WHERE cache_expires_at IS NOT NULL AND cache_expires_at <= NOW()
                       ) AS expired,
                       MIN(cache_expires_at) FILTER (
                           WHERE cache_expires_at IS NOT NULL AND cache_expires_at > NOW()
                       ) AS soonest_expire,
                       MAX(cache_expires_at) FILTER (
                           WHERE cache_expires_at IS NOT NULL AND cache_expires_at > NOW()
                       ) AS latest_expire
                   FROM tasks {where}""",
                params,
            )
            row = cur.fetchone()
            return {
                "active": int(row["active"] or 0),
                "expired": int(row["expired"] or 0),
                "soonest_expire": row["soonest_expire"],
                "latest_expire": row["latest_expire"],
            }
    finally:
        _pool.putconn(conn)


def list_cache_entries(
    page: int = 1,
    page_size: int = 20,
    only_active: bool = True,
    model_names: Optional[List[str]] = None,
) -> Dict:
    """
    分页列出参与缓存复用的任务（cache_expires_at 非 NULL）。
    only_active=True 时仅列未过期的。model_names 非 None 时按模型过滤
    （NER / ATT&CK 分类视图用）。返回 {total, items}。
    """
    offset = (page - 1) * page_size
    where = "WHERE t.cache_expires_at IS NOT NULL"
    params: list = []
    if only_active:
        where += " AND t.cache_expires_at > NOW()"
    if model_names is not None:
        where += " AND t.model_name = ANY(%s)"
        params.append(model_names)

    conn = _pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f"SELECT COUNT(*) AS cnt FROM tasks t {where}", params)
            total = cur.fetchone()["cnt"]

            cur.execute(
                f"""SELECT t.task_id, t.task_hash, t.model_name, t.status,
                           t.created_at, t.finished_at, t.cache_expires_at,
                           (t.cache_expires_at <= NOW()) AS expired,
                           LEFT(t.input_text, 80) AS input_text_preview,
                           t.labels,
                           jsonb_array_length(r.entities) AS entity_count
                    FROM tasks t
                    LEFT JOIN task_results r ON r.task_id = t.task_id
                    {where}
                    ORDER BY t.cache_expires_at DESC
                    LIMIT %s OFFSET %s""",
                params + [page_size, offset],
            )
            items = [dict(r) for r in cur.fetchall()]
        return {"total": int(total), "items": items}
    finally:
        _pool.putconn(conn)


def clear_expired_cache(model_names: Optional[List[str]] = None) -> int:
    """把所有已过期任务的 cache_expires_at 置 NULL（停用复用，不删除任务）。

    model_names 非 None 时仅处理这些模型（分类清理用）。返回受影响行数。"""
    where = "WHERE cache_expires_at IS NOT NULL AND cache_expires_at <= NOW()"
    params: list = []
    if model_names is not None:
        where += " AND model_name = ANY(%s)"
        params.append(model_names)
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE tasks SET cache_expires_at = NULL {where}", params)
            affected = cur.rowcount
            conn.commit()
            return affected
    finally:
        _pool.putconn(conn)


def clear_all_cache(model_names: Optional[List[str]] = None) -> int:
    """把所有任务的 cache_expires_at 置 NULL（清空全部可复用缓存，不删除任务）。

    model_names 非 None 时仅处理这些模型（分类清理用）。返回受影响行数。"""
    where = "WHERE cache_expires_at IS NOT NULL"
    params: list = []
    if model_names is not None:
        where += " AND model_name = ANY(%s)"
        params.append(model_names)
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE tasks SET cache_expires_at = NULL {where}", params)
            affected = cur.rowcount
            conn.commit()
            return affected
    finally:
        _pool.putconn(conn)


def invalidate_cache_entry(task_id: str) -> bool:
    """让单条任务的缓存失效（cache_expires_at 置 NULL）。返回是否命中。"""
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET cache_expires_at = NULL WHERE task_id = %s::uuid",
                (task_id,),
            )
            affected = cur.rowcount
            conn.commit()
            return affected > 0
    finally:
        _pool.putconn(conn)


def delete_expired_cache_tasks() -> int:
    """
    物理删除所有已过期缓存对应的任务及其结果（后台定时清理用）。

    仅删除 cache_expires_at 非 NULL 且已过期的任务行；先删 task_results 再删 tasks，
    满足外键约束。返回删除的任务行数。

    注意:cache_expires_at IS NULL 的任务（历史任务 / 未启用复用 / 已被手动失效）
    不在清理范围内,避免误删非缓存任务。
    """
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            # 收集待删除的 task_id
            cur.execute(
                """SELECT task_id FROM tasks
                   WHERE cache_expires_at IS NOT NULL AND cache_expires_at <= NOW()"""
            )
            task_ids = [row[0] for row in cur.fetchall()]
            if not task_ids:
                conn.commit()
                return 0

            cur.execute(
                "DELETE FROM task_results WHERE task_id = ANY(%s::uuid[])",
                (task_ids,),
            )
            cur.execute(
                "DELETE FROM tasks WHERE task_id = ANY(%s::uuid[])",
                (task_ids,),
            )
            affected = cur.rowcount
            conn.commit()
            return affected
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


# ─── 回调失败记录相关 ──────────────────────────────────────────────────────────

def list_callback_failed_logs(
    page: int = 1,
    page_size: int = 20,
    status: Optional[str] = None,
    task_id: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    user_id: Optional[int] = None,
    kind: Optional[str] = None,
    kind_models: Optional[list] = None,
    all_models: Optional[list] = None,
) -> Dict:
    """分页查询回调失败记录列表。kind 按回调来源过滤：ti / ner / attack / unknown。"""
    offset = (page - 1) * page_size
    conditions = []
    params: list = []
    
    if status:
        conditions.append("c.status = %s")
        params.append(status)
    if task_id:
        conditions.append("c.task_id = %s::uuid")
        params.append(task_id)
    if start_time:
        conditions.append("c.failed_at >= %s::timestamptz")
        params.append(start_time)
    if end_time:
        conditions.append("c.failed_at <= %s::timestamptz")
        params.append(end_time)
    if kind == "ti":
        conditions.append("ti.platform IS NOT NULL")
    elif kind in ("ner", "attack"):
        conditions.append("ti.platform IS NULL AND t.model_name = ANY(%s)")
        params.append(kind_models or [])
    elif kind == "unknown":
        conditions.append("ti.platform IS NULL AND (t.model_name IS NULL OR NOT (t.model_name = ANY(%s)))")
        params.append(all_models or [])
    if user_id is not None:
        conditions.append("t.user_id = %s")
        params.append(user_id)
    # 恒 LEFT JOIN：带出回调来源（NER/ATT&CK 模型任务 or 联网情报任务），供列表展示回调类型
    join_clause = (
        "LEFT JOIN tasks t ON c.task_id = t.task_id "
        "LEFT JOIN ti_tasks ti ON c.task_id = ti.task_id"
    )
    
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    
    conn = _pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f"SELECT COUNT(*) AS cnt FROM callback_failed_logs c {join_clause} {where}", params)
            total = cur.fetchone()["cnt"]
            
            cur.execute(
                f"""SELECT c.id, c.task_id, c.callback_url, c.callback_method, c.callback_headers,
                           c.request_body, c.result_data,
                           c.error_message, c.http_status_code, c.failed_at, c.retry_count,
                           c.last_retry_at, c.status, c.created_at,
                           t.model_name, ti.platform AS ti_platform
                    FROM callback_failed_logs c {join_clause} {where}
                    ORDER BY c.failed_at DESC
                    LIMIT %s OFFSET %s""",
                params + [page_size, offset],
            )
            items = [dict(r) for r in cur.fetchall()]
        return {"total": int(total), "page": page, "page_size": page_size, "items": items}
    finally:
        _pool.putconn(conn)


def get_callback_failed_log(log_id: int) -> Optional[Dict]:
    """获取单个回调失败记录详情"""
    conn = _pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT * FROM callback_failed_logs WHERE id = %s""",
                (log_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        _pool.putconn(conn)


def update_callback_failed_log_retry(log_id: int) -> bool:
    """更新回调失败记录的重试信息"""
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE callback_failed_logs
                   SET retry_count = retry_count + 1,
                       last_retry_at = NOW(),
                       status = 'retrying'
                   WHERE id = %s""",
                (log_id,),
            )
            updated = cur.rowcount > 0
            conn.commit()
            return updated
    finally:
        _pool.putconn(conn)


def update_callback_failed_log_resolved(log_id: int) -> bool:
    """标记回调失败记录为已解决"""
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE callback_failed_logs
                   SET status = 'resolved'
                   WHERE id = %s""",
                (log_id,),
            )
            updated = cur.rowcount > 0
            conn.commit()
            return updated
    finally:
        _pool.putconn(conn)


def delete_callback_failed_log(log_id: int) -> bool:
    """删除回调失败记录"""
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM callback_failed_logs WHERE id = %s", (log_id,))
            deleted = cur.rowcount > 0
            conn.commit()
            return deleted
    finally:
        _pool.putconn(conn)


def delete_callback_failed_logs_batch(log_ids: List[int]) -> int:
    """批量删除回调失败记录，返回实际删除条数"""
    if not log_ids:
        return 0
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM callback_failed_logs WHERE id = ANY(%s)", (log_ids,))
            deleted = cur.rowcount
            conn.commit()
            return deleted
    finally:
        _pool.putconn(conn)


def clear_callback_failed_logs(user_id: Optional[int] = None) -> int:
    """清空回调失败记录，返回实际删除条数。传入 user_id 时仅清空该用户任务的记录（与列表可见范围一致）。"""
    where = ""
    params: list = []
    if user_id is not None:
        where = "WHERE task_id IN (SELECT task_id FROM tasks WHERE user_id = %s)"
        params.append(user_id)
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM callback_failed_logs {where}", params)
            deleted = cur.rowcount
            conn.commit()
            return deleted
    finally:
        _pool.putconn(conn)


# ─── 回调成功日志查询 ─────────────────────────────────────────────────────────

def list_callback_success_logs(
    page: int = 1,
    page_size: int = 20,
    task_id: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    user_id: Optional[int] = None,
    kind: Optional[str] = None,
    kind_models: Optional[list] = None,
    all_models: Optional[list] = None,
) -> Tuple[List[Dict], int]:
    """查询回调成功日志列表。kind 按回调来源过滤：ti / ner / attack / unknown。"""
    conn = _pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            where_clauses = []
            params = []

            if task_id:
                where_clauses.append("c.task_id = %s::uuid")
                params.append(task_id)
            if start_time:
                where_clauses.append("c.created_at >= %s::timestamptz")
                params.append(start_time)
            if end_time:
                where_clauses.append("c.created_at <= %s::timestamptz")
                params.append(end_time)
            if kind == "ti":
                where_clauses.append("ti.platform IS NOT NULL")
            elif kind in ("ner", "attack"):
                where_clauses.append("ti.platform IS NULL AND t.model_name = ANY(%s)")
                params.append(kind_models or [])
            elif kind == "unknown":
                where_clauses.append("ti.platform IS NULL AND (t.model_name IS NULL OR NOT (t.model_name = ANY(%s)))")
                params.append(all_models or [])
            if user_id is not None:
                where_clauses.append("t.user_id = %s")
                params.append(user_id)
            # 恒 LEFT JOIN：带出回调来源（NER/ATT&CK 模型任务 or 联网情报任务）
            join_clause = (
                "LEFT JOIN tasks t ON c.task_id = t.task_id "
                "LEFT JOIN ti_tasks ti ON c.task_id = ti.task_id"
            )

            where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

            cur.execute(f"SELECT COUNT(*) as count FROM callback_success_logs c {join_clause} {where_sql}", params)
            total = cur.fetchone()["count"]

            offset = (page - 1) * page_size
            params.extend([page_size, offset])
            cur.execute(
                f"""SELECT c.*, t.model_name, ti.platform AS ti_platform FROM callback_success_logs c
                    {join_clause}
                    {where_sql}
                    ORDER BY c.created_at DESC
                    LIMIT %s OFFSET %s""",
                params,
            )
            items = cur.fetchall()
            return [dict(item) for item in items], total
    finally:
        _pool.putconn(conn)



def get_callback_success_log(log_id: int) -> Optional[Dict]:
    """查询单个回调成功日志详情"""
    conn = _pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM callback_success_logs WHERE id = %s", (log_id,))
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        _pool.putconn(conn)


def delete_callback_success_log(log_id: int) -> bool:
    """删除回调成功日志"""
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM callback_success_logs WHERE id = %s", (log_id,))
            deleted = cur.rowcount > 0
            conn.commit()
            return deleted
    finally:
        _pool.putconn(conn)


def delete_callback_success_logs_batch(log_ids: List[int]) -> int:
    """批量删除回调成功日志，返回实际删除条数"""
    if not log_ids:
        return 0
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM callback_success_logs WHERE id = ANY(%s)", (log_ids,))
            deleted = cur.rowcount
            conn.commit()
            return deleted
    finally:
        _pool.putconn(conn)


def clear_callback_success_logs(user_id: Optional[int] = None) -> int:
    """清空回调成功日志，返回实际删除条数。传入 user_id 时仅清空该用户任务的记录（与列表可见范围一致）。"""
    where = ""
    params: list = []
    if user_id is not None:
        where = "WHERE task_id IN (SELECT task_id FROM tasks WHERE user_id = %s)"
        params.append(user_id)
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM callback_success_logs {where}", params)
            deleted = cur.rowcount
            conn.commit()
            return deleted
    finally:
        _pool.putconn(conn)


# ─── 回调统计 ─────────────────────────────────────────────────────────────────

def get_callback_stats(user_id: Optional[int] = None) -> Dict:
    """获取回调统计信息"""
    conn = _pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            join_clause = "JOIN tasks t ON c.task_id = t.task_id" if user_id is not None else ""
            where_clause = "WHERE t.user_id = %s" if user_id is not None else ""
            params = [user_id] if user_id is not None else []

            # 成功回调统计
            cur.execute(f"""
                SELECT 
                    COUNT(*) as success_count,
                    AVG(c.response_time_ms) as avg_response_time
                FROM callback_success_logs c
                {join_clause}
                {where_clause}
            """, params)
            success_stats = cur.fetchone()
            
            # 失败回调统计
            cur.execute(f"""
                SELECT 
                    COUNT(*) as failed_count,
                    COUNT(CASE WHEN c.status = 'pending' THEN 1 END) as pending_count
                FROM callback_failed_logs c
                {join_clause}
                {where_clause}
            """, params)
            failed_stats = cur.fetchone()
            
            # 失败原因分布
            cur.execute(f"""
                SELECT 
                    CASE 
                        WHEN c.error_message LIKE '%%超时%%' OR c.error_message LIKE '%%timeout%%' THEN '超时'
                        WHEN c.error_message LIKE '%%连接%%' OR c.error_message LIKE '%%connect%%' THEN '连接失败'
                        WHEN c.error_message LIKE '%%HTTP 4%%' THEN '客户端错误(4xx)'
                        WHEN c.error_message LIKE '%%HTTP 5%%' THEN '服务器错误(5xx)'
                        ELSE '其他错误'
                    END as reason,
                    COUNT(*) as count
                FROM callback_failed_logs c
                {join_clause}
                {where_clause}
                GROUP BY reason
            """, params)
            failed_by_reason = {row["reason"]: row["count"] for row in cur.fetchall()}
            
            total_callbacks = success_stats["success_count"] + failed_stats["failed_count"]
            success_rate = (
                success_stats["success_count"] / total_callbacks
                if total_callbacks > 0
                else 0.0
            )
            
            return {
                "total_callbacks": total_callbacks,
                "success_callbacks": success_stats["success_count"],
                "failed_callbacks": failed_stats["failed_count"],
                "pending_retries": failed_stats["pending_count"],
                "success_rate": round(success_rate, 4),
                "avg_response_time_ms": (
                    round(float(success_stats["avg_response_time"]), 2)
                    if success_stats["avg_response_time"]
                    else None
                ),
                "failed_by_reason": failed_by_reason,
            }
    finally:
        _pool.putconn(conn)


# ─── 用户管理 ─────────────────────────────────────────────────────────────────

def ensure_admin_user(password_hash: str) -> None:
    """确保 admin 用户存在。若不存在则创建，若已存在则跳过。"""
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM users WHERE username = 'admin'")
            if not cur.fetchone():
                cur.execute(
                    """INSERT INTO users (username, password_hash, is_admin, limit_5h, limit_weekly)
                       VALUES ('admin', %s, TRUE, 0, 0)""",
                    (password_hash,),
                )
                logger.info("初始管理员用户 admin 已创建")
            else:
                logger.info("管理员用户 admin 已存在，跳过创建")
        conn.commit()
    finally:
        _pool.putconn(conn)


def create_user(
    username: str, password_hash: str, is_admin: bool = False,
    limit_5h: int = 100, limit_weekly: int = 1000,
) -> Optional[Dict]:
    """创建用户，返回用户信息 dict；用户名重复时返回 None。"""
    conn = _pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            try:
                cur.execute(
                    """INSERT INTO users (username, password_hash, is_admin, limit_5h, limit_weekly)
                       VALUES (%s, %s, %s, %s, %s)
                       RETURNING id, username, is_admin, limit_5h, limit_weekly, is_active, created_at""",
                    (username, password_hash, is_admin, limit_5h, limit_weekly),
                )
                row = cur.fetchone()
                conn.commit()
                return dict(row) if row else None
            except psycopg2.errors.UniqueViolation:
                conn.rollback()
                return None
    finally:
        _pool.putconn(conn)


def get_user_by_username(username: str) -> Optional[Dict]:
    conn = _pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE username = %s", (username,))
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        _pool.putconn(conn)


def get_user_by_id(user_id: int) -> Optional[Dict]:
    conn = _pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        _pool.putconn(conn)


def list_users(page: int = 1, page_size: int = 20) -> Dict:
    offset = (page - 1) * page_size
    conn = _pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM users")
            total = cur.fetchone()["cnt"]
            cur.execute(
                """SELECT id, username, is_admin, limit_5h, limit_weekly, is_active, created_at, updated_at
                   FROM users ORDER BY created_at ASC LIMIT %s OFFSET %s""",
                (page_size, offset),
            )
            items = [dict(r) for r in cur.fetchall()]
        return {"total": int(total), "items": items}
    finally:
        _pool.putconn(conn)


def update_user(user_id: int, **kwargs) -> Optional[Dict]:
    """
    更新用户字段。支持的 kwargs: password_hash, limit_5h, limit_weekly, is_active.
    返回更新后的用户信息，用户不存在返回 None。
    """
    allowed = {"password_hash", "limit_5h", "limit_weekly", "is_active"}
    fields = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
    if not fields:
        return get_user_by_id(user_id)

    set_clause = ", ".join(f"{k} = %s" for k in fields)
    set_clause += ", updated_at = NOW()"
    values = list(fields.values()) + [user_id]

    conn = _pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""UPDATE users SET {set_clause} WHERE id = %s
                    RETURNING id, username, is_admin, limit_5h, limit_weekly, is_active, created_at, updated_at""",
                values,
            )
            row = cur.fetchone()
            conn.commit()
            return dict(row) if row else None
    finally:
        _pool.putconn(conn)


def delete_user(user_id: int) -> bool:
    """删除用户（级联删除关联的 api_keys 和 usage_logs）。不允许删除 admin。"""
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            # 禁止删除 admin
            cur.execute("SELECT is_admin FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            if not row:
                return False
            if row[0]:
                return False  # 不能删除管理员
            cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
            deleted = cur.rowcount > 0
            conn.commit()
            return deleted
    except Exception:
        conn.rollback()
        return False
    finally:
        _pool.putconn(conn)


# ─── API Key 管理 ─────────────────────────────────────────────────────────────

def create_api_key(
    user_id: int, key_hash: str, key_prefix: str,
    name: str, expires_at: str,
) -> Optional[Dict]:
    conn = _pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """INSERT INTO api_keys (user_id, key_hash, key_prefix, name, expires_at)
                   VALUES (%s, %s, %s, %s, %s::timestamptz)
                   RETURNING id, user_id, key_prefix, name, expires_at, is_active, created_at""",
                (user_id, key_hash, key_prefix, name, expires_at),
            )
            row = cur.fetchone()
            conn.commit()
            return dict(row) if row else None
    finally:
        _pool.putconn(conn)


def get_api_key_by_hash(key_hash: str) -> Optional[Dict]:
    """通过 key_hash 查找 API Key 记录（含关联用户信息）。"""
    conn = _pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT k.*, u.username, u.is_admin, u.limit_5h, u.limit_weekly, u.is_active AS user_active
                   FROM api_keys k
                   JOIN users u ON u.id = k.user_id
                   WHERE k.key_hash = %s""",
                (key_hash,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        _pool.putconn(conn)


def list_api_keys(user_id: int) -> List[Dict]:
    conn = _pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT id, user_id, key_prefix, name, expires_at, is_active, created_at
                   FROM api_keys WHERE user_id = %s ORDER BY created_at DESC""",
                (user_id,),
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        _pool.putconn(conn)


def deactivate_api_key(key_id: int, user_id: int) -> bool:
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE api_keys SET is_active = FALSE WHERE id = %s AND user_id = %s",
                (key_id, user_id),
            )
            updated = cur.rowcount > 0
            conn.commit()
            return updated
    finally:
        _pool.putconn(conn)


def delete_api_key(key_id: int, user_id: int) -> bool:
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM api_keys WHERE id = %s AND user_id = %s",
                (key_id, user_id),
            )
            deleted = cur.rowcount > 0
            conn.commit()
            return deleted
    finally:
        _pool.putconn(conn)


# ─── 调用限额 ─────────────────────────────────────────────────────────────────

def log_usage(user_id: int, endpoint: str = "/extract") -> None:
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO usage_logs (user_id, endpoint) VALUES (%s, %s)",
                (user_id, endpoint),
            )
            conn.commit()
    finally:
        _pool.putconn(conn)


def get_usage_count_5h(user_id: int) -> int:
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM usage_logs WHERE user_id = %s AND created_at >= NOW() - INTERVAL '5 hours'",
                (user_id,),
            )
            return cur.fetchone()[0]
    finally:
        _pool.putconn(conn)


def get_usage_count_weekly(user_id: int) -> int:
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM usage_logs WHERE user_id = %s AND created_at >= NOW() - INTERVAL '7 days'",
                (user_id,),
            )
            return cur.fetchone()[0]
    finally:
        _pool.putconn(conn)


def get_usage_stats(user_id: int) -> Dict:
    """返回用户的限额和已用量汇总。"""
    user = get_user_by_id(user_id)
    if not user:
        return {}
    used_5h = get_usage_count_5h(user_id)
    used_weekly = get_usage_count_weekly(user_id)
    return {
        "limit_5h": user["limit_5h"],
        "used_5h": used_5h,
        "limit_weekly": user["limit_weekly"],
        "used_weekly": used_weekly,
    }


def get_all_usage_stats(page: int = 1, page_size: int = 20) -> Dict:
    """管理员用：获取所有用户的使用统计。"""
    offset = (page - 1) * page_size
    conn = _pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM users")
            total = cur.fetchone()["cnt"]
            cur.execute(
                """SELECT u.id, u.username, u.is_admin, u.limit_5h, u.limit_weekly, u.is_active,
                          COALESCE(h5.cnt, 0) AS used_5h,
                          COALESCE(w7.cnt, 0) AS used_weekly
                   FROM users u
                   LEFT JOIN (
                       SELECT user_id, COUNT(*) AS cnt
                       FROM usage_logs WHERE created_at >= NOW() - INTERVAL '5 hours'
                       GROUP BY user_id
                   ) h5 ON h5.user_id = u.id
                   LEFT JOIN (
                       SELECT user_id, COUNT(*) AS cnt
                       FROM usage_logs WHERE created_at >= NOW() - INTERVAL '7 days'
                       GROUP BY user_id
                   ) w7 ON w7.user_id = u.id
                   ORDER BY u.id
                   LIMIT %s OFFSET %s""",
                (page_size, offset),
            )
            items = [dict(r) for r in cur.fetchall()]
        return {"total": int(total), "items": items}
    finally:
        _pool.putconn(conn)

