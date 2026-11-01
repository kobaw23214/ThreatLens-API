"""
实体标记工具 (entity-marking)

在不改动 input_text(原始文本,前端依赖其字符偏移做高亮)的前提下,
为最终回调 / 查询返回的 JSON 额外提供两类带标记字段:

  1. mark_text:
     整篇文本,把每个实体用 ⟦label⟧ ... ⟦/label⟧ 包裹 (类 XML 标记)。
     例: "攻击者利用漏洞从 ⟦IPv4⟧185.220.101.5⟦/IPv4⟧ 对受害单位主机发起扫描"

  2. 每个实体的 marked_context:
     该实体所在的"上下文片段",实体本身用 ⟦label⟧ 包裹。
     上下文边界不是固定字符数硬切,而是:
       - 先从实体往前 / 往后保证至少 MIN_BEFORE / MIN_AFTER 个字符,
       - 再继续往前 / 往后找到第一个"分隔符"(句末标点等)才真正切断,
       从而尽量保留完整句子。

环境变量 (在 api/.env 配置):
  MARK_CONTEXT_MIN_BEFORE : 实体前方至少截取的字符数 (默认 30)
  MARK_CONTEXT_MIN_AFTER  : 实体后方至少截取的字符数 (默认 30)
  MARK_CONTEXT_SEPARATORS : 分隔符集合。值中的每个字符都视为一个分隔符;
                            支持转义 \\n \\r \\t。默认 "。！？；!?;\\n"

标记字段是"实时计算、不落库"的:不改 DB schema,不影响缓存 / 历史任务,
input_text 字段保持原样,完全向后兼容。
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

# 标记符 (Mathematical White Square Bracket: U+27E6 / U+27E7)
_OPEN = "\u27e6"   # ⟦
_CLOSE = "\u27e7"  # ⟧

# 默认配置
_DEFAULT_MIN_BEFORE = 30
_DEFAULT_MIN_AFTER = 30
_DEFAULT_SEPARATORS = "。！？；!?;\n"


def _label_of(entity: Dict) -> str:
    """取用于标记的标签:优先映射后的 label,退化到 raw_label,再退化到 ENTITY。"""
    label = entity.get("label") or entity.get("raw_label")
    label = str(label).strip() if label is not None else ""
    return label or "ENTITY"


def _open_tag(label: str) -> str:
    return f"{_OPEN}{label}{_CLOSE}"


def _close_tag(label: str) -> str:
    return f"{_OPEN}/{label}{_CLOSE}"


def parse_separators(raw: str) -> set:
    """把分隔符配置字符串解析为字符集合,支持 \\n \\r \\t 转义。"""
    if raw is None:
        raw = ""
    raw = raw.replace("\\n", "\n").replace("\\r", "\r").replace("\\t", "\t")
    return set(raw)


def load_mark_config() -> Tuple[int, int, set]:
    """从环境变量读取标记上下文配置。非法值回落到默认值。"""
    try:
        min_before = int(os.environ.get("MARK_CONTEXT_MIN_BEFORE", _DEFAULT_MIN_BEFORE))
    except (TypeError, ValueError):
        min_before = _DEFAULT_MIN_BEFORE
    try:
        min_after = int(os.environ.get("MARK_CONTEXT_MIN_AFTER", _DEFAULT_MIN_AFTER))
    except (TypeError, ValueError):
        min_after = _DEFAULT_MIN_AFTER

    min_before = max(0, min_before)
    min_after = max(0, min_after)

    seps = parse_separators(os.environ.get("MARK_CONTEXT_SEPARATORS", _DEFAULT_SEPARATORS))
    return min_before, min_after, seps


def _valid_span(entity: Dict, text_len: int) -> bool:
    s = entity.get("start")
    e = entity.get("end")
    return (
        isinstance(s, int) and isinstance(e, int)
        and 0 <= s < e <= text_len
    )


def _before_cut(text: str, start: int, min_before: int, seps: set) -> int:
    """
    计算上下文左边界:
    先保证 [start - min_before, start) 至少 min_before 个字符,
    再从 (start - min_before) 继续向左找到第一个分隔符,在其后切断。
    """
    b0 = start - min_before
    if b0 <= 0:
        return 0
    i = b0 - 1
    while i >= 0:
        if text[i] in seps:
            return i + 1  # 分隔符本身不计入上下文
        i -= 1
    return 0


def _after_cut(text: str, end: int, min_after: int, seps: set) -> int:
    """
    计算上下文右边界:
    先保证 [end, end + min_after) 至少 min_after 个字符,
    再从 (end + min_after) 继续向右找到第一个分隔符,把它一起包含进上下文。
    """
    n = len(text)
    a0 = end + min_after
    if a0 >= n:
        return n
    j = a0
    while j < n:
        if text[j] in seps:
            return j + 1  # 句末分隔符一并保留
        j += 1
    return n


def build_marked_context(
    text: str,
    entity: Dict,
    min_before: int,
    min_after: int,
    seps: set,
) -> Optional[str]:
    """为单个实体生成带 ⟦label⟧ 包裹的上下文片段;span 非法时返回 None。"""
    if not text or not _valid_span(entity, len(text)):
        return None

    s = entity["start"]
    e = entity["end"]
    left = _before_cut(text, s, min_before, seps)
    right = _after_cut(text, e, min_after, seps)

    ctx = text[left:right]
    rel_s = s - left
    rel_e = e - left
    label = _label_of(entity)
    return (
        ctx[:rel_s]
        + _open_tag(label)
        + ctx[rel_s:rel_e]
        + _close_tag(label)
        + ctx[rel_e:]
    )


def build_mark_text(text: str, entities: List[Dict]) -> str:
    """
    生成整篇带标记文本。实体按 (start, end) 升序插入标记;
    出现重叠区间时跳过靠后的实体,保证标记不交叉破坏文本。
    """
    if not text:
        return text or ""
    if not entities:
        return text

    text_len = len(text)
    spans = sorted(
        (e for e in entities if _valid_span(e, text_len)),
        key=lambda e: (e["start"], e["end"]),
    )

    out: List[str] = []
    cursor = 0
    for e in spans:
        s = e["start"]
        t = e["end"]
        if s < cursor:
            continue  # 与前一个实体重叠,跳过避免标记交叉
        label = _label_of(e)
        out.append(text[cursor:s])
        out.append(_open_tag(label))
        out.append(text[s:t])
        out.append(_close_tag(label))
        cursor = t
    out.append(text[cursor:])
    return "".join(out)


def enrich_entities(
    text: Optional[str],
    entities: Optional[List[Dict]],
    config: Optional[Tuple[int, int, set]] = None,
) -> List[Dict]:
    """
    返回实体列表的浅拷贝,每项新增 marked_context 字段。
    原始实体对象不被修改。text 为空或实体为空时原样返回(拷贝)。
    """
    if not entities:
        return []
    if config is None:
        config = load_mark_config()
    min_before, min_after, seps = config

    enriched: List[Dict] = []
    for e in entities:
        item = dict(e)
        if text:
            item["marked_context"] = build_marked_context(
                text, item, min_before, min_after, seps
            )
        else:
            item["marked_context"] = None
        enriched.append(item)
    return enriched


def build_marked_payload(
    text: Optional[str],
    entities: Optional[List[Dict]],
) -> Tuple[str, List[Dict]]:
    """
    一次性产出 (mark_text, 带 marked_context 的实体列表),供回调 / 查询接口直接使用。
    """
    config = load_mark_config()
    mark_text = build_mark_text(text or "", entities or [])
    enriched = enrich_entities(text, entities, config)
    return mark_text, enriched
