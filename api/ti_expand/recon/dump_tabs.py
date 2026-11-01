"""tab 面板 DOM 转储工具：逐一点击微步 ip/domain 页的分析 tab，保存面板 HTML+截图。

用法：
  PLAYWRIGHT_BROWSERS_PATH=ti_data/browsers \
    .venv/bin/python -m api.ti_expand.recon.dump_tabs "https://x.threatbook.com/v5/ip/8.210.88.161"

产物：ti_data/recon/threatbook/tabs/{host}/{tab名}.html / .png
仅用于离线分析 DOM 结构（校准 selectors/*.yml）。
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
logger = logging.getLogger("dump_tabs")


async def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    url = sys.argv[1]

    from dotenv import load_dotenv
    load_dotenv("api/.env")

    from api.ti_expand.adapters.browser import BrowserSession
    from api.ti_expand.adapters.session import ensure_session
    from api.ti_expand.config import load_config
    import yaml

    cfg = load_config()
    selectors = yaml.safe_load(
        open("api/ti_expand/selectors/threatbook.yml", encoding="utf-8"))
    pcfg = cfg.platforms.get("threatbook")
    browser = BrowserSession("threatbook", cfg.data_dir,
                             headful=cfg.headful, channel=cfg.browser_channel)
    page = await browser.new_page()
    try:
        await ensure_session(browser, "threatbook", selectors,
                             pcfg.username if pcfg else "", pcfg.password if pcfg else "",
                             page, solver_cfg=(cfg.captcha_solver, cfg.captcha_max_attempts))
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_selector(".summary-top", timeout=30000)
        await asyncio.sleep(5)

        host = url.split("/")[2].replace(".", "_")
        out_dir = Path(cfg.data_dir) / "recon" / "threatbook" / "tabs" / host
        out_dir.mkdir(parents=True, exist_ok=True)

        tabs = page.locator("#analysisControllUl li.controller-item")
        n = await tabs.count()
        logger.info("共 %d 个 tab", n)
        for i in range(n):
            tab = tabs.nth(i)
            label = (await tab.inner_text() or "").strip()
            label = re.sub(r"[0-9K+\s]+$", "", label) or f"tab{i}"
            try:
                await tab.click()
                await asyncio.sleep(4)  # 等 /v5/node/** XHR 渲染
                pane = page.locator(".pane-content").first
                html = await pane.inner_html() if await pane.count() else ""
                stem = out_dir / f"{i:02d}_{label}"
                stem.with_suffix(".html").write_text(html, encoding="utf-8")
                await page.screenshot(path=str(stem.with_suffix(".png")), full_page=False)
                logger.info("[%d/%d] %s → %s (%d chars)", i + 1, n, label, stem, len(html))
            except Exception as exc:
                logger.warning("[%d/%d] %s 失败: %s", i + 1, n, label, exc)
            await asyncio.sleep(1.5)  # tab 间隔，防风控
    finally:
        try:
            await page.close()
        except Exception:
            pass
        await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
