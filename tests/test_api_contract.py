"""
API 契约测试 (FastAPI TestClient):
  - /extract 的参数越界 → 422
  - /extract → /tasks/{id}/result 端到端,返回 chunk_metadata
  - reuse_cache=true 同参数二次提交 → cached=true 且 chunk_metadata 与首次完全相同
  - 回调 payload 结构包含 chunk_metadata,其他字段与旧契约保持一致
  - 直接插入 chunk_metadata IS NULL 的历史任务,API 返回 chunk_metadata: null

对应 Requirements: 3.4 / 3.5 / 3.6 / 6.1 / 6.2 / 6.3 / 6.4 / 9.1 / 9.4 / 9.5
"""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import pytest
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent / "api" / ".env")


pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def client():
    """启动 FastAPI app,触发 lifespan(DB + 队列 + 模型加载)。"""
    try:
        from fastapi.testclient import TestClient
        from api.main import app
    except Exception as exc:
        pytest.skip(f"FastAPI 或 main 模块加载失败: {exc}")

    # 开启 TestClient → 进入 lifespan → 加载模型 → 进入 yield
    with TestClient(app) as c:
        yield c


# ─── 用例 1: 参数越界 → 422 ─────────────────────────────────────────────

@pytest.mark.parametrize("field,bad_value", [
    ("chunk_overlap", 600),
    ("chunk_overlap", -1),
    ("chunk_safety_margin", 100),
    ("chunk_safety_margin", -1),
    ("threshold", 1.5),
    ("threshold", -0.1),
])
def test_extract_rejects_out_of_range(client, field, bad_value):
    """
    _Requirements: 3.4, 3.5, 3.6_
    """
    body = {
        "text": "测试",
        "labels": ["人物"],
        field: bad_value,
    }
    r = client.post("/extract", json=body)
    assert r.status_code == 422, \
        f"{field}={bad_value} 应返回 422,实际 {r.status_code}: {r.text}"


# ─── 用例 2: 长文本端到端 → /tasks/{id}/result 返回 chunk_metadata ───────

def _wait_done(client, task_id, timeout_sec: int = 180):
    """轮询等待任务到 done/failed 状态。"""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        r = client.get(f"/tasks/{task_id}/result")
        if r.status_code != 200:
            raise AssertionError(f"GET /tasks/{{}}/result 返回 {r.status_code}: {r.text}")
        data = r.json()
        if data.get("status") in (1, -1):
            return data
        time.sleep(0.3)
    raise AssertionError(f"任务 {task_id} 在 {timeout_sec}s 内未完成")


def test_extract_returns_chunk_metadata(client):
    """
    长文本 + 分块开启:/tasks/{id}/result 必须包含 chunk_metadata,
    entities 每一项都有 chunk_index 字段。

    _Requirements: 6.1, 6.2, 6.3, 9.1_
    """
    paragraph = "乔治·华盛顿是美国第一任总统,他于1789年在纽约就任。" * 30
    r = client.post("/extract", json={
        "text": paragraph,
        "labels": ["人物", "地点", "时间"],
        "model_name": "gliner_multi-v2.1",
        "chunk_enabled": True,
        "chunk_overlap": 32,
        "chunk_safety_margin": 16,
        "threshold": 0.5,
    })
    assert r.status_code == 202, f"预期 202,实际 {r.status_code}: {r.text}"
    body = r.json()
    task_id = body["task_id"]
    assert body["cached"] is False

    data = _wait_done(client, task_id)
    assert data["status"] == 1, f"任务未成功: {data}"

    meta = data["chunk_metadata"]
    assert meta is not None
    assert meta["chunking_enabled"] is True
    assert meta["chunk_count"] >= 2
    assert meta["model_max_tokens"] == 384
    for k in ("chunking_enabled", "model_max_tokens", "labels_token_count",
              "effective_max_tokens", "overlap", "safety_margin", "threshold",
              "chunk_count", "chunks"):
        assert k in meta, f"chunk_metadata 缺字段 {k}"
    for c in meta["chunks"]:
        for k in ("chunk_index", "char_start", "char_end", "char_length",
                  "token_count", "status"):
            assert k in c, f"chunk_info 缺字段 {k}"

    # entities 每项带 chunk_index
    for e in data["entities"]:
        assert e.get("chunk_index") is not None

    # 清理
    client.get(f"/tasks/{task_id}?action=delete")


# ─── 用例 3: 缓存命中复用 chunk_metadata + 回调 payload 结构 ─────────────

