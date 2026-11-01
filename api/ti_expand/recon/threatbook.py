"""微步在线（x.threatbook.com）采集预研脚本（P0）。

运行方式（项目根目录）：
    PLAYWRIGHT_BROWSERS_PATH=$PWD/ti_data/browsers \\
        .venv/bin/python -m api.ti_expand.recon.threatbook

产物目录：ti_data/recon/threatbook/（ti_data/ 已 gitignore）

站点结构（2026-07-24 第一轮预研确认）：
  - 整站 Vue SPA，无独立 /login 路由（/login → v5/notFound）
  - 登录入口 = 首页头部红色「登录」按钮，点击弹登录对话框
  - 查询入口 = 头部搜索框，输入种子回车后 SPA 跳转详情页

安全红线：
  - 绝不打印/落盘密码明文（日志里用户名亦打码）
  - 登录提交最多 1 次；遇验证码/滑块立即停止并截图
  - 全程 headless；登录阶段的 XHR 不做响应体抓取（避免凭据落盘）
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
import time
from pathlib import Path

# 浏览器在项目内，import playwright 前设好路径
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    "/Users/wangy/Documents/ThreatLens-API/ti_data/browsers",
)

from dotenv import load_dotenv  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]  # ThreatLens-API/
ENV_PATH = ROOT / "api" / ".env"
OUT_DIR = ROOT / "ti_data" / "recon" / "threatbook"

PLATFORM = "threatbook"
HOME_URL = "https://x.threatbook.com/"
LOGIN_BTN = "button.x-comp-btn-red"  # 头部红色「登录」按钮（首轮确认）
SEARCH_INPUT = "input[placeholder*='搜索']"  # 头部搜索框

# 查询目标：官方 apiDocs permalink 确认的详情页 URL 模式
# （file 哈希为 EICAR 标准测试样本，无害；MD5 形式已验证 404，改用 SHA256 探）
TARGETS = [
    ("ip", "8.8.8.8", "https://x.threatbook.com/v5/ip/8.8.8.8"),
    ("domain", "example.com", "https://x.threatbook.com/v5/domain/example.com"),
    ("file", "275a021bbfb6489e54b471899f7db9d1663fc695ec2fe2a2c4538aabf651fd0f",
     "https://x.threatbook.com/v5/file/275a021bbfb6489e54b471899f7db9d1663fc695ec2fe2a2c4538aabf651fd0f"),
]

# 验证码/滑块常见特征
CAPTCHA_SELECTORS = [
    "[class*='geetest']", "[id*='geetest']", "[src*='geetest']",
    "iframe[src*='captcha']", "iframe[src*='verify']", "iframe[src*='geetest']",
    "[class*='captcha']", "[id*='captcha']", "img[src*='captcha']",
    "[class*='slider-verify']", "[class*='verify-img']", "[class*='verifycode']",
    "[id*='nocaptcha']", "[class*='nc_wrapper']",  # 阿里系滑块
    "canvas[class*='verify']", "canvas[class*='captcha']",
]

# 登录成功标志（登录后才出现的用户态元素候选）
SUCCESS_SELECTORS = [
    "[class*='avatar']", "[class*='user-name']", "[class*='userName']",
    "[class*='user-info']", "[class*='userInfo']", "[class*='logout']",
]

_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
"""

