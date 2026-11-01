"""TI 联网情报模块单元测试（不依赖 DB / Playwright / 网络）。

覆盖：
  - 种子格式校验（schemas）
  - 代理解析（adapters.browser.parse_proxy）与代理三态/脱敏/推入
  - TiQueueManager：采集全链路（假适配器）/ 队列满拒绝 / 日上限 /
    selector 未就绪失败路径 / 回调触发 / 缓存开关（cache_enabled 写入窗口）
  - 统计聚合（routes._aggregate_overview）
  - 缓存设置覆盖（ti.cache_enabled / ti.cache_ttl_seconds）与掩码视图
  - admin 缓存分类解析（user_routes._resolve_cache_category）
"""
from __future__ import annotations

import asyncio
import sys
import time
import uuid
from datetime import date, datetime

import pytest

from api.ti_expand.adapters.browser import parse_proxy
from api.ti_expand.config import TiConfig
from api.ti_expand.manager import TiQueueManager
from api.ti_expand.routes import _aggregate_overview
from api.ti_expand.schemas import TiExpandRequest


# ─── 种子校验 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("seed_type,value", [
    ("ip", "8.8.8.8"),
    ("ip", "2001:4860:4860::8888"),
    ("domain", "test.example.com"),
    ("file", "d41d8cd98f00b204e9800998ecf8427e"),   # MD5
    ("file", "da39a3ee5e6b4b0d3255bfef95601890afd80709"),  # SHA1
    ("file", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"),  # SHA256
])
def test_seed_format_valid(seed_type, value):
    req = TiExpandRequest(seed_type=seed_type, seed_value=value)
    assert req.validate_seed_value_format() is None


@pytest.mark.parametrize("seed_type,value", [
    ("ip", "999.1.1.1"),
    ("ip", "not-an-ip"),
    ("domain", "-bad-.com"),
    ("domain", "no_tld"),
    ("file", "xyz123"),
    ("file", "d41d8cd9"),  # 长度不够
])
def test_seed_format_invalid(seed_type, value):
    req = TiExpandRequest(seed_type=seed_type, seed_value=value)
    assert req.validate_seed_value_format() is not None


def test_platforms_validation():
    req = TiExpandRequest(seed_type="ip", seed_value="8.8.8.8",
                          platforms=["threatbook", "ThreatBook", "qianxin"])
    assert req.platforms == ["threatbook", "qianxin"]
    with pytest.raises(Exception):
        TiExpandRequest(seed_type="ip", seed_value="8.8.8.8", platforms=["unknown"])
    with pytest.raises(Exception):
        TiExpandRequest(seed_type="ip", seed_value="8.8.8.8", platforms=[])


# ─── 代理解析 ────────────────────────────────────────────────────────────────

def test_parse_proxy():
    assert parse_proxy("") is None
    assert parse_proxy("http://1.2.3.4:8080") == {"server": "http://1.2.3.4:8080"}
    assert parse_proxy("socks5://1.2.3.4:1080") == {"server": "socks5://1.2.3.4:1080"}
    p = parse_proxy("http://user:pass@1.2.3.4:8080")
    assert p["server"] == "http://1.2.3.4:8080"
    assert p["username"] == "user" and p["password"] == "pass"
    # 无 scheme 按 http 处理
    assert parse_proxy("1.2.3.4:8080")["server"].startswith("http://")


# ─── Manager（内存 store + 假适配器，不触 DB / 浏览器）────────────────────────

class _FakeStore:
    def __init__(self, today_count: int = 0):
        self.tasks = {}
        self.calls = []
        self._today_count = today_count

    def update_ti_task_running(self, task_id):
        self.tasks[task_id]["status"] = "running"

    def update_ti_task_done(self, task_id, sections, raw, diagnostics,
                            latency_ms, cache_ttl_seconds=None):
        self.tasks[task_id].update(status="done", sections=sections,
                                   raw=raw, diagnostics=diagnostics,
                                   cache_ttl=cache_ttl_seconds)

    def update_ti_task_failed(self, task_id, error_msg):
        self.tasks[task_id].update(status="failed", error_msg=error_msg)

    def get_today_call_count(self, platform):
        return self._today_count

    def record_call(self, platform, channel, success):
        self.calls.append((platform, channel, success))

    def get_ti_task(self, task_id):
        return self.tasks.get(task_id)

    def upsert_session_state(self, *args, **kwargs):
        pass


