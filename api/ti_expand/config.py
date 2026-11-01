"""TI 联网情报配置。

配置来源（优先级从高到低）：
  1. app_settings 表（admin 设置页写入，键见 _SETTING_KEYS，改完无需重启）
  2. .env 环境变量（TI_*）

代理生效优先级：平台级（settings > env）> 全局（settings > env）> 直连。
Playwright 的代理由 browser context 启动参数携带，配置变更后下一次
context 重建自然生效，无需重启服务。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, Optional
from urllib.parse import urlsplit

# 平台标识 → 展示名
PLATFORMS = ("threatbook", "qianxin")
PLATFORM_NAMES = {"threatbook": "微步在线", "qianxin": "奇安信"}

# admin 设置页可覆盖的 app_settings 键
SETTING_PROXY_ENABLED = "ti.proxy_enabled"
SETTING_PROXY_URL = "ti.proxy_url"
SETTING_DAILY_LIMIT = "ti.daily_limit"
SETTING_CACHE_ENABLED = "ti.cache_enabled"
SETTING_CACHE_TTL = "ti.cache_ttl_seconds"


def platform_proxy_key(platform: str) -> str:
    return f"ti.proxy_url.{platform}"


def platform_credential_keys(platform: str) -> tuple:
    return (f"ti.credentials.{platform}.username", f"ti.credentials.{platform}.password")


@dataclass
class PlatformConfig:
    platform: str
    username: str = ""
    password: str = ""
    proxy_url: str = ""  # 平台级代理（env 层）


@dataclass
class TiConfig:
    enabled: bool = True
    queue_size: int = 50
    task_timeout: int = 120        # 单任务采集超时（秒）
    min_interval: float = 3.0      # 同平台两次采集的最小间隔（秒）
    max_interval: float = 8.0      # 同平台两次采集的最大间隔（秒）
    daily_limit: int = 5000        # 每平台每日外部调用上限
    cache_enabled: bool = True     # 结果缓存复用总开关（关闭后不命中也不写入）
    cache_ttl: int = 315360000    # 结果缓存复用 TTL（秒），默认 10 年（87600 小时）
    data_dir: str = "ti_data"      # 会话 / 截图留证目录
    headful: bool = False          # 有头模式：采集/自动登录浏览器可视化运行（排查用；无显示器需 xvfb）
    proxy_enabled: Optional[bool] = None  # 代理开关：None=未配置（回退"URL 非空即启用"旧语义）
    proxy_url: str = ""            # 全局代理（env 层）
    platforms: Dict[str, PlatformConfig] = field(default_factory=dict)
    captcha_solver: bool = True    # 极验滑块自动求解（ddddocr 本地识别，失败回落人工）
    captcha_max_attempts: int = 3  # 单轮滑块求解最大尝试次数
    browser_channel: str = ""      # Playwright channel（"chrome"=本机 Chrome，指纹更干净）
    session_keepalive_s: int = 21600  # 空闲会话保活间隔（秒，0=关闭）
    browser_max_age_s: int = 21600    # 浏览器 context 最大存活时间（秒，0=不重建）；
                                      # 长跑浏览器会退化（微应用卡死/白屏），到期下次任务/保活前
                                      # 自动重建（persistent profile 在磁盘，会话不受影响）
    login_viewer: str = "auto"     # 人工登录窗口形态：auto=mac/有显示器本机弹窗、无显示器走
                                   # 网页远程桌面（Xvfb+noVNC）；local=强制本机弹窗；vnc=强制远程桌面


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _opt_bool(value: Optional[str]) -> Optional[bool]:
    """三态布尔解析：未设置/空串 → None（回退默认语义）。"""
    if value is None:
        return None
    v = str(value).strip().lower()
    if not v:
        return None
    return v in ("1", "true", "yes", "on")


def load_config() -> TiConfig:
    """从环境变量加载基础配置（不触 DB）。"""
    cfg = TiConfig(
        enabled=_env_bool("TI_ENABLED", True),
        queue_size=int(os.environ.get("TI_QUEUE_SIZE", "50")),
        task_timeout=int(os.environ.get("TI_TASK_TIMEOUT", "120")),
        min_interval=float(os.environ.get("TI_MIN_INTERVAL", "3")),
        max_interval=float(os.environ.get("TI_MAX_INTERVAL", "8")),
        daily_limit=int(os.environ.get("TI_DAILY_LIMIT", "5000")),
        cache_enabled=_env_bool("TI_CACHE_ENABLED", True),
        cache_ttl=int(os.environ.get("TI_CACHE_TTL", "315360000")),
        data_dir=os.environ.get("TI_DATA_DIR", "ti_data"),
        headful=_env_bool("TI_HEADFUL", False),
        proxy_enabled=_opt_bool(os.environ.get("TI_PROXY_ENABLED")),
        proxy_url=os.environ.get("TI_PROXY_URL", "").strip(),
        captcha_solver=_env_bool("TI_CAPTCHA_SOLVER", True),
        captcha_max_attempts=int(os.environ.get("TI_CAPTCHA_MAX_ATTEMPTS", "3")),
        browser_channel=os.environ.get("TI_BROWSER_CHANNEL", "").strip(),
        session_keepalive_s=int(os.environ.get("TI_SESSION_KEEPALIVE_S", "21600")),
        browser_max_age_s=int(os.environ.get("TI_BROWSER_MAX_AGE_S", "21600")),
        login_viewer=os.environ.get("TI_LOGIN_VIEWER", "auto").strip().lower() or "auto",
    )
    for p in PLATFORMS:
        cfg.platforms[p] = PlatformConfig(
            platform=p,
            username=os.environ.get(f"TI_{p.upper()}_USERNAME", "").strip(),
            password=os.environ.get(f"TI_{p.upper()}_PASSWORD", "").strip(),
            proxy_url=os.environ.get(f"TI_PROXY_URL_{p.upper()}", "").strip(),
        )
    return cfg


def apply_settings_overrides(cfg: TiConfig) -> TiConfig:
    """合并 app_settings 覆盖项，返回生效配置（原地修改并返回 cfg）。

    在任务执行前调用（延迟读 DB），使设置页改动对后续任务立即生效。
    DB 不可用时静默回退到 env 配置。
    """
    try:
        from ..db import get_setting
    except Exception:
        return cfg

    def _get(key: str) -> Optional[str]:
        try:
            return get_setting(key)
        except Exception:
            return None

    v = _get(SETTING_PROXY_ENABLED)
    if v is not None:
        cfg.proxy_enabled = _opt_bool(v)
    v = _get(SETTING_PROXY_URL)
    if v is not None:
        cfg.proxy_url = v.strip()
    v = _get(SETTING_DAILY_LIMIT)
    if v:
        try:
            cfg.daily_limit = int(v)
        except ValueError:
            pass
    v = _get(SETTING_CACHE_ENABLED)
    if v is not None:
        cfg.cache_enabled = v.strip().lower() in ("1", "true", "yes", "on")
    v = _get(SETTING_CACHE_TTL)
    if v:
        try:
            cfg.cache_ttl = int(v)
        except ValueError:
            pass
    for p, pcfg in cfg.platforms.items():
        v = _get(platform_proxy_key(p))
        if v is not None:
            pcfg.proxy_url = v.strip()
        ukey, pkey = platform_credential_keys(p)
        v = _get(ukey)
        if v:
            pcfg.username = v.strip()
        v = _get(pkey)
        if v:
            pcfg.password = v
    return cfg


def effective_proxy(cfg: TiConfig, platform: str) -> str:
    """平台生效代理：开关关闭 → 直连；否则 平台级 > 全局 > 空（直连）。

    proxy_enabled 为 None（从未配置开关）时回退旧语义：URL 非空即启用，
    保证存量 env / app_settings 配置行为不变。
    """
    if cfg.proxy_enabled is False:
        return ""
    pcfg = cfg.platforms.get(platform)
    if pcfg and pcfg.proxy_url:
        return pcfg.proxy_url
    return cfg.proxy_url


def sanitize_proxy(proxy_url: str) -> str:
    """脱敏代理 URL（剥账密），用于 proxy_used 落库与前端展示。"""
    proxy_url = (proxy_url or "").strip()
    if not proxy_url:
        return ""
    if "://" not in proxy_url:
        proxy_url = "http://" + proxy_url
    parts = urlsplit(proxy_url)
    host = parts.hostname or ""
    if not host:
        return ""
    return f"{parts.scheme}://{host}:{parts.port or 8080}"


def masked_settings_view(cfg: TiConfig) -> Dict:
    """GET /ti/settings 的掩码视图：密码不回显，账号中间打码。

    代理 URL 同样可能携带出口机账密 → 视图一律剥成 scheme://host:port
    （auth_set 标记是否含认证），与"密码不回显"同一口径；完整值只留在
    服务端配置内部（采集与自检使用）。
    """
    def _mask(s: str) -> str:
        if not s:
            return ""
        if len(s) <= 4:
            return s[0] + "***"
        return s[:2] + "***" + s[-2:]

    def _proxy_masked(url: str) -> Dict:
        url = (url or "").strip()
        if not url:
            return {"url": "", "auth_set": False}
        full = url if "://" in url else "http://" + url
        return {"url": sanitize_proxy(url), "auth_set": bool(urlsplit(full).username)}

    return {
        "enabled": cfg.enabled,
        "proxy_enabled": (cfg.proxy_enabled if cfg.proxy_enabled is not None
                          else bool(cfg.proxy_url)),
        "proxy_url": _proxy_masked(cfg.proxy_url),
        "daily_limit": cfg.daily_limit,
        "cache_enabled": cfg.cache_enabled,
        "cache_ttl": cfg.cache_ttl,
        "min_interval": cfg.min_interval,
        "max_interval": cfg.max_interval,
        "platforms": {
            p: {
                "name": PLATFORM_NAMES.get(p, p),
                "username": _mask(pcfg.username),
                "password_set": bool(pcfg.password),
                "proxy_url": _proxy_masked(pcfg.proxy_url),
                "effective_proxy": sanitize_proxy(effective_proxy(cfg, p)),
            }
            for p, pcfg in cfg.platforms.items()
        },
    }
