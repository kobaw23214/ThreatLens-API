"""浏览器采集适配器（Web 通道）。

P2 状态：真实采集已落地。
  - ensure_session（adapters/session.py）：storage_state 种子 + 登录态探测
    + selector 驱动自动登录；captcha_marker → need_manual（不硬闯）、
    auth_fail_marker → auth_failed（不重试）；登录成功回写种子
  - query：导航 query.{seed_type}.url_tpl → page.on("response") 按 intercepts
    拦截 JSON → 等待数据就绪（yml poll 块：任务映射 + 轮询至关键任务
    status==10000，超时兜底；无 poll 块则按「拦截静默 3s」收尾）→
    按 sections 的 selector 抽 DOM innerText 解析为 kv/table/text →
    raw 保存拦截到的原始 JSON
  - 失败留证：截图 + HTML → data_dir/artifacts/{platform}-{ts}/

selector yml 结构（P2 新增字段）：
  login.probe_url          登录态探测页（缺省用 login.url）
  login.sso_url_pattern    SSO 页 URL glob（命中即未登录 / 会话失效）
  login.auth_fail_marker   认证失败提示 selector（可见 → auth_failed）
  query.{type}.page_ready  查询页就绪标志 selector（等待其出现）
  query.{type}.poll        可选，任务式数据就绪等待：
      map_pattern      任务映射响应 URL glob（data={任务名: 任务ID}）
      result_pattern   任务结果响应 URL glob（data.{任务ID}.status）
      key_tasks        关键任务名（全部 status==10000 即收尾）
      timeout_s        兜底超时（默认 25）
  query.{type}.intercepts  元素可为 glob 字符串或 {pattern, usage} 映射
  sections[].click         可选，取数前先点击的 selector（tab/折叠面板），
                           配合 wait_ms（默认 3000）等渲染；点击失败仅记 debug
  sections[].wait_for      可选，配合 click 使用：点击后等该 selector 出现
                           （懒加载 tab 的「查询中」场景），wait_ms 为超时上限，
                           超时按当前状态取数（空态自动降级）；未配则固定 sleep
  sections[].item_selector 可选，kv 区块的结构化解析：每个 item 元素
                           innerText 首行为键、其余行合并为值（如微步 .count-item）；
                           可与 blocks 同配（blocks 结果排在前面合并，如标题区时间戳）
  sections[].blocks        可选（format=blocks），声明式子块拼装 kv：
                           [{key, selector, multiple?, strip?}]，multiple=true 时
                           该键值为文本数组（标签/端口等 chip 列表），未命中跳过；
                           产出仍按 kv 区块返回（前端无需感知）
  sections[].row_selector  可选（format=table），声明式行 selector（非 <tr> 的
                           div 列表结构），配合 column_map {列名: 单元格 selector}
                           取值；selector 支持 self / self:head / self:tail /
                           css / xpath=…
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from pathlib import Path
from typing import Dict, List, Optional

from .base import BaseTiAdapter, TiAdapterError
from .browser import BrowserSession
from .session import captcha_visible, ensure_session, glob_match

logger = logging.getLogger(__name__)

_SELECTORS_DIR = Path(__file__).parent.parent / "selectors"

# 会话探测缓存：有效期内不再重复打开探测页
_SESSION_RECHECK_S = 600.0
# 无 poll 配置时的收尾判定：最后一次拦截后静默这么久即认为加载完成
_SETTLE_QUIET_S = 3.0
_SETTLE_TIMEOUT_S = 25.0
# 提取/保留上限
_MAX_ROWS = 50
_MAX_CELL = 200
_MAX_TEXT = 8000
_MAX_BODY = 200_000
_MAX_RAW = 400_000


def _kv_put(items: Dict, key: str, value: str) -> None:
    if key in items:
        old = items[key]
        if isinstance(old, list):
            old.append(value)
        else:
            items[key] = [old, value]
    else:
        items[key] = value


def parse_kv_text(text: str) -> Dict:
    """把「键:\n值」或「键: 值」混排的 innerText 解析为 kv 字典（尽力而为）。"""
    import re

    items: Dict = {}
    pending: Optional[str] = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = re.match(r"^([^\s:：/\\.][^:：/\\.]{0,19})\s*[:：]\s*(.*)$", line)
        if m:
            key, val = m.group(1).strip(), m.group(2).strip()
            if val:
                _kv_put(items, key, val)
                pending = None
            else:
                pending = key
        elif pending is not None:
            _kv_put(items, pending, line)
            pending = None
    return items


class WebAdapter(BaseTiAdapter):
    """基于 Playwright 的平台采集器，由 selectors/{platform}.yml 驱动。"""

    channel = "web"

    def __init__(self, browser: BrowserSession, platform: str, cfg=None) -> None:
        self._browser = browser
        self.platform = platform
        self._cfg = cfg  # TiConfig 引用（apply_settings_overrides 原地生效）
        self._selectors = self._load_selectors(platform)
        self._session_checked_at = 0.0

    @staticmethod
    def _load_selectors(platform: str) -> Dict:
        path = _SELECTORS_DIR / f"{platform}.yml"
        if not path.exists():
            logger.warning("[%s] selector 文件不存在: %s", platform, path)
            return {}
        try:
            import yaml
        except ImportError:
            logger.warning("PyYAML 未安装，selector 文件不可用: %s", path)
            return {}
        try:
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            logger.error("[%s] selector 文件解析失败: %s", platform, exc)
            return {}

    @property
    def selectors_version(self) -> Optional[str]:
        return self._selectors.get("version")

    def _credentials(self) -> tuple:
        pcfg = (self._cfg.platforms.get(self.platform) if self._cfg else None)
        if pcfg is None:
            return "", ""
        return pcfg.username, pcfg.password

    # ─── 会话 ────────────────────────────────────────────────────────────

    async def _ensure_session(self, page) -> str:
        """在传入页面上验证/建立登录态；探测结果缓存 _SESSION_RECHECK_S 秒。
        返回会话来源标记（cached/verified），供 diagnostics 记录。"""
        if time.monotonic() - self._session_checked_at < _SESSION_RECHECK_S:
            return "cached"
        username, password = self._credentials()
        await ensure_session(
            self._browser, self.platform, self._selectors, username, password, page,
            solver_cfg=self._solver_cfg())
        self._session_checked_at = time.monotonic()
        return "verified"

    def _solver_cfg(self):
        """滑块求解配置 (enabled, max_attempts)；无 cfg 引用时 None（用缺省）。"""
        if self._cfg is None:
            return None
        return (getattr(self._cfg, "captcha_solver", True),
                getattr(self._cfg, "captcha_max_attempts", 3))

    async def keepalive(self) -> None:
        """会话保活：访问探测页刷新 cookie 有效期；失效则尝试自动重登
        （含滑块自动求解）。失败抛 TiAdapterError 由调用方记账，此处不重试。"""
        page = None
        try:
            page = await self._browser.new_page()
            username, password = self._credentials()
            await ensure_session(
                self._browser, self.platform, self._selectors, username, password, page,
                solver_cfg=self._solver_cfg())
            self._session_checked_at = time.monotonic()
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass

    # ─── 查询 ────────────────────────────────────────────────────────────

    async def query(self, seed_type: str, seed_value: str) -> Dict:
        query_rules = (self._selectors.get("query") or {}).get(seed_type)
        if not query_rules:
            raise TiAdapterError(
                "not_ready",
                f"[{self.platform}] selectors/{self.platform}.yml 未配置 "
                f"{seed_type} 查询规则（P0 未产出该类型规则）",
            )
        if not query_rules.get("url_tpl"):
            raise TiAdapterError(
                "not_ready",
                f"[{self.platform}] selectors/{self.platform}.yml 的 "
                f"query.{seed_type}.url_tpl 为空",
            )

        t0 = time.perf_counter()
        page = None
        try:
            page = await self._browser.new_page()
            session_how = await self._ensure_session(page)
            result = await self._collect(page, query_rules, seed_type, seed_value)
        except TiAdapterError as exc:
            if page is not None:
                await self._leave_evidence(page, exc)
            if exc.reason in ("session_expired", "auth_failed", "need_manual"):
                self._session_checked_at = 0.0  # 下次重新探测/登录
            raise
        except Exception as exc:  # noqa: BLE001
            err = TiAdapterError("query_failed", f"[{self.platform}] 采集异常: {exc}")
            if page is not None:
                await self._leave_evidence(page, err)
            raise err from exc
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass

        duration_ms = int((time.perf_counter() - t0) * 1000)
        result["diagnostics"].update({
            "channel": self.channel,
            "duration_ms": duration_ms,
            "selector_version": self.selectors_version,
            "session": session_how,
            "artifacts": [],
        })
        return result

    async def _leave_evidence(self, page, exc: TiAdapterError) -> None:
        evidence_id = f"{self.platform}-{int(time.time())}"
        try:
            artifacts = await self._browser.save_artifacts(evidence_id, page)
            if artifacts:
                logger.warning("[%s] 失败留证: %s", self.platform, artifacts)
        except Exception:
            pass

    # ─── 采集主流程 ───────────────────────────────────────────────────────

    async def _collect(self, page, rules: Dict, seed_type: str,
                       seed_value: str) -> Dict:
        patterns = self._intercept_patterns(rules)
        pending: List = []  # 待读取 body 的 response
        collected: List[Dict] = []  # {url, json}

        def on_response(resp) -> None:
            try:
                if resp.request.resource_type not in ("xhr", "fetch"):
                    return
                if not any(glob_match(resp.url, p) for p in patterns):
                    return
                pending.append(resp)
            except Exception:
                pass

        async def drain() -> None:
            while pending:
                resp = pending.pop(0)
                try:
                    body = await resp.body()
                    if len(body) > _MAX_BODY:
                        body = body[:_MAX_BODY]
                    collected.append({"url": resp.url,
                                      "json": json.loads(body.decode("utf-8", "replace"))})
                except Exception:
                    pass

        page.on("response", on_response)

        url = rules["url_tpl"].format(value=seed_value)
        logger.info("[%s] 查询 %s=%s → %s", self.platform, seed_type, seed_value, url)
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(random.uniform(2.0, 3.5))

        # 会话失效：被踢回 SSO / 登录页
        login_cfg = self._selectors.get("login") or {}
        sso = login_cfg.get("sso_url_pattern") or ""
        if sso and glob_match(page.url, sso):
            raise TiAdapterError(
                "session_expired",
                f"[{self.platform}] 查询页跳转至登录页（{page.url}），会话已失效",
            )
        hit = await captcha_visible(page, login_cfg)
        if hit:
            raise TiAdapterError(
                "need_manual",
                f"[{self.platform}] 查询页出现验证码/风控（{hit}），已停止",
            )

        page_ready = rules.get("page_ready") or ""
        if page_ready:
            try:
                # 实测奇安信 qiankun 微应用正常需 7~10s，15s 余量太小
                await page.wait_for_selector(page_ready, timeout=30000)
            except Exception:
                # SPA 跳转可能延迟：就绪标志超时后先复查是否被弹回登录页，
                # 避免会话失效被误报成"页面改版"
                if sso and glob_match(page.url, sso):
                    raise TiAdapterError(
                        "session_expired",
                        f"[{self.platform}] 查询页跳转至登录页（{page.url}），会话已失效",
                    )
                raise TiAdapterError(
                    "selector_broken",
                    f"[{self.platform}] 查询页就绪标志未出现（{page_ready}），"
                    f"页面疑似改版，当前 {page.url}",
                )

        poll = rules.get("poll") or {}
        if poll.get("map_pattern") and poll.get("result_pattern"):
            task_map, tasks = await self._poll_tasks(poll, drain, collected)
        else:
            await self._wait_settled(drain, collected)
            task_map, tasks = None, {}

        await drain()
        await asyncio.sleep(random.uniform(1.0, 2.0))

        sections = await self._extract_sections(page, rules.get("sections") or [])
        if not sections:
            raise TiAdapterError(
                "selector_broken",
                f"[{self.platform}] 所有 section selector 均未取到内容，页面疑似改版",
            )
        raw = self._build_raw(collected, poll, task_map, tasks)
        return {"sections": sections, "raw": raw,
                "diagnostics": {"channel": self.channel,
                                "interceptions": len(collected), "artifacts": []}}

    # ─── 数据就绪等待 ─────────────────────────────────────────────────────

    async def _poll_tasks(self, poll: Dict, drain, collected: List[Dict]) -> tuple:
        """任务式等待（奇安信模式）：map_pattern 响应拿 {任务名: 任务ID}，
        result_pattern 响应轮询至关键任务 status==10000 或超时。
        任务 ID 必须来自当次 map 响应，不可硬编码。"""
        map_pat = poll["map_pattern"]
        result_pat = poll["result_pattern"]
        key_tasks = poll.get("key_tasks") or []
        timeout_s = float(poll.get("timeout_s") or 25)

        task_map: Optional[Dict] = None
        tasks: Dict = {}  # 任务ID → 最新结果体
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            await drain()
            for item in collected:
                data = item.get("json", {}).get("data")
                if not isinstance(data, dict):
                    continue
                if glob_match(item["url"], map_pat):
                    if data and all(isinstance(v, str) for v in data.values()):
                        task_map = data  # 最新一份 {任务名: 任务ID}
                elif glob_match(item["url"], result_pat):
                    for tid, body in data.items():
                        tasks[tid] = body  # 同 ID 后写覆盖（取最新轮询）
            if task_map:
                key_ids = [task_map[k] for k in key_tasks if k in task_map]
                if key_ids and all(
                        (tasks.get(tid) or {}).get("status") == 10000
                        for tid in key_ids):
                    logger.info("[%s] 关键任务已就绪 %s", self.platform, key_tasks)
                    break
            await asyncio.sleep(1.5)
        else:
            logger.warning("[%s] 任务轮询超时（%.0fs），按当前数据收尾",
                           self.platform, timeout_s)
        return task_map, tasks

    async def _wait_settled(self, drain, collected: List[Dict]) -> None:
        """通用等待：拦截静默 _SETTLE_QUIET_S 秒即收尾（兜底超时）。"""
        deadline = time.monotonic() + _SETTLE_TIMEOUT_S
        last_count = -1
        quiet_since = time.monotonic()
        await asyncio.sleep(4.0)  # 首屏最少等待
        while time.monotonic() < deadline:
            await drain()
            if len(collected) != last_count:
                last_count = len(collected)
                quiet_since = time.monotonic()
            elif time.monotonic() - quiet_since >= _SETTLE_QUIET_S:
                return
            await asyncio.sleep(0.8)
        logger.warning("[%s] 拦截等待超时（%.0fs），按当前数据收尾",
                       self.platform, _SETTLE_TIMEOUT_S)

    # ─── 区块提取 ─────────────────────────────────────────────────────────

    async def _extract_sections(self, page, section_rules: List[Dict]) -> List[Dict]:
        sections: List[Dict] = []
        for rule in section_rules:
            name = rule.get("name") or ""
            fmt = rule.get("format") or "text"
            sel = rule.get("selector") or ""
            if not (name and sel):
                continue
            # 需先点击展开的区块（tab/折叠面板）：点击后等渲染再取数。
            # 同类 tab 共用同一 panel 容器时，务必把无 click 的默认面板 section 排在前面
            if click := (rule.get("click") or ""):
                try:
                    btn = page.locator(click).first
                    if await btn.count() and await btn.is_visible():
                        await btn.click()
                        wait_ms = float(rule.get("wait_ms", 3000))
                        if wait_for := (rule.get("wait_for") or ""):
                            # 懒加载 tab（点击后「查询中」）：等数据行出现，超时按当前状态取数
                            try:
                                await page.wait_for_selector(wait_for, timeout=wait_ms)
                            except Exception:
                                pass
                        else:
                            await asyncio.sleep(wait_ms / 1000.0)
                except Exception as exc:
                    logger.debug("[%s] section 点击展开失败 %s(%s): %s",
                                 self.platform, name, click, exc)
            try:
                loc = page.locator(sel).first
                if not await loc.count():
                    logger.debug("[%s] section 未命中: %s (%s)", self.platform, name, sel)
                    continue
                text = (await loc.inner_text() or "").strip()
                if not text:
                    continue
                if fmt == "kv" and (item_sel := rule.get("item_selector") or ""):
                    # 结构化键值区块：每个 item 元素首行为键、其余行为值；
                    # 可同时声明 blocks（标题区时间戳等零散字段），blocks 在前合并
                    items = await self._parse_kv_items(loc, item_sel)
                    if rule.get("blocks"):
                        extra = await self._parse_blocks(loc, rule.get("blocks") or [])
                        items = {**extra, **items}
                    if items:
                        sections.append({"name": name, "format": "kv", "items": items})
                        continue
                if fmt == "blocks":
                    # 声明式子块拼装 kv：blocks[] 每项 {key, selector, multiple?, strip?}，
                    # multiple=true 时该键值为数组（标签/端口等 chip 列表），未命中的子块跳过
                    items = await self._parse_blocks(loc, rule.get("blocks") or [])
                    if items:
                        sections.append({"name": name, "format": "kv", "items": items})
                    else:
                        sections.append({"name": name, "format": "text",
                                         "content": text[:_MAX_TEXT]})
                elif fmt == "kv":
                    items = parse_kv_text(text)
                    if items:
                        sections.append({"name": name, "format": "kv", "items": items})
                    else:
                        sections.append({"name": name, "format": "text",
                                         "content": text[:_MAX_TEXT]})
                elif fmt == "table":
                    table = await self._parse_table(loc, rule)
                    if table:
                        sections.append({"name": name, "format": "table", **table})
                    else:
                        sections.append({"name": name, "format": "text",
                                         "content": text[:_MAX_TEXT]})
                else:
                    sections.append({"name": name, "format": "text",
                                     "content": text[:_MAX_TEXT]})
            except Exception as exc:
                logger.debug("[%s] section 提取失败 %s: %s", self.platform, name, exc)
        return sections

    @staticmethod
    async def _parse_kv_items(loc, item_selector: str) -> Dict:
        """item_selector 命中的每个元素解析为一条 kv：首行键、其余行合并为值。"""
        items: Dict = {}
        try:
            els = loc.locator(item_selector)
            n = await els.count()
        except Exception:
            return items
        for i in range(min(n, 60)):
            try:
                lines = [x.strip() for x in
                         (await els.nth(i).inner_text() or "").splitlines() if x.strip()]
            except Exception:
                continue
            if len(lines) >= 2:
                _kv_put(items, lines[0], " ".join(lines[1:])[:_MAX_CELL])
            elif lines:
                _kv_put(items, lines[0], "")
        return items

    @staticmethod
    async def _parse_blocks(loc, blocks: List[Dict]) -> Dict:
        """声明式子块拼装 kv：每项 {key, selector, multiple?, strip?}。
        multiple=true → 值为文本数组（chip 列表场景）；未命中/空文本的子块跳过。"""
        items: Dict = {}
        for b in blocks:
            key, sel = b.get("key"), b.get("selector")
            if not key or not sel:
                continue
            try:
                els = loc.locator(sel)
                n = await els.count()
                if not n:
                    continue
                if b.get("multiple"):
                    vals = []
                    for i in range(min(n, 60)):
                        t = (await els.nth(i).inner_text() or "").strip()
                        if t:
                            vals.append(t[:_MAX_CELL])
                    if vals:
                        items[key] = vals
                else:
                    t = (await els.first.inner_text() or "").strip()
                    strip = b.get("strip") or ""
                    if strip:
                        t = t.replace(strip, "").strip()
                    if t:
                        _kv_put(items, key, t[:_MAX_CELL])
            except Exception:
                continue
        return items

    @staticmethod
    async def _cell_text(el, sel: str) -> str:
        """column_map 单元格取值：self / self:head / self:tail / css / xpath=…，未命中返回 ''。"""
        try:
            if sel in ("self", "self:head", "self:tail"):
                t = (await el.inner_text() or "").strip()
                if sel == "self":
                    return t[:_MAX_CELL]
                head, _, tail = t.rpartition(" ")
                return (head if sel == "self:head" else tail)[:_MAX_CELL] if head else ("" if sel == "self:head" else t[:_MAX_CELL])
            sub = el.locator(sel).first
            if not await sub.count():
                return ""
            return ((await sub.inner_text() or "").strip())[:_MAX_CELL]
        except Exception:
            return ""

    async def _parse_table(self, loc, rule: Optional[Dict] = None) -> Optional[Dict]:
        """提取表格：默认 <tr> 结构；rule.row_selector 存在时按声明式行+列映射提取
        （row_selector 定行元素，column_map {列名: 单元格 selector}，selector 支持
        self/self:head/self:tail/css/xpath=…）。无有效表格返回 None（调用方降级 text）。"""
        rule = rule or {}
        try:
            row_sel = rule.get("row_selector") or ""
            if row_sel:
                rows_el = loc.locator(row_sel)
                n = await rows_el.count()
                if not n:
                    return None
                cmap: Dict = rule.get("column_map") or {}
                columns = list(cmap.keys()) or ["内容"]
                rows: List[List[str]] = []
                for i in range(min(n, _MAX_ROWS)):
                    el = rows_el.nth(i)
                    if cmap:
                        vals = [await self._cell_text(el, sel) for sel in cmap.values()]
                    else:
                        vals = [((await el.inner_text() or "").strip())[:_MAX_CELL]]
                    if any(vals):
                        rows.append(vals)
                if not rows:
                    return None
                return {"columns": columns, "rows": rows}
            trs = loc.locator("tr")
            n = await trs.count()
            if n < 2:
                return None
            columns: List[str] = []
            rows: List[List[str]] = []
            for i in range(min(n, _MAX_ROWS + 1)):
                tr = trs.nth(i)
                cells = tr.locator("th, td")
                c = await cells.count()
                vals = []
                for j in range(c):
                    v = (await cells.nth(j).inner_text() or "").strip()
                    vals.append(v[:_MAX_CELL])
                if not any(vals):
                    continue
                if not columns and await tr.locator("th").count():
                    columns = vals
                elif not columns and i == 0:
                    columns = vals
                else:
                    rows.append(vals)
            if not rows:
                return None
            if not columns:
                columns = [f"列{j + 1}" for j in range(max(len(r) for r in rows))]
            # 丢弃「表头为空且整列无数据」的列（平台的操作/展开占位空列）
            keep = [j for j, c in enumerate(columns)
                    if c.strip() or any(j < len(r) and r[j].strip() for r in rows)]
            if len(keep) < len(columns):
                columns = [columns[j] for j in keep]
                rows = [[r[j] if j < len(r) else "" for j in keep] for r in rows]
            return {"columns": columns, "rows": rows}
        except Exception:
            return None

    # ─── 原始数据 ─────────────────────────────────────────────────────────

    @staticmethod
    def _intercept_patterns(rules: Dict) -> List[str]:
        """intercepts 元素兼容 glob 字符串或 {pattern, usage} 映射。"""
        out = []
        for entry in rules.get("intercepts") or []:
            if isinstance(entry, str):
                out.append(entry)
            elif isinstance(entry, dict) and entry.get("pattern"):
                out.append(entry["pattern"])
        return out

    def _build_raw(self, collected: List[Dict], poll: Dict,
                   task_map, tasks) -> Dict:
        """raw：任务式平台给 {task_map, tasks}，其余给 interceptions 清单。"""
        raw: Dict = {}
        if task_map is not None:
            id_to_name = {v: k for k, v in task_map.items()}
            raw["task_map"] = task_map
            raw["tasks"] = {id_to_name.get(tid, tid): body
                            for tid, body in tasks.items()}
        if collected:
            # 按 URL 去重（轮询响应只留最后一次）
            dedup: Dict[str, Dict] = {}
            for item in collected:
                dedup[item["url"]] = item
            raw["interceptions"] = [{"url": u, "json": c["json"]}
                                    for u, c in dedup.items()][:50]
        self._cap_raw(raw)
        return raw or None

    @staticmethod
    def _cap_raw(raw: Dict) -> None:
        """raw 体积兜底：超限时截断各任务 table 行。"""
        try:
            if len(json.dumps(raw, ensure_ascii=False)) <= _MAX_RAW:
                return
            for body in (raw.get("tasks") or {}).values():
                table = body.get("table") if isinstance(body, dict) else None
                if isinstance(table, list) and len(table) > 20:
                    body["table"] = table[:20]
                    body["_truncated"] = True
        except Exception:
            pass

    async def close(self) -> None:
        await self._browser.close()
