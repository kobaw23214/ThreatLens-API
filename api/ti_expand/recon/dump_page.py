"""页面 DOM 转储工具（selector 校准用）：复用已存会话打开指定 URL，保存 HTML+截图。

用法：
  PLAYWRIGHT_BROWSERS_PATH=ti_data/browsers \
    .venv/bin/python -m api.ti_expand.recon.dump_page threatbook "https://x.threatbook.com/v5/domain/google.com"

产物：ti_data/recon/{platform}/dump_{host}_{ts}.html / .png
仅用于离线分析 DOM 结构（校准 selectors/*.yml），不发起业务查询以外的动作。
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("dump_page")


async def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    platform, url = sys.argv[1], sys.argv[2]

    from dotenv import load_dotenv
    load_dotenv("api/.env")

    from api.ti_expand.adapters.browser import BrowserSession
    from api.ti_expand.adapters.session import ensure_session
    from api.ti_expand.config import load_config
    import yaml

    cfg = load_config()
    pcfg = cfg.platforms.get(platform)
    selectors = yaml.safe_load(
        open(f"api/ti_expand/selectors/{platform}.yml", encoding="utf-8"))
    browser = BrowserSession(platform, cfg.data_dir,
                             headful=cfg.headful, channel=cfg.browser_channel)
    page = await browser.new_page()
    try:
        await ensure_session(browser, platform, selectors,
                             pcfg.username if pcfg else "", pcfg.password if pcfg else "",
                             page, solver_cfg=(cfg.captcha_solver, cfg.captcha_max_attempts))
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(8)  # 等 XHR 渲染（校准用，等足）
        out_dir = Path(cfg.data_dir) / "recon" / platform
        out_dir.mkdir(parents=True, exist_ok=True)
        host = url.split("/")[2].replace(".", "_")
        stem = out_dir / f"dump_{host}_{int(time.time())}"
        (stem.with_suffix(".html")).write_text(await page.content(), encoding="utf-8")
        await page.screenshot(path=str(stem.with_suffix(".png")), full_page=True)
        logger.info("已保存: %s.{html,png}  当前页 %s", stem, page.url)
    finally:
        try:
            await page.close()
        except Exception:
            pass
        await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
