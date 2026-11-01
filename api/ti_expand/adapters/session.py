"""平台登录会话管理（selector yml 驱动，Playwright async）。

流程（ensure_session）：
  1. 探测：打开 login.probe_url（缺省 login.url），按 success_url_pattern /
     success_marker / entry_button 判定登录态；已登录直接返回页面
  2. 未登录且配了凭据 → 按 login 块自动登录：dismiss_modal → entry_button（跳 SSO）
     → password_tab_text → 填账号密码 → agreement_checkbox → submit_button
  3. 提交后轮询结果（≤ wait_timeout_s）：
     captcha_marker 可见      → 先走 captcha.solve_geetest_slide 自动过滑块，
                                仍不过才 TiAdapterError(need_manual)（不硬闯，防风控）
     auth_fail_marker 可见    → TiAdapterError(auth_failed)（不自动重试，防锁号）
     登录态成立               → 回写 storage_state 种子，返回页面
     超时                     → TiAdapterError(query_failed)

安全红线：本模块任何日志/异常消息都不含密码明文。
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

from .base import TiAdapterError
from .captcha import solve_geetest_slide

logger = logging.getLogger(__name__)

# 登录提交后等待跳转/标志的最长时间
LOGIN_WAIT_TIMEOUT_S = 25.0
# 滑块自动求解缺省次数（solver_cfg 未传入时）；单次登录最多求解轮数
DEFAULT_SOLVER_ATTEMPTS = 3
MAX_SOLVER_RUNS_PER_LOGIN = 2
# storage_state 种子陈旧阈值：会话有效但种子超过该年龄时顺手回写
# （种子只在自动登录成功/人工首登时更新，长期靠 profile 运行的平台种子会过期，
#  一旦 reset_session 清空 profile 就会拿过期种子重建 → 被迫人工重登）
SEED_REFRESH_AGE_S = 7 * 24 * 3600


def glob_match(url: str, pattern: str) -> bool:
    """Playwright 风格 glob 匹配：`**` 跨路径，`*` 不跨 `/`。"""
    if not pattern:
        return False
    out = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if i + 1 < len(pattern) and pattern[i + 1] == "*":
                out.append(".*")
                i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif ch == "?":
            out.append(".")
            i += 1
        else:
            out.append(re.escape(ch))
            i += 1
    return re.match("".join(out) + "$", url) is not None


def _split_selectors(value: str) -> list:
    """yml 中逗号分隔的 selector 列表（单串也兼容）。"""
    return [s.strip() for s in (value or "").split(",") if s.strip()]


async def _first_visible(page, selector: str):
    for sel in _split_selectors(selector):
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                return loc
        except Exception:
            pass
    return None


async def _wait_first_visible(page, selector: str, timeout_ms: int = 10000):
    """_first_visible 的有界等待版：候选 selector 逐个等可见，命中即返回。

    用于慢渲染入口（如实测奇安信 portal 游客页「登录」链接 ~7s 才出现，
    零等待查询会与渲染竞态导致漏点）；每个候选均分等待预算。
    """
    sels = _split_selectors(selector)
    if not sels:
        return None
    per = max(1000, timeout_ms // len(sels))
    for sel in sels:
        try:
            await page.wait_for_selector(sel, timeout=per, state="visible")
            return page.locator(sel).first
        except Exception:
            continue
    return None


async def captcha_visible(page, login_cfg: Dict) -> Optional[str]:
    """返回可见的验证码 selector；无则 None。"""
    marker = login_cfg.get("captcha_marker") or ""
    for sel in _split_selectors(marker):
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                return sel
        except Exception:
            pass
    return None


async def auth_failure_visible(page, login_cfg: Dict) -> Optional[str]:
    """返回可见的认证失败提示 selector；无则 None。"""
    marker = login_cfg.get("auth_fail_marker") or ""
    for sel in _split_selectors(marker):
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                return sel
        except Exception:
            pass
    return None


async def is_logged_in(page, login_cfg: Dict) -> bool:
    """登录态判定：SSO 页 → False；URL 模式不匹配 → False；
    有 success_marker 看 marker；有 entry_button 看按钮是否消失；
    两者都未配 → URL 即证据（适用于数据面探测：匿名必被弹回 SSO，
    未被弹回且落在 success_url_pattern 上即视为会话有效）。"""
    sso = login_cfg.get("sso_url_pattern") or ""
    if sso and glob_match(page.url, sso):
        return False
    url_pat = login_cfg.get("success_url_pattern") or ""
    if url_pat and not re.search(url_pat, page.url):
        return False
    marker = login_cfg.get("success_marker") or ""
    if marker:
        return await _first_visible(page, marker) is not None
    entry = login_cfg.get("entry_button") or ""
    if entry:
        # 假阳性修正：页面刚 domcontentloaded 时登录按钮可能尚未渲染
        # （实测匿名首页按钮 5s+ 才出现），立即判「按钮消失=已登录」会误判；
        # 先给每个候选 selector 一段渲染窗口（已登录会话每次探测多等这几秒，可接受）
        for sel in _split_selectors(entry):
            try:
                await page.wait_for_selector(sel, timeout=4000, state="visible")
                break  # 按钮出现了 → 未登录，无需再等
            except Exception:
                continue
        return await _first_visible(page, entry) is None
    # URL 即证据模式：success_url_pattern 已通过校验
    return bool(url_pat)


# ─── 数据面探测（manual_login 子进程 / Cookie 导入共用）───────────────────────


def load_login_cfg(platform: str) -> Dict:
    """读 selector yml 的 login 块；文件缺失或未配 login.url 抛 ValueError。"""
    import yaml

    path = Path(__file__).resolve().parents[1] / "selectors" / f"{platform}.yml"
    if not path.exists():
        raise ValueError(f"selector 文件不存在: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    login = data.get("login") or {}
    if not login.get("url"):
        raise ValueError(f"{path} 未配置 login.url")
    return login


async def verify_data_plane(context, login_cfg: Dict) -> bool:
    """开新页访问 login.probe_url（数据面地址），未被弹回 SSO 且未被 WAF 拦截即真登录。

    判定口径（2026-07-25 修正，防首页假阳性）：以数据面地址实测为准——
    匿名访问必被弹回 SSO，未被弹回且 is_logged_in 成立才算会话有效。
    """
    probe = login_cfg.get("probe_url") or login_cfg["url"]
    sso = login_cfg.get("sso_url_pattern") or ""
    page = await context.new_page()
    try:
        resp = await page.goto(probe, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(6.0)  # 等 SPA 重定向链走完
        if resp is not None and resp.status >= 400:
            logger.warning("数据面返回 HTTP %s（WAF 拦截）", resp.status)
            return False
        if sso and glob_match(page.url, sso):
            return False
        return await is_logged_in(page, login_cfg)
    except Exception as exc:  # noqa: BLE001 — 探测异常一律按未通过处理
        logger.warning("数据面实测异常（按未通过处理）: %s", exc)
        return False
    finally:
        try:
            await page.close()
        except Exception:
            pass


def _need_manual(platform: str, captcha_sel: str) -> TiAdapterError:
    return TiAdapterError(
        "need_manual",
        f"[{platform}] 出现验证码/滑块（{captcha_sel}），自动求解未通过；"
        f"请人工首登：.venv/bin/python -m api.ti_expand.recon.manual_login {platform}",
    )


async def _try_solver(page, platform: str, solver_cfg: Optional[Tuple[bool, int]]) -> bool:
    """命中验证码时尝试自动过滑块。solver_cfg=(enabled, max_attempts)，None 用缺省。"""
    enabled, attempts = solver_cfg if solver_cfg else (True, DEFAULT_SOLVER_ATTEMPTS)
    if not enabled:
        return False
    try:
        return await solve_geetest_slide(page, platform=platform, max_attempts=attempts)
    except Exception as exc:  # noqa: BLE001 — 求解器异常不阻断原有兜底
        logger.warning("[%s] 滑块自动求解异常: %s", platform, exc)
        return False


async def _type_like_human(loc, text: str) -> None:
    await loc.click()
    await asyncio.sleep(random.uniform(0.5, 1.2))
    await loc.press_sequentially(text, delay=random.randint(70, 140))


async def _tick_checkbox(page, selector: str) -> None:
    """勾选协议复选框：原生 input 看 checked，包装 label 直接点击。"""
    loc = await _first_visible(page, selector)
    if loc is None:
        return
    try:
        if await loc.evaluate("el => el.tagName === 'INPUT'") and not await loc.is_checked():
            await loc.click()
        elif not await loc.evaluate("el => el.tagName === 'INPUT'"):
            await loc.click()
    except Exception as exc:
        logger.warning("勾选协议复选框失败（继续提交流程）: %s", exc)


async def auto_login(page, platform: str, login_cfg: Dict,
                     username: str, password: str,
                     solver_cfg: Optional[Tuple[bool, int]] = None) -> None:
    """在当前页面上执行自动登录。成功返回；失败抛 TiAdapterError。"""
    # 关闭首页公告/弹窗（如微步 AntD modal）
    dismiss = login_cfg.get("dismiss_modal") or ""
    if dismiss:
        loc = await _first_visible(page, dismiss)
        if loc is not None:
            try:
                await loc.click()
                await asyncio.sleep(random.uniform(1.0, 2.0))
            except Exception:
                pass

    # 登录入口 → SSO（如微步 passport；奇安信 r8 起 portal 游客页点「登录」链接）
    # 有界等待：入口可能慢渲染（奇安信 portal 实测 ~7s），零等待查询会竞态漏点
    entry = login_cfg.get("entry_button") or ""
    if entry:
        loc = await _wait_first_visible(page, entry)
        if loc is not None:
            await loc.click()
            sso = login_cfg.get("sso_url_pattern") or ""
            if sso:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and not glob_match(page.url, sso):
                    await asyncio.sleep(0.5)
            await asyncio.sleep(random.uniform(2.0, 4.0))

    # 默认扫码 tab 的平台（微步）需切到密码登录
    tab_text = login_cfg.get("password_tab_text") or ""
    if tab_text:
        try:
            await page.get_by_text(tab_text, exact=True).first.click()
            await asyncio.sleep(random.uniform(1.5, 3.0))
        except Exception as exc:
            raise TiAdapterError("selector_broken",
                                 f"[{platform}] 密码登录 tab（{tab_text}）不可点: {exc}")

    hit = await captcha_visible(page, login_cfg)
    if hit:
        if await _try_solver(page, platform, solver_cfg):
            logger.info("[%s] 提交前滑块已自动通过", platform)
        else:
            raise _need_manual(platform, hit)

    # SSO 页为慢渲染 SPA（奇安信实测 ~8-12s 才出表单），零等待查询会竞态误判表单缺失
    user_loc = await _wait_first_visible(page, login_cfg.get("username_input") or "", timeout_ms=15000)
    pwd_loc = await _wait_first_visible(page, login_cfg.get("password_input") or "", timeout_ms=15000)
    if user_loc is None or pwd_loc is None:
        raise TiAdapterError(
            "selector_broken",
            f"[{platform}] 登录表单定位失败（username_input/password_input），"
            f"当前页 {page.url}",
        )
    await _type_like_human(user_loc, username)
    await asyncio.sleep(random.uniform(1.5, 3.0))
    await _type_like_human(pwd_loc, password)
    await asyncio.sleep(random.uniform(1.0, 2.5))

    agreement = login_cfg.get("agreement_checkbox") or ""
    if agreement:
        await _tick_checkbox(page, agreement)
        await asyncio.sleep(random.uniform(1.0, 2.0))

    submit = await _first_visible(page, login_cfg.get("submit_button") or "")
    if submit is None:
        raise TiAdapterError("selector_broken",
                             f"[{platform}] 登录提交按钮定位失败（submit_button）")
    await submit.click()
    await asyncio.sleep(random.uniform(1.5, 3.0))

    # 协议二次确认弹窗（微步：勾选协议后点登录仍弹「同意并继续」，确认后才出滑块）
    confirm_text = login_cfg.get("agreement_confirm_text") or ""
    if confirm_text:
        try:
            btn = page.get_by_text(confirm_text, exact=True).first
            if await btn.count() and await btn.is_visible():
                await btn.click()
                await asyncio.sleep(random.uniform(1.5, 3.0))
        except Exception:
            pass

    deadline = time.monotonic() + LOGIN_WAIT_TIMEOUT_S
    solver_runs = 0
    while time.monotonic() < deadline:
        await asyncio.sleep(1.5)
        hit = await captcha_visible(page, login_cfg)
        if hit:
            solver_runs += 1
            if solver_runs <= MAX_SOLVER_RUNS_PER_LOGIN and \
                    await _try_solver(page, platform, solver_cfg):
                continue  # 滑块已过，继续等登录完成
            raise _need_manual(platform, hit)
        fail = await auth_failure_visible(page, login_cfg)
        if fail:
            raise TiAdapterError(
                "auth_failed",
                f"[{platform}] 平台提示认证失败（{fail}），请检查凭据；不自动重试",
            )
        if await is_logged_in(page, login_cfg):
            return
    raise TiAdapterError(
        "query_failed",
        f"[{platform}] 登录提交后 {LOGIN_WAIT_TIMEOUT_S:.0f}s 内未确认成功，"
        f"当前页 {page.url}",
    )


async def _refresh_seed_if_stale(browser, platform: str, page) -> None:
    """会话有效且 storage_state 种子超过 SEED_REFRESH_AGE_S 未更新时，
    把当前 context 的会话回写种子文件（尽力而为，失败仅告警）。

    种子是 reset_session 清 profile 后唯一的会话来源：长期靠 persistent
    profile 运行的平台种子会过期，届时清空 profile 就被迫人工重登。
    """
    seed = getattr(browser, "storage_state_seed", None)
    if seed is None:
        return
    try:
        if seed.exists() and \
                time.time() - seed.stat().st_mtime < SEED_REFRESH_AGE_S:
            return
        seed.parent.mkdir(parents=True, exist_ok=True)
        await page.context.storage_state(path=str(seed))
        logger.info("[%s] storage_state 种子已回写刷新", platform)
    except Exception as exc:  # noqa: BLE001 — 刷新失败不影响主流程
        logger.warning("[%s] 回写 storage_state 种子失败: %s", platform, exc)


async def ensure_session(browser, platform: str, selectors: Dict,
                         username: str, password: str, page,
                         solver_cfg: Optional[Tuple[bool, int]] = None):
    """在传入页面上完成登录态探测 + 自动登录。未配置 login 块的平台视为免登录。

    page 由调用方创建并持有：自动登录失败抛错时，调用方仍可用它留证。
    solver_cfg=(enabled, max_attempts)：滑块自动求解开关与次数，None 用缺省。
    """
    login_cfg = selectors.get("login") or {}
    if not login_cfg.get("url"):
        return page

    probe = login_cfg.get("probe_url") or login_cfg["url"]
    resp = await page.goto(probe, wait_until="domcontentloaded", timeout=45000)
    await asyncio.sleep(random.uniform(2.0, 4.0))

    if resp is not None and resp.status >= 400:
        raise TiAdapterError(
            "query_failed",
            f"[{platform}] 探测页被平台拒绝（HTTP {resp.status}），疑似 WAF 风控拦截，"
            f"请检查指纹/代理或稍后重试",
        )

    hit = await captcha_visible(page, login_cfg)
    if hit:
        if not await _try_solver(page, platform, solver_cfg):
            raise _need_manual(platform, hit)
    # success_marker 渲染可能滞后（实测奇安信 portal 需 8s+）：先给一段渲染窗口
    # 再判定，避免「会话有效但页面慢」被误判为失效而触发不必要的自动登录
    sso_pat = login_cfg.get("sso_url_pattern") or ""
    if not (sso_pat and glob_match(page.url, sso_pat)):
        for sel in _split_selectors(login_cfg.get("success_marker") or ""):
            try:
                await page.wait_for_selector(sel, timeout=8000, state="visible")
                break
            except Exception:
                continue
    if await is_logged_in(page, login_cfg):
        logger.info("[%s] 会话有效，免登录", platform)
        await _refresh_seed_if_stale(browser, platform, page)
        return page

    if not (username and password):
        raise TiAdapterError(
            "auth_failed",
            f"[{platform}] 未配置凭据（TI_{platform.upper()}_USERNAME/PASSWORD "
            f"或设置页），无法自动登录",
        )
    logger.info("[%s] 会话失效，尝试自动登录", platform)
    await auto_login(page, platform, login_cfg, username, password,
                     solver_cfg=solver_cfg)

    # 登录成功：回写 storage_state 种子（与人工首登产物同位，互为备份）
    try:
        browser.storage_state_seed.parent.mkdir(parents=True, exist_ok=True)
        await page.context.storage_state(path=str(browser.storage_state_seed))
        logger.info("[%s] 登录成功，已回写 storage_state 种子", platform)
    except Exception as exc:
        logger.warning("[%s] 回写 storage_state 失败: %s", platform, exc)
    return page