class _FakeCallback:
    def __init__(self):
        self.payloads = []

    def enqueue_callback(self, task_id, callback_config, payload):
        self.payloads.append(payload)
        return True


class _FakeAdapter:
    """模拟采集完成的适配器：返回平台原生 sections + raw 结构。"""

    channel = "web"

    def __init__(self, platform):
        self.platform = platform

    async def query(self, seed_type, seed_value):
        return {
            "sections": [
                {"name": "威胁判定", "format": "kv",
                 "items": {"判定": "未知", "标签": []}},
                {"name": "相关样本", "format": "table",
                 "columns": ["哈希", "时间"], "rows": [["abc123", "2026-07-01"]]},
            ],
            "raw": {"verdict": "unknown"},
            "diagnostics": {"channel": "web", "artifacts": []},
        }

    async def close(self):
        pass


def _run_manager(monkeypatch, cfg, fake, tasks_to_submit, use_fake_adapter=True):
    """驱动 manager 直到所有任务终态，返回 (fake, callback, accepted)。"""
    cb = _FakeCallback()
    monkeypatch.setattr("api.ti_expand.manager.store", fake)
    if use_fake_adapter:
        monkeypatch.setattr(
            "api.ti_expand.manager.build_adapter",
            lambda c, platform: _FakeAdapter(platform),
        )

    async def main():
        mgr = TiQueueManager(cfg, callback_manager=cb)
        await mgr.start()
        accepted = []
        for t in tasks_to_submit:
            fake.tasks[t["task_id"]] = {"task_id": t["task_id"], "status": "pending"}
            accepted.append(await mgr.submit(t))
        for _ in range(200):
            await asyncio.sleep(0.05)
            if all(fake.tasks[t["task_id"]]["status"] in ("done", "failed")
                   for t in tasks_to_submit):
                break
        await mgr.shutdown()
        return accepted

    accepted = asyncio.run(main())
    return fake, cb, accepted


def _mk_task(seed_type="ip", seed_value="8.8.8.8", platform="threatbook", callback=None):
    return {"task_id": str(uuid.uuid4()), "seed_type": seed_type,
            "seed_value": seed_value, "platform": platform, "callback_config": callback}


def test_manager_e2e_with_callback(monkeypatch):
    cfg = TiConfig(min_interval=0, max_interval=0)
    task = _mk_task(callback={"url": "http://example.com/cb"})
    fake, cb, accepted = _run_manager(monkeypatch, cfg, _FakeStore(), [task])

    assert accepted == [True]
    rec = fake.tasks[task["task_id"]]
    assert rec["status"] == "done"
    assert rec["sections"] and rec["raw"] == {"verdict": "unknown"}
    assert fake.calls == [("threatbook", "web", True)]
    assert len(cb.payloads) == 1
    payload = cb.payloads[0]
    assert payload["status"] == 1
    assert payload["platform"] == "threatbook"
    assert payload["result"]["sections"] == rec["sections"]
    # raw 不进回调体（可能很大）
    assert "raw" not in payload["result"]


def test_manager_daily_limit(monkeypatch):
    cfg = TiConfig(min_interval=0, max_interval=0, daily_limit=10)
    task = _mk_task(callback={"url": "http://example.com/cb"})
    fake, cb, _ = _run_manager(monkeypatch, cfg, _FakeStore(today_count=10), [task])

    rec = fake.tasks[task["task_id"]]
    assert rec["status"] == "failed"
    assert "上限" in rec["error_msg"]
    assert cb.payloads[0]["status"] == -1


def test_manager_web_adapter_not_ready(monkeypatch):
    """selector 未配置该类型查询规则 → 任务失败(not_ready)，不启动浏览器。"""
    cfg = TiConfig(min_interval=0, max_interval=0)
    # 直接桩掉 selector 加载（空规则），不依赖 yml 实际配置内容
    monkeypatch.setattr("api.ti_expand.adapters.web.WebAdapter._load_selectors",
                        staticmethod(lambda platform: {}))
    task = _mk_task(seed_type="file", seed_value="d41d8cd98f00b204e9800998ecf8427e")
    fake, cb, _ = _run_manager(monkeypatch, cfg, _FakeStore(), [task],
                               use_fake_adapter=False)

    rec = fake.tasks[task["task_id"]]
    assert rec["status"] == "failed"
    assert "未配置" in rec["error_msg"]
    assert fake.calls == [("threatbook", "web", False)]