def test_cache_hit_reuses_chunk_metadata_and_callback_payload_shape(client):
    """
    同 (text, labels, model, chunk_cfg) 两次提交:
      - 第二次命中缓存,cached=true
      - 两次任务的 chunk_metadata 完全一致
      - 回调 payload 新增 chunk_metadata 字段;其他字段(status/task_id/model_name/
        result/error_message/created_at/finished_at/latency_ms/information_json)
        的字段名必须全部存在,不删除不重命名。

    _Requirements: 6.4, 9.4, 9.5_
    """
    # 为避免踩到回调 URL 白名单 / 内网阻断等逻辑,直接断言 callback 配置被
    # 回调管理器接收(不实际发送 HTTP):本测试只校验 payload 结构。
    from api.main import callback_manager

    collected = []
    original_enqueue = callback_manager.enqueue_callback

    def _capture(task_id, cb_cfg, payload):
        collected.append((task_id, dict(payload)))
        return True

    callback_manager.enqueue_callback = _capture

    try:
        paragraph = "乔治·华盛顿是美国第一任总统,他于1789年在纽约就任。" * 30
        body = {
            "text": paragraph,
            "labels": ["人物", "地点", "时间"],
            "model_name": "gliner_multi-v2.1",
            "chunk_enabled": True,
            "chunk_overlap": 32,
            "chunk_safety_margin": 16,
            "threshold": 0.5,
            "reuse_cache": True,
            "callback": {
                "url": "http://127.0.0.1:65535/no-such-endpoint",
                "method": "POST",
                "timeout": 5,
            },
        }

        # 第一次提交
        r1 = client.post("/extract", json=body)
        assert r1.status_code == 202
        resp1 = r1.json()
        tid1 = resp1["task_id"]
        assert resp1["cached"] is False
        data1 = _wait_done(client, tid1)
        assert data1["status"] == 1
        meta1 = data1["chunk_metadata"]
        assert meta1 is not None

        # 第二次同参数提交 → 命中缓存
        r2 = client.post("/extract", json=body)
        assert r2.status_code == 202
        resp2 = r2.json()
        tid2 = resp2["task_id"]
        assert resp2["cached"] is True
        assert resp2["cached_from"] == tid1

        data2 = _wait_done(client, tid2)
        assert data2["status"] == 1
        meta2 = data2["chunk_metadata"]
        assert meta2 is not None
        # chunk_metadata 完全一致
        assert meta1 == meta2, "缓存命中时 chunk_metadata 应与首次完全一致"

        # 回调 payload 校验
        assert collected, "应该触发了回调"
        task_ids_seen = [t for t, _ in collected]
        assert tid1 in task_ids_seen
        assert tid2 in task_ids_seen

        # 检查两个任务各一条回调 payload 的结构
        required_fields = {
            "status", "msg", "task_id", "model_name", "result",
            "error_message", "created_at", "finished_at",
            "latency_ms", "information_json", "chunk_metadata",
        }
        for _, payload in collected:
            missing = required_fields - payload.keys()
            assert not missing, f"回调 payload 缺字段: {missing}"
            assert isinstance(payload["chunk_metadata"], (dict, type(None)))

        # 清理
        for tid in (tid1, tid2):
            client.get(f"/tasks/{tid}?action=delete")
    finally:
        callback_manager.enqueue_callback = original_enqueue


# ─── 用例 4: 历史任务 chunk_metadata=None 兼容 ────────────────────────────

def test_legacy_task_returns_null_chunk_metadata(client):
    """
    直接插入一条 chunk_metadata IS NULL 的老任务,
    GET /tasks/{id}/result 必须返回 chunk_metadata: null,不报错。

    _Requirements: 6.4_
    """
    from api.db import insert_task, update_task_done, delete_task

    task_id = str(uuid.uuid4())
    insert_task(
        task_id=task_id,
        model_name="gliner_multi-v2.1",
        input_text="测试文本",
        labels=["人物"],
        label_mapping=None,
        task_hash=f"legacy_{task_id[:8]}",
        information_json={"legacy": True},
        callback_config=None,
    )
    # chunk_metadata=None → task_results.chunk_metadata 写 SQL NULL
    update_task_done(
        task_id,
        result=[{"text": "张三", "label": "PERSON", "raw_label": "人物",
                 "start": 0, "end": 2, "score": 0.9}],
        latency_ms=99.9,
        chunk_metadata=None,
    )

    try:
        r = client.get(f"/tasks/{task_id}/result")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == 1
        assert data["chunk_metadata"] is None, "历史任务应返回 chunk_metadata: null"
        # entities 仍正常返回
        assert len(data["entities"]) == 1
        assert data["entities"][0]["text"] == "张三"

        # /tasks/{id} (TaskResult 路由) 也应返回 chunk_metadata: None
        r2 = client.get(f"/tasks/{task_id}")
        assert r2.status_code == 200
        assert r2.json().get("chunk_metadata") is None

        # list_tasks 派生列 chunk_count 应为 None
        r3 = client.get("/tasks?page=1&page_size=100")
        items = r3.json()["items"]
        row = next(x for x in items if x["task_id"] == task_id)
        assert row.get("chunk_count") is None
    finally:
        delete_task(task_id)
