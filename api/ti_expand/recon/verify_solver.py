"""一次性验证脚本：微步自动登录 + 极验滑块自动求解（真实站点）。

用法：
  PLAYWRIGHT_BROWSERS_PATH=ti_data/browsers .venv/bin/python -m api.ti_expand.recon.verify_solver

只跑登录链路（探测 → 自动登录 → 滑块求解 → 会话种子回写），不执行业务查询。
凭据从 api/.env 读取，日志不打印密码。
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time

import yaml

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("verify_solver")


async def main() -> int:
    from dotenv import load_dotenv
    load_dotenv("api/.env")

    from api.ti_expand.adapters.browser import BrowserSession
    from api.ti_expand.adapters.session import ensure_session
    from api.ti_expand.config import load_config

    cfg = load_config()
    pcfg = cfg.platforms["threatbook"]
    if not (pcfg.username and pcfg.password):
        logger.error("未配置 TI_THREATBOOK_USERNAME/PASSWORD")
        return 2
    logger.info("配置: headful=%s channel=%r solver=%s attempts=%d",
                cfg.headful, cfg.browser_channel or "(bundled)",
                cfg.captcha_solver, cfg.captcha_max_attempts)

    selectors = yaml.safe_load(
        open("api/ti_expand/selectors/threatbook.yml", encoding="utf-8"))
    browser = BrowserSession("threatbook", cfg.data_dir,
                             headful=cfg.headful, channel=cfg.browser_channel)
    page = await browser.new_page()
    t0 = time.monotonic()
    try:
        await ensure_session(
            browser, "threatbook", selectors, pcfg.username, pcfg.password, page,
            solver_cfg=(cfg.captcha_solver, cfg.captcha_max_attempts))
    except Exception as exc:
        logger.error("登录失败(%.0fs): %s", time.monotonic() - t0, exc)
        arts = await browser.save_artifacts("verify-solver", page)
        logger.error("留证: %s", arts)
        return 1
    finally:
        try:
            await page.close()
        except Exception:
            pass
        await browser.close()

    logger.info("登录成功(%.0fs)，会话种子已回写 ti_data/sessions/threatbook/",
                time.monotonic() - t0)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