def test_manager_queue_full_rejects(monkeypatch):
    cfg = TiConfig(queue_size=1, min_interval=0, max_interval=0)
    monkeypatch.setattr("api.ti_expand.manager.store", _FakeStore())

    async def main():
        mgr = TiQueueManager(cfg)  # 不 start：无人消费，队列必满
        t1, t2 = _mk_task(), _mk_task()
        ok1 = await mgr.submit(t1)
        ok2 = await mgr.submit(t2)
        return ok1, ok2, mgr.can_accept("threatbook")

    ok1, ok2, can = asyncio.run(main())
    assert ok1 is True
    assert ok2 is False
    assert can is False


def test_manager_reset_session(monkeypatch, tmp_path):
    """人工登录后接管会话：关闭旧适配器、清 profile（保留种子）、状态置 ok。"""
    session_dir = tmp_path / "sessions" / "threatbook"
    (session_dir / "Default").mkdir(parents=True)
    (session_dir / "Default" / "Cookies").write_text("stale")
    (session_dir / "SingletonLock").write_text("lock")
    (session_dir / "storage_state.json").write_text('{"cookies": []}')

    upserts = []
    fake = _FakeStore()
    fake.upsert_session_state = lambda *a, **kw: upserts.append((a, kw))
    monkeypatch.setattr("api.ti_expand.manager.store", fake)

    class _Adapter:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    async def main():
        mgr = TiQueueManager(TiConfig(data_dir=str(tmp_path)))
        adapter = _Adapter()
        mgr._adapters["threatbook"] = adapter
        await mgr.reset_session("threatbook")
        return adapter, "threatbook" in mgr._adapters

    adapter, still_registered = asyncio.run(main())
    assert adapter.closed is True
    assert still_registered is False                    # 适配器已移除，下次重建
    assert (session_dir / "storage_state.json").exists()  # 种子保留
    assert not (session_dir / "Default").exists()         # 旧 profile 已清
    assert not (session_dir / "SingletonLock").exists()
    # 成功路径：(platform, "ok", fail_reason=None, proxy_used=直连空串, logged_in=True)
    assert upserts == [(("threatbook", "ok", None, ""), {"logged_in": True})]


# ─── 统计聚合（/ti/stats/overview 的纯函数，不触 DB）──────────────────────────

def test_aggregate_overview():
    daily = [
        # 今日：threatbook 外部 3 成 1 败 + 2 次缓存命中；qianxin 外部 1 成
        {"day": 20260725, "platform": "threatbook", "channel": "web",
         "success_count": 3, "fail_count": 1},
        {"day": 20260725, "platform": "threatbook", "channel": "cache",
         "success_count": 2, "fail_count": 0},
        {"day": 20260725, "platform": "qianxin", "channel": "web",
         "success_count": 1, "fail_count": 0},
        # 昨日：threatbook 外部 1 败
        {"day": 20260724, "platform": "threatbook", "channel": "web",
         "success_count": 0, "fail_count": 1},
    ]
    call_totals = {"external_success": 10, "external_fail": 3, "cache_hits": 7}
    task_stats = {
        "overall": {"tasks": 20, "tasks_done": 17, "tasks_failed": 3,
                    "avg_latency_ms": 31234.5},
        "platforms": {
            "threatbook": {"tasks": 12, "tasks_done": 10, "tasks_failed": 2,
                           "avg_latency_ms": 30100.0},
        },
        "seed_type_dist": [{"seed_type": "ip", "count": 15},
                           {"seed_type": "domain", "count": 5}],
    }
    sessions = {"threatbook": {"login_state": "ok"},
                "qianxin": {"login_state": "expired"}}

    ov = _aggregate_overview(daily, call_totals, task_stats, sessions,
                             daily_limit=300, today=20260725, window_days=30)

    # 全量合计
    assert ov["totals"]["external_calls"] == 13
    assert ov["totals"]["cache_hits"] == 7
    assert ov["totals"]["success_rate"] == round(10 / 13, 4)
    assert ov["totals"]["tasks"] == 20
    assert ov["totals"]["avg_latency_ms"] == 31234.5

    # 今日（缓存命中不计外部调用）
    assert ov["today"] == {"external_calls": 5, "success": 4, "fail": 1, "cache_hits": 2}

    # 平台窗口指标
    tb = next(p for p in ov["platforms"] if p["platform"] == "threatbook")
    assert tb["today_calls"] == 4 and tb["today_cache_hits"] == 2
    assert tb["calls"] == 5 and tb["success"] == 3 and tb["fail"] == 2
    assert tb["success_rate"] == round(3 / 5, 4)
    assert tb["cache_hits"] == 2 and tb["hit_rate"] == round(2 / 7, 4)
    assert tb["login_state"] == "ok" and tb["daily_limit"] == 300
    assert tb["tasks"] == 12 and tb["avg_latency_ms"] == 30100.0

    qx = next(p for p in ov["platforms"] if p["platform"] == "qianxin")
    assert qx["login_state"] == "expired"
    assert qx["tasks"] == 0 and qx["avg_latency_ms"] is None  # 无任务行时的兜底

    # 趋势按天升序、cache 单独成列
    assert ov["daily_trend"] == [
        {"day": "2026-07-24", "success": 0, "fail": 1, "cache": 0},
        {"day": "2026-07-25", "success": 4, "fail": 1, "cache": 2},
    ]
    assert ov["seed_type_dist"][0]["seed_type"] == "ip"


