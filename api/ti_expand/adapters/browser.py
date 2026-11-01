"""Playwright 浏览器会话基类。

职责：
  - 每平台一个 persistent context（user_data_dir 持久化登录态）
  - storage_state 种子：profile 为空且 session_dir/storage_state.json 存在时，
    启动后注入其 cookies 初始化会话（人工首登/预研产出的会话由此复用）
  - 代理注入（http://[user:pass@]host:port / socks5://host:port）
  - webdriver 指纹隐藏
  - 失败留证（截图 + HTML 存 data_dir/artifacts/{task_id}/）

Playwright 是可选依赖：import 延迟到 ensure_context 首次调用，
未安装 playwright 的环境（如纯 API 部署）不受影响。
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# 隐藏自动化指纹的注入脚本（每次文档加载前执行）
_STEALTH_INIT_SCRIPT = """
// webdriver / 语言 / 插件 / chrome 运行时
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
if (!window.chrome) window.chrome = {runtime: {}};
// permissions.query 对 notifications 的应答修正
(function () {
  const q = window.navigator.permissions && window.navigator.permissions.query;
  if (q) {
    window.navigator.permissions.query = (p) =>
      p && p.name === 'notifications'
        ? Promise.resolve({state: Notification.permission})
        : q.call(window.navigator.permissions, p);
  }
})();
// WebGL vendor/renderer 伪装（headless 常为 SwiftShader）
(function () {
  const patch = (proto) => {
    if (!proto || !proto.getParameter) return;
    const orig = proto.getParameter;
    proto.getParameter = function (name) {
      if (name === 37445) return 'Intel Inc.';              // UNMASKED_VENDOR_WEBGL
      if (name === 37446) return 'Intel Iris OpenGL Engine'; // UNMASKED_RENDERER_WEBGL
      return orig.call(this, name);
    };
  };
  try { patch(window.WebGLRenderingContext && WebGLRenderingContext.prototype); } catch (e) {}
  try { patch(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype); } catch (e) {}
})();
// headless 下 outerWidth/outerHeight 为 0 的修正
if (window.outerWidth === 0) {
  Object.defineProperty(window, 'outerWidth', {get: () => window.innerWidth});
  Object.defineProperty(window, 'outerHeight', {get: () => window.innerHeight});
}
"""


def parse_proxy(proxy_url: str) -> Optional[Dict]:
    """把代理 URL 转成 Playwright proxy 配置。空串/非法返回 None（直连）。"""
    proxy_url = (proxy_url or "").strip()
    if not proxy_url:
        return None
    if "://" not in proxy_url:
        proxy_url = "http://" + proxy_url
    parts = urlsplit(proxy_url)
    if not parts.hostname:
        return None
    proxy = {"server": f"{parts.scheme}://{parts.hostname}:{parts.port or 8080}"}
    if parts.username:
        proxy["username"] = parts.username
        if parts.password:
            proxy["password"] = parts.password
    return proxy


class BrowserSession:
    """单平台浏览器会话。非线程安全，由 manager 的平台串行 worker 独占使用。"""

    def __init__(self, platform: str, data_dir: str,
                 proxy_url: str = "", headful: bool = False,
                 channel: str = "", max_age_s: int = 0) -> None:
        self.platform = platform
        self.data_dir = Path(data_dir)
        self.proxy_url = proxy_url
        self.headful = headful
        self.channel = channel  # Playwright channel（如 "chrome" 用本机 Chrome）
        self.max_age_s = max_age_s  # context 最大存活秒数（0=不限制，到期自动重建）
        self._playwright = None
        self._context = None
        self._context_started_at = 0.0  # monotonic 时间戳，配合 max_age_s 用
        self._proxy_fingerprint = (proxy_url, channel)  # 代理/通道变更 → 重建 context

    @property
    def session_dir(self) -> Path:
        return self.data_dir / "sessions" / self.platform

    @property
    def storage_state_seed(self) -> Path:
        """会话种子文件（人工首登工具 / 预研产出写入，自动登录成功后回写）。"""
        return self.session_dir / "storage_state.json"

    @property
    def imported_ua_file(self) -> Path:
        """Cookie 导入时快照的 UA（采集端须同 UA，防平台会话-UA 绑定校验）。"""
        return self.session_dir / "imported_ua.txt"

    def _profile_empty(self) -> bool:
        """persistent profile 是否尚未初始化（无 Default 目录即视为空）。"""
        return not (self.session_dir / "Default").exists()

    async def _seed_storage_state(self) -> None:
        """空 profile 且种子存在时，把种子 cookies 注入 context（尽力而为）。"""
        seed = self.storage_state_seed
        if not seed.exists():
            return
        try:
            data = json.loads(seed.read_text(encoding="utf-8"))
            cookies = data.get("cookies") or []
            if cookies:
                await self._context.add_cookies(cookies)
                logger.info("[%s] 空 profile，已从 storage_state 种子注入 %d 个 cookie",
                            self.platform, len(cookies))
        except Exception as exc:
            logger.warning("[%s] storage_state 种子注入失败: %s", self.platform, exc)

    async def _read_ua(self) -> str:
        """读当前 context 的 navigator.userAgent（用于 headless UA 检测）。"""
        try:
            page = await self._context.new_page()
            try:
                return await page.evaluate("navigator.userAgent")
            finally:
                await page.close()
        except Exception:
            return ""

    async def ensure_context(self):
        """返回可用的 browser context；代理/通道配置变更或超过 max_age_s 时自动重建。

        长跑的 Chromium 会退化（实测 ~30h 后 qiankun 微应用引导卡死、页面白屏），
        重建只换浏览器进程，persistent profile 在磁盘，登录会话不受影响。
        """
        if self._context is not None and \
                self._proxy_fingerprint == (self.proxy_url, self.channel):
            if self.max_age_s > 0 and \
                    time.monotonic() - self._context_started_at > self.max_age_s:
                logger.info("[%s] 浏览器 context 已运行 %.1f 小时(上限 %ds),自动重建",
                            self.platform,
                            (time.monotonic() - self._context_started_at) / 3600,
                            self.max_age_s)
                await self.close()
            else:
                return self._context
        else:
            await self.close()

        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError(
                "playwright 未安装：pip install playwright && playwright install chromium"
            ) from exc

        self.session_dir.mkdir(parents=True, exist_ok=True)
        profile_empty = self._profile_empty()
        self._playwright = await async_playwright().start()
        kwargs = {
            "user_data_dir": str(self.session_dir),
            "headless": not self.headful,
            "viewport": {"width": 1440, "height": 900},
            "locale": "zh-CN",
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            # 容器 root 运行必须关 Chromium 沙箱，否则浏览器拒绝启动
            kwargs["chromium_sandbox"] = False
        if self.channel:
            kwargs["channel"] = self.channel
        ua_file = self.imported_ua_file
        if ua_file.exists():
            imported_ua = ua_file.read_text(encoding="utf-8").strip()
            if imported_ua:
                kwargs["user_agent"] = imported_ua
        proxy = parse_proxy(self.proxy_url)
        if proxy:
            kwargs["proxy"] = proxy
            logger.info("[%s] 浏览器经由代理: %s", self.platform, proxy["server"])
        self._context = await self._playwright.chromium.launch_persistent_context(**kwargs)
        if not self.headful and "user_agent" not in kwargs:
            # headless UA 带 HeadlessChrome 标记（风控高危信号）：读出真实 UA
            # 去掉标记后重建 context（header 与 JS 两侧同时生效）
            ua = await self._read_ua()
            if ua and "HeadlessChrome" in ua:
                fixed = ua.replace("HeadlessChrome", "Chrome")
                logger.info("[%s] headless UA 修正: %s", self.platform, fixed)
                await self._context.close()
                kwargs["user_agent"] = fixed
                self._context = await self._playwright.chromium.launch_persistent_context(
                    **kwargs)
        await self._context.add_init_script(_STEALTH_INIT_SCRIPT)
        if profile_empty:
            await self._seed_storage_state()
        self._proxy_fingerprint = (self.proxy_url, self.channel)
        self._context_started_at = time.monotonic()
        return self._context

    async def new_page(self):
        context = await self.ensure_context()
        return await context.new_page()

    async def save_artifacts(self, task_id: str, page) -> List[str]:
        """失败留证：截图 + 页面 HTML，返回相对路径列表。尽力而为，不抛异常。"""
        artifacts: List[str] = []
        out_dir = self.data_dir / "artifacts" / task_id
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            png = out_dir / "failure.png"
            await page.screenshot(path=str(png), full_page=True)
            artifacts.append(str(png))
        except Exception as exc:
            logger.warning("[%s] 截图留证失败: %s", self.platform, exc)
        try:
            html = out_dir / "failure.html"
            html.write_text(await page.content(), encoding="utf-8")
            artifacts.append(str(html))
        except Exception as exc:
            logger.warning("[%s] HTML 留证失败: %s", self.platform, exc)
        return artifacts

    async def close(self) -> None:
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
