"""
回调管理器
- 回调队列 + 回调线程池：发送 HTTP 回调请求
- 失败队列 + 失败处理单线程：将失败记录写入数据库
"""
from __future__ import annotations

import asyncio
import logging
import os
import ipaddress
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from queue import Queue
from threading import Thread
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx
from psycopg2.extras import Json

logger = logging.getLogger(__name__)


class CallbackManager:
    def __init__(
        self,
        callback_worker_threads: int,
        callback_queue_size: int,
        callback_failed_queue_size: int,
        callback_timeout: int,
        allowed_hosts: str,
        block_private_ip: bool,
    ) -> None:
        self.callback_queue: Queue = Queue(maxsize=callback_queue_size)
        self.failed_queue: Queue = Queue(maxsize=callback_failed_queue_size)
        
        self.callback_executor = ThreadPoolExecutor(
            max_workers=callback_worker_threads,
            thread_name_prefix="callback_worker",
        )
        
        self._callback_worker_threads = callback_worker_threads
        self._callback_timeout = callback_timeout
        self._allowed_hosts = allowed_hosts
        self._block_private_ip = block_private_ip
        
        # 回调线程状态
        self._callback_worker_status: Dict[int, Dict] = {
            i: {"worker_id": i, "busy": False, "task_id": None, "url": None}
            for i in range(callback_worker_threads)
        }
        
        # 失败处理线程状态
        self._failed_handler_status = {"busy": False}
        
        # 失败处理线程
        self._failed_handler_thread: Optional[Thread] = None
        self._running = False

    def start(self) -> None:
        """启动回调管理器"""
        self._running = True
        
        # 启动回调工作线程
        for i in range(self._callback_worker_threads):
            thread = Thread(target=self._callback_worker, args=(i,), daemon=True)
            thread.start()
        
        # 启动失败处理线程
        self._failed_handler_thread = Thread(target=self._failed_handler, daemon=True)
        self._failed_handler_thread.start()
        
        logger.info(
            f"回调管理器已启动：{self._callback_worker_threads} 个回调线程，1 个失败处理线程"
        )

    def enqueue_callback(
        self,
        task_id: str,
        callback_config: Dict,
        result_data: Dict,
    ) -> bool:
        """将回调任务加入队列"""
        if self.callback_queue.full():
            logger.warning(f"回调队列已满，任务 {task_id} 回调失败")
            return False
        
        self.callback_queue.put({
            "task_id": task_id,
            "callback_config": callback_config,
            "result_data": result_data,
            "enqueued_at": datetime.now().isoformat(),
        })
        return True

    def retry_failed_callback(
        self,
        failed_log_id: int,
        task_id: str,
        callback_config: Dict,
        result_data: Optional[Dict],
    ) -> bool:
        """
        重试失败的回调：直接发送，成功更新为 resolved，失败更新错误信息。
        不走失败队列，不会产生新记录。
        """
        if self.callback_queue.full():
            logger.warning(f"回调队列已满，重试任务 {task_id} 失败")
            return False

        self.callback_queue.put({
            "task_id": task_id,
            "callback_config": callback_config,
            "result_data": result_data,
            "enqueued_at": datetime.now().isoformat(),
            "_retry_log_id": failed_log_id,  # 标记这是重试，携带原记录 ID
        })
        return True

    def list_pending(self) -> list:
        """导出回调队列中等待发送的条目快照(不消费队列,线程安全)。"""
        with self.callback_queue.mutex:
            items = list(self.callback_queue.queue)
        return [
            {
                "task_id": item["task_id"],
                "url": item["callback_config"].get("url"),
                "method": (item["callback_config"].get("method") or "POST").upper(),
                "enqueued_at": item.get("enqueued_at"),
                "is_retry": bool(item.get("_retry_log_id")),
                "failed_log_id": item.get("_retry_log_id"),
            }
            for item in items
        ]

    def get_status(self) -> Dict:
        """获取回调管理器状态"""
        return {
            "callback_queue": {
                "size": self.callback_queue.qsize(),
                "max_size": self.callback_queue.maxsize,
                "workers": list(self._callback_worker_status.values()),
            },
            "callback_failed_queue": {
                "size": self.failed_queue.qsize(),
                "max_size": self.failed_queue.maxsize,
                "handler_status": self._failed_handler_status["busy"],
            },
        }

    def shutdown(self) -> None:
        """关闭回调管理器"""
        self._running = False
        self.callback_executor.shutdown(wait=False)
        logger.info("回调管理器已关闭")

    # ─── 内部方法 ─────────────────────────────────────────────────────────────

    def _callback_worker(self, worker_id: int) -> None:
        """回调工作线程"""
        while self._running:
            try:
                task = self.callback_queue.get(timeout=1)
            except:
                continue
            
            task_id = task["task_id"]
            callback_config = task["callback_config"]
            result_data = task["result_data"]
            retry_log_id = task.get("_retry_log_id")  # 重试时携带原记录 ID
            
            # 更新状态
            self._callback_worker_status[worker_id].update({
                "busy": True,
                "task_id": task_id,
                "url": callback_config.get("url"),
            })
            
            try:
                if retry_log_id:
                    self._send_retry_callback(retry_log_id, task_id, callback_config, result_data)
                else:
                    self._send_callback(task_id, callback_config, result_data)
            except Exception as e:
                logger.error(f"回调线程 {worker_id} 处理任务 {task_id} 失败: {e}")
            finally:
                self._callback_worker_status[worker_id].update({
                    "busy": False,
                    "task_id": None,
                    "url": None,
                })
                self.callback_queue.task_done()

    def _send_callback(
        self,
        task_id: str,
        callback_config: Dict,
        result_data: Dict,
    ) -> None:
        """发送回调请求"""
        url = callback_config.get("url")
        method = callback_config.get("method", "POST").upper()
        headers = callback_config.get("headers") or {}
        timeout = callback_config.get("timeout", self._callback_timeout)
        
        # 验证 URL
        if not self._validate_url(url):
            error_msg = f"回调 URL 验证失败: {url}"
            logger.warning(error_msg)
            # URL 验证失败时，补充能加的默认头
            headers.setdefault("Content-Type", "application/json")
            headers.setdefault("User-Agent", "NER-API-Callback/1.0")
            headers["X-Task-ID"] = task_id
            headers["X-Callback-Timestamp"] = datetime.now().isoformat()
            self._enqueue_failed(task_id, callback_config, result_data, error_msg, None,
                                 actual_headers=headers, request_body=result_data)
            return
        
        # 添加默认请求头
        headers.setdefault("Content-Type", "application/json")
        headers.setdefault("User-Agent", "NER-API-Callback/1.0")
        headers["X-Task-ID"] = task_id
        headers["X-Callback-Timestamp"] = datetime.now().isoformat()
        
        try:
            start_time = datetime.now()
            with httpx.Client(timeout=timeout) as client:
                response = client.request(
                    method=method,
                    url=url,
                    json=result_data,
                    headers=headers,
                )
                response_time_ms = (datetime.now() - start_time).total_seconds() * 1000
                
                # 检查响应状态
                if 200 <= response.status_code < 300:
                    logger.info(f"任务 {task_id} 回调成功: {url} (HTTP {response.status_code}, {response_time_ms:.2f}ms)")
                    self._update_callback_success(task_id)
                    # 记录成功日志
                    self._log_callback_success(
                        task_id, url, method, response.status_code,
                        response_time_ms, headers, dict(response.headers),
                        response.text[:1000] if response.text else None,
                        request_body=result_data,
                    )
                else:
                    error_msg = f"HTTP {response.status_code}: {response.text[:200]}"
                    logger.warning(f"任务 {task_id} 回调失败: {error_msg}")
                    self._enqueue_failed(
                        task_id, callback_config, result_data, error_msg, response.status_code,
                        actual_headers=headers, request_body=result_data
                    )
        
        except httpx.TimeoutException as e:
            error_msg = f"连接超时 ({timeout}s)"
            logger.warning(f"任务 {task_id} 回调超时: {url}")
            self._enqueue_failed(task_id, callback_config, result_data, error_msg, None,
                                 actual_headers=headers, request_body=result_data)
        
        except httpx.ConnectError as e:
            error_msg = f"连接失败: {str(e)}"
            logger.warning(f"任务 {task_id} 回调连接失败: {url}")
            self._enqueue_failed(task_id, callback_config, result_data, error_msg, None,
                                 actual_headers=headers, request_body=result_data)
        
        except Exception as e:
            error_msg = f"未知错误: {str(e)}"
            logger.error(f"任务 {task_id} 回调异常: {e}")
            self._enqueue_failed(task_id, callback_config, result_data, error_msg, None,
                                 actual_headers=headers, request_body=result_data)

    def _send_retry_callback(
        self,
        failed_log_id: int,
        task_id: str,
        callback_config: Dict,
        result_data: Optional[Dict],
    ) -> None:
        """
        重试发送回调。成功则标记原记录为 resolved，失败则更新原记录的错误信息。
        不会产生新的失败记录。
        """
        from .db import _pool

        url = callback_config.get("url")
        method = callback_config.get("method", "POST").upper()
        headers = callback_config.get("headers") or {}
        timeout = callback_config.get("timeout", self._callback_timeout)

        # 验证 URL
        if not self._validate_url(url):
            error_msg = f"回调 URL 验证失败: {url}"
            logger.warning(error_msg)
            self._update_retry_failed(failed_log_id, error_msg, None)
            return

        headers.setdefault("Content-Type", "application/json")
        headers.setdefault("User-Agent", "NER-API-Callback/1.0")
        headers["X-Task-ID"] = task_id
        headers["X-Callback-Timestamp"] = datetime.now().isoformat()

        try:
            start_time = datetime.now()
            with httpx.Client(timeout=timeout) as client:
                response = client.request(
                    method=method,
                    url=url,
                    json=result_data,
                    headers=headers,
                )
                response_time_ms = (datetime.now() - start_time).total_seconds() * 1000

                if 200 <= response.status_code < 300:
                    logger.info(f"重试任务 {task_id} 回调成功: {url} (HTTP {response.status_code})")
                    # 标记原记录为 resolved
                    self._update_retry_resolved(failed_log_id)
                    self._update_callback_success(task_id)
                    self._log_callback_success(
                        task_id, url, method, response.status_code,
                        response_time_ms, headers, dict(response.headers),
                        response.text[:1000] if response.text else None,
                        request_body=result_data,
                    )
                else:
                    error_msg = f"HTTP {response.status_code}: {response.text[:200]}"
                    logger.warning(f"重试任务 {task_id} 回调失败: {error_msg}")
                    self._update_retry_failed(failed_log_id, error_msg, response.status_code)

        except httpx.TimeoutException:
            error_msg = f"连接超时 ({timeout}s)"
            logger.warning(f"重试任务 {task_id} 回调超时: {url}")
            self._update_retry_failed(failed_log_id, error_msg, None)

        except httpx.ConnectError as e:
            error_msg = f"连接失败: {str(e)}"
            logger.warning(f"重试任务 {task_id} 回调连接失败: {url}")
            self._update_retry_failed(failed_log_id, error_msg, None)

        except Exception as e:
            error_msg = f"未知错误: {str(e)}"
            logger.error(f"重试任务 {task_id} 回调异常: {e}")
            self._update_retry_failed(failed_log_id, error_msg, None)

    def _update_retry_resolved(self, failed_log_id: int) -> None:
        """重试成功，标记原失败记录为 resolved"""
        from .db import _pool
        try:
            conn = _pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """UPDATE callback_failed_logs 
                           SET status = 'resolved', last_retry_at = NOW(), retry_count = retry_count + 1
                           WHERE id = %s""",
                        (failed_log_id,),
                    )
                    conn.commit()
            finally:
                _pool.putconn(conn)
        except Exception as e:
            logger.error(f"更新重试成功状态失败: {e}")

    def _update_retry_failed(self, failed_log_id: int, error_message: str, http_status_code: Optional[int],
                              actual_headers: Optional[Dict] = None) -> None:
        """重试失败，更新原失败记录的错误信息、重试次数和请求头"""
        from .db import _pool
        try:
            conn = _pool.getconn()
            try:
                with conn.cursor() as cur:
                    if actual_headers:
                        cur.execute(
                            """UPDATE callback_failed_logs 
                               SET error_message = %s, http_status_code = %s,
                                   status = 'pending', last_retry_at = NOW(), retry_count = retry_count + 1,
                                   callback_headers = %s
                               WHERE id = %s""",
                            (error_message, http_status_code, Json(actual_headers), failed_log_id),
                        )
                    else:
                        cur.execute(
                            """UPDATE callback_failed_logs 
                               SET error_message = %s, http_status_code = %s,
                                   status = 'pending', last_retry_at = NOW(), retry_count = retry_count + 1
                               WHERE id = %s""",
                            (error_message, http_status_code, failed_log_id),
                        )
                    conn.commit()
            finally:
                _pool.putconn(conn)
        except Exception as e:
            logger.error(f"更新重试失败状态失败: {e}")

    def _validate_url(self, url: str) -> bool:
        """验证回调 URL"""
        try:
            parsed = urlparse(url)
            
            # 检查协议
            if parsed.scheme not in ("http", "https"):
                logger.warning(f"不支持的协议: {parsed.scheme}")
                return False
            
            # 检查主机名
            if not parsed.hostname:
                logger.warning("缺少主机名")
                return False
            
            # 白名单检查
            if self._allowed_hosts != "*":
                allowed = [h.strip() for h in self._allowed_hosts.split(",")]
                if parsed.hostname not in allowed:
                    logger.warning(f"主机名不在白名单中: {parsed.hostname}")
                    return False
            
            # 内网地址检查（放行本机默认回调端点）
            if self._block_private_ip:
                try:
                    ip = ipaddress.ip_address(parsed.hostname)
                    if ip.is_private or ip.is_loopback or ip.is_link_local:
                        # 放行指向本机的 /callback/default 端点
                        if parsed.path.rstrip("/") == "/callback/default":
                            logger.debug(f"放行本机默认回调端点: {url}")
                        else:
                            logger.warning(f"禁止回调到内网地址: {parsed.hostname}")
                            return False
                except ValueError:
                    # 不是 IP 地址（域名），检查 localhost
                    if parsed.hostname == "localhost" and self._block_private_ip:
                        if parsed.path.rstrip("/") == "/callback/default":
                            logger.debug(f"放行本机默认回调端点: {url}")
                        else:
                            logger.warning(f"禁止回调到内网地址: {parsed.hostname}")
                            return False
            
            return True
        
        except Exception as e:
            logger.error(f"URL 验证异常: {e}")
            return False

    def _enqueue_failed(
        self,
        task_id: str,
        callback_config: Dict,
        result_data: Dict,
        error_message: str,
        http_status_code: Optional[int],
        actual_headers: Optional[Dict] = None,
        request_body: Optional[Dict] = None,
    ) -> None:
        """将失败记录加入失败队列"""
        if self.failed_queue.full():
            logger.error(f"失败队列已满，任务 {task_id} 失败记录丢失")
            return
        
        self.failed_queue.put({
            "task_id": task_id,
            "callback_url": callback_config.get("url"),
            "callback_method": callback_config.get("method", "POST"),
            "callback_headers": actual_headers or callback_config.get("headers"),
            "request_body": request_body,
            "result_data": result_data,
            "error_message": error_message,
            "http_status_code": http_status_code,
            "failed_at": datetime.now(),
        })
        
        # 更新任务表的回调状态
        self._update_callback_failed(task_id, error_message)

    def _failed_handler(self) -> None:
        """失败处理线程：将失败记录写入数据库"""
        from .db import _pool
        
        while self._running:
            try:
                failed_record = self.failed_queue.get(timeout=1)
            except:
                continue
            
            self._failed_handler_status["busy"] = True
            
            try:
                conn = _pool.getconn()
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            """INSERT INTO callback_failed_logs 
                               (task_id, callback_url, callback_method, callback_headers,
                                request_body, result_data, error_message, http_status_code, failed_at)
                               VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s)""",
                            (
                                failed_record["task_id"],
                                failed_record["callback_url"],
                                failed_record["callback_method"],
                                Json(failed_record["callback_headers"]) if failed_record["callback_headers"] else None,
                                Json(failed_record["request_body"]) if failed_record.get("request_body") else None,
                                Json(failed_record["result_data"]) if failed_record.get("result_data") else None,
                                failed_record["error_message"],
                                failed_record["http_status_code"],
                                failed_record["failed_at"],
                            ),
                        )
                        conn.commit()
                        logger.info(f"任务 {failed_record['task_id']} 失败记录已写入数据库")
                finally:
                    _pool.putconn(conn)
            
            except Exception as e:
                logger.error(f"写入失败记录异常: {e}")
            finally:
                self._failed_handler_status["busy"] = False
                self.failed_queue.task_done()

    def _update_callback_success(self, task_id: str) -> None:
        """更新任务表的回调状态为成功（tasks / ti_tasks 双写，命中其一）"""
        from .db import _pool
        
        try:
            conn = _pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """UPDATE tasks 
                           SET callback_status = 'success', callback_finished_at = NOW()
                           WHERE task_id = %s::uuid""",
                        (task_id,),
                    )
                    cur.execute(
                        """UPDATE ti_tasks 
                           SET callback_status = 'success', callback_finished_at = NOW()
                           WHERE task_id = %s::uuid""",
                        (task_id,),
                    )
                    conn.commit()
            finally:
                _pool.putconn(conn)
        except Exception as e:
            logger.error(f"更新回调成功状态失败: {e}")

    def _update_callback_failed(self, task_id: str, error_message: str) -> None:
        """更新任务表的回调状态为失败（tasks / ti_tasks 双写，命中其一）"""
        from .db import _pool
        
        try:
            conn = _pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """UPDATE tasks 
                           SET callback_status = 'failed', 
                               callback_error = %s,
                               callback_finished_at = NOW()
                           WHERE task_id = %s::uuid""",
                        (error_message, task_id),
                    )
                    cur.execute(
                        """UPDATE ti_tasks 
                           SET callback_status = 'failed', 
                               callback_error = %s,
                               callback_finished_at = NOW()
                           WHERE task_id = %s::uuid""",
                        (error_message, task_id),
                    )
                    conn.commit()
            finally:
                _pool.putconn(conn)
        except Exception as e:
            logger.error(f"更新回调失败状态失败: {e}")

    def _log_callback_success(
        self,
        task_id: str,
        callback_url: str,
        callback_method: str,
        http_status_code: int,
        response_time_ms: float,
        request_headers: Dict,
        response_headers: Dict,
        response_body: Optional[str],
        request_body: Optional[Dict] = None,
    ) -> None:
        """记录回调成功日志"""
        from .db import _pool
        
        try:
            conn = _pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO callback_success_logs 
                           (task_id, callback_url, callback_method, http_status_code,
                            response_time_ms, request_headers, request_body, response_headers, response_body)
                           VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (
                            task_id,
                            callback_url,
                            callback_method,
                            http_status_code,
                            response_time_ms,
                            Json(request_headers),
                            Json(request_body) if request_body else None,
                            Json(response_headers),
                            response_body,
                        ),
                    )
                    conn.commit()
            finally:
                _pool.putconn(conn)
        except Exception as e:
            logger.error(f"记录回调成功日志失败: {e}")
