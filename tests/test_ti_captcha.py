"""TI 滑块自动求解 / 指纹配置 / 会话保活 单元测试（不依赖浏览器 / 网络）。

覆盖：
  - captcha.gen_track 轨迹形态（累计位移 ≈ 目标距离、步数与停顿合理）
  - captcha._png_size / _match_gap（假 ddddocr）
  - solve_geetest_slide 在 ddddocr 不可用时安全回落 False
  - session._try_solver 开关语义
  - config 新增 env 项解析（TI_CAPTCHA_SOLVER / TI_BROWSER_CHANNEL /
    TI_SESSION_KEEPALIVE_S / TI_CAPTCHA_MAX_ATTEMPTS）
  - TiQueueManager 空闲保活（假适配器，不触浏览器）
"""
from __future__ import annotations

import asyncio
import struct
import zlib

from api.ti_expand.adapters import captcha, session, web
from api.ti_expand.config import load_config
from api.ti_expand.manager import TiQueueManager


# ─── gen_track 轨迹 ──────────────────────────────────────────────────────────

def test_gen_track_total_distance():
    for distance in (80.0, 123.4, 200.0):
        track = captcha.gen_track(distance)
        total = sum(dx for dx, _, _ in track)
        assert abs(total - distance) < 4.0, f"累计位移 {total} 偏离目标 {distance}"
        assert len(track) >= 20
        assert all(wait > 0 for _, _, wait in track)


def test_gen_track_overshoot_and_easeout():
    track = captcha.gen_track(120.0)
    # 累计位移曲线存在过冲（峰值 > 最终值）后再回退
    cum, peak, curve = 0.0, 0.0, []
    for dx, _, _ in track:
        cum += dx
        curve.append(cum)
        peak = max(peak, cum)
    assert peak > curve[-1]  # 过冲
    # ease-out：前半程位移占比应明显大于 50%
    half = sum(dx for dx, _, _ in track[: len(track) // 2])
    assert half > curve[-1] * 0.5


# ─── _png_size / _match_gap ──────────────────────────────────────────────────

def _mk_png(width: int, height: int) -> bytes:
    """构造最小合法 PNG（1xN 纯色，仅文件头尺寸参与断言）。"""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data)))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"\x00" + b"\xff\x00\x00" * width
    idat = zlib.compress(raw * height)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


def test_png_size():
    assert captcha._png_size(_mk_png(300, 150)) == (300, 150)
    assert captcha._png_size(b"not-a-png") == (0, 0)
    assert captcha._png_size(b"") == (0, 0)


def test_match_gap_with_fake_ddddocr(monkeypatch):
    class _FakeDet:
        def slide_match(self, piece, bg, simple_target=False):
            return {"target": [111, 30, 161, 80]}

    monkeypatch.setattr(captcha, "_DET", _FakeDet())
    monkeypatch.setattr(captcha, "_DET_FAILED", False)
    assert captcha._match_gap(b"piece", b"bg") == 111


def test_match_gap_ddddocr_error_falls_through(monkeypatch):
    class _BadDet:
        def slide_match(self, *a, **kw):
            raise RuntimeError("boom")

    monkeypatch.setattr(captcha, "_DET", _BadDet())
    monkeypatch.setattr(captcha, "_DET_FAILED", False)
    # ddddocr 异常 → 走 cv2 兜底；垃圾输入不崩溃，返回 None 或整数
    res = captcha._match_gap(b"junk", b"junk")
    assert res is None or isinstance(res, int)


def test_solve_returns_false_without_ddddocr(monkeypatch):
    monkeypatch.setattr(captcha, "_load_ddddocr", lambda: None)
    assert asyncio.run(captcha.solve_geetest_slide(None, platform="t")) is False


# ─── session._try_solver ─────────────────────────────────────────────────────

def test_try_solver_disabled(monkeypatch):
    called = []

    async def _fake(*a, **kw):
        called.append(1)
        return True

    monkeypatch.setattr(session, "solve_geetest_slide", _fake)
    assert asyncio.run(session._try_solver(None, "t", (False, 3))) is False
    assert not called  # 关闭时不调用求解器


def test_try_solver_success_and_exception(monkeypatch):
    async def _ok(page, platform="", max_attempts=3):
        return True

    monkeypatch.setattr(session, "solve_geetest_slide", _ok)
    assert asyncio.run(session._try_solver(None, "t", (True, 3))) is True
    assert asyncio.run(session._try_solver(None, "t", None)) is True  # None 用缺省（开）

    async def _boom(page, platform="", max_attempts=3):
        raise RuntimeError("solver crash")

    monkeypatch.setattr(session, "solve_geetest_slide", _boom)
    # 求解器异常不阻断原有 need_manual 兜底
    assert asyncio.run(session._try_solver(None, "t", (True, 3))) is False