def test_aggregate_overview_empty():
    """全空输入：零值兜底，不得抛除零异常。"""
    ov = _aggregate_overview(
        [], {"external_success": 0, "external_fail": 0, "cache_hits": 0},
        {"overall": {"tasks": 0, "tasks_done": 0, "tasks_failed": 0,
                     "avg_latency_ms": None},
         "platforms": {}, "seed_type_dist": []},
        {}, daily_limit=300, today=20260725, window_days=30)
    assert ov["totals"]["success_rate"] == 0.0
    assert ov["today"]["external_calls"] == 0
    assert all(p["hit_rate"] == 0.0 and p["login_state"] == "unknown"
               for p in ov["platforms"])
    assert ov["daily_trend"] == []


# ─── 代理配置（effective_proxy 三态 + 脱敏 + manager 推入）───────────────────

def test_effective_proxy_tristate():
    from api.ti_expand.config import PlatformConfig, effective_proxy

    cfg = TiConfig(proxy_url="http://global:8080")
    cfg.platforms["threatbook"] = PlatformConfig(
        platform="threatbook", proxy_url="http://tb:8080")
    cfg.platforms["qianxin"] = PlatformConfig(platform="qianxin")

    # None（从未配置开关）：回退旧语义，平台级 > 全局
    assert cfg.proxy_enabled is None
    assert effective_proxy(cfg, "threatbook") == "http://tb:8080"
    assert effective_proxy(cfg, "qianxin") == "http://global:8080"
    # 显式关闭：一律直连（URL 保留）
    cfg.proxy_enabled = False
    assert effective_proxy(cfg, "threatbook") == ""
    assert effective_proxy(cfg, "qianxin") == ""
    assert cfg.proxy_url == "http://global:8080"
    # 显式开启：恢复优先级解析
    cfg.proxy_enabled = True
    assert effective_proxy(cfg, "threatbook") == "http://tb:8080"
    assert effective_proxy(cfg, "qianxin") == "http://global:8080"


def test_sanitize_proxy():
    from api.ti_expand.config import sanitize_proxy

    assert sanitize_proxy("") == ""
    assert sanitize_proxy("http://user:pass@10.0.0.1:8080") == "http://10.0.0.1:8080"
    assert sanitize_proxy("socks5://10.0.0.1:1080") == "socks5://10.0.0.1:1080"
    assert sanitize_proxy("10.0.0.1:8080") == "http://10.0.0.1:8080"  # 无 scheme 按 http


