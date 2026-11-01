# -*- coding: utf-8 -*-
"""
ATT&CK 公共工具

TRAM(tram_extractor)与大模型 ATT&CK 映射(attack_mapper)共用的:
  - 技术元数据加载(models/attack/techniques.json)
  - full / filter 模式的 labels 解析(统一两个模型的语义)
  - 技术编号校验(过滤大模型幻觉编号)
  - 技术编号富化(补名称 / 战术 / 官方 URL)

把这套逻辑集中在一处,保证「TRAM 与 LLM 的 full/过滤行为完全一致」。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# 形如 T1059 或 T1059.003 的 ATT&CK 技术编号
TECH_ID_RE = re.compile(r"^T\d{4}(?:\.\d{3})?$", re.IGNORECASE)

# "全量"哨兵词(归一化后比较): 出现即表示不做过滤,返回全部命中技术。
FULL_TOKENS = frozenset({
    "full", "all", "*", "全量", "全部", "attack", "att&ck", "技战术", "ttp", "技术", "战术",
})


def normalize_token(s: str) -> str:
    """归一化: 小写 + 去除空白/下划线/连字符,便于战术名/哨兵词匹配。"""
    return re.sub(r"[\s_\-]+", "", (s or "").strip().lower())


def load_attack_metadata(path: Path) -> Dict[str, Dict[str, Any]]:
    """读取蒸馏后的 ATT&CK 技术元数据(models/attack/techniques.json)。缺失时返回空表。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        techniques = data.get("techniques", {})
        if isinstance(techniques, dict):
            return techniques
        logger.warning("ATT&CK 元数据格式异常(techniques 不是对象): %s", path)
        return {}
    except FileNotFoundError:
        logger.warning(
            "ATT&CK 元数据不存在(%s),技术编号将不做富化/校验。"
            "可运行 benchmark/download_attack_data.py 生成。", path,
        )
        return {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("ATT&CK 元数据解析失败(%s): %s,技术编号将不做富化/校验。", path, exc)
        return {}


def build_known_tactics(attack_meta: Dict[str, Dict[str, Any]]) -> Set[str]:
    """从元数据汇总所有战术 shortname(归一化),供按战术过滤。"""
    out: Set[str] = set()
    for meta in attack_meta.values():
        for tac in meta.get("tactics", []) or []:
            out.add(normalize_token(tac))
    return out


def parse_filters(labels: List[str], known_tactics: Set[str]) -> Tuple[Set[str], Set[str]]:
    """
    把用户 labels 解析成过滤条件 (technique_ids, tactics)。

    规则:
      - 形如 T1059 / T1059.003 的项 → 技术编号过滤(父编号 T1059 匹配其全部子技术)。
      - 与已知战术 shortname 匹配的项(忽略大小写/空格/连字符)→ 战术过滤。
      - "full"/"全量"/"ATT&CK"/"TTP" 等哨兵词及其它无法识别的语义词 → 忽略。
    两个集合都为空即「全量模式」: 返回全部命中的技术。
    """
    technique_ids: Set[str] = set()
    tactics: Set[str] = set()
    for lb in labels or []:
        raw = (lb or "").strip()
        if not raw:
            continue
        if TECH_ID_RE.match(raw):
            technique_ids.add(raw.upper())
            continue
        norm = normalize_token(raw)
        if norm in FULL_TOKENS:
            continue
        if norm in known_tactics:
            tactics.add(norm)
        # 其它无法识别的词一律忽略(等同全量),不报错
    return technique_ids, tactics


def passes_filter(
    technique: str,
    technique_ids: Set[str],
    tactics: Set[str],
    attack_meta: Dict[str, Dict[str, Any]],
) -> bool:
    """技术编号是否通过过滤;两集合都空 → 全量放行。"""
    if not technique_ids and not tactics:
        return True
    if technique_ids:
        tid = technique.upper()
        if tid in technique_ids:
            return True
        if tid.split(".")[0] in technique_ids:  # 父编号匹配子技术
            return True
    if tactics:
        meta = attack_meta.get(technique, {})
        for tac in meta.get("tactics", []) or []:
            if normalize_token(tac) in tactics:
                return True
    return False


def is_valid_technique(technique: str, attack_meta: Dict[str, Dict[str, Any]]) -> bool:
    """
    技术编号是否有效:格式合法,且(在元数据存在且未废弃)。
    元数据缺失时只校验格式(不误杀),用于过滤大模型幻觉编号。
    """
    if not technique or not TECH_ID_RE.match(technique):
        return False
    if not attack_meta:
        return True
    meta = attack_meta.get(technique) or attack_meta.get(technique.upper())
    if meta is None:
        return False
    return not bool(meta.get("deprecated", False))


def technique_display_name(technique: str, attack_meta: Dict[str, Dict[str, Any]]) -> str:
    """
    技术的可读全名。子技术拼上父技术名,便于大模型理解语义,例如:
      T1003.001 → "OS Credential Dumping: LSASS Memory"
      T1059.003 → "Command and Scripting Interpreter: Windows Command Shell"
    元数据缺失时返回空串。
    """
    meta = attack_meta.get(technique) or attack_meta.get(technique.upper()) or {}
    name = meta.get("name") or ""
    parent_id = meta.get("parent")
    if parent_id:
        parent_meta = attack_meta.get(parent_id) or {}
        parent_name = parent_meta.get("name")
        if parent_name and name:
            return f"{parent_name}: {name}"
    return name


def format_candidate_catalog(
    technique_ids: Set[str],
    tactics: Set[str],
    attack_meta: Dict[str, Dict[str, Any]],
    max_items: int = 120,
) -> str:
    """
    把过滤范围(技术编号 / 战术)渲染成带名称的候选清单,供大模型 prompt 使用。
    形如:
      - T1003.001  OS Credential Dumping: LSASS Memory  [credential-access]
      - T1005      Data from Local System  [collection]
    让模型从「带标签的菜单」中选择,而不是凭记忆回想编号含义,显著提升映射准确率。
    元数据缺失时返回空串(退化为仅列编号)。
    """
    if not attack_meta:
        return ""

    # 收集候选编号:用户明确给定的技术编号 + 属于指定战术的技术。
    # 注意:不把父编号展开成子技术,以免污染受限标签空间;父/子匹配由 passes_filter 兜底。
    candidates: Set[str] = set(technique_ids)
    if tactics:
        for full_id, meta in attack_meta.items():
            for tac in meta.get("tactics", []) or []:
                if normalize_token(tac) in tactics:
                    candidates.add(full_id)
                    break

    # 只保留元数据中存在且未废弃的
    valid = [c for c in candidates if is_valid_technique(c, attack_meta)]
    if not valid:
        return ""
    valid.sort()

    lines: List[str] = []
    for tid in valid[:max_items]:
        name = technique_display_name(tid, attack_meta)
        meta = attack_meta.get(tid, {})
        tac_str = ", ".join(meta.get("tactics", []) or [])
        suffix = f"  [{tac_str}]" if tac_str else ""
        lines.append(f"- {tid}  {name}{suffix}" if name else f"- {tid}{suffix}")
    return "\n".join(lines)


def enrich(technique: str, attack_meta: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """用 ATT&CK 元数据补充技术名称 / 战术 / URL 等字段;缺失时给出兜底 URL。"""
    meta = attack_meta.get(technique) or attack_meta.get(technique.upper()) or {}
    default_url = f"https://attack.mitre.org/techniques/{technique.replace('.', '/')}/"
    return {
        "technique_id": technique,
        "technique_name": meta.get("name"),
        "tactics": meta.get("tactics", []),
        "technique_url": meta.get("url", default_url),
        "is_subtechnique": meta.get("is_subtechnique"),
        "parent_technique": meta.get("parent"),
    }