# ─── config 新 env 项 ────────────────────────────────────────────────────────

def test_config_new_env_defaults(monkeypatch):
    for k in ("TI_CAPTCHA_SOLVER", "TI_CAPTCHA_MAX_ATTEMPTS",
              "TI_BROWSER_CHANNEL", "TI_SESSION_KEEPALIVE_S"):
        monkeypatch.delenv(k, raising=False)
    cfg = load_config()
    assert cfg.captcha_solver is True
    assert cfg.captcha_max_attempts == 3
    assert cfg.browser_channel == ""
    assert cfg.session_keepalive_s == 21600


def test_config_new_env_overrides(monkeypatch):
    monkeypatch.setenv("TI_CAPTCHA_SOLVER", "0")
    monkeypatch.setenv("TI_CAPTCHA_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("TI_BROWSER_CHANNEL", "chrome")
    monkeypatch.setenv("TI_SESSION_KEEPALIVE_S", "60")
    cfg = load_config()
    assert cfg.captcha_solver is False
    assert cfg.captcha_max_attempts == 5
    assert cfg.browser_channel == "chrome"
    assert cfg.session_keepalive_s == 60


# ─── web._parse_kv_items（item_selector 结构化 kv 解析）───────────────────────

class _FakeEl:
    def __init__(self, text):
        self._t = text

    async def inner_text(self):
        return self._t


class _FakeEls:
    def __init__(self, texts):
        self._texts = texts

    async def count(self):
        return len(self._texts)

    def nth(self, i):
        return _FakeEl(self._texts[i])


class _FakeLoc:
    def __init__(self, texts):
        self._texts = texts

    def locator(self, sel):
        return _FakeEls(self._texts)


def test_parse_kv_items():
    loc = _FakeLoc(["相关URL\n0", "ASN\nAS 45102(ALIBABA-CN-NET)",
                    "多行\n值1\n值2", "单项"])
    items = asyncio.run(web.WebAdapter._parse_kv_items(loc, ".count-item"))
    assert items == {"相关URL": "0", "ASN": "AS 45102(ALIBABA-CN-NET)",
                     "多行": "值1 值2", "单项": ""}


# ─── web._parse_blocks / _parse_table(row_selector/column_map)（r5 结构化区块）─

class _Sel:
    """模拟 Playwright Locator：一组文本元素。"""

    def __init__(self, texts):
        self._texts = list(texts)

    async def count(self):
        return len(self._texts)

    def nth(self, i):
        return _Sel([self._texts[i]])

    @property
    def first(self):
        return _Sel(self._texts[:1])

    async def inner_text(self):
        return self._texts[0] if self._texts else ""


class _BlocksLoc:
    """_parse_blocks 用：selector → 元素文本列表。"""

    def __init__(self, mapping):
        self._m = mapping

    def locator(self, sel):
        return _Sel(self._m.get(sel, []))


def test_parse_blocks():
    loc = _BlocksLoc({
        ".time": ["2026-03-27 情报更新"],
        ".tags": ["傀儡机", "垃圾邮件"],
        ".empty": [],
    })
    blocks = [
        {"key": "情报更新", "selector": ".time", "strip": "情报更新"},
        {"key": "威胁标签", "selector": ".tags", "multiple": True},
        {"key": "不存在", "selector": ".empty"},
    ]
    items = asyncio.run(web.WebAdapter._parse_blocks(loc, blocks))
    assert items == {"情报更新": "2026-03-27", "威胁标签": ["傀儡机", "垃圾邮件"]}


class _RowEl:
    """row_selector 命中的行元素：自身文本 + column_map selector → 文本。"""

    def __init__(self, text, cells=None):
        self._t = text
        self._cells = cells or {}

    async def inner_text(self):
        return self._t

    def locator(self, sel):
        return _Sel(self._cells.get(sel, []))


class _RowsLoc:
    def __init__(self, rows):
        self._rows = rows

    def locator(self, sel):
        return _RowsEl(self._rows)


class _RowsEl:
    def __init__(self, rows):
        self._rows = rows

    async def count(self):
        return len(self._rows)

    def nth(self, i):
        return self._rows[i]


def test_parse_table_row_selector_column_map():
    rows = [
        _RowEl("", {".name": ["2026你懂的"], ".cnt": ["3"]}),
        _RowEl("", {".name": ["可疑"], ".cnt": ["144"]}),
    ]
    rule = {"row_selector": ".tag", "column_map": {"标签": ".name", "提及": ".cnt"}}
    adapter = web.WebAdapter.__new__(web.WebAdapter)
    table = asyncio.run(adapter._parse_table(_RowsLoc(rows), rule))
    assert table == {"columns": ["标签", "提及"],
                     "rows": [["2026你懂的", "3"], ["可疑", "144"]]}


def test_cell_text_self_splits():
    el = _RowEl("HTTP AUTH暴力破解 1")
    assert asyncio.run(web.WebAdapter._cell_text(el, "self")) == "HTTP AUTH暴力破解 1"
    assert asyncio.run(web.WebAdapter._cell_text(el, "self:head")) == "HTTP AUTH暴力破解"
    assert asyncio.run(web.WebAdapter._cell_text(el, "self:tail")) == "1"
    # 无空格：head 为空、tail 取原文（用户标签无计数场景）
    el2 = _RowEl("可疑")
    assert asyncio.run(web.WebAdapter._cell_text(el2, "self:head")) == ""
    assert asyncio.run(web.WebAdapter._cell_text(el2, "self:tail")) == "可疑"
    # 未命中 selector → 空串
    assert asyncio.run(web.WebAdapter._cell_text(el, ".missing")) == ""


# ─── Manager 空闲保活 ────────────────────────────────────────────────────────

class _KeepaliveStore:
    def __init__(self):
        self.session_states = []

    def upsert_session_state(self, platform, state, note="", proxy_used=None, **kw):
        self.session_states.append((platform, state))

    # 以下为保活路径不触及的接口，占位防误调
    def get_today_call_count(self, platform):
        return 0

    def record_call(self, *a):
        raise AssertionError("保活不应计调用数")


class _KeepaliveAdapter:
    channel = "web"

    def __init__(self, platform):
        self.platform = platform
        self.calls = 0

    async def keepalive(self):
        self.calls += 1

    async def query(self, *a):  # 不应被调到
        raise AssertionError("无任务不应触发 query")

    async def close(self):
        pass


def test_manager_idle_keepalive(monkeypatch):
    from api.ti_expand.config import TiConfig

    adapters = {}

    def _build(cfg, platform):
        adapters[platform] = _KeepaliveAdapter(platform)
        return adapters[platform]

    store = _KeepaliveStore()
    monkeypatch.setattr("api.ti_expand.manager.store", store)
    monkeypatch.setattr("api.ti_expand.manager.build_adapter", _build)

    async def main():
        cfg = TiConfig(min_interval=0, max_interval=0, session_keepalive_s=1)
        mgr = TiQueueManager(cfg)
        await mgr.start()
        await asyncio.sleep(2.6)  # 保活周期 1s → 至少触发一次
        await mgr.shutdown()
        return mgr

    asyncio.run(main())
    assert adapters["threatbook"].calls >= 1
    assert ("threatbook", "ok") in store.session_states


# ─── is_logged_in：URL 即证据模式（数据面探测口径，threatbook r6）──────────────

class _UrlOnlyPage:
    """success_marker / entry_button 均未配置时，is_logged_in 只读 page.url。"""

    def __init__(self, url):
        self.url = url


def test_is_logged_in_url_as_evidence():
    cfg = {
        "sso_url_pattern": "https://passport.threatbook.cn/login**",
        "success_url_pattern": r"^https://x\.threatbook\.com",
        "success_marker": "",
        # 无 entry_button → URL 即证据
    }
    # 数据面未被弹回 → 会话有效
    assert asyncio.run(session.is_logged_in(
        _UrlOnlyPage("https://x.threatbook.com/v5/ip/8.8.8.8"), cfg)) is True
    # 被弹回 SSO → 无效
    assert asyncio.run(session.is_logged_in(
        _UrlOnlyPage("https://passport.threatbook.cn/login?service=x"), cfg)) is False
    # 落在其他域 → 无效
    assert asyncio.run(session.is_logged_in(
        _UrlOnlyPage("https://example.com/"), cfg)) is False
    # 未配 success_url_pattern 且无 marker/entry → 保守判无效
    assert asyncio.run(session.is_logged_in(
        _UrlOnlyPage("https://x.threatbook.com/v5/ip/8.8.8.8"),
        {"sso_url_pattern": "", "success_url_pattern": ""})) is False