# 页面表单/可点元素结构 dump
JS_FORM_DUMP = r"""
() => {
  const visible = el => !!(el.offsetWidth || el.offsetHeight);
  const clsOf = el => (typeof el.className === 'string' ? el.className : '').slice(0, 120);
  const selOf = el => {
    if (el.id) return '#' + CSS.escape(el.id);
    const name = el.getAttribute('name');
    if (name) return el.tagName.toLowerCase() + '[name="' + name + '"]';
    const ph = el.getAttribute('placeholder');
    if (ph) return el.tagName.toLowerCase() + '[placeholder*="' + ph.slice(0, 8) + '"]';
    const cls = clsOf(el).trim().split(/\s+/).filter(c => !/^[a-z0-9]{6,}$/i.test(c)).slice(0, 2);
    return el.tagName.toLowerCase() + (cls.length ? '.' + cls.join('.') : '');
  };
  return {
    url: location.href,
    title: document.title,
    inputs: [...document.querySelectorAll('input')].map(el => ({
      selector: selOf(el), type: el.type || 'text', name: el.name || '',
      id: el.id || '', placeholder: el.placeholder || '', class: clsOf(el),
      visible: visible(el),
    })),
    buttons: [...document.querySelectorAll(
      'button, input[type="submit"], [class*="login-btn"], [class*="submit"]'
    )].map(el => ({
      selector: selOf(el), tag: el.tagName.toLowerCase(),
      text: (el.innerText || el.value || '').trim().slice(0, 30),
      class: clsOf(el), visible: visible(el),
    })),
    clickables: [...document.querySelectorAll(
      '[class*="tab"], [role="tab"], a, [class*="login-way"], [class*="login-tab"]'
    )].filter(visible).map(el => ({
      selector: selOf(el), tag: el.tagName.toLowerCase(),
      text: (el.innerText || '').trim().slice(0, 20),
      class: clsOf(el),
    })).filter(x => x.text),
    iframes: [...document.querySelectorAll('iframe')].map(f => (f.src || '').slice(0, 200)),
    dialogs: [...document.querySelectorAll(
      '[class*="dialog"], [class*="modal"], [role="dialog"]'
    )].filter(visible).map(el => ({ selector: selOf(el), class: clsOf(el) })),
  };
}
"""

# 详情页区块结构分析
JS_SECTION_ANALYSIS = r"""
() => {
  const txt = el => (el.innerText || '').replace(/\s+/g, ' ').trim();
  const clsOf = el => (typeof el.className === 'string' ? el.className : '');
  function cssPath(el) {
    const parts = [];
    let cur = el;
    while (cur && cur !== document.body && parts.length < 7) {
      let p = cur.tagName.toLowerCase();
      if (cur.id) { parts.unshift(p + '#' + CSS.escape(cur.id)); return parts.join(' > '); }
      const cls = clsOf(cur).trim().split(/\s+/)
        .filter(c => !/^[a-z0-9]{6,}$/i.test(c)).slice(0, 2);
      if (cls.length) p += '.' + cls.join('.');
      parts.unshift(p);
      cur = cur.parentElement;
    }
    return parts.join(' > ');
  }
  const headings = [...document.querySelectorAll(
    'h1,h2,h3,h4,h5,[class*="title" i],[class*="card-header" i],[class*="section-title" i]'
  )]
    .filter(el => el.offsetWidth || el.offsetHeight)
    .map(el => ({ el, t: txt(el) }))
    .filter(x => x.t && x.t.length <= 40);
  const seen = new Set();
  const sections = [];
  for (const h of headings) {
    const cont = h.el.closest(
      '[class*="card" i],[class*="panel" i],[class*="block" i],[class*="section" i],[class*="module" i]'
    ) || h.el.parentElement;
    if (!cont || seen.has(cont)) continue;
    seen.add(cont);
    const tables = cont.querySelectorAll(
      'table, [class*="el-table"], [class*="ant-table"], [class*="vxe-table"]');
    sections.push({
      heading: h.t,
      heading_selector: cssPath(h.el),
      container_selector: cssPath(cont),
      container_class: clsOf(cont).trim().slice(0, 120),
      has_table: tables.length > 0,
      table_count: tables.length,
      text_len: txt(cont).length,
      text_preview: txt(cont).slice(0, 200),
    });
  }
  return {
    url: location.href,
    title: document.title,
    body_text_len: txt(document.body).length,
    framework_hints: {
      element_ui: !!document.querySelector('[class*="el-"]'),
      antd: !!document.querySelector('[class*="ant-"]'),
      vue: !!document.querySelector('[data-v-]'),
    },
    sections,
  };
}
"""


def pause(lo: float = 2.0, hi: float = 5.0) -> None:
    """拟人停顿。"""
    time.sleep(random.uniform(lo, hi))


