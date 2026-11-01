"""
任务队列 + 线程池管理
- asyncio.Queue 接收任务
- ThreadPoolExecutor 执行 CPU 密集型模型推理
- SSE 广播：每次队列/worker 状态变化推送给所有已连接客户端
- 回调管理：任务完成后触发回调

long-text-chunking-ner 改造(任务 5):
  - enqueue 新增 chunk_cfg 参数(Dict 序列化自 ChunkConfig)
  - _process_task 调用 model.predict_with_chunks 获取 (entities, chunk_metadata)
  - label_mapping 归一化后,调 dedup_by_overlap 做跨块去重,再按 (start,end,label) 排序
  - 捕获 ChunkingCapacityError → 任务 failed,回调 status=-1
  - 捕获"所有 chunk 都 failed" → 任务 failed
  - 单块失败仍作 done,chunk_metadata.chunks[k].status 反映细节
  - 回调 payload 新增 chunk_metadata 字段,其他字段保持不变
"""
from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from .chunking import ChunkConfig, ChunkingCapacityError, ChunkingError
from .marking import build_marked_payload

logger = logging.getLogger(__name__)


# ─── 跨块去重 (Requirement 5) ───────────────────────────────────────────────

def dedup_by_overlap(entities: List[Dict]) -> List[Dict]:
    """
    对来自多个 Chunk 的实体列表做去重。

    冲突判定 (Requirement 5.2):
      两个实体的 raw_label 相同 且 [start, end) 区间存在非空交集,
      即 max(a.start, b.start) < min(a.end, b.end)。

    保留规则 (Requirement 5.3 / 5.4 / 5.5 / 5.6):
      依次按 (score 降序, chunk_index 升序, (end-start) 降序, 原有顺序升序) 比较胜出者。

    多方冲突处理:
      当 cur 同时与 kept 里多个实体冲突时,把 cur + 所有冲突项放在一起做 N 选 1,
      再从 kept 中移除所有冲突项,追加胜者。这保证算法对输入顺序健壮,
      dedup(dedup(xs)) 与 dedup(xs) 完全一致 (同元素、同顺序)。

    线程安全: 纯函数,不修改入参。

    Args:
        entities: 已完成 offset 回写(start/end 为全局偏移)、已应用 label_mapping 的实体列表。
                  每项必须至少含字段: raw_label, start, end, score, chunk_index。
                  输入列表的顺序决定 "原有顺序" tiebreaker(索引越小优先级越高)。

    Returns:
        去重后的实体列表。
    """
    if not entities:
        return []

    # kept 每项: (entity_dict, original_index)。original_index 用于 tiebreaker。
    kept: List[tuple] = []

    for cur_idx, cur in enumerate(entities):
        cur_raw = cur.get("raw_label")
        # 找 kept 中所有与 cur 冲突的位置
        conflict_positions = []
        if cur_raw is not None:
            cur_s, cur_e = cur["start"], cur["end"]
            for i, (k, _) in enumerate(kept):
                k_raw = k.get("raw_label")
                if k_raw is None or k_raw != cur_raw:
                    continue
                if max(cur_s, k["start"]) < min(cur_e, k["end"]):
                    conflict_positions.append(i)

        if not conflict_positions:
            kept.append((cur, cur_idx))
            continue

        # 多方冲突: 把 cur 与所有冲突的 kept 项集中 N 选 1
        candidates = [(cur, cur_idx)] + [kept[i] for i in conflict_positions]
        winner, winner_idx = candidates[0]
        for cand_entity, cand_orig_idx in candidates[1:]:
            picked = _pick_winner(winner, winner_idx, cand_entity, cand_orig_idx)
            if picked is cand_entity:
                winner, winner_idx = cand_entity, cand_orig_idx

        # 从后向前 pop 避免索引失效
        for i in reversed(conflict_positions):
            kept.pop(i)
        kept.append((winner, winner_idx))

    return [pair[0] for pair in kept]