def test_masked_settings_view_hides_proxy_credentials():
    """掩码视图不得回显代理 URL 中的账密（与"密码不回显"同一口径）。"""
    import json

    from api.ti_expand.config import PlatformConfig, masked_settings_view

    cfg = TiConfig(proxy_enabled=True, proxy_url="http://user:pass@10.0.0.1:8080")
    cfg.platforms["threatbook"] = PlatformConfig(
        platform="threatbook", proxy_url="socks5://10.0.0.2:1080")
    cfg.platforms["qianxin"] = PlatformConfig(platform="qianxin")

    view = masked_settings_view(cfg)
    assert view["proxy_enabled"] is True
    assert view["proxy_url"] == {"url": "http://10.0.0.1:8080", "auth_set": True}
    assert view["platforms"]["threatbook"]["proxy_url"] == {
        "url": "socks5://10.0.0.2:1080", "auth_set": False}
    # 平台级优先：生效代理为 socks5；且整个视图无账密材料泄露（键名 username 等不算）
    assert view["platforms"]["threatbook"]["effective_proxy"] == "socks5://10.0.0.2:1080"
    assert view["platforms"]["qianxin"]["effective_proxy"] == "http://10.0.0.1:8080"
    assert "user:pass" not in json.dumps(view)


def test_manager_refresh_proxy_pushes_into_adapter(monkeypatch):
    """管理页改代理后：任务执行前 browser.proxy_url 被刷新为最新生效值，
    context 重建由 BrowserSession._proxy_fingerprint 机制接管。"""
    class _FakeBrowser:
        proxy_url = "http://old:8080"

    class _ProxyAdapter(_FakeAdapter):
        def __init__(self, platform, browser):
            super().__init__(platform)
            self.browser = browser

    browser = _FakeBrowser()
    fake = _FakeStore()
    monkeypatch.setattr("api.ti_expand.manager.store", fake)
    monkeypatch.setattr(
        "api.ti_expand.manager.build_adapter",
        lambda c, platform: _ProxyAdapter(platform, browser),
    )

    cfg = TiConfig(min_interval=0, max_interval=0, proxy_url="http://new:8080")
    task = _mk_task()

    async def main():
        mgr = TiQueueManager(cfg)
        # 预注册适配器（模拟此前已按旧代理构建），验证任务前 refresh 推入新代理
        mgr._adapters["threatbook"] = _ProxyAdapter("threatbook", browser)
        await mgr.start()
        fake.tasks[task["task_id"]] = {"task_id": task["task_id"], "status": "pending"}
        await mgr.submit(task)
        for _ in range(200):
            await asyncio.sleep(0.05)
            if fake.tasks[task["task_id"]]["status"] in ("done", "failed"):
                break
        await mgr.shutdown()

    asyncio.run(main())
    assert fake.tasks[task["task_id"]]["status"] == "done"
    assert browser.proxy_url == "http://new:8080"


# ─── 缓存复用设置（ti.cache_enabled / ti.cache_ttl_seconds）────────────────────

def _patch_app_settings(monkeypatch, values: dict):
    """桩掉 app_settings 读取：values 为 {key: value}，缺键返回 default。"""
    import api.db as _db
    monkeypatch.setattr(
        _db, "get_setting", lambda key, default=None: values.get(key, default))


def test_apply_settings_overrides_cache(monkeypatch):
    from api.ti_expand.config import (
        SETTING_CACHE_ENABLED,
        SETTING_CACHE_TTL,
        apply_settings_overrides,
    )

    # app_settings 覆盖生效
    _patch_app_settings(monkeypatch,
                        {SETTING_CACHE_ENABLED: "false", SETTING_CACHE_TTL: "600"})
    cfg = apply_settings_overrides(TiConfig())
    assert cfg.cache_enabled is False
    assert cfg.cache_ttl == 600

    # 未配置时保持 env / 默认值
    _patch_app_settings(monkeypatch, {})
    cfg = apply_settings_overrides(TiConfig(cache_enabled=True, cache_ttl=21600))
    assert cfg.cache_enabled is True and cfg.cache_ttl == 21600

    # 非法值静默忽略，不得污染配置
    _patch_app_settings(monkeypatch, {SETTING_CACHE_TTL: "abc"})
    cfg = apply_settings_overrides(TiConfig(cache_ttl=21600))
    assert cfg.cache_ttl == 21600


def test_masked_settings_view_cache_fields():
    from api.ti_expand.config import masked_settings_view

    view = masked_settings_view(TiConfig(cache_enabled=False, cache_ttl=600))
    assert view["cache_enabled"] is False
    assert view["cache_ttl"] == 600