def mask(s: str) -> str:
    """日志打码（用于用户名）。"""
    if not s:
        return ""
    return (s[:2] + "***" + s[-2:]) if len(s) > 4 else s[:1] + "***"


def log(msg: str) -> None:
    print(f"[recon] {msg}", flush=True)


class XhrRecorder:
    """查询阶段的 XHR/fetch 监听：记录 URL 清单，JSON 响应体存盘。"""

    def __init__(self) -> None:
        self.entries: list[dict] = []
        self.current: str = "-"
        self._dir = OUT_DIR / "xhr"
        self._dir.mkdir(parents=True, exist_ok=True)

    def attach(self, page) -> None:
        page.on("response", self._on_response)

    def _on_response(self, resp) -> None:
        try:
            req = resp.request
            if req.resource_type not in ("xhr", "fetch"):
                return
            ctype = resp.headers.get("content-type", "") or ""
            entry = {
                "target": self.current,
                "method": req.method,
                "url": resp.url,
                "status": resp.status,
                "content_type": ctype.split(";")[0],
                "resource_type": req.resource_type,
            }
            if "json" in ctype:
                try:
                    body = resp.body()
                    entry["body_size"] = len(body)
                    fname = re.sub(r"[^A-Za-z0-9_.-]+", "_", resp.url.split("?")[0])[-130:]
                    fname = f"{self.current}__{fname}.json"
                    (self._dir / fname).write_bytes(body[:300_000])
                    entry["body_file"] = f"xhr/{fname}"
                    entry["body_preview"] = body[:240].decode("utf-8", "replace")
                except Exception as exc:  # 响应体可能已被浏览器丢弃
                    entry["body_error"] = str(exc)[:120]
            self.entries.append(entry)
        except Exception:
            pass


def find_captcha(page) -> list[str]:
    """返回页面上可见的验证码/滑块特征 selector 清单。"""
    hits = []
    for sel in CAPTCHA_SELECTORS:
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 3)):
                if loc.nth(i).is_visible():
                    hits.append(sel)
                    break
        except Exception:
            continue
    return hits


def dump_captcha_details(page) -> list[dict]:
    """记录验证码特征的详细证据（元素结构，不含任何凭据）。"""
    details = []
    for sel in CAPTCHA_SELECTORS:
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 3)):
                el = loc.nth(i)
                try:
                    details.append({
                        "selector": sel,
                        "visible": el.is_visible(),
                        "tag": el.evaluate("e => e.tagName.toLowerCase()"),
                        "class": (el.get_attribute("class") or "")[:120],
                        "id": (el.get_attribute("id") or "")[:60],
                        "text": (el.inner_text() or "")[:80],
                        "outer_html": el.evaluate(
                            "e => e.outerHTML.slice(0, 300)"),
                    })
                except Exception:
                    continue
        except Exception:
            continue
    return details


def pick_login_selectors(form: dict) -> dict:
    """从表单 dump 中挑选用户名/密码/提交按钮 selector。"""
    vis_inputs = [i for i in form["inputs"] if i["visible"]]
    pwd = next((i for i in vis_inputs if i["type"] == "password"), None)
    user = None
    text_like = [i for i in vis_inputs if i["type"] in ("text", "email", "tel", "")]
    for i in text_like:
        hint = (i["placeholder"] + i["name"] + i["id"]).lower()
        if any(k in hint for k in ("user", "mail", "账号", "邮", "手机", "phone", "account")):
            user = i
            break
    if user is None and text_like:
        user = text_like[0]
    submit = None
    for b in form["buttons"]:
        if not b["visible"]:
            continue
        if any(k in b["text"] for k in ("登录", "登 录", "登錄", "Sign in", "Log in")):
            submit = b
            break
    if submit is None:
        vis_btns = [b for b in form["buttons"] if b["visible"]]
        submit = vis_btns[0] if vis_btns else None
    return {
        "username_input": user["selector"] if user else "",
        "password_input": pwd["selector"] if pwd else "",
        "submit_button": submit["selector"] if submit else "",
        "submit_text": submit["text"] if submit else "",
    }


