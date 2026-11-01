"""会话 Cookie 导入：运维在自己浏览器登录平台后，把 cookie 粘贴进来变成本平台会话种子。

定位：滑块/验证码场景的兜底登录方式——「去登录」远程桌面不可用时（或需要快速恢复时），
运维在本机浏览器正常登录平台官网（滑块就地过掉），用 Cookie-Editor 类插件导出
cookie 粘贴提交；服务端注入一次性 headless 浏览器做数据面探测，通过才落盘接管。

支持三种粘贴格式：
  1. Cookie-Editor / EditThisCookie 导出的 JSON 数组：[{"name": ..., "value": ..., ...}]
  2. Playwright storage_state JSON：{"cookies": [...], "origins": [...]}
  3. Cookie 请求头 / document.cookie 文本：k1=v1; k2=v2（缺 domain，按平台主域兜底；
     F12 → Network → 任意请求 → 复制 Cookie 请求头可拿到含 HttpOnly 的完整条目）

安全口径：cookie 即会话凭据——不写日志、接口仅 admin、验证不通过不落盘。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Union
from urllib.parse import urlsplit

from .adapters.browser import _STEALTH_INIT_SCRIPT, parse_proxy
from .adapters.session import load_login_cfg, verify_data_plane
from .config import TiConfig, effective_proxy

logger = logging.getLogger(__name__)

# Cookie-Editor 的 sameSite 取值 → Playwright 取值
_SAMESITE_MAP = {
    "strict": "Strict",
    "lax": "Lax",
    "none": "None",
    "no_restriction": "None",
    "unspecified": "Lax",
}


def default_cookie_domain(platform: str) -> str:
    """Cookie 头文本格式缺 domain 时的兜底：平台数据面主域（带点前缀）。

    取 probe_url（数据面）而非 login.url：运维复制 Cookie 请求头的对象页面就是
    数据面（如 x.threatbook.com），而 SSO 可能在另一主域（passport.threatbook.cn）；
    采集只依赖数据面会话。两平台均为三段式子域，取末两级；带点前缀对全部子域生效。
    """
    try:
        login = load_login_cfg(platform)
        host = urlsplit(login.get("probe_url") or login["url"]).hostname or ""
    except ValueError:
        return ""
    labels = host.split(".")
    return "." + ".".join(labels[-2:]) if len(labels) >= 2 else host


def _normalize_cookie(item: Dict, default_domain: str) -> Optional[Dict]:
    """把一条外部 cookie 记录规整成 Playwright 格式；缺 name/domain 返回 None。

    Playwright 的 storage_state / add_cookies 口径：expires 缺省 -1（会话 cookie），
    sameSite 缺省 Lax（与浏览器 Lax-by-default 一致），secure/httpOnly 缺省 False。
    """
    name = str(item.get("name") or "").strip()
    if not name:
        return None
    domain = str(item.get("domain") or default_domain).strip()
    if not domain:
        return None  # Playwright 要求 domain+path 或 url 其一
    expires = item.get("expires", item.get("expirationDate"))
    try:
        expires = float(expires)
    except (TypeError, ValueError):
        expires = 0
    return {
        "name": name,
        "value": str(item.get("value") if item.get("value") is not None else ""),
        "domain": domain,
        "path": str(item.get("path") or "/"),
        "expires": expires if expires > 0 else -1,
        "httpOnly": bool(item.get("httpOnly")),
        "secure": bool(item.get("secure")),
        "sameSite": _SAMESITE_MAP.get(str(item.get("sameSite") or "").strip().lower(), "Lax"),
    }


def parse_cookie_text(raw: Union[str, list, dict], default_domain: str = "") -> List[Dict]:
    """解析三种粘贴格式为 Playwright cookie 列表；无法解析/无有效条目抛 ValueError。"""
    if isinstance(raw, list):          # 调用方直接给 JSON 数组
        items = raw
    elif isinstance(raw, dict):        # storage_state 对象
        items = raw.get("cookies") or []
    else:
        text = str(raw or "").strip()
        if not text:
            raise ValueError("cookie 内容为空")
        if text[:1] in "[{":
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSON 解析失败: {exc}") from exc
            if isinstance(data, dict):
                data = data.get("cookies") or []
            if not isinstance(data, list):
                raise ValueError("JSON 必须是 cookie 数组或含 cookies 字段的对象")
            items = data
        else:
            # Cookie 请求头 / document.cookie：k1=v1; k2=v2
            if text.lower().startswith("cookie:"):
                text = text[7:].strip()
            items = []
            for pair in text.split(";"):
                pair = pair.strip()
                if not pair or "=" not in pair:
                    continue
                k, v = pair.split("=", 1)
                items.append({"name": k.strip(), "value": v.strip()})
    cookies = [c for c in (_normalize_cookie(i, default_domain) for i in items) if c]
    if not cookies:
        raise ValueError("未解析到有效 cookie 条目（需含 name/value）")
    return cookies


async def import_session_cookies(manager, cfg: TiConfig, platform: str,
                                 cookies: List[Dict], user_agent: str = "") -> Dict:
    """验证并接管导入的会话。返回 {"ok": bool, "detail": str}。

    流程：一次性 headless 浏览器注入 cookie → 数据面探测 → 通过才写
    storage_state 种子（+ UA 快照 imported_ua.txt，采集端同 UA 防会话绑定校验）
    → manager.reset_session 接管（红绿灯变绿）。验证不通过不写任何文件。
    """
    from playwright.async_api import async_playwright

    try:
        login_cfg = load_login_cfg(platform)
    except ValueError as exc:
        return {"ok": False, "detail": str(exc)}

    launch_kwargs: Dict = {"headless": True}
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        # 容器 root 运行必须关 Chromium 沙箱，否则浏览器拒绝启动
        launch_kwargs["chromium_sandbox"] = False
    proxy = parse_proxy(effective_proxy(cfg, platform))
    if proxy:
        launch_kwargs["proxy"] = proxy
    context_kwargs: Dict = {
        "storage_state": {"cookies": cookies, "origins": []},
        "viewport": {"width": 1440, "height": 900},
        "locale": "zh-CN",
    }
    if user_agent:
        context_kwargs["user_agent"] = user_agent

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(**launch_kwargs)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": f"浏览器启动失败: {exc}"}
        try:
            context = await browser.new_context(**context_kwargs)
            if not user_agent:
                # 未带 UA：headless UA 的 HeadlessChrome 标记是风控高危信号，修正后重建
                page = await context.new_page()
                ua = await page.evaluate("navigator.userAgent")
                await page.close()
                if ua and "HeadlessChrome" in ua:
                    await context.close()
                    context_kwargs["user_agent"] = ua.replace("HeadlessChrome", "Chrome")
                    context = await browser.new_context(**context_kwargs)
            await context.add_init_script(_STEALTH_INIT_SCRIPT)
            if not await verify_data_plane(context, login_cfg):
                return {"ok": False,
                        "detail": "数据面验证未通过：平台仍视为未登录。"
                                  "请确认 cookie 完整（含 HttpOnly 条目，推荐 Cookie-Editor "
                                  "导出 JSON 或复制完整 Cookie 请求头）且未过期"}
        finally:
            await browser.close()

    # 验证通过 → 落盘种子 + UA 快照，再接管会话
    session_dir = Path(cfg.data_dir) / "sessions" / platform
    session_dir.mkdir(parents=True, exist_ok=True)
    seed = session_dir / "storage_state.json"
    seed.write_text(json.dumps({"cookies": cookies, "origins": []},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    ua_file = session_dir / "imported_ua.txt"
    if user_agent:
        ua_file.write_text(user_agent, encoding="utf-8")
    else:
        ua_file.unlink(missing_ok=True)
    await manager.reset_session(platform)
    logger.info("[ti %s] 导入会话 cookie %d 条，数据面验证通过，会话已接管",
                platform, len(cookies))
    return {"ok": True, "detail": ""}
