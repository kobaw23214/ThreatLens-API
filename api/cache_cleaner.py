"""
后台缓存清理任务。

定时物理删除已过期的缓存任务行,避免数据库无限膨胀:
  - 提取缓存(tasks + task_results)
  - 联网解析缓存(ti_tasks,ti_task_results 级联删除)
仅删除 cache_expires_at 非 NULL 且已过期的任务,不影响历史任务和未启用复用的任务。

通过 .env 配置:
    CACHE_CLEANUP_ENABLED   是否启用后台清理(true/false),默认 true
    CACHE_CLEANUP_INTERVAL  执行间隔(秒),默认 3600(1 小时)
"""
from __future__ import annotations

import asyncio
import logging

from . import db

logger = logging.getLogger(__name__)


class CacheCleaner:
    """周期性删除过期缓存任务行的后台协程。"""

    def __init__(self, interval_seconds: int = 3600):
        # 至少 60 秒,避免误配成 0 导致空转
        self.interval = max(60, int(interval_seconds))
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run())
        logger.info("缓存清理任务已启动,间隔 %d 秒", self.interval)

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            try:
                # 等待一个周期,期间若收到停止信号则提前退出
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
                break  # 收到停止信号
            except asyncio.TimeoutError:
                pass  # 正常到点,执行清理

            try:
                # db 调用是同步的(psycopg2),放到线程池避免阻塞事件循环
                deleted = await loop.run_in_executor(None, db.delete_expired_cache_tasks)
                # TI 联网解析缓存同一语义：过期任务行物理删除（结果级联）
                from .ti_expand import store as ti_store
                deleted_ti = await loop.run_in_executor(
                    None, ti_store.delete_expired_ti_cache_tasks)
                if deleted or deleted_ti:
                    logger.info("缓存清理:已删除 %d 条过期任务（联网解析 %d 条）",
                                deleted, deleted_ti)
                else:
                    logger.debug("缓存清理:无过期任务")
            except Exception as exc:  # noqa: BLE001
                logger.error("缓存清理执行失败: %s", exc)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            finally:
                self._task = None
        logger.info("缓存清理任务已停止")
