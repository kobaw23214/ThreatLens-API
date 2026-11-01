"""TI 联网情报 HTTP 路由（前缀 /ti）。

定位：纯情报查询展示服务——登录微步在线 / 奇安信查询 IP/域名/样本，
把平台页面原生内容取回展示，不做扩线等业务加工。
触发方式与 NER 一致：认证用户提交任务（202 + task_id），
异步执行，可选 callback_config 走全局 CallbackManager 回调。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import subprocess
import sys
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Optional

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response

from ..auth import check_rate_limit, get_current_user, require_admin
from . import store
from .exporter import build_ti_export_sql
from .cookie_import import default_cookie_domain, import_session_cookies, parse_cookie_text
from .config import (
    PLATFORMS,
    PLATFORM_NAMES,
    SETTING_DAILY_LIMIT,
    SETTING_PROXY_ENABLED,
    SETTING_PROXY_URL,
    SETTING_CACHE_ENABLED,
    SETTING_CACHE_TTL,
    TiConfig,
    apply_settings_overrides,
    effective_proxy,
    load_config,
    masked_settings_view,
    platform_credential_keys,
    platform_proxy_key,
    sanitize_proxy,
)
from .manager import TiQueueManager
from .schemas import TiExpandRequest, TiExpandResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ti", tags=["ti"])

# 由 main.py lifespan 注入
_ti_manager: Optional[TiQueueManager] = None
_ti_config: Optional[TiConfig] = None


def setup_ti(manager: TiQueueManager, cfg: TiConfig) -> None:
    global _ti_manager, _ti_config
    _ti_manager = manager
    _ti_config = cfg


def _require_ready() -> tuple:
    if _ti_manager is None or _ti_config is None or not _ti_config.enabled:
        raise HTTPException(status_code=503, detail="TI 联网情报服务未启用（TI_ENABLED=false）")
    return _ti_manager, _ti_config


# ─── 任务提交 ─────────────────────────────────────────────────────────────────

@router.post("/expand", response_model=TiExpandResponse, status_code=202)
async def ti_expand(req: TiExpandRequest, request: Request):
    """提交一个种子的联网情报查询任务（每个目标平台生成一个任务）。"""
    user = await get_current_user(request)
    check_rate_limit(user)
    from ..db import log_usage
    log_usage(user["id"], "/ti/expand")

    manager, cfg = _require_ready()
    cfg = apply_settings_overrides(cfg)

    err = req.validate_seed_value_format()
    if err:
        raise HTTPException(status_code=422, detail=err)

    platforms = req.platforms or list(PLATFORMS)

    # 预检查：任一目标平台队列满 → 整体 503，避免产生半提交状态
    for p in platforms:
        if not manager.can_accept(p):
            raise HTTPException(
                status_code=503,
                detail=f"平台 {p} 任务队列已满（{cfg.queue_size}），请稍后重试",
            )

    tasks = []
    for p in platforms:
        # 缓存复用：TTL 内有同种子同平台的完成结果直接返回
        #（ti.cache_enabled 总开关关闭时不命中；任务完成后也不再写入缓存窗口）
        if req.reuse_cache and cfg.cache_enabled:
            cached = store.find_cached_ti_task(req.seed_type, req.seed_value, p, cfg.cache_ttl)
            if cached:
                # 缓存命中计数（channel='cache'）：不触达平台、不占日上限，供缓存效益统计
                store.record_call(p, "cache", True)
                tasks.append({"task_id": cached["task_id"], "platform": p, "cached": True})
                continue

        task_id = str(uuid.uuid4())
        store.insert_ti_task(
            task_id=task_id,
            seed_type=req.seed_type,
            seed_value=req.seed_value,
            platform=p,
            channel="web",
            callback_config=req.callback,
            information_json=req.information_json,
            user_id=user["id"],
        )
        ok = await manager.submit({
            "task_id": task_id,
            "seed_type": req.seed_type,
            "seed_value": req.seed_value,
            "platform": p,
            "callback_config": req.callback,
        })
        if not ok:  # 竞态：预检查后队列被填满
            store.update_ti_task_failed(task_id, "队列已满（提交竞态）")
            raise HTTPException(status_code=503, detail=f"平台 {p} 任务队列已满，请稍后重试")
        tasks.append({"task_id": task_id, "platform": p, "cached": False})

    return JSONResponse(status_code=202, content={"tasks": tasks})


# ─── 任务查询 ─────────────────────────────────────────────────────────────────

@router.get("/tasks")
async def ti_tasks(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    platform: Optional[str] = None,
    status: Optional[str] = None,
):
    """任务列表（非管理员仅见自己的任务）。"""
    user = await get_current_user(request)
    _require_ready()
    user_id = None if user.get("is_admin") else user["id"]
    return store.list_ti_tasks(page=page, page_size=page_size,
                               platform=platform, status=status, user_id=user_id)


@router.get("/tasks/{task_id}")
async def ti_task_detail(task_id: str, request: Request):
    """任务详情（含 relations / sections / diagnostics）。"""
    user = await get_current_user(request)
    _require_ready()
    task = store.get_ti_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not user.get("is_admin") and task.get("user_id") not in (None, user["id"]):
        raise HTTPException(status_code=403, detail="无权查看该任务")
    return task


@router.delete("/tasks/{task_id}")
async def ti_task_delete(task_id: str, request: Request):
    """删除任务（非管理员仅可删自己的任务；结果行级联删除）。"""
    user = await get_current_user(request)
    _require_ready()
    task = store.get_ti_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not user.get("is_admin") and task.get("user_id") not in (None, user["id"]):
        raise HTTPException(status_code=403, detail="无权删除该任务")
    store.delete_ti_task(task_id)
    return {"message": "任务已删除", "task_id": task_id}


# ─── 数据导出（管理员）──────────────────────────────────────────────────────────

def _parse_export_date(value: Optional[str], name: str) -> Optional[date]:
    """解析 YYYY-MM-DD 查询参数；空值返回 None，非法格式抛 422。"""
    if not value or not value.strip():
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{name} 格式应为 YYYY-MM-DD")


@router.get("/export")
async def ti_export(
    request: Request,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    batch_id: Optional[str] = None,
):
    """导出 TI 库数据为 SQL 备份文件（仅管理员，全量用户数据）。

    导出 ti_tasks + ti_task_results。过滤条件（可叠加）：
    - start_date / end_date：按 ti_tasks.created_at 过滤（含首尾；缺省不限）；
    - batch_id：按任务组过滤（批量提交写入 information_json 的 batch_id）。
    生成的 SQL 幂等（ON CONFLICT DO NOTHING），可在另一套 PostgreSQL 中
    `psql -f` 直接加载。仅读库，不依赖 TI 服务开关。
    done 任务的 cache_expires_at 改写为 NOW() + 当前配置 TTL（导入时求值）：
    内网导入场景下数据默认直接进入缓存复用窗口（仅影响新插入行）。
    """
    await require_admin(request)
    start = _parse_export_date(start_date, "start_date")
    end = _parse_export_date(end_date, "end_date")
    if start and end and start > end:
        raise HTTPException(status_code=422, detail="start_date 不能晚于 end_date")
    if batch_id is not None:
        batch_id = batch_id.strip()
        if not batch_id or len(batch_id) > 128:
            raise HTTPException(status_code=422, detail="batch_id 非法")

    # 当前生效的缓存 TTL（env + 管理页覆盖），不依赖 TI 开关/worker 状态
    cfg = apply_settings_overrides(load_config())
    tasks, results = store.fetch_ti_export_rows(start, end, batch_id=batch_id)
    sql = build_ti_export_sql(tasks, results, start, end,
                              cache_ttl_seconds=cfg.cache_ttl)
    fname = f"ti_export_{batch_id or start or 'all'}_{end or 'all' if not batch_id else 'batch'}.sql"
    logger.info("TI 数据导出: %s ~ %s batch=%s，任务 %d 行 / 结果 %d 行",
                start, end, batch_id, len(tasks), len(results))
    return Response(
        content=sql.encode("utf-8"),
        media_type="application/sql",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ─── 状态 / 统计 / 会话 ───────────────────────────────────────────────────────

@router.get("/status")
async def ti_status(request: Request):
    """队列与各平台 worker 状态。"""
    await get_current_user(request)
    manager, cfg = _require_ready()
    status = manager.get_status()
    status["enabled"] = cfg.enabled
    return status


@router.get("/stats/daily")
async def ti_stats_daily(request: Request, days: int = Query(30, ge=1, le=365)):
    """外部平台调用统计（按天 / 平台 / 通道，含 channel='cache' 缓存命中行）。"""
    await get_current_user(request)
    _require_ready()
    return {"items": store.get_call_daily(days)}


def _aggregate_overview(
    daily_rows,
    call_totals: Dict,
    task_stats: Dict,
    sessions: Dict,
    daily_limit: int,
    today: int,
    window_days: int,
) -> Dict:
    """联网解析统计聚合（纯函数，便于单测）。

    口径：
      - 外部调用 = 真实触达平台的浏览器采集（ti_call_daily 中 channel != 'cache'）；
      - 缓存命中 = channel='cache' 行的 success_count，不占日上限；
      - 窗口指标（platforms.* / daily_trend）来自 daily_rows（近 window_days 天）；
      - totals / 任务数为全量（任务行可被用户删除，计数随之变化）。
    """
    def _blank():
        return {"success": 0, "fail": 0, "cache": 0,
                "today_success": 0, "today_fail": 0, "today_cache": 0}

    plat: Dict[str, Dict] = {p: _blank() for p in PLATFORMS}
    trend: Dict[int, Dict] = {}
    for r in daily_rows:
        slot = plat.setdefault(r["platform"], _blank())
        tslot = trend.setdefault(r["day"], {"success": 0, "fail": 0, "cache": 0})
        is_today = r["day"] == today
        if r["channel"] == "cache":
            slot["cache"] += r["success_count"]
            tslot["cache"] += r["success_count"]
            if is_today:
                slot["today_cache"] += r["success_count"]
        else:
            slot["success"] += r["success_count"]
            slot["fail"] += r["fail_count"]
            tslot["success"] += r["success_count"]
            tslot["fail"] += r["fail_count"]
            if is_today:
                slot["today_success"] += r["success_count"]
                slot["today_fail"] += r["fail_count"]

    task_plats = task_stats.get("platforms", {})
    platforms = []
    for p in PLATFORMS:
        d = plat[p]
        t = task_plats.get(p, {})
        calls = d["success"] + d["fail"]
        reach = calls + d["cache"]  # 窗口内平台触达总量（外部调用 + 缓存承接）
        platforms.append({
            "platform": p,
            "name": PLATFORM_NAMES.get(p, p),
            "login_state": sessions.get(p, {}).get("login_state", "unknown"),
            "daily_limit": daily_limit,
            "today_calls": d["today_success"] + d["today_fail"],
            "today_cache_hits": d["today_cache"],
            "calls": calls,
            "success": d["success"],
            "fail": d["fail"],
            "success_rate": round(d["success"] / calls, 4) if calls else 0.0,
            "cache_hits": d["cache"],
            "hit_rate": round(d["cache"] / reach, 4) if reach else 0.0,
            "tasks": t.get("tasks", 0),
            "tasks_done": t.get("tasks_done", 0),
            "tasks_failed": t.get("tasks_failed", 0),
            "avg_latency_ms": t.get("avg_latency_ms"),
        })

    external_calls = call_totals["external_success"] + call_totals["external_fail"]
    totals = {
        "external_calls": external_calls,
        "external_success": call_totals["external_success"],
        "external_fail": call_totals["external_fail"],
        "cache_hits": call_totals["cache_hits"],
        "success_rate": (round(call_totals["external_success"] / external_calls, 4)
                         if external_calls else 0.0),
        **task_stats.get("overall", {}),
    }
    today_totals = {
        "external_calls": sum(d["today_success"] + d["today_fail"] for d in plat.values()),
        "success": sum(d["today_success"] for d in plat.values()),
        "fail": sum(d["today_fail"] for d in plat.values()),
        "cache_hits": sum(d["today_cache"] for d in plat.values()),
    }
    daily_trend = [
        {"day": f"{str(day)[:4]}-{str(day)[4:6]}-{str(day)[6:8]}", **slots}
        for day, slots in sorted(trend.items())
    ]
    return {
        "window_days": window_days,
        "daily_limit": daily_limit,
        "totals": totals,
        "today": today_totals,
        "platforms": platforms,
        "seed_type_dist": task_stats.get("seed_type_dist", []),
        "daily_trend": daily_trend,
    }


@router.get("/stats/overview")
async def ti_stats_overview(request: Request, days: int = Query(30, ge=1, le=365)):
    """联网解析运营指标聚合（任务统计页「联网解析统计」板块数据源）。

    外部调用量 / 成功率 / 缓存效益 / 采集耗时 / 近 N 天趋势；平台级口径，不按用户过滤。
    """
    await get_current_user(request)
    _, cfg = _require_ready()
    cfg = apply_settings_overrides(cfg)
    return _aggregate_overview(
        store.get_call_daily(days),
        store.get_call_totals(),
        store.get_task_stats(),
        {s["platform"]: s for s in store.get_sessions()},
        cfg.daily_limit,
        int(datetime.now().strftime("%Y%m%d")),
        days,
    )


@router.get("/sessions")
async def ti_sessions(request: Request):
    """各平台登录会话状态（红绿灯数据源）。"""
    await get_current_user(request)
    _require_ready()
    store.ensure_session_rows(PLATFORMS)
    states = {s["platform"]: s for s in store.get_sessions()}
    return {
        "items": [
            {"platform": p, "name": PLATFORM_NAMES.get(p, p),
             **states.get(p, {"login_state": "unknown", "last_login_at": None,
                              "last_check_at": None, "fail_reason": None, "proxy_used": None})}
            for p in PLATFORMS
        ]
    }


# ─── 人工登录（滑块/验证码场景的会话恢复）─────────────────────────────────────

# platform → 运行中的 manual_login 子进程（防重复开窗）
_LOGIN_PROCS: Dict[str, subprocess.Popen] = {}
# platform → 远程登录桌面入口 {ws_port, ticket, expires}（一次性票据，20 分钟有效）
_LOGIN_VIEWERS: Dict[str, Dict] = {}

_VIEWER_TTL_S = 20 * 60


def _viewer_url(platform: str, entry: Dict) -> str:
    """noVNC 远程桌面地址（同源；票据只用于首访，随后种 cookie 放行静态资源/WS）。

    path 必须带前导斜杠：noVNC 按页面 URL 相对解析该参数，不带斜杠会拼出
    /ti/login-view/{p}/ti/login-view/{p}/websockify 的双重路径。
    """
    return (f"/ti/login-view/{platform}/vnc.html?t={entry['ticket']}"
            f"&autoconnect=true&resize=scale"
            f"&path=/ti/login-view/{platform}/websockify")


@router.post("/sessions/{platform}/manual-login", status_code=202)
async def ti_manual_login(platform: str, request: Request):
    """打开平台人工登录窗口，由人工完成一次登录（滑块/扫码/验证码）。

    窗口形态：本机有显示器 → 本机弹窗；无显示器（线上容器）→ 网页远程桌面
    （Xvfb + noVNC，响应带 viewer_url，前端弹窗内操作）。
    子进程跑 recon.manual_login：检测到登录成功会写 storage_state 种子并以 0 退出；
    watcher 随后调 manager.reset_session 接管会话（清旧 profile + 状态置 ok），
    前端轮询 /ti/sessions 可见红灯变绿。已在进行的登录返回 409（vnc 模式附带
    viewer_url 供重新接入）。
    """
    await require_admin(request)
    manager, cfg = _require_ready()
    if platform not in PLATFORMS:
        raise HTTPException(status_code=404, detail=f"未知平台: {platform}")

    proc = _LOGIN_PROCS.get(platform)
    if proc is not None and proc.poll() is None:
        content = {"detail": "登录窗口已打开，请到弹出的窗口中完成登录"}
        entry = _LOGIN_VIEWERS.get(platform)
        if entry and time.time() < entry["expires"]:
            content["viewer_url"] = _viewer_url(platform, entry)
        return JSONResponse(status_code=409, content=content)

    # 生效代理在父进程算好（子进程无 DB 连接），经 env 透传给人工登录窗口
    cfg = apply_settings_overrides(cfg)
    child_env = {**os.environ,
                 "TI_EFFECTIVE_PROXY": effective_proxy(cfg, platform),
                 "TI_LOGIN_VIEWER": cfg.login_viewer}

    root = Path(__file__).resolve().parents[2]
    log_path = root / "ti_data" / "artifacts" / f"manual_login_{platform}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    status_path = root / "ti_data" / "artifacts" / f"manual_login_{platform}.json"
    status_path.unlink(missing_ok=True)  # 清残留，防读到上一轮的状态
    log_fp = open(log_path, "ab", buffering=0)
    proc = subprocess.Popen(
        [sys.executable, "-m", "api.ti_expand.recon.manual_login", platform],
        cwd=str(root), stdout=log_fp, stderr=subprocess.STDOUT,
        start_new_session=True, env=child_env,
    )
    log_fp.close()  # 子进程已持有副本，父进程不再写
    _LOGIN_PROCS[platform] = proc
    asyncio.get_running_loop().create_task(_watch_manual_login(manager, platform, proc))
    logger.info("[ti %s] 人工登录窗口已打开 (pid=%s)，等待人工完成登录", platform, proc.pid)

    # 等子进程上报窗口形态（local 本机弹窗 / vnc 网页远程桌面），最长 15s
    mode, ws_port = "local", None
    for _ in range(30):
        if proc.poll() is not None:
            break  # 启动即失败（详见 manual_login_{platform}.log）
        try:
            st = json.loads(status_path.read_text(encoding="utf-8"))
            if st.get("pid") == proc.pid:
                mode = st.get("mode") or "local"
                ws_port = st.get("ws_port")
                break
        except Exception:  # noqa: BLE001 — 文件未就绪/写一半，继续等
            pass
        await asyncio.sleep(0.5)

    if proc.poll() is not None:
        raise HTTPException(
            status_code=500,
            detail=f"登录窗口启动失败（退出码 {proc.returncode}），"
                   f"详见 ti_data/artifacts/manual_login_{platform}.log")

    resp = {"platform": platform, "status": "launched", "pid": proc.pid, "mode": mode}
    if mode == "vnc" and ws_port:
        entry = {"ws_port": ws_port, "ticket": secrets.token_urlsafe(24),
                 "expires": time.time() + _VIEWER_TTL_S}
        _LOGIN_VIEWERS[platform] = entry
        resp["viewer_url"] = _viewer_url(platform, entry)
        resp["message"] = "远程登录桌面已就绪，在弹出的网页窗口中完成登录"
    else:
        resp["message"] = "登录窗口已在服务器本机打开，完成后会话状态自动恢复"
    return resp


async def _watch_manual_login(manager: TiQueueManager, platform: str,
                              proc: subprocess.Popen) -> None:
    """等 manual_login 子进程退出；成功（0）则接管会话。"""
    rc = await asyncio.to_thread(proc.wait)
    _LOGIN_PROCS.pop(platform, None)
    _LOGIN_VIEWERS.pop(platform, None)
    if rc == 0:
        try:
            await manager.reset_session(platform)
        except Exception as exc:  # noqa: BLE001
            logger.error("[ti %s] 人工登录后接管会话失败: %s", platform, exc)
    else:
        logger.info("[ti %s] 人工登录未完成（退出码 %s），会话状态不变", platform, rc)


# ─── 远程登录桌面（noVNC 静态页 + websockify 反代，一次性票据鉴权）─────────────

# noVNC web 根目录候选（Debian/Ubuntu 包安装位置；可用 NOVNC_ROOT 覆盖）
_NOVNC_ROOTS = (
    os.environ.get("NOVNC_ROOT", ""),
    "/usr/share/novnc",
    "/usr/local/share/novnc",
    "/opt/homebrew/share/novnc",
)


def _novnc_root() -> Optional[Path]:
    for cand in _NOVNC_ROOTS:
        if cand and Path(cand).is_dir():
            return Path(cand)
    return None


def _check_viewer_ticket(platform: str, ticket: str) -> Optional[Dict]:
    entry = _LOGIN_VIEWERS.get(platform)
    if not entry or time.time() > entry["expires"]:
        return None
    if ticket and secrets.compare_digest(str(ticket), entry["ticket"]):
        return entry
    return None


@router.get("/login-view/{platform}", include_in_schema=False)
@router.get("/login-view/{platform}/{file_path:path}", include_in_schema=False)
async def ti_login_view(request: Request, platform: str, file_path: str = "vnc.html"):
    """noVNC 远程登录桌面静态页。

    鉴权：首访 vnc.html 凭 ?t= 一次性票据（admin 发起 manual-login 时签发）→
    响应种 path 限定 cookie；后续静态资源与 websockify 凭 cookie 放行。
    票据只在内存、20 分钟过期、随登录子进程退出作废。
    """
    _require_ready()
    ticket = request.query_params.get("t") or request.cookies.get(f"lv_{platform}") or ""
    entry = _check_viewer_ticket(platform, ticket)
    if entry is None:
        raise HTTPException(status_code=403,
                            detail="远程桌面票据无效或已过期（登录已完成/超时），请重新发起")
    root = _novnc_root()
    if root is None:
        raise HTTPException(status_code=503,
                            detail="noVNC 未安装（Debian/Ubuntu: apt-get install novnc）")
    target = (root / (file_path or "vnc.html")).resolve()
    if not target.is_relative_to(root.resolve()):
        raise HTTPException(status_code=403, detail="非法路径")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    resp = FileResponse(target)
    if request.query_params.get("t") and target.name == "vnc.html":
        resp.set_cookie(f"lv_{platform}", entry["ticket"],
                        path=f"/ti/login-view/{platform}", samesite="lax",
                        max_age=_VIEWER_TTL_S)
    return resp


@router.websocket("/login-view/{platform}/websockify")
async def ti_login_view_ws(websocket: WebSocket, platform: str):
    """noVNC 画面/输入通道：双向反代到 manual_login 拉起的 websockify（仅 127.0.0.1）。"""
    ticket = websocket.query_params.get("t") or websocket.cookies.get(f"lv_{platform}") or ""
    entry = _check_viewer_ticket(platform, ticket)
    subprotocols = list(websocket.scope.get("subprotocols") or [])
    # 先 accept 再 close：uvicorn sansio 实现对「握手前 close」会抛 AssertionError
    await websocket.accept(subprotocol=subprotocols[0] if subprotocols else None)
    if entry is None:
        await websocket.close(code=4403)
        return

    import websockets as _ws

    try:
        async with _ws.connect(f"ws://127.0.0.1:{entry['ws_port']}",
                               subprotocols=subprotocols or None,
                               max_size=None, ping_interval=None) as upstream:
            async def client_to_upstream():
                while True:
                    msg = await websocket.receive()
                    if msg.get("type") == "websocket.disconnect":
                        break
                    if msg.get("bytes") is not None:
                        await upstream.send(msg["bytes"])
                    elif msg.get("text") is not None:
                        await upstream.send(msg["text"])

            async def upstream_to_client():
                async for frame in upstream:
                    if isinstance(frame, bytes):
                        await websocket.send_bytes(frame)
                    else:
                        await websocket.send_text(frame)

            _, pending = await asyncio.wait(
                (asyncio.create_task(client_to_upstream()),
                 asyncio.create_task(upstream_to_client())),
                return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
    except Exception as exc:  # noqa: BLE001 — 远端掉线/子进程退出，收尾即可
        logger.warning("[ti %s] 远程桌面通道中断: %s", platform, exc)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


@router.post("/sessions/{platform}/import-cookies")
async def ti_import_cookies(platform: str, request: Request):
    """导入运维本机浏览器登录后导出的会话 Cookie（滑块/验证码场景的兜底登录）。

    接受三种粘贴格式：Cookie-Editor JSON 数组 / storage_state JSON / Cookie 请求头文本；
    服务端注入一次性 headless 浏览器做数据面探测，通过才写会话种子并接管（红绿灯变绿），
    不通过返回 400 且不落盘。cookie 即会话凭据，不写日志。
    """
    await require_admin(request)
    manager, cfg = _require_ready()
    if platform not in PLATFORMS:
        raise HTTPException(status_code=404, detail=f"未知平台: {platform}")

    body = await request.json()
    if not isinstance(body, dict) or "cookies" not in body:
        raise HTTPException(status_code=422, detail="请求体必须是含 cookies 字段的 JSON 对象")
    user_agent = str(body.get("user_agent") or "").strip()
    try:
        cookies = parse_cookie_text(body["cookies"],
                                    default_domain=default_cookie_domain(platform))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    cfg = apply_settings_overrides(cfg)
    result = await import_session_cookies(manager, cfg, platform, cookies, user_agent)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["detail"])
    return {"platform": platform, "status": "ok", "cookies": len(cookies),
            "message": "会话导入成功，数据面验证已通过"}


# ─── 设置（管理员）────────────────────────────────────────────────────────────

@router.get("/settings")
async def ti_settings(request: Request):
    """生效配置视图（密码不回显，账号打码）。"""
    await require_admin(request)
    _, cfg = _require_ready()
    return masked_settings_view(apply_settings_overrides(cfg))


@router.put("/settings")
async def ti_settings_update(request: Request):
    """更新可覆盖配置项（写 app_settings，后续任务即生效）。

    可更新：proxy_enabled / proxy_url / proxy_url_<platform> / daily_limit /
    cache_enabled / cache_ttl / credentials.<platform>.{username,password}
    """
    await require_admin(request)
    from ..db import set_setting

    _, cfg = _require_ready()
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="请求体必须是 JSON 对象")

    updated = []
    if "proxy_enabled" in body:
        set_setting(SETTING_PROXY_ENABLED,
                    "true" if body["proxy_enabled"] else "false")
        updated.append("proxy_enabled")
    if "proxy_url" in body:
        set_setting(SETTING_PROXY_URL, str(body["proxy_url"] or "").strip())
        updated.append("proxy_url")
    if "daily_limit" in body:
        try:
            v = int(body["daily_limit"])
            if v < 0:
                raise ValueError
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="daily_limit 必须是非负整数")
        set_setting(SETTING_DAILY_LIMIT, str(v))
        updated.append("daily_limit")
    if "cache_enabled" in body:
        set_setting(SETTING_CACHE_ENABLED,
                    "true" if body["cache_enabled"] else "false")
        updated.append("cache_enabled")
    if "cache_ttl" in body:
        try:
            v = int(body["cache_ttl"])
            if not 0 <= v <= 3_153_600_000:
                raise ValueError
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="cache_ttl 必须是 0~3153600000 的整数（秒，即 0~876000 小时/100 年）")
        set_setting(SETTING_CACHE_TTL, str(v))
        updated.append("cache_ttl")

    for p in PLATFORMS:
        proxy_key = f"proxy_url_{p}"
        if proxy_key in body:
            set_setting(platform_proxy_key(p), str(body[proxy_key] or "").strip())
            updated.append(proxy_key)

    creds = body.get("credentials") or {}
    if not isinstance(creds, dict):
        raise HTTPException(status_code=422, detail="credentials 必须是对象")
    for p, pair in creds.items():
        if p not in PLATFORMS:
            raise HTTPException(status_code=422, detail=f"未知平台: {p}")
        if not isinstance(pair, dict):
            continue
        ukey, pkey = platform_credential_keys(p)
        if pair.get("username"):
            set_setting(ukey, str(pair["username"]).strip())
            updated.append(f"credentials.{p}.username")
        if pair.get("password"):
            set_setting(pkey, str(pair["password"]))
            updated.append(f"credentials.{p}.password")

    logger.info("TI 设置已更新: %s", updated)
    return {"updated": updated, "settings": masked_settings_view(apply_settings_overrides(cfg))}


# ─── 代理连通性自检（管理员）─────────────────────────────────────────────────

# 探测目标用平台数据面首页（非 SSO 地址）：仅验证出网连通性，状态码即证据
_PLATFORM_HOME = {
    "threatbook": "https://x.threatbook.com/",
    "qianxin": "https://ti.qianxin.com/portal",
}


async def _probe_via_proxy(platform: str, proxy_url: str) -> Dict:
    import time as _time

    t0 = _time.perf_counter()
    item = {"platform": platform, "name": PLATFORM_NAMES.get(platform, platform),
            "proxy": sanitize_proxy(proxy_url) or "直连",
            "http_status": None, "latency_ms": None, "error": None}
    try:
        async with httpx.AsyncClient(
            proxy=proxy_url or None, timeout=10.0, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                                   "Chrome/126.0.0.0 Safari/537.36"},
        ) as client:
            resp = await client.get(_PLATFORM_HOME[platform])
            item["http_status"] = resp.status_code
    except Exception as exc:  # noqa: BLE001 — 自检要回报一切异常（代理不可达/证书/DNS）
        item["error"] = f"{type(exc).__name__}: {exc}"
        if "407" in item["error"]:
            item["error"] += "（代理要求认证：地址需含 user:pass@，例 http://user:pass@host:port）"
    item["latency_ms"] = round((_time.perf_counter() - t0) * 1000)
    return item


@router.post("/settings/proxy-test")
async def ti_proxy_test(request: Request):
    """代理连通性自检：不起浏览器，httpx 经代理访问两平台首页。

    请求体可选 {"proxy_url": "..."}——给出则测该（未保存的表单）地址；
    否则按当前生效配置逐平台解析（平台级 > 全局 > 直连）。
    返回每平台 {proxy, http_status, latency_ms, error}。
    """
    await require_admin(request)
    _, cfg = _require_ready()
    cfg = apply_settings_overrides(cfg)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — 空 body 合法
        body = {}
    override = str((body or {}).get("proxy_url") or "").strip()

    items = []
    for p in PLATFORMS:
        items.append(await _probe_via_proxy(p, override or effective_proxy(cfg, p)))
    return {"items": items}