def check_login_success(page) -> tuple[bool, str]:
    """登录成功判定：离开 passport 域 / 头部「登录」按钮消失 / 出现用户态元素。"""
    if "passport.threatbook.cn" in page.url:
        return False, ""
    if "x.threatbook.com" in page.url:
        try:
            btn = page.locator(LOGIN_BTN)
            if btn.count() == 0 or not btn.first.is_visible():
                return True, f"SSO 回调离开 passport 且「登录」按钮消失: {page.url[:80]}"
        except Exception:
            pass
    for sel in SUCCESS_SELECTORS:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                return True, f"出现用户态元素: {sel}"
        except Exception:
            continue
    return False, ""


def dismiss_blocking_modal(page) -> None:
    """关闭首页可能存在的公告/活动弹窗（Ant Design modal 遮罩会拦截点击）。"""
    try:
        wrap = page.locator("[class*='modal-wrap']")
        if wrap.count() == 0 or not wrap.first.is_visible():
            return
        log("检测到首页弹窗，尝试关闭")
        for sel in [
            "[class*='modal'] [class*='modal-close']",
            "[class*='modal'] button:has-text('知道了')",
            "[class*='modal'] button:has-text('关闭')",
            "[class*='modal'] button:has-text('取消')",
            "[class*='modal'] [class*='close']",
        ]:
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click()
                    pause(1, 2)
                    return
            except Exception:
                continue
        page.keyboard.press("Escape")
        pause(1, 2)
    except Exception:
        pass


def try_open_login_dialog(page) -> bool:
    """点击头部「登录」按钮打开登录对话框。"""
    try:
        btn = page.locator(LOGIN_BTN)
        if btn.count() == 0:
            return False
        btn.first.click()
        pause(2, 3)
        return True
    except Exception as exc:
        log(f"点击登录按钮失败: {exc}")
        return False


def ensure_password_tab(page, form: dict) -> dict:
    """passport 登录页默认是 APP 扫码登录；切到「密码登录」tab。"""
    has_pwd = any(i["type"] == "password" and i["visible"] for i in form["inputs"])
    if has_pwd:
        return form
    for txt in ("密码登录", "账号密码登录", "账号登录", "帐号登录"):
        try:
            tab = page.get_by_text(txt, exact=True)
            if tab.count() > 0 and tab.first.is_visible():
                log(f"切换到「{txt}」tab")
                tab.first.click()
                pause(2, 3)
                return page.evaluate(JS_FORM_DUMP)
        except Exception as exc:
            log(f"切换 tab({txt}) 失败: {str(exc)[:100]}")
    return form


def agree_terms(page) -> bool:
    """勾选「已阅读并同意」协议 checkbox（未勾选时提交会被拦截）。"""
    try:
        cb = page.locator("input[type='checkbox']")
        for i in range(min(cb.count(), 5)):
            el = cb.nth(i)
            if el.is_visible():
                if not el.is_checked():
                    el.check()
                    pause(0.5, 1.5)
                log("协议 checkbox 已勾选")
                return True
        # 自定义样式 checkbox：点 label 触发
        label = page.get_by_text("已阅读并同意", exact=False)
        if label.count() > 0 and label.first.is_visible():
            label.first.click()
            pause(0.5, 1.5)
            log("通过文本 label 勾选协议")
            return True
    except Exception as exc:
        log(f"勾选协议失败: {str(exc)[:100]}")
    return False


