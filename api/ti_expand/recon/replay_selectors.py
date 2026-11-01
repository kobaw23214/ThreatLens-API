"""selector 离线重放校验工具：不打平台，用转储 HTML 校准/回归 sections 配置。

用法：
    PLAYWRIGHT_BROWSERS_PATH=ti_data/browsers \
      .venv/bin/python -m api.ti_expand.recon.replay_selectors <platform> <seed_type> <html> [html...]
          [--section 名1,名2] [--force-visible]

参数：
    platform        threatbook / qianxin（读 api/ti_expand/selectors/{platform}.yml）
    seed_type       ip / domain / file（取 query.{seed_type}.sections）
    html            转储文件路径（file:// 打开）。tab 显隐切换的页面（奇安信 file），
                    传「点击对应 tab 后的转储」并配合 --section 只跑该 tab 的区块
    --section       逗号分隔的区块名，只跑这些区块（缺省跑全部）
    --force-visible 移除 DOM 内联 display:none（模拟 tab 点击后的可见态；
                    ⚠ 真实页面是点击后框架切换可见性，本开关只是近似模拟）

机制说明：
    - 直接调用引擎 WebAdapter._extract_sections（不经队列/网络），与线上同一套解析。
    - sections 里的 click 会被剥离（转储已是点击后状态，静态 DOM 的 Vue 事件不工作）。
    - 注意 Blink 行为：display:none 子树的 innerText 不退空而是退化为 textContent，
      所以跑「不该命中的区块」可能出脏文本——用 --section 明确指定目标区块。
例子：
    # 奇安信 ip 全区块
    .venv/bin/python -m api.ti_expand.recon.replay_selectors qianxin ip \\
        ti_data/recon/qianxin/dump_ti_qianxin_com_1784946984.html
    # 奇安信 file 检测结果 tab
    .venv/bin/python -m api.ti_expand.recon.replay_selectors qianxin file \\
        ti_data/recon/qianxin/file_tab_检测结果.html --section 检测概览,引擎检测明细
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import sys
from pathlib import Path

import yaml
from playwright.async_api import async_playwright

from ..adapters.web import WebAdapter

_FORCE_VISIBLE_JS = (
    "document.querySelectorAll('[style]').forEach("
    "e=>{if(e.style.display==='none')e.style.display=''})"
)


async def _run(args: argparse.Namespace) -> int:
    yml_path = Path(__file__).parent.parent / "selectors" / f"{args.platform}.yml"
    cfg = yaml.safe_load(yml_path.read_text(encoding="utf-8"))
    sections = ((cfg.get("query") or {}).get(args.seed_type) or {}).get("sections") or []
    if not sections:
        print(f"!! {args.platform}.yml 无 query.{args.seed_type}.sections", file=sys.stderr)
        return 2
    only = {s.strip() for s in args.section.split(",") if s.strip()} if args.section else None
    rules = [copy.deepcopy(r) for r in sections if only is None or r.get("name") in only]
    for r in rules:  # 转储已是点击后状态：剥离 click/wait_ms
        r.pop("click", None)
        r.pop("wait_ms", None)

    adapter = object.__new__(WebAdapter)  # 跳过 __init__（不需要浏览器/配置）
    adapter.platform = args.platform

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for html in args.html:
            path = Path(html).resolve()
            if not path.exists():
                print(f"!! 文件不存在: {path}", file=sys.stderr)
                continue
            page = await browser.new_page()
            await page.goto(f"file://{path}")
            if args.force_visible:
                await page.evaluate(_FORCE_VISIBLE_JS)
            print(f"\n{'=' * 78}\n[{args.platform}/{args.seed_type}] {path.name}"
                  + (" (force-visible)" if args.force_visible else ""))
            out = await adapter._extract_sections(page, rules)
            if not out:
                print("  (无产出)")
            for s in out:
                fmt = s.get("format")
                if fmt == "kv":
                    items = s.get("items") or {}
                    preview = {k: (v[:4] if isinstance(v, list) else v)
                               for k, v in list(items.items())[:8]}
                    print(f"  ▣ {s['name']} [kv] {len(items)}键: "
                          f"{json.dumps(preview, ensure_ascii=False)[:400]}")
                elif fmt == "table":
                    cols, rows = s.get("columns") or [], s.get("rows") or []
                    print(f"  ▣ {s['name']} [table] {len(rows)}行 列={cols}")
                    for row in rows[:3]:
                        print(f"      {json.dumps(row, ensure_ascii=False)[:300]}")
                else:
                    content = (s.get("content") or "").replace("\n", "⏎")
                    print(f"  ▣ {s['name']} [text] {len(s.get('content') or '')}字符: "
                          f"{content[:140]}")
            await page.close()
        await browser.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="selector 离线重放校验（转储 HTML → 引擎解析）")
    ap.add_argument("platform", choices=["threatbook", "qianxin"])
    ap.add_argument("seed_type", choices=["ip", "domain", "file"])
    ap.add_argument("html", nargs="+", help="转储 HTML 路径（可多个）")
    ap.add_argument("--section", help="只跑这些区块（逗号分隔区块名）")
    ap.add_argument("--force-visible", action="store_true",
                    help="移除内联 display:none（模拟 tab 点击后可见态）")
    return asyncio.run(_run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
