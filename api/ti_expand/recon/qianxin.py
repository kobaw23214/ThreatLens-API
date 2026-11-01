"""奇安信（ti.qianxin.com）P0 采集预研脚本。

用法：
    PLAYWRIGHT_BROWSERS_PATH=ti_data/browsers .venv/bin/python -m api.ti_expand.recon.qianxin

产出（ti_data/recon/qianxin/）：
    *.png               各阶段截图（登录页 / 提交后 / 查询结果页 / 风控页）
    xhr_log.json        全程 XHR/fetch 请求清单（按阶段标注，JSON 响应带 body 文件名）
    xhr_bodies/*.json   JSON 响应体样本（截断至 200KB）
    dom_*.html          查询页主体 DOM 片段（截断至 300KB）
    outline_*.txt       页面区块标题/容器结构清单（辅助填写 sections）
    storage_state.json  登录会话快照（cookies/localStorage，供复用验证）
    findings.json       结构化预研结论

安全红线：本脚本绝不打印、落盘密码明文；截图中密码框为掩码圆点。
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

_ROOT = Path(__file__).resolve().parents[3]  # ThreatLens-API/
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(_ROOT / "ti_data" / "browsers"))

from dotenv import load_dotenv  # noqa: E402
from playwright.sync_api import TimeoutError as PWTimeout  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

load_dotenv(_ROOT / "api" / ".env")

OUT = _ROOT / "ti_data" / "recon" / "qianxin"
(OUT / "xhr_bodies").mkdir(parents=True, exist_ok=True)

USERNAME = os.environ.get("TI_QIANXIN_USERNAME", "").strip()
PASSWORD = os.environ.get("TI_QIANXIN_PASSWORD", "")
BASE = "https://ti.qianxin.com"

# 验证码/滑块常见特征（canvas 单独列出仅为线索，需人工看截图确认）
CAPTCHA_SELECTORS = [
    "[class*=geetest]", "[id*=geetest]", "[src*=geetest]",
    "[class*=captcha]", "[id*=captcha]", "img[src*=captcha]",
    "iframe[src*=captcha]", "iframe[src*=verify]",
    "[class*=slider-verify]", "[class*=slide-verify]",
    "[class*=nc-container]", "[id*=nc_]",  # 阿里滑块
    "[class*=verify-img]", "[class*=verifybox]",
    "canvas",
]

LOGIN_SUCCESS_SELECTORS = [
    "[class*=avatar]", "[class*=user-name]", "[class*=userName]",
    "[class*=user-info]", "[class*=userInfo]", "[class*=logout]",
]

SEARCH_INPUT_CANDIDATES = [
    'input[placeholder*="IP"]', 'input[placeholder*="搜索"]',
    'input[placeholder*="请输入"]', 'input[placeholder*="域名"]',
    'input[type="search"]', "input[class*=search]", "input[id*=search]",
    ".search input", "[class*=search] input",
]

JS_HELPERS = """
const cssPath = (el) => {
  let p = [];
  while (el && el.nodeType === 1 && p.length < 7) {
    let s = el.tagName.toLowerCase();
    if (el.id) { s += '#' + el.id; p.unshift(s); break; }
    if (el.className && typeof el.className === 'string') {
      const c = el.className.trim().split(/\\s+/).filter(x => x && !/^(active|hover|focus)/.test(x)).slice(0, 2);
      if (c.length) s += '.' + c.join('.');
    }
    p.unshift(s); el = el.parentElement;
  }
  return p.join(' > ');
};
const vis = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
"""

JS_ANALYZE_FORM = JS_HELPERS + """
const inputs = [...document.querySelectorAll('input')].filter(vis).map(el => ({
  type: el.type, name: el.name || '', id: el.id || '',
  placeholder: el.placeholder || '', selector: cssPath(el),
}));
const buttons = [...document.querySelectorAll('button, input[type=submit], a, [role=button], span, div')]
  .filter(vis)
  .filter(el => /^(登录|登 录|登录\\/注册|Login|Log in|Sign in|提交)$/.test(((el.textContent || '').trim())))
  .slice(0, 10)
  .map(el => ({tag: el.tagName.toLowerCase(), text: (el.textContent || '').trim(), selector: cssPath(el)}));
