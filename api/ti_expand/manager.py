"""TI 联网情报任务队列管理器。

与 NER QueueManager 的差异：
  - 浏览器采集是 IO 密集型且 Playwright 为 async 原生 → worker 用协程而非线程池
  - 每平台一个独立队列 + 一个串行 worker（反爬：单平台串行 + 随机间隔 + 日上限）
  - 采集前刷新 app_settings 配置覆盖（代理/日上限/白名单改动即生效）
"""
from __future__ import annotations

import asyncio
import logging
import random
import shutil
import time
from pathlib import Path
from typing import Dict, Optional

from . import store
from .adapters import build_adapter
from .adapters.base import BaseTiAdapter, TiAdapterError
from .config import (
    PLATFORMS,
    TiConfig,
    apply_settings_overrides,
    effective_proxy,
    sanitize_proxy,
)

logger = logging.getLogger(__name__)

# 会话类失败原因 → ti_session.login_state 的映射
_SESSION_FAIL_STATES = {
    "session_expired": "expired",
    "auth_failed": "auth_failed",
    "need_manual": "need_manual",
}


class TiQueueManager:
    def __init__(self, cfg: TiConfig, callback_manager=None) -> None:
        self._cfg = cfg
        self._callback_manager = callback_manager
        self._queues: Dict[str, asyncio.Queue] = {
            p: asyncio.Queue(maxsize=cfg.queue_size) for p in PLATFORMS
        }
        self._worker_status: Dict[str, Dict] = {
            p: {"platform": p, "busy": False, "task_id": None} for p in PLATFORMS
        }
        self._adapters: Dict[str, BaseTiAdapter] = {}
        self._workers: list = []
        self._last_activity: Dict[str, float] = {}  # 平台 → 最近一次任务/保活时间
        self._status_hook = None  # 状态变化回调（SSE 广播用，async callable）

    def set_status_hook(self, hook) -> None:
        """注册状态变化钩子（如 QueueManager.broadcast_status），
        队列深度 / worker 忙闲变化时触发，驱动前端队列页实时刷新。"""
        self._status_hook = hook

    async def _notify_status(self) -> None:
        if self._status_hook is None:
            return
        try:
            await self._status_hook()
        except Exception:  # noqa: BLE001 — 广播失败不得影响采集 worker
            pass

    async def start(self) -> None:
        now = time.monotonic()
        for p in PLATFORMS:
            self._last_activity[p] = now
            self._workers.append(asyncio.create_task(self._worker(p)))
        logger.info("TI 联网情报队列已启动（平台: %s）", ",".join(PLATFORMS))

    def can_accept(self, platform: str) -> bool:
        q = self._queues.get(platform)
        return q is not None and not q.full()

    async def submit(self, task: Dict) -> bool:
        """task: {task_id, seed_type, seed_value, platform, callback_config}"""
        q = self._queues.get(task["platform"])
        if q is None or q.full():
            return False
        await q.put(task)
        await self._notify_status()
        return True

    def get_status(self) -> Dict:
        return {
            "platforms": {
                p: {
                    "queue_size": q.qsize(),
                    "queue_max": q.maxsize,
                    **self._worker_status[p],
                }
                for p, q in self._queues.items()
            },
        }

    async def reset_session(self, platform: str) -> None:
        """人工登录成功后的会话接管（POST /ti/sessions/{platform}/manual-login 链路）。

        旧 persistent profile 里的 cookie 就是失效源：关掉旧浏览器释放 profile 锁，
        清空 profile 目录（保留 manual_login 写入的 storage_state.json 种子），
        下次任务/保活启动时按空 profile 重新注入种子；会话状态直接置 ok。
        """
        adapter = self._adapters.pop(platform, None)
        if adapter is not None:
            try:
                await adapter.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[ti %s] 重置会话关闭浏览器异常: %s", platform, exc)
        session_dir = Path(self._cfg.data_dir) / "sessions" / platform
        if session_dir.exists():
            for child in session_dir.iterdir():
                if child.name in ("storage_state.json", "imported_ua.txt"):
                    continue
                try:
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink(missing_ok=True)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[ti %s] 清理 profile 失败 %s: %s", platform, child, exc)
        cfg = await asyncio.to_thread(apply_settings_overrides, self._cfg)
        await asyncio.to_thread(
            store.upsert_session_state, platform, "ok", None,
            sanitize_proxy(effective_proxy(cfg, platform)), logged_in=True)
        logger.info("[ti %s] 人工登录完成，旧 profile 已清理，会话标记有效", platform)

    async def shutdown(self) -> None:
        for w in self._workers:
            w.cancel()
        for adapter in self._adapters.values():
            try:
                await adapter.close()
            except Exception:
                pass
        self._workers.clear()

    # ─── 内部 ─────────────────────────────────────────────────────────────────

    def _get_adapter(self, platform: str) -> BaseTiAdapter:
        if platform not in self._adapters:
            self._adapters[platform] = build_adapter(self._cfg, platform)
        return self._adapters[platform]

    def _refresh_proxy(self, platform: str, cfg: TiConfig) -> None:
        """把最新生效代理推入已建适配器（管理页改代理后无需重启服务）。

        BrowserSession 构造时拷贝的是当时的 proxy_url；这里按任务/保活前
        刷新，值变时 ensure_context 的 _proxy_fingerprint 机制自动重建
        context（persistent profile 不变，会话保留），值不变零开销。
        """
        adapter = self._adapters.get(platform)
        browser = getattr(adapter, "browser", None)
        if browser is not None:
            browser.proxy_url = effective_proxy(cfg, platform)

    async def _worker(self, platform: str) -> None:
        q = self._queues[platform]
        while True:
            timeout = self._keepalive_in(platform)
            try:
                if timeout is None:
                    task = await q.get()
                else:
                    task = await asyncio.wait_for(q.get(), timeout)
            except asyncio.TimeoutError:
                await self._keepalive(platform)  # 空闲超时：会话保活
                continue
            self._worker_status[platform].update({"busy": True, "task_id": task["task_id"]})
            await self._notify_status()
            try:
                await self._run_task(platform, task)
            except Exception as exc:  # noqa: BLE001 — worker 不得退出
                logger.exception("[ti %s] 任务 %s 处理异常: %s", platform, task["task_id"], exc)
            finally:
                self._worker_status[platform].update({"busy": False, "task_id": None})
                self._last_activity[platform] = time.monotonic()
                q.task_done()
                await self._notify_status()

    def _keepalive_in(self, platform: str) -> Optional[float]:
        """距下次保活的秒数；保活关闭返回 None。"""
        interval = getattr(self._cfg, "session_keepalive_s", 0)
        if interval <= 0:
            return None
        last = self._last_activity.get(platform, time.monotonic())
        return max(1.0, interval - (time.monotonic() - last))

    async def _keepalive(self, platform: str) -> None:
        """空闲保活：刷新平台会话有效期；失效则尝试自动重登（含滑块求解）。
        不计日上限（仅访问探测页，非业务查询），失败记会话状态，下周期再试。"""
        self._last_activity[platform] = time.monotonic()
        # 合并设置覆盖 + 刷新代理（与 _run_task 同一口径，代理改动即时生效）
        cfg = await asyncio.to_thread(apply_settings_overrides, self._cfg)
        self._refresh_proxy(platform, cfg)
        try:
            await self._get_adapter(platform).keepalive()
        except TiAdapterError as exc:
            if exc.reason in _SESSION_FAIL_STATES:
                await asyncio.to_thread(
                    store.upsert_session_state, platform,
                    _SESSION_FAIL_STATES[exc.reason], str(exc))
            logger.info("[ti %s] 会话保活未通过(%s): %s", platform, exc.reason, exc)
        except Exception as exc:  # noqa: BLE001 — worker 不得退出
            logger.warning("[ti %s] 会话保活异常: %s", platform, exc)
        else:
            await asyncio.to_thread(
                store.upsert_session_state, platform, "ok", None,
                sanitize_proxy(effective_proxy(cfg, platform)))
            logger.info("[ti %s] 会话保活完成", platform)

    async def _run_task(self, platform: str, task: Dict) -> None:
        task_id = task["task_id"]
        # 每次执行前合并 app_settings 覆盖（代理/日上限改动即生效）
        cfg = await asyncio.to_thread(apply_settings_overrides, self._cfg)
        self._refresh_proxy(platform, cfg)  # 代理改动即时推入已建适配器

        await asyncio.to_thread(store.update_ti_task_running, task_id)

        # 日上限（成功+失败都计数，防失控重试打爆平台）
        today_count = await asyncio.to_thread(store.get_today_call_count, platform)
        if today_count >= cfg.daily_limit:
            await self._fail(task, cfg, f"平台 {platform} 已达日调用上限 {cfg.daily_limit}")
            return

        # 反爬：同平台任务间隔（首个任务也等待，避免启动瞬间突发）
        interval = random.uniform(cfg.min_interval, cfg.max_interval)
        if interval > 0:
            await asyncio.sleep(interval)

        adapter = self._get_adapter(platform)
        t0 = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                adapter.query(task["seed_type"], task["seed_value"]),
                timeout=cfg.task_timeout,
            )
        except TiAdapterError as exc:
            if exc.reason in _SESSION_FAIL_STATES:
                await asyncio.to_thread(
                    store.upsert_session_state, platform,
                    _SESSION_FAIL_STATES[exc.reason], str(exc),
                )
            await asyncio.to_thread(store.record_call, platform, adapter.channel, False)
            await self._fail(task, cfg, str(exc), reason=exc.reason)
            return
        except asyncio.TimeoutError:
            await asyncio.to_thread(store.record_call, platform, adapter.channel, False)
            await self._fail(task, cfg, f"采集超时（>{cfg.task_timeout}s）", reason="timeout")
            return
        except Exception as exc:  # noqa: BLE001
            await asyncio.to_thread(store.record_call, platform, adapter.channel, False)
            await self._fail(task, cfg, f"采集异常: {exc}", reason="query_failed")
            return

        latency_ms = (time.perf_counter() - t0) * 1000
        try:
            # 缓存总开关关闭时不进入缓存复用窗口（cache_expires_at 写 NULL）
            await asyncio.to_thread(
                store.update_ti_task_done,
                task_id,
                result.get("sections", []),
                result.get("raw"),
                result.get("diagnostics"),
                latency_ms,
                cfg.cache_ttl if cfg.cache_enabled else None,
            )
        except Exception as exc:  # noqa: BLE001
            await self._fail(task, cfg, f"写入任务结果失败: {exc}", reason="db_error")
            return

        await asyncio.to_thread(store.record_call, platform, adapter.channel, True)
        # 查询成功即证明会话有效，刷新会话状态（前端红绿灯数据源）
        await asyncio.to_thread(
            store.upsert_session_state, platform, "ok", None,
            sanitize_proxy(effective_proxy(cfg, platform)))
        self._trigger_callback(task, cfg, status=1, result=result, latency_ms=latency_ms)

    async def _fail(self, task: Dict, cfg: TiConfig, error_msg: str,
                    reason: str = "query_failed") -> None:
        logger.warning("[ti %s] 任务 %s 失败(%s): %s",
                       task["platform"], task["task_id"], reason, error_msg)
        await asyncio.to_thread(store.update_ti_task_failed, task["task_id"], error_msg)
        self._trigger_callback(task, cfg, status=-1, error_message=error_msg)

    # ─── 回调（复用全局 CallbackManager：白名单/失败落库/重试）─────────────────

    def _trigger_callback(
        self,
        task: Dict,
        cfg: TiConfig,
        status: int,
        result: Optional[Dict] = None,
        latency_ms: Optional[float] = None,
        error_message: Optional[str] = None,
    ) -> None:
        callback_config = task.get("callback_config")
        if not callback_config or not self._callback_manager:
            return
        try:
            record = store.get_ti_task(task["task_id"]) or {}
            # raw（平台原始 JSON）可能很大，不进回调体，由接收方按需调 GET /ti/tasks/{id} 拉取
            payload = {
                "status": status,
                "msg": "任务执行成功" if status == 1 else f"任务执行失败: {error_message}",
                "task_id": task["task_id"],
                "platform": task["platform"],
                "seed_type": task["seed_type"],
                "seed_value": task["seed_value"],
                "result": {
                    "sections": result.get("sections", []),
                } if result else None,
                "diagnostics": result.get("diagnostics") if result else None,
                "error_message": error_message,
                "created_at": record.get("created_at"),
                "finished_at": record.get("finished_at"),
                "latency_ms": latency_ms,
                "information_json": record.get("information_json") or {},
            }
            self._callback_manager.enqueue_callback(task["task_id"], callback_config, payload)
        except Exception as exc:  # noqa: BLE001
            logger.error("触发 TI 回调失败: %s", exc)