def test_manager_cache_disabled_writes_no_window(monkeypatch):
    """ti.cache_enabled=false：任务完成不进入缓存复用窗口（ttl 传 None）。"""
    cfg = TiConfig(min_interval=0, max_interval=0, cache_enabled=False)
    task = _mk_task()
    fake, _, _ = _run_manager(monkeypatch, cfg, _FakeStore(), [task])
    assert fake.tasks[task["task_id"]]["status"] == "done"
    assert fake.tasks[task["task_id"]]["cache_ttl"] is None


def test_manager_cache_enabled_writes_window(monkeypatch):
    """ti.cache_enabled=true（默认）：完成任务写入缓存复用窗口。"""
    cfg = TiConfig(min_interval=0, max_interval=0, cache_enabled=True, cache_ttl=600)
    task = _mk_task()
    fake, _, _ = _run_manager(monkeypatch, cfg, _FakeStore(), [task])
    assert fake.tasks[task["task_id"]]["status"] == "done"
    assert fake.tasks[task["task_id"]]["cache_ttl"] == 600


# ─── admin 缓存分类（user_routes._resolve_cache_category）─────────────────────

class _FakeRequest:
    """最小请求替身：仅带 app.state.model_types。"""

    def __init__(self, model_types):
        self.app = type("App", (), {
            "state": type("State", (), {"model_types": model_types})(),
        })()


def test_resolve_cache_category():
    from api.user_routes import _resolve_cache_category

    req = _FakeRequest({"gliner": "ner", "gliner_multi": "ner",
                        "attack-auto": "attack"})

    # 不传 category → 提取缓存全量（不过滤）
    assert _resolve_cache_category(req, None) == (False, None)
    # ti → 走 ti_tasks 表
    assert _resolve_cache_category(req, "ti") == (True, None)
    # ner → 所有非 attack 模型
    is_ti, names = _resolve_cache_category(req, "ner")
    assert is_ti is False and sorted(names) == ["gliner", "gliner_multi"]
    # attack → 仅 attack 模型
    is_ti, names = _resolve_cache_category(req, "attack")
    assert is_ti is False and names == ["attack-auto"]
    # 未知分类 → 422
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        _resolve_cache_category(req, "bogus")
    assert exc_info.value.status_code == 422
    # model_types 缺失（如启动异常）→ 空过滤而非报错
    is_ti, names = _resolve_cache_category(_FakeRequest(None), "ner")
    assert is_ti is False and names == []


# ─── Cookie 导入（cookie_import）──────────────────────────────────────────────

from api.ti_expand.cookie_import import default_cookie_domain, parse_cookie_text


def test_parse_cookie_editor_json():
    """Cookie-Editor 导出 JSON：sameSite 映射 / expirationDate → expires。"""
    raw = ('[{"name":"sid","value":"abc","domain":".threatbook.com","path":"/",'
           '"expirationDate":1893456000,"httpOnly":true,"secure":true,'
           '"sameSite":"no_restriction"}]')
    cookies = parse_cookie_text(raw)
    assert len(cookies) == 1
    c = cookies[0]
    assert c["sameSite"] == "None" and c["expires"] == 1893456000
    assert c["httpOnly"] is True and c["secure"] is True
    assert c["domain"] == ".threatbook.com" and c["path"] == "/"


def test_parse_storage_state_object():
    """storage_state 对象：会话 cookie expires=-1，sameSite 缺省 Lax。"""
    cookies = parse_cookie_text({"cookies": [{"name": "a", "value": "1",
                                              "domain": "x.threatbook.com"}]})
    assert cookies[0]["expires"] == -1 and cookies[0]["sameSite"] == "Lax"


def test_parse_cookie_header_text():
    """Cookie 请求头文本：剥离 Cookie: 前缀，值原样保留，domain 用兜底主域。"""
    cookies = parse_cookie_text("Cookie: k1=v1; k2=v2%3D; ",
                                default_domain=".threatbook.com")
    assert [c["name"] for c in cookies] == ["k1", "k2"]
    assert cookies[1]["value"] == "v2%3D"
    assert all(c["domain"] == ".threatbook.com" for c in cookies)


@pytest.mark.parametrize("raw", ["", "  ", "not a cookie", "{}", "[]",
                                 '{"cookies": []}', '[{"value": "no-name"}]'])
def test_parse_cookie_rejects_bad_input(raw):
    with pytest.raises(ValueError):
        parse_cookie_text(raw)