return {url: location.href, title: document.title, inputs, buttons};
"""

JS_OUTLINE = JS_HELPERS + """
const els = [...document.querySelectorAll(
  'h1,h2,h3,h4,h5,[class*=title],[class*=Title],[class*=header],[class*=card-name],[class*=section]'
)];
const seen = new Set(); const out = [];
for (const el of els) {
  if (!vis(el)) continue;
  const t = (el.textContent || '').trim().replace(/\\s+/g, ' ');
  if (!t || t.length > 60) continue;
  const p = cssPath(el);
  const key = p + '|' + t;
  if (seen.has(key)) continue; seen.add(key);
  const box = el.closest('[class*=card],[class*=section],[class*=panel],[class*=block],[class*=item]') || el.parentElement;
  let fmt = 'text';
  if (box) {
    if (box.querySelector('table, [class*=table], [class*=Table]')) fmt = 'table';
    else {
      const txt = (box.innerText || '');
      if ((txt.match(/[:：]/g) || []).length >= 3 && txt.length < 1200) fmt = 'kv';
    }
  }
  out.push({text: t, selector: p, tag: el.tagName.toLowerCase(), fmt_hint: fmt});
  if (out.length >= 150) break;
}
return out;
"""

findings: dict = {
    "platform": "qianxin",
    "base": BASE,
    "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    "credential_configured": bool(USERNAME and PASSWORD),
    "errors": [],
}
xhr_log: list = []
state = {"phase": "init", "idx": 0}


def log(msg: str) -> None:
    print(f"[recon] {msg}", flush=True)


def save_body(resp, idx: int):
    try:
        body = resp.body()
    except Exception:
        return None
    if len(body) > 200_000:
        body = body[:200_000]
    path = urlparse(resp.url).path.strip("/").replace("/", "_") or "root"
    fname = f"{idx:03d}_{re.sub(r'[^A-Za-z0-9_.-]', '_', path)[:80]}.json"
    (OUT / "xhr_bodies" / fname).write_bytes(body)
    return f"xhr_bodies/{fname}"


def on_response(resp) -> None:
    try:
        req = resp.request
        if req.resource_type not in ("xhr", "fetch"):
            return
        ctype = resp.headers.get("content-type", "")
        entry = {
            "phase": state["phase"],
            "method": req.method,
            "url": resp.url,
            "status": resp.status,
            "resource_type": req.resource_type,
            "content_type": ctype.split(";")[0],
        }
        if "json" in ctype:
            state["idx"] += 1
            entry["body_file"] = save_body(resp, state["idx"])
        xhr_log.append(entry)
    except Exception:
        pass


def shot(page, name: str) -> None:
    try:
        page.screenshot(path=str(OUT / name), full_page=False)
        log(f"截图 {name}")
    except Exception as exc:
        findings["errors"].append(f"screenshot {name}: {exc}")


def nap(lo=2.0, hi=5.0) -> None:
    time.sleep(random.uniform(lo, hi))


def detect_captcha(page) -> list:
    hits = []
    for sel in CAPTCHA_SELECTORS:
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 3)):
                if loc.nth(i).is_visible():
                    hits.append(sel)
                    break
        except Exception:
            pass
    return hits


def first_visible(page, selectors: list):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                return sel, loc
        except Exception:
            pass
    return None, None


def dump_dom_and_outline(page, tag: str) -> None:
    try:
        html = page.locator("body").inner_html()
        (OUT / f"dom_{tag}.html").write_text(html[:300_000], encoding="utf-8")
        log(f"DOM 片段 dom_{tag}.html ({len(html)} chars, 截断 300KB)")
    except Exception as exc:
        findings["errors"].append(f"dom {tag}: {exc}")
    try:
        outline = page.evaluate(f"() => {{ {JS_OUTLINE} }}")
        lines = [f"{o['fmt_hint']:6s} {o['tag']:8s} {o['text'][:40]:42s} {o['selector']}" for o in outline]
        (OUT / f"outline_{tag}.txt").write_text("\n".join(lines), encoding="utf-8")
        log(f"区块清单 outline_{tag}.txt ({len(outline)} 条)")
    except Exception as exc:
        findings["errors"].append(f"outline {tag}: {exc}")


def check_login_state(page) -> dict:
    hits = []
    for sel in LOGIN_SUCCESS_SELECTORS:
        try:
            if page.locator(sel).first.count() and page.locator(sel).first.is_visible():
                hits.append(sel)
        except Exception:
            pass
    api_status = None
    try:
        resp = page.context.request.get(
            "https://ti.qianxin.com/alpha-api/v2/user/info-detail?lang=zh-CN")
        api_status = resp.json().get("status")
    except Exception:
        pass
    return {
        "url": page.url,
        "markers": hits,
        "cookie_count": len(page.context.cookies()),
        "user_api_status": api_status,  # 10102=non-login，200=已登录
        "logged_in": (api_status == 200)
        or (bool(hits) and "login" not in page.url.lower()),
    }


def do_query(page, seed: str, tag: str, new_pages: list) -> dict:
    """在首页/当前页搜索框查询种子，记录落地 URL、XHR、DOM 与截图。

    奇安信搜索结果可能新开 Tab（window.open），通过 new_pages 捕获。
    """
    result = {"seed": seed}
    state["phase"] = f"query_{tag}"
    try:
        sel, loc = first_visible(page, SEARCH_INPUT_CANDIDATES)
        if not loc:
            result["error"] = "未找到搜索输入框"
            return result
        result["search_input"] = sel
        nap()
        loc.click()
        loc.fill("")  # 结果页搜索框会残留上次关键词，先清空
        loc.type(seed, delay=random.randint(60, 130))
        nap(1.0, 2.0)
        shot(page, f"query_{tag}_before_enter.png")

        before_url = page.url
        n_popups_before = len(new_pages)
        loc.press("Enter")
        nap(4.0, 6.0)
        # Enter 无效时点击搜索图标（放大镜）
        if page.url == before_url and len(new_pages) == n_popups_before:
            icon_sel, icon_loc = first_visible(page, [
                ".search-box [class*=search-btn]", ".search-box [class*=btn]",
                ".integrated-search [class*=suffix] i",
                ".integrated-search [class*=suffix] svg",
                ".search-box i", ".search-box svg", ".search-box img",
                ".integrated-search [class*=icon]",
            ])
            if icon_loc:
                result["search_button"] = icon_sel
                icons = page.locator(icon_sel)
                # 有多个图标（上传样本云图标 + 放大镜）时点最后一个
                icons.nth(max(icons.count() - 1, 0)).click()
                nap(4.0, 6.0)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except PWTimeout:
            pass

        # 优先使用新开的 Tab
        if len(new_pages) > n_popups_before:
            result["opened_in"] = "new_tab"
            page = new_pages[-1]
            page.bring_to_front()
            nap(3.0, 5.0)
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except PWTimeout:
                pass
        result["final_url"] = page.url
        result["title"] = page.title()
        shot(page, f"query_{tag}_result.png")
        dump_dom_and_outline(page, tag)
        cap = detect_captcha(page)
        if cap:
            result["captcha_hits"] = cap
        result["page"] = page  # 供调用方继续使用（不写入 findings）
    except Exception as exc:
        result["error"] = str(exc)[:300]
        findings["errors"].append(f"query {tag}: {exc}")
    return result


def main() -> int:
    query_only = "--query-only" in sys.argv  # 复用 storage_state，不再登录
    state_file = OUT / "storage_state.json"
    if not (USERNAME and PASSWORD) and not (query_only and state_file.exists()):
        log("未配置 TI_QIANXIN_USERNAME / TI_QIANXIN_PASSWORD，仅做匿名页面结构预研")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx_kwargs = dict(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        if query_only and state_file.exists():
            ctx_kwargs["storage_state"] = str(state_file)
            log("query-only 模式：复用 storage_state.json，跳过登录")
        ctx = browser.new_context(**ctx_kwargs)
        page = ctx.new_page()
        page.on("response", on_response)

        # 搜索结果可能新开 Tab：捕获并同样挂 response 监听
        new_pages: list = []

        def _on_new_page(p) -> None:
            p.on("response", on_response)
            new_pages.append(p)

        ctx.on("page", _on_new_page)

        # ── a. 打开首页/登录页，分析表单 ─────────────────────────────
        state["phase"] = "home"
        log(f"打开 {BASE} ...")
        page.goto(BASE, wait_until="domcontentloaded", timeout=45000)
        nap(3.0, 5.0)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except PWTimeout:
            pass
        findings["landing"] = {"url": page.url, "title": page.title()}
        log(f"落地页: {page.url} | {page.title()}")
        shot(page, "01_landing.png")

        form = page.evaluate(f"() => {{ {JS_ANALYZE_FORM} }}")
        findings["landing_form"] = form
        log(f"可见输入框 {len(form['inputs'])} 个，登录按钮候选 {len(form['buttons'])} 个")

        # 若首页无密码框，尝试点击“登录”入口打开登录页/弹窗
        pwd_sel, pwd_loc = first_visible(page, ['input[type="password"]'])
        if not pwd_loc and not query_only:
            btn_sel, btn_loc = first_visible(page, [
                'a:has-text("登录")', 'button:has-text("登录")',
                'span:has-text("登录")', '[class*=login]',
            ])
            if btn_loc:
                log(f"首页无密码框，点击登录入口: {btn_sel}")
                btn_loc.click()
                nap(2.0, 4.0)
                findings["login_page"] = {"url": page.url, "title": page.title()}
                shot(page, "02_login_page.png")
                form2 = page.evaluate(f"() => {{ {JS_ANALYZE_FORM} }}")
                findings["login_form"] = form2
                log(f"登录页: {page.url} | 输入框 {len(form2['inputs'])} 个")

        # 记录输入框/按钮特征（不含任何值）
        pwd_sel, pwd_loc = first_visible(page, [
            'input[placeholder*="密码"]', 'input[type="password"]',
        ])
        user_sel, user_loc = first_visible(page, [
            'input[placeholder*="手机号或邮箱"]',
            'input[name*=user i]', 'input[name*=account i]', 'input[name*=phone i]',
            'input[placeholder*="账号"]', 'input[placeholder*="用户名"]',
            'input[placeholder*="手机"]', 'input[placeholder*="邮箱"]',
            'form input[type="text"]', 'form input:not([type])',
        ])
        cap_before = detect_captcha(page)
        findings["captcha_before_login"] = cap_before
        if cap_before:
            log(f"登录前检出验证码特征: {cap_before}（canvas 类需看截图人工确认）")

        # ── b. 自动登录尝试（最多 1 次；遇验证码停止）─────────────────
        findings["login_attempt"] = {"attempted": False, "success": False}
        captcha_blocking = [s for s in cap_before if s != "canvas"]
        if query_only:
            findings["login_attempt"] = {
                "attempted": False, "success": True,
                "note": "query-only 模式复用 storage_state，未发起登录请求",
            }
        elif not (USERNAME and PASSWORD):
            log("无凭据，跳过登录尝试")
        elif not (pwd_loc and user_loc):
            log("未定位到用户名/密码输入框，跳过登录尝试（可能仅扫码登录，见截图）")
        elif captcha_blocking:
            log("登录表单已存在验证码元素，为避免风控不尝试自动登录")
        else:
            log(f"定位输入框 user=`{user_sel}` pwd=`{pwd_sel}`，开始拟人登录（1 次尝试）")
            findings["login_attempt"] = {
                "attempted": True,
                "success": False,
                "username_input": user_sel,
                "password_input": pwd_sel,
            }
            try:
                user_loc.click()
                nap(0.8, 1.5)
                user_loc.type(USERNAME, delay=random.randint(70, 140))
                nap(2.0, 3.5)
                pwd_loc.click()
                nap(0.8, 1.5)
                pwd_loc.type(PASSWORD, delay=random.randint(70, 140))
                nap(2.0, 4.0)
                # 勾选《用户许可协议》《隐私政策》（首轮失败原因：未勾选会被拦截提交）
                cb_sel, cb_loc = first_visible(page, [
                    "label.q-checkbox", "[class*=agreement] label",
                    "input.q-checkbox__original", "[class*=agreement] [class*=checkbox]",
                ])
                if cb_loc:
                    cb_loc.click()
                    nap(1.0, 2.0)
                    findings["login_attempt"]["agreement_checkbox"] = cb_sel
                shot(page, "03_login_filled.png")  # 密码为掩码圆点
                submit_sel, submit_loc = first_visible(page, [
                    'button:has-text("登录")', 'button[type="submit"]',
                    'input[type="submit"]', '[class*=submit]',
                    'button:has-text("登 录")',
                ])
                findings["login_attempt"]["submit_button"] = submit_sel
                if submit_loc:
                    submit_loc.click()
                else:
                    pwd_loc.press("Enter")
                # 登录成功会跳回 next 参数指定的 ti.qianxin.com 地址
                try:
                    page.wait_for_url(re.compile(r"^https://ti\.qianxin\.com/"), timeout=20000)
                except PWTimeout:
                    pass
                nap(3.0, 5.0)
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except PWTimeout:
                    pass
                shot(page, "04_after_submit.png")
                cap_after = detect_captcha(page)
                findings["captcha_after_submit"] = cap_after
                st = check_login_state(page)
                findings["login_attempt"].update(st)
                if st["logged_in"]:
                    log(f"登录成功，标志: {st['markers']}")
                    findings["login_attempt"]["success"] = True
                    ctx.storage_state(path=str(OUT / "storage_state.json"))
                    log("已保存 storage_state.json")
                else:
                    log(f"登录未确认成功（url={st['url']} markers={st['markers']} captcha={cap_after}）")
            except Exception as exc:
                findings["errors"].append(f"login: {exc}")
                log(f"登录流程异常: {exc}")

        # ── c. 会话复用验证（新 context + storage_state）──────────────
        if findings["login_attempt"].get("success"):
            try:
                ctx2 = browser.new_context(
                    viewport={"width": 1440, "height": 900},
                    storage_state=str(OUT / "storage_state.json"),
                )
                p2 = ctx2.new_page()
                p2.goto(BASE, wait_until="domcontentloaded", timeout=45000)
                nap(3.0, 5.0)
                st2 = check_login_state(p2)
                findings["session_reuse"] = st2
                log(f"会话复用验证: logged_in={st2['logged_in']} markers={st2['markers']}")
                shot(p2, "05_session_reuse.png")
                ctx2.close()
            except Exception as exc:
                findings["errors"].append(f"session_reuse: {exc}")

        # ── d. 查询预研：IP 8.8.8.8 / 域名 example.com ───────────────
        logged_in = findings["login_attempt"].get("success")
        blocked = bool(findings.get("captcha_after_submit")) and not logged_in
        findings["queries"] = {}
        if blocked:
            log("疑似被验证码/风控拦截，跳过查询阶段（账号安全优先）")
        else:
            # 回到平台首页再查（结果页通常也带搜索框，统一从首页发起）
            state["phase"] = "before_query"
            try:
                if BASE not in page.url:
                    page.goto(BASE, wait_until="domcontentloaded", timeout=45000)
                    nap(3.0, 5.0)
            except Exception as exc:
                findings["errors"].append(f"goto home for query: {exc}")
            for seed, tag in (("8.8.8.8", "ip"), ("example.com", "domain")):
                log(f"查询 {tag}: {seed}")
                r = do_query(page, seed, tag, new_pages)
                findings["queries"][tag] = {k: v for k, v in r.items() if k != "page"}
                if r.get("page") is not None:
                    page = r["page"]
                # 下一次查询前若当前页无搜索框，回首页
                if tag == "ip":
                    try:
                        sel, _ = first_visible(page, SEARCH_INPUT_CANDIDATES)
                        if not sel:
                            page.goto(BASE, wait_until="domcontentloaded", timeout=45000)
                            nap(3.0, 5.0)
                    except Exception:
                        pass

        # ── e. 汇总 ─────────────────────────────────────────────────
        (OUT / "xhr_log.json").write_text(
            json.dumps(xhr_log, ensure_ascii=False, indent=1), encoding="utf-8")
        findings["xhr_summary"] = {
            "total": len(xhr_log),
            "json_count": sum(1 for x in xhr_log if "body_file" in x),
        }
        findings["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        (OUT / "findings.json").write_text(
            json.dumps(findings, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"完成。XHR {findings['xhr_summary']}，产物目录 {OUT}")

        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