def _pick_winner(a: Dict, a_idx: int, b: Dict, b_idx: int) -> Dict:
    """按 Dedup_Rule 四级比较规则选胜者: score↓ → chunk_index↑ → length↓ → original_index↑"""
    # 1. score 严格不等 → 高分胜 (Req 5.3)
    if a["score"] != b["score"]:
        return a if a["score"] > b["score"] else b

    # 2. chunk_index 不等 → 更小的胜 (Req 5.4)
    #    chunk_index 可能为 None (未分块的旧数据),None 视为最大
    a_ci = a.get("chunk_index")
    b_ci = b.get("chunk_index")
    if a_ci != b_ci:
        if a_ci is None:
            return b
        if b_ci is None:
            return a
        return a if a_ci < b_ci else b

    # 3. 区间长度不等 → 更长的胜 (Req 5.5)
    a_len = a["end"] - a["start"]
    b_len = b["end"] - b["start"]
    if a_len != b_len:
        return a if a_len > b_len else b

    # 4. 原有顺序靠前的胜 (Req 5.5 后半)
    return a if a_idx <= b_idx else b


# ─── 队列管理 ────────────────────────────────────────────────────────────────

class QueueManager:
    def __init__(self, max_queue_size: int, worker_threads: int) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue_size)
        self.executor = ThreadPoolExecutor(
            max_workers=worker_threads,
            thread_name_prefix="ner_worker",
        )
        self._worker_threads = worker_threads
        self._models: Dict[str, Any] = {}
        self._worker_status: Dict[int, Dict] = {
            i: {"worker_id": i, "busy": False, "model": None, "task_id": None}
            for i in range(worker_threads)
        }
        self._sse_clients: Set[asyncio.Queue] = set()
        self._callback_manager: Optional[Any] = None  # 回调管理器

    def set_callback_manager(self, callback_manager: Any) -> None:
        """设置回调管理器"""
        self._callback_manager = callback_manager

    async def start(self, models: Dict[str, Any]) -> None:
        self._models = models
        for i in range(self._worker_threads):
            asyncio.create_task(self._worker(i))

    async def enqueue(
        self,
        task_id: str,
        text: str,
        labels: List[str],
        model_name: Optional[str] = None,
        label_mapping: Optional[dict] = None,
        callback_config: Optional[dict] = None,
        chunk_cfg: Optional[dict] = None,
        cache_ttl_seconds: Optional[int] = None,
    ) -> bool:
        """
        将任务加入队列。chunk_cfg 为 ChunkConfig.to_dict() 的结果,
        None 时 worker 会走"不分块"降级路径。
        cache_ttl_seconds 为该任务完成后参与缓存复用的有效期(秒);
        None 表示该任务不参与缓存复用。
        """
        if self.queue.full():
            return False
        if not model_name:
            if not self._models:
                return False
            model_name = next(iter(self._models))
        elif model_name not in self._models:
            return False

        await self.queue.put(
            {
                "task_id":         task_id,
                "model_name":      model_name,
                "text":            text,
                "labels":          labels,
                "label_mapping":   label_mapping or {},
                "callback_config": callback_config,
                "chunk_cfg":       chunk_cfg,  # Dict or None
                "cache_ttl_seconds": cache_ttl_seconds,  # int or None
            }
        )
        await self._broadcast_status()
        return True

    def add_sse_client(self, q: asyncio.Queue) -> None:
        self._sse_clients.add(q)

    def remove_sse_client(self, q: asyncio.Queue) -> None:
        self._sse_clients.discard(q)

    def get_status(self) -> Dict:
        return {
            "queue_size": self.queue.qsize(),
            "workers": list(self._worker_status.values()),
            "timestamp": datetime.now().isoformat(),
        }

    async def broadcast_status(self) -> None:
        """对外公开的状态广播（供 TI 队列等外部模块在自身状态变化时触发 SSE 推送）。"""
        await self._broadcast_status()

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False)

    # ─── 内部 ─────────────────────────────────────────────────────────────────

    async def _broadcast_status(self) -> None:
        status = self.get_status()
        for client_q in list(self._sse_clients):
            try:
                client_q.put_nowait(status)
            except asyncio.QueueFull:
                pass

    async def _worker(self, worker_id: int) -> None:
        loop = asyncio.get_running_loop()
        while True:
            task = await self.queue.get()
            self._worker_status[worker_id].update(
                {"busy": True, "model": task["model_name"], "task_id": task["task_id"]}
            )
            await self._broadcast_status()
            try:
                await loop.run_in_executor(self.executor, self._process_task, task)
            finally:
                self._worker_status[worker_id].update(
                    {"busy": False, "model": None, "task_id": None}
                )
                self.queue.task_done()
                await self._broadcast_status()

    def _process_task(self, task: Dict) -> None:
        """
        在线程池中执行:
          1. 组装 ChunkConfig (从 task['chunk_cfg'] dict 还原,缺失字段用默认值)
          2. 调 model.predict_with_chunks(text, labels, chunk_cfg)
             → (entities[], chunk_metadata_dict)
          3. 应用 label_mapping 保留 raw_label
          4. dedup_by_overlap 跨块去重
          5. 按 (start, end, label) 升序排序
          6. 若所有 chunk 都 failed,转任务 failed;否则 update_task_done + 触发回调
        """
        from .db import update_task_running, update_task_done, update_task_failed

        task_id         = task["task_id"]
        model_name      = task["model_name"]
        label_mapping   = task.get("label_mapping") or {}
        callback_config = task.get("callback_config")
        chunk_cfg_dict  = task.get("chunk_cfg") or {}
        cache_ttl_seconds = task.get("cache_ttl_seconds")
        chunk_cfg = ChunkConfig(
            enabled=chunk_cfg_dict.get("enabled", True),
            overlap=chunk_cfg_dict.get("overlap", 32),
            safety_margin=chunk_cfg_dict.get("safety_margin", 16),
            threshold=chunk_cfg_dict.get("threshold", 0.5),
        )

        update_task_running(task_id)

        model = self._models.get(model_name)
        if not model:
            update_task_failed(task_id, f"模型 {model_name} 未加载")
            if callback_config and self._callback_manager:
                self._trigger_callback_failed(
                    task_id, callback_config,
                    f"模型 {model_name} 未加载", model_name, chunk_metadata=None,
                )
            return

        # ── 推理 ────────────────────────────────────────────────────────
        try:
            t0 = time.perf_counter()
            entities, chunk_metadata = model.predict_with_chunks(
                task["text"], task["labels"], chunk_cfg,
            )
            latency_ms = (time.perf_counter() - t0) * 1000
        except ChunkingCapacityError as exc:
            msg = f"模型容量配置异常: {exc}"
            logger.error(f"[task {task_id}] {msg}")
            update_task_failed(task_id, msg)
            if callback_config and self._callback_manager:
                self._trigger_callback_failed(
                    task_id, callback_config, msg, model_name, chunk_metadata=None,
                )
            return
        except ChunkingError as exc:
            msg = f"分块失败: {exc}"
            logger.error(f"[task {task_id}] {msg}")
            update_task_failed(task_id, msg)
            if callback_config and self._callback_manager:
                self._trigger_callback_failed(
                    task_id, callback_config, msg, model_name, chunk_metadata=None,
                )
            return
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            logger.error(f"[task {task_id}] 推理异常: {msg}")
            update_task_failed(task_id, msg)
            if callback_config and self._callback_manager:
                self._trigger_callback_failed(
                    task_id, callback_config, msg, model_name, chunk_metadata=None,
                )
            return

        # ── 所有 chunk 都失败 → 任务 failed (Requirement 4.5 推论) ──────
        chunks_meta = chunk_metadata.get("chunks", [])
        if chunks_meta and all(c.get("status") == "failed" for c in chunks_meta):
            msg = "所有分块推理均失败"
            logger.error(f"[task {task_id}] {msg}: chunk_count={len(chunks_meta)}")
            update_task_failed(task_id, msg)
            if callback_config and self._callback_manager:
                self._trigger_callback_failed(
                    task_id, callback_config, msg, model_name, chunk_metadata=chunk_metadata,
                )
            return

        # ── 注入 model_name（auto 模式已在子模型层注入;普通模型在此补充）──
        for e in entities:
            e.setdefault("model_name", model_name)

        # ── auto 路由实际子模型（统计口径）：实体携带的子模型名与请求模型不同
        #    即为路由结果；多个子模型逗号拼接。直连模型或无实体产出时为 None。
        routed_set = sorted(
            {e["model_name"] for e in entities if e.get("model_name")} - {model_name}
        )
        routed_model = ",".join(routed_set) if routed_set else None

        # ── label_mapping 归一化 (保留 raw_label) ──────────────────────
        for e in entities:
            raw_label = e.get("label")
            mapped = label_mapping.get(raw_label, raw_label)
            e["raw_label"] = raw_label
            e["label"] = mapped if mapped and str(mapped).strip() else raw_label

        # ── 跨块去重 (Req 5) + 排序 (Req 4.6) ──────────────────────────
        entities = dedup_by_overlap(entities)
        entities.sort(key=lambda x: (x["start"], x["end"], x["label"]))

        # ── 持久化 + 回调 ───────────────────────────────────────────────
        try:
            update_task_done(
                task_id, entities, latency_ms,
                chunk_metadata=chunk_metadata,
                cache_ttl_seconds=cache_ttl_seconds,
                routed_model=routed_model,
            )
        except Exception as exc:  # noqa: BLE001
            msg = f"写入任务结果失败: {exc}"
            logger.error(f"[task {task_id}] {msg}")
            update_task_failed(task_id, msg)
            if callback_config and self._callback_manager:
                self._trigger_callback_failed(
                    task_id, callback_config, msg, model_name, chunk_metadata=chunk_metadata,
                )
            return

        if callback_config and self._callback_manager:
            self._trigger_callback(
                task_id, callback_config, entities,
                model_name, latency_ms, chunk_metadata=chunk_metadata,
            )

    # ─── 回调触发 ─────────────────────────────────────────────────────

    def _trigger_callback(
        self,
        task_id: str,
        callback_config: Dict,
        result: List[Dict],
        model_name: str,
        latency_ms: float,
        chunk_metadata: Optional[Dict] = None,
    ) -> None:
        """触发成功回调。payload 新增 chunk_metadata 字段,其他字段不变 (Req 9.5)。"""
        from .db import get_task

        try:
            task = get_task(task_id)
            if not task:
                return

            # 生成带标记字段:mark_text(整篇) + 每个实体的 marked_context。
            # 不改动 input_text(前端依赖其原始偏移),实时计算不落库。
            input_text = task.get("input_text") or ""
            mark_text, marked_result = build_marked_payload(input_text, result)

            result_data = {
                "status": 1,
                "msg": "任务执行成功",
                "task_id": task_id,
                "model_name": model_name,
                "result": marked_result,
                "error_message": None,
                "created_at": task["created_at"].isoformat() if task.get("created_at") else None,
                "finished_at": task["finished_at"].isoformat() if task.get("finished_at") else None,
                "latency_ms": latency_ms,
                "information_json": task.get("information_json") or {},
                "mark_text": mark_text,
                "chunk_metadata": chunk_metadata,
            }
            self._callback_manager.enqueue_callback(task_id, callback_config, result_data)

        except Exception as e:
            logger.error(f"触发回调失败: {e}")

    def _trigger_callback_failed(
        self,
        task_id: str,
        callback_config: Dict,
        error_message: str,
        model_name: str,
        chunk_metadata: Optional[Dict] = None,
    ) -> None:
        """触发失败回调。chunk_metadata 可能为 None (分块前就异常) 或部分填充。"""
        from .db import get_task

        try:
            task = get_task(task_id)
            if not task:
                return

            result_data = {
                "status": -1,
                "msg": f"任务执行失败: {error_message}",
                "task_id": task_id,
                "model_name": model_name,
                "result": None,
                "error_message": error_message,
                "created_at": task["created_at"].isoformat() if task.get("created_at") else None,
                "finished_at": task["finished_at"].isoformat() if task.get("finished_at") else None,
                "latency_ms": None,
                "information_json": task.get("information_json") or {},
                "mark_text": None,
                "chunk_metadata": chunk_metadata,
            }
            self._callback_manager.enqueue_callback(task_id, callback_config, result_data)

        except Exception as e:
            logger.error(f"触发失败回调失败: {e}")