def analyze_query_page(page, recorder: XhrRecorder, kind: str, value: str,
                       url: str) -> dict:
    """直连详情页：记录落地 URL、XHR、区块结构、截图、DOM 存盘。"""
    log(f"打开 {kind} 详情页: {url}")
    recorder.current = f"{kind}_{value}"
    before = len(recorder.entries)
    result: dict = {"kind": kind, "value": value, "probe_url": url}
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        pause(3, 5)  # 等 SPA 懒加载 XHR
        dismiss_blocking_modal(page)
        pause(1, 2)
    except Exception as exc:
        result["goto_error"] = str(exc)[:200]
    result["final_url"] = page.url
    result["redirected_to_login"] = "passport.threatbook.cn" in page.url
    result["not_found"] = "notFound" in page.url

    try:
        result["structure"] = page.evaluate(JS_SECTION_ANALYSIS)
    except Exception as exc:
        result["structure"] = {"error": str(exc)[:200]}

    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{kind}_{value}")
    try:
        page.screenshot(path=str(OUT_DIR / f"page_{safe}.png"), full_page=True)
    except Exception as exc:
        log(f"{kind} 截图失败: {exc}")
    try:
        html = page.content()
        (OUT_DIR / f"page_{safe}.html").write_text(html, encoding="utf-8")
        result["html_file"] = f"page_{safe}.html"
        result["html_size"] = len(html)
    except Exception as exc:
        log(f"{kind} DOM 存盘失败: {exc}")

    got = recorder.entries[before:]
    result["xhr_count"] = len(got)
    result["xhr_json"] = [
        {"url": e["url"], "status": e["status"], "size": e.get("body_size")}
        for e in got if "json" in e.get("content_type", "")
    ]
    log(f"{kind} 页捕获 XHR {len(got)} 条，其中 JSON {len(result['xhr_json'])} 条")
    return result


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    load_dotenv(ENV_PATH)
    username = os.environ.get("TI_THREATBOOK_USERNAME", "").strip()
    password = os.environ.get("TI_THREATBOOK_PASSWORD", "").strip()
    if not username or not password:
        log("缺少 TI_THREATBOOK_USERNAME / TI_THREATBOOK_PASSWORD，终止")
        return 2
    log(f"凭据已加载（用户名 {mask(username)}，密码已隐藏）")

    report: dict = {"platform": PLATFORM, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    recorder = XhrRecorder()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"),
        )
        context.add_init_script(_STEALTH_INIT_SCRIPT)
        page = context.new_page()

        # ── a. 首页 + 登录对话框结构分析 ─────────────────────────────────
        log(f"打开首页: {HOME_URL}")
        try:
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
        except Exception as exc:
            report["login"] = {"goto_error": str(exc)[:300]}
            page.screenshot(path=str(OUT_DIR / "01_home_error.png"))
            (OUT_DIR / "report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            log(f"首页打不开: {exc}")
            return 1
        pause(2, 4)
        page.screenshot(path=str(OUT_DIR / "01_home.png"), full_page=True)
        dismiss_blocking_modal(page)

        cookies_before = {c["name"] for c in context.cookies()}
        if not try_open_login_dialog(page):
            report["login"] = {"note": "头部未找到「登录」按钮，无法打开登录页"}
            (OUT_DIR / "report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            log(report["login"]["note"])
            return 1
        # 点击「登录」会跳转 passport.threatbook.cn SSO 页
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        pause(1, 2)
        page.screenshot(path=str(OUT_DIR / "02_login_dialog.png"), full_page=True)

        form = page.evaluate(JS_FORM_DUMP)
        form = ensure_password_tab(page, form)
        picked = pick_login_selectors(form)
        captcha_before = find_captcha(page)
        captcha_details = dump_captcha_details(page)
        page.screenshot(path=str(OUT_DIR / "02b_login_form.png"), full_page=True)
        try:
            (OUT_DIR / "02_passport.html").write_text(
                page.content(), encoding="utf-8")
        except Exception:
            pass
        report["login"] = {
            "entry": f"首页点击 {LOGIN_BTN} 跳转 passport SSO 登录页",
            "form": form,
            "picked_selectors": picked,
            "captcha_before_submit": captcha_before,
            "captcha_details": captcha_details,
        }
        (OUT_DIR / "login_form.json").write_text(
            json.dumps(form, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"表单: inputs={len(form['inputs'])} buttons={len(form['buttons'])} "
            f"dialogs={len(form.get('dialogs', []))} iframes={len(form['iframes'])}")
        log(f"挑选 selector: {json.dumps(picked, ensure_ascii=False)}")
        if captcha_before:
            log(f"提交前已出现验证码特征: {captcha_before}")

        # ── b. 登录提交（最多 1 次，遇验证码即止）────────────────────────
        login_ok, login_note = False, ""
        if captcha_before:
            login_note = "登录框已存在验证码特征，跳过自动登录（账号安全优先）"
        elif not all([picked["username_input"], picked["password_input"],
                      picked["submit_button"]]):
            login_note = f"表单 selector 不完整: {picked}"
        else:
            log("开始登录尝试（慢速拟人）……")
            try:
                agree_terms(page)
                pause(1, 2)
                page.click(picked["username_input"])
                pause(1, 2)
                page.type(picked["username_input"], username,
                          delay=random.randint(70, 130))
                pause(2, 4)
                page.click(picked["password_input"])
                pause(1, 2)
                page.type(picked["password_input"], password,
                          delay=random.randint(70, 130))
                pause(2, 4)
                page.screenshot(path=str(OUT_DIR / "03_before_submit.png"))
                cap = find_captcha(page)
                if cap:
                    login_note = f"提交前出现验证码特征，放弃提交: {cap}"
                    page.screenshot(path=str(OUT_DIR / "04_captcha_abort.png"))
                else:
                    page.click(picked["submit_button"])
                    log("已点击登录，等待 SSO 回调……")
                    try:
                        page.wait_for_url(
                            lambda u: "passport.threatbook.cn" not in u,
                            timeout=15000)
                    except Exception:
                        pass
                    pause(3, 5)
                    try:
                        page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass
                    page.screenshot(path=str(OUT_DIR / "04_after_submit.png"),
                                    full_page=True)
                    cap = find_captcha(page)
                    if cap:
                        login_note = f"提交后出现验证码/滑块，停止尝试: {cap}"
                    else:
                        login_ok, login_note = check_login_success(page)
                        if not login_ok:
                            login_note = "提交后未检测到成功标志（可能账号密码错误或需二次验证）"
            except Exception as exc:
                login_note = f"登录流程异常: {str(exc)[:200]}"
                try:
                    page.screenshot(path=str(OUT_DIR / "04_login_exception.png"))
                except Exception:
                    pass

        cookies_after = {c["name"] for c in context.cookies()}
        new_cookies = sorted(cookies_after - cookies_before)
        report["login"].update({
            "attempted": not captcha_before,
            "success": login_ok,
            "note": login_note,
            "final_url": page.url,
            "new_cookie_names": new_cookies,
        })
        log(f"登录结果: success={login_ok} note={login_note}")
        log(f"新增 cookie 名: {new_cookies}")

        if login_ok:
            context.storage_state(path=str(OUT_DIR / "storage_state.json"))
            log("已保存 storage_state.json（会话可复用）")

        # ── c. 详情页直连探测（匿名也试：验证是否免登录可查）─────────────
        recorder.attach(page)
        report["queries"] = []
        for kind, value, url in TARGETS:
            report["queries"].append(
                analyze_query_page(page, recorder, kind, value, url))
            pause(3, 6)

        browser.close()

    (OUT_DIR / "xhr_log.json").write_text(
        json.dumps(recorder.entries, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_DIR / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 终端摘要（无敏感信息）──────────────────────────────────────────
    print("\n===== RECON SUMMARY =====")
    print(json.dumps({
        "login": {k: v for k, v in report["login"].items() if k != "form"},
        "queries": [
            {
                "kind": q["kind"], "value": q["value"],
                "final_url": q.get("final_url"),
                "redirected_to_login": q.get("redirected_to_login"),
                "not_found": q.get("not_found"),
                "sections": [s["heading"] for s in
                             q.get("structure", {}).get("sections", [])],
                "xhr_json_count": len(q.get("xhr_json", [])),
                "xhr_json_urls": [x["url"] for x in q.get("xhr_json", [])],
            } for q in report.get("queries", [])
        ],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