def test_parse_cookie_header_requires_default_domain():
    """头文本格式不给兜底 domain 时无域信息，应判无有效条目。"""
    with pytest.raises(ValueError):
        parse_cookie_text("k1=v1; k2=v2")


def test_default_cookie_domain():
    """兜底主域取数据面（probe_url）而非 SSO：微步必须落到 .com 而非 .cn。"""
    assert default_cookie_domain("threatbook") == ".threatbook.com"
    assert default_cookie_domain("qianxin") == ".qianxin.com"


# ─── 人工登录窗口形态（manual_login._resolve_mode）─────────────────────────────

from api.ti_expand.recon.manual_login import _resolve_mode


def test_resolve_mode_auto_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("TI_LOGIN_VIEWER", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    assert _resolve_mode() == "local"


def test_resolve_mode_auto_headless_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("TI_LOGIN_VIEWER", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    assert _resolve_mode() == "vnc"


def test_resolve_mode_auto_linux_with_display(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("TI_LOGIN_VIEWER", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    assert _resolve_mode() == "local"


@pytest.mark.parametrize("value,expected", [("vnc", "vnc"), ("local", "local"),
                                            ("bogus", "vnc"), ("", "vnc")])
def test_resolve_mode_explicit_and_invalid(monkeypatch, value, expected):
    """显式指定优先；非法/空值回落 auto（headless linux → vnc）。"""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("TI_LOGIN_VIEWER", value)
    assert _resolve_mode() == expected


# ─── 远程桌面票据（routes._check_viewer_ticket）────────────────────────────────

from api.ti_expand import routes as ti_routes


def test_viewer_ticket_check(monkeypatch):
    monkeypatch.setitem(ti_routes._LOGIN_VIEWERS, "threatbook",
                        {"ws_port": 12345, "ticket": "tok123",
                         "expires": time.time() + 60})
    assert ti_routes._check_viewer_ticket("threatbook", "tok123")["ws_port"] == 12345
    assert ti_routes._check_viewer_ticket("threatbook", "wrong") is None
    assert ti_routes._check_viewer_ticket("threatbook", "") is None
    assert ti_routes._check_viewer_ticket("qianxin", "tok123") is None
    # 过期
    monkeypatch.setitem(ti_routes._LOGIN_VIEWERS, "threatbook",
                        {"ws_port": 1, "ticket": "tok123",
                         "expires": time.time() - 1})
    assert ti_routes._check_viewer_ticket("threatbook", "tok123") is None


# ─── 数据导出（exporter.build_ti_export_sql / routes._parse_export_date）────────

from api.ti_expand.exporter import build_ti_export_sql
from api.ti_expand.routes import _parse_export_date


def _task_row(**over):
    row = [
        "3fa85f64-5717-4562-b3fc-2c963f66afa6",  # task_id
        "ip", "8.8.8.8", "threatbook", "web", "done", None,
        datetime(2026, 8, 1, 10, 0, 0),          # created_at
        datetime(2026, 8, 1, 10, 0, 30),         # finished_at
        30123.4,                                  # latency_ms
        {"url": "http://example.com/cb"},         # callback_config
        "success", None,                          # callback_status / callback_error
        datetime(2026, 8, 1, 10, 0, 31),          # callback_finished_at
        1,                                        # user_id
        datetime(2026, 8, 1, 18, 0, 0),           # cache_expires_at
        {"file_id": "abc"},                       # information_json
    ]
    over_row = dict(zip([
        "task_id", "seed_type", "seed_value", "platform", "channel", "status",
        "error_msg", "created_at", "finished_at", "latency_ms",
        "callback_config", "callback_status", "callback_error",
        "callback_finished_at", "user_id", "cache_expires_at", "information_json",
    ], row))
    over_row.update(over)
    return list(over_row.values())


def test_export_sql_structure_and_casts():
    tasks = [_task_row()]
    results = [("3fa85f64-5717-4562-b3fc-2c963f66afa6",
                [{"name": "威胁判定", "format": "kv", "items": {"判定": "恶意"}}],
                {"verdict": "malicious"}, None,
                datetime(2026, 8, 1, 10, 0, 30))]
    sql = build_ti_export_sql(tasks, results, date(2026, 8, 1), date(2026, 8, 10))

    assert "CREATE TABLE IF NOT EXISTS ti_tasks" in sql
    assert "CREATE TABLE IF NOT EXISTS ti_task_results" in sql
    assert sql.count("INSERT INTO ti_tasks ") == 1
    assert sql.count("INSERT INTO ti_task_results ") == 1
    assert "ON CONFLICT (task_id) DO NOTHING" in sql
    assert "'3fa85f64-5717-4562-b3fc-2c963f66afa6'::uuid" in sql
    assert "'2026-08-01T10:00:00'::timestamptz" in sql
    assert '"判定": "恶意"' in sql and "::jsonb" in sql
    assert "BEGIN;" in sql and "COMMIT;" in sql
    assert "2026-08-01 ~ 2026-08-10" in sql
    # 幂等加载所需的事务/建表在 INSERT 之前
    assert sql.index("BEGIN;") < sql.index("INSERT INTO ti_tasks")


def test_export_sql_string_escaping():
    """单引号双写转义；NULL 落为 NULL 关键字；无任务时仍可加载（只有 DDL）。"""
    row = _task_row(seed_value="o'neil.com", error_msg="it's bad", raw=None)
    sql = build_ti_export_sql([row], [], None, None)
    assert "'o''neil.com'" in sql
    assert "'it''s bad'" in sql
    assert " NULL," in sql  # error_msg 之外还有 callback_error 等 NULL
    assert "全部（未按日期过滤）" in sql

    empty = build_ti_export_sql([], [], date(2026, 8, 1), date(2026, 8, 2))
    assert "INSERT INTO" not in empty
    assert "任务行数: 0，结果行数: 0" in empty


def test_export_sql_executable_roundtrip(tmp_path):
    """生成 SQL 在真实 PostgreSQL 不可用时的最低保障：逐语句可被 sqlparse 之外的
    简单规则切分，且每条 INSERT 括号配平（防转义回归截断语句）。"""
    sql = build_ti_export_sql([_task_row()], [], date(2026, 8, 1), date(2026, 8, 1))
    for stmt in sql.splitlines():
        if stmt.startswith("INSERT INTO"):
            assert stmt.count("(") == stmt.count(")")
            assert stmt.endswith(";")


def test_export_sql_import_enters_cache_window():
    """cache_ttl_seconds>0（内网导入场景）：done 行 cache_expires_at 改写为
    NOW()+ttl 表达式（目标库加载时求值），非 done 行保持源库原值。"""
    done = _task_row()
    failed = _task_row(task_id="3fa85f64-5717-4562-b3fc-2c963f66afa7", status="failed")
    sql = build_ti_export_sql([done, failed], [], cache_ttl_seconds=315360000)

    done_insert = [l for l in sql.splitlines() if "2c963f66afa6" in l][0]
    failed_insert = [l for l in sql.splitlines() if "2c963f66afa7" in l][0]
    # done 行：cache_expires_at 为导入时表达式，不再含源库原值字面量
    assert "NOW() + (315360000 || ' seconds')::interval" in done_insert
    assert "2026-08-01T18:00:00" not in done_insert
    # failed 行：不参与缓存复用，保持原值
    assert "'2026-08-01T18:00:00'::timestamptz" in failed_insert
    assert "NOW() +" not in failed_insert
    # 文件头注明缓存行为；表达式含括号，INSERT 仍需配平
    assert "导入即进缓存窗口" in sql
    for stmt in sql.splitlines():
        if stmt.startswith("INSERT INTO"):
            assert stmt.count("(") == stmt.count(")")

    # 默认不传 ttl：纯备份语义，done 行保持源库原值
    plain = build_ti_export_sql([done], [])
    assert "'2026-08-01T18:00:00'::timestamptz" in plain
    assert "NOW() +" not in plain


@pytest.mark.parametrize("value,expected", [
    ("2026-08-01", date(2026, 8, 1)),
    (" 2026-8-1 ", date(2026, 8, 1)),
    (None, None),
    ("", None),
    ("   ", None),
])
def test_parse_export_date_ok(value, expected):
    assert _parse_export_date(value, "start_date") == expected


@pytest.mark.parametrize("value", ["2026/08/01", "08-01", "not-a-date", "2026-13-01"])
def test_parse_export_date_bad(value):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        _parse_export_date(value, "start_date")
    assert exc_info.value.status_code == 422
