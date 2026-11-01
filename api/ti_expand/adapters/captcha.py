"""极验 v4 滑块自动求解（ddddocr 本地缺口识别 + 拟人轨迹拖动）。

识别链（按优先级）：
  1. 读取 .geetest_bg / .geetest_slice_bg 的 background-image 原图
     （static.geetest.com CDN，div 主题实测于此），ddddocr.slide_match 找缺口
  2. 元素截图 + slide_match（canvas 主题兜底）
  3. cv2 边缘模板匹配兜底（opencv 为 ddddocr 自带依赖）
拖动：gen_track 生成「先快后慢 + 过冲回退 + y 抖动」拟人轨迹，Playwright mouse 执行。

ddddocr 为可选依赖（惰性 import）：未安装时 solve_geetest_slide 直接返回 False，
调用方按原逻辑抛 need_manual，其余流程不受影响。
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import struct
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# 极验 v4 元素（class 带哈希后缀，用 [class~=token] 精确匹配无后缀 token）
SEL_PANEL = ["[class~=geetest_window]", "[class~=geetest_box]"]
SEL_BG = ["canvas[class~=geetest_bg]", "[class~=geetest_bg] canvas", "[class~=geetest_bg]"]
SEL_PIECE_IMG = ["canvas[class~=geetest_slice]", "[class~=geetest_slice_bg] canvas",
                 "[class~=geetest_slice_bg]", "[class~=geetest_slice]"]
SEL_PIECE_POS = ["[class~=geetest_slice]"]      # 位置载体（left 随拖动变化）
SEL_BUTTON = ["[class~=geetest_btn]", "[class~=geetest_slider_button]"]
SEL_REFRESH = ["[class~=geetest_refresh]"]

_DET = None          # ddddocr 实例（进程级复用）
_DET_FAILED = False  # 导入失败标记（不再重试）


def _load_ddddocr():
    """惰性加载 ddddocr；不可用返回 None（调用方回落 need_manual）。"""
    global _DET, _DET_FAILED
    if _DET is not None or _DET_FAILED:
        return _DET
    try:
        import ddddocr
        _DET = ddddocr.DdddOcr(det=False, ocr=False, show_ad=False)
        logger.info("ddddocr 已加载，极验滑块自动求解可用")
    except Exception as exc:
        _DET_FAILED = True
        logger.warning("ddddocr 不可用（%s），滑块将走人工兜底", exc)
    return _DET


def _png_size(data: bytes) -> Tuple[int, int]:
    """从 PNG 文件头读 (宽, 高)；非 PNG 返回 (0, 0)。"""
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", data[16:24])
    return 0, 0


def gen_track(distance: float) -> List[Tuple[float, float, float]]:
    """生成拟人拖动轨迹，返回 [(x步长, y步长, 步后停顿秒)]，累计 x 位移 ≈ distance。

    形态：匀加速起步 → 高速中段 → 减速 → 过冲 2~5px → 回退 → 末端停顿微抖。
    """
    target = distance + random.uniform(-1.0, 1.0)
    overshoot = random.uniform(2.0, 5.0)
    far = target + overshoot
    steps = max(20, int(far / random.uniform(3.5, 6.0)))
    track: List[Tuple[float, float, float]] = []
    moved = 0.0
    for i in range(steps):
        t = (i + 1) / steps
        x = far * (1 - (1 - t) ** 3) - moved  # ease-out：先快后慢
        moved += x
        track.append((x, random.uniform(-1.2, 1.2), random.uniform(0.008, 0.03)))
    back = max(3, int(overshoot))
    for _ in range(back):
        track.append((-overshoot / back * random.uniform(0.8, 1.2),
                      random.uniform(-0.6, 0.6), random.uniform(0.01, 0.04)))
    track.append((0.0, 0.0, random.uniform(0.1, 0.25)))
    return track


def _cv_match_gap(piece_png: bytes, bg_png: bytes) -> Optional[int]:
    """cv2 边缘模板匹配兜底：返回缺口在背景图 x 坐标（图片像素）。"""
    try:
        import cv2
        import numpy as np
    except Exception:
        return None
    try:
        bg = cv2.imdecode(np.frombuffer(bg_png, np.uint8), cv2.IMREAD_COLOR)
        piece = cv2.imdecode(np.frombuffer(piece_png, np.uint8), cv2.IMREAD_COLOR)
        if bg is None or piece is None:
            return None
        res = cv2.matchTemplate(cv2.Canny(bg, 100, 200),
                                cv2.Canny(piece, 100, 200), cv2.TM_CCOEFF_NORMED)
        _, _, _, max_loc = cv2.minMaxLoc(res)
        return int(max_loc[0])
    except Exception:
        return None


def _match_gap(piece_png: bytes, bg_png: bytes) -> Optional[int]:
    """缺口识别：ddddocr slide_match（simple_target 两档）→ cv2 兜底。"""
    det = _load_ddddocr()
    if det is not None:
        for simple in (True, False):
            try:
                res = det.slide_match(piece_png, bg_png, simple_target=simple)
                x = (res or {}).get("target", [None])[0]
                if x is not None:
                    return int(x)
            except Exception as exc:
                logger.debug("slide_match(simple=%s) 失败: %s", simple, exc)
    return _cv_match_gap(piece_png, bg_png)


async def _first_visible(page, selectors: List[str]):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                return loc
        except Exception:
            pass
    return None


async def panel_visible(page) -> bool:
    """滑块面板是否仍在显示（外层 .geetest_captcha 高 0 不可见，须取内层）。"""
    return await _first_visible(page, SEL_PANEL) is not None


async def _bg_image_url(loc) -> Optional[str]:
    """取元素 background-image 的 URL（div 主题）；无则 None。"""
    try:
        style = await loc.evaluate(
            "el => (el.style && el.style.backgroundImage)"
            " || getComputedStyle(el).backgroundImage || ''")
    except Exception:
        return None
    m = re.search(r"url\([\"']?(.*?)[\"']?\)", style or "")
    return m.group(1) if m else None


async def _fetch(page, url: str) -> Optional[bytes]:
    """用浏览器 context 的请求栈拉取图片（继承代理/cookie）。"""
    try:
        resp = await page.context.request.get(url, timeout=10000)
        if resp.ok:
            return await resp.body()
    except Exception as exc:
        logger.debug("拉取滑块图片失败 %s: %s", url, exc)
    return None


async def _drag(page, btn, distance: float) -> None:
    """按 gen_track 轨迹拖动滑块按钮 distance（CSS px）。"""
    box = await btn.bounding_box()
    if not box:
        return
    x = box["x"] + box["width"] / 2 + random.uniform(-2, 2)
    y = box["y"] + box["height"] / 2 + random.uniform(-2, 2)
    await page.mouse.move(x - random.uniform(30, 80), y + random.uniform(-15, 15), steps=6)
    await asyncio.sleep(random.uniform(0.1, 0.3))
    await page.mouse.move(x, y, steps=4)
    await asyncio.sleep(random.uniform(0.15, 0.4))
    await page.mouse.down()
    await asyncio.sleep(random.uniform(0.05, 0.15))
    min_y, max_y = box["y"] - 4, box["y"] + box["height"] + 4
    for dx, dy, wait in gen_track(distance):
        x += dx
        y = min(max_y, max(min_y, y + dy))
        await page.mouse.move(x, y)
        await asyncio.sleep(wait)
    await asyncio.sleep(random.uniform(0.05, 0.2))
    await page.mouse.up()


async def _solve_once(page, platform: str, attempt: int) -> bool:
    """单次求解：取图 → 识别缺口 → 拖动 → 判结果。"""
    bg = await _first_visible(page, SEL_BG)
    piece = await _first_visible(page, SEL_PIECE_IMG)
    pos = await _first_visible(page, SEL_PIECE_POS)
    btn = await _first_visible(page, SEL_BUTTON)
    if bg is None or piece is None or pos is None or btn is None:
        logger.warning("[%s] 滑块元素定位失败(bg=%s piece=%s pos=%s btn=%s)",
                       platform, bg is not None, piece is not None,
                       pos is not None, btn is not None)
        return False
    await asyncio.sleep(random.uniform(1.0, 2.0))  # 等图片加载稳定

    # 1) div 主题：直接拉 background-image 原图（原分辨率，最准）
    bg_img = piece_img = None
    bg_url, piece_url = await _bg_image_url(bg), await _bg_image_url(piece)
    if bg_url and piece_url:
        bg_img, piece_img = await _fetch(page, bg_url), await _fetch(page, piece_url)
    # 2) canvas 主题兜底：元素截图
    if not bg_img or not piece_img:
        try:
            bg_img, piece_img = await bg.screenshot(), await piece.screenshot()
        except Exception as exc:
            logger.warning("[%s] 滑块取图失败: %s", platform, exc)
            return False

    gap_x = _match_gap(piece_img, bg_img)
    if gap_x is None:
        logger.warning("[%s] 第 %d 次：缺口识别失败", platform, attempt)
        return False

    bg_box = await bg.bounding_box()
    pos_box = await pos.bounding_box()
    if not bg_box or not pos_box:
        return False
    png_w, _ = _png_size(bg_img)
    scale = png_w / bg_box["width"] if png_w and bg_box["width"] else 1.0
    distance = gap_x / scale - (pos_box["x"] - bg_box["x"])
    if not (15 <= distance <= bg_box["width"] - 15):
        logger.warning("[%s] 第 %d 次：缺口距离异常(%.1fpx)，按识别失败处理",
                       platform, attempt, distance)
        return False

    logger.info("[%s] 第 %d 次：缺口 x=%.1f（拖动 %.1fpx）",
                platform, attempt, gap_x / scale, distance)
    await _drag(page, btn, distance)
    await asyncio.sleep(random.uniform(2.2, 3.5))
    return not await panel_visible(page)


async def _refresh(page) -> None:
    loc = await _first_visible(page, SEL_REFRESH)
    if loc is not None:
        try:
            await loc.click()
        except Exception:
            pass
    await asyncio.sleep(random.uniform(1.5, 2.5))


async def solve_geetest_slide(page, platform: str = "", max_attempts: int = 3) -> bool:
    """尝试自动过极验 v4 滑块。True=面板已通过；False=未装依赖/元素缺失/多次失败。"""
    if _load_ddddocr() is None:
        return False
    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            ok = await _solve_once(page, platform, attempt)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] 滑块求解第 %d 次异常: %s", platform, attempt, exc)
            ok = False
        if ok or not await panel_visible(page):
            if ok:
                logger.info("[%s] 滑块第 %d 次求解通过", platform, attempt)
            return True
        if attempt < max_attempts:
            await _refresh(page)  # 换新图再来
    logger.warning("[%s] 滑块 %d 次求解均未通过", platform, max_attempts)
    return False
