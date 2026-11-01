"""奇安信 tab 面板 DOM 转储工具：逐一点击 ip/domain/file 研判页的侧导航 tab，
保存每个 tab 激活状态下的完整页面 HTML+截图（离线校准 selectors/qianxin.yml 用）。

用法：
  PLAYWRIGHT_BROWSERS_PATH=ti_data/browsers \
    .venv/bin/python -m api.ti_expand.recon.dump_tabs_qianxin \
      ip 8.210.88.161 domain fget-career.com file 657483b5...881f8

参数为「类型 种子值」成对出现，可一次给多对；页面间间隔 ≥60s（风控纪律）。
产物：ti_data/recon/qianxin/tabs/{type}/{序号}_{tab名}.html / .png
仅用于离线分析 DOM 结构，不发起业务查询以外的动作。
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("dump_tabs_qianxin")

PAGE_READY = {"ip": "div#ti-ip", "domain": "div#ti-domain", "file": "div#ti-md5"}
PAGE_INTERVAL_S = 60      # 页面间最小间隔（风控纪律）
TAB_WAIT_S = 4.0          # 点击 tab 后等待渲染（懒加载 tab 需等 XHR）
READY_WAIT_S = 18         # 进入页面后等任务轮询渲染


async def dump_one(page, qtype: str, seed: str, out_root: Path) -> None:
    url = f"https://ti.qianxin.com/v2/search?type={qtype}&value={seed}"
    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_selector(PAGE_READY[qtype], timeout=30000)
    await asyncio.sleep(READY_WAIT_S)

    out_dir = out_root / qtype
    out_dir.mkdir(parents=True, exist_ok=True)

    tabs = page.locator("a.result-nav-name")
    n = await tabs.count()
    logger.info("[%s] 共 %d 个 tab", qtype, n)
    labels: list[str] = []
    for i in range(n):
        label = (await tabs.nth(i).inner_text() or "").strip()
        label = re.sub(r"\s*\(\s*[\d+]+\s*\)\s*$", "", label).strip() or f"tab{i}"
        labels.append(label)
    logger.info("[%s] tabs: %s", qtype, labels)

    for i, label in enumerate(labels):
        try:
            await tabs.nth(i).click()
            await asyncio.sleep(TAB_WAIT_S)
            stem = out_dir / f"{i:02d}_{label}"
            stem.with_suffix(".html").write_text(await page.content(), encoding="utf-8")
            await page.screenshot(path=str(stem.with_suffix(".png")), full_page=False)
            logger.info("[%s] [%d/%d] %s → %s", qtype, i + 1, n, label, stem)
        except Exception as exc:
            logger.warning("[%s] [%d/%d] %s 失败: %s", qtype, i + 1, n, label, exc)
        await asyncio.sleep(1.5)  # tab 间隔，防风控


async def main() -> int:
    args = sys.argv[1:]
    if len(args) < 2 or len(args) % 2:
        print(__doc__)
        return 2
    pairs = [(args[i], args[i + 1]) for i in range(0, len(args), 2)]
    for qtype, _ in pairs:
        if qtype not in PAGE_READY:
            print(f"未知类型: {qtype}（支持 {list(PAGE_READY)}）")
            return 2

    from dotenv import load_dotenv
    load_dotenv("api/.env")

    from api.ti_expand.adapters.browser import BrowserSession
    from api.ti_expand.adapters.session import ensure_session
    from api.ti_expand.config import load_config
    import yaml

    cfg = load_config()
    pcfg = cfg.platforms.get("qianxin")
    selectors = yaml.safe_load(
        open("api/ti_expand/selectors/qianxin.yml", encoding="utf-8"))
    browser = BrowserSession("qianxin", cfg.data_dir,
                             headful=cfg.headful, channel=cfg.browser_channel)
    page = await browser.new_page()
    out_root = Path(cfg.data_dir) / "recon" / "qianxin" / "tabs"
    try:
        await ensure_session(browser, "qianxin", selectors,
                             pcfg.username if pcfg else "", pcfg.password if pcfg else "",
                             page, solver_cfg=(cfg.captcha_solver, cfg.captcha_max_attempts))
        last_at = 0.0
        for qtype, seed in pairs:
            gap = time.time() - last_at
            if last_at and gap < PAGE_INTERVAL_S:
                wait = PAGE_INTERVAL_S - gap
                logger.info("页面间隔纪律：等待 %.0fs", wait)
                await asyncio.sleep(wait)
            await dump_one(page, qtype, seed, out_root)
            last_at = time.time()
    finally:
        try:
            await page.close()
        except Exception:
            pass
        await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
