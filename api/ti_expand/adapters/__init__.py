"""适配器注册：为每个平台构建浏览器采集适配器。"""
from __future__ import annotations

from ..config import TiConfig, effective_proxy
from .base import BaseTiAdapter
from .browser import BrowserSession
from .web import WebAdapter


def build_adapter(cfg: TiConfig, platform: str) -> BaseTiAdapter:
    """每平台一个 WebAdapter（Playwright 浏览器通道）。"""
    browser = BrowserSession(
        platform=platform,
        data_dir=cfg.data_dir,
        proxy_url=effective_proxy(cfg, platform),
        headful=cfg.headful,
        channel=getattr(cfg, "browser_channel", ""),
        max_age_s=getattr(cfg, "browser_max_age_s", 0),
    )
    return WebAdapter(browser, platform, cfg=cfg)
