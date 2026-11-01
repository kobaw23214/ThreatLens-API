# -*- coding: utf-8 -*-
"""
手册内容服务

集中管理可对外提供的 Markdown 手册:集成手册 + 各模型手册。
文件以项目内的源文件为唯一真相源(不复制副本),通过 /manuals 系列接口对外提供。

分组(group)与子分组(subgroup):
  - group        : 一级分组,integration(集成手册) / models(模型手册)
  - subgroup     : 二级分组,仅 models 组使用,ner(NER) / attack(ATT&CK);
                   具体模型(ioc-finder / tram ...)是三级条目。
  集成手册组没有子分组,条目直接挂在组下。

新增手册只需在 MANUAL_REGISTRY 里加一条记录。
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from .features import as_dict as _feature_flags

# 项目根目录 = api/ 的父目录
_ROOT = Path(__file__).parent.parent


class ManualEntry:
    def __init__(
        self, key: str, title: str, group: str, rel_path: str,
        subgroup: Optional[str] = None,
    ) -> None:
        self.key = key
        self.title = title
        self.group = group              # 一级分组:integration / models
        self.subgroup = subgroup        # 二级分组(仅 models):ner / attack
        self.path = _ROOT / rel_path

    def exists(self) -> bool:
        return self.path.is_file()

    def read(self) -> str:
        return self.path.read_text(encoding="utf-8")


# 手册注册表(顺序即前端展示顺序)
MANUAL_REGISTRY: List[ManualEntry] = [
    # ── 集成手册 ──────────────────────────────────────────────────────
    ManualEntry(
        key="api",
        title="提取标签 API 手册",
        group="integration",
        rel_path="docs/提取标签API手册-v26_06_11.md",
    ),
    ManualEntry(
        key="file-parsing",
        title="文件解析手册",
        group="integration",
        rel_path="docs/文件解析手册.md",
    ),
    ManualEntry(
        key="attack-integration",
        title="ATT&CK 集成手册",
        group="integration",
        rel_path="docs/att&ck/ATT&CK集成手册.md",
    ),
    ManualEntry(
        key="ti-integration",
        title="联网情报手册",
        group="integration",
        rel_path="docs/联网情报手册.md",
    ),
    # ── 模型手册 · NER ────────────────────────────────────────────────
    ManualEntry(
        key="ioc-finder",
        title="ioc-finder",
        group="models",
        subgroup="ner",
        rel_path="docs/ner/ioc-finder-手册.md",
    ),
    ManualEntry(
        key="iocextract",
        title="iocextract",
        group="models",
        subgroup="ner",
        rel_path="docs/ner/iocextract-手册.md",
    ),
    # ── 模型手册 · ATT&CK ─────────────────────────────────────────────
    ManualEntry(
        key="tram",
        title="tram",
        group="models",
        subgroup="attack",
        rel_path="docs/att&ck/tram-手册.md",
    ),
    ManualEntry(
        key="attack-llm",
        title="attack-llm",
        group="models",
        subgroup="attack",
        rel_path="docs/att&ck/attack-llm-手册.md",
    ),
    ManualEntry(
        key="qwen3-attack-retriever",
        title="qwen3-attack-retriever",
        group="models",
        subgroup="attack",
        rel_path="docs/att&ck/qwen3-attack-retriever-手册.md",
    ),
]

# 一级分组中文标题
GROUP_TITLES: Dict[str, str] = {
    "integration": "集成手册",
    "models": "模型手册",
}

# 二级分组标题(models 组内)
SUBGROUP_TITLES: Dict[str, str] = {
    "ner": "NER",
    "attack": "ATT&CK",
}

# 集成手册(integration 组,无 subgroup) → 功能模块开关归属
# 提取标签 API 手册 / 文件解析手册归 ner;ATT&CK 集成手册归 attack;联网情报手册归 ti
_INTEGRATION_MANUAL_FEATURE: Dict[str, str] = {
    "api": "ner",
    "file-parsing": "ner",
    "attack-integration": "attack",
    "ti-integration": "ti",
}

_BY_KEY: Dict[str, ManualEntry] = {m.key: m for m in MANUAL_REGISTRY}


def get_manual(key: str) -> Optional[ManualEntry]:
    return _BY_KEY.get(key)


def list_manual_groups() -> List[dict]:
    """
    返回分组后的手册清单(仅含文件存在的条目),供前端构建导航。

    结构(每个 group 二选一):
      - 无子分组: {"key","title","items":[{key,title}...]}
      - 有子分组: {"key","title","subgroups":[{"key","title","items":[...]}]}
    顺序均按 MANUAL_REGISTRY 中首次出现的先后。
    """
    # 一级分组容器(保持首次出现顺序)
    groups: Dict[str, dict] = {}
    group_order: List[str] = []

    # 功能模块开关:被关闭功能的手册不出现在清单中(分组结构随条目自然消失)
    flags = _feature_flags()

    for m in MANUAL_REGISTRY:
        if not m.exists():
            continue
        # models 组按 subgroup(ner/attack)归台;integration 组按 key 映射
        feat = m.subgroup or _INTEGRATION_MANUAL_FEATURE.get(m.key)
        if feat and not flags.get(feat, True):
            continue
        if m.group not in groups:
            groups[m.group] = {
                "key": m.group,
                "title": GROUP_TITLES.get(m.group, m.group),
                "items": [],          # 无子分组时使用
                "subgroups": [],      # 有子分组时使用
                "_subgroup_index": {},  # subgroup key -> dict(内部用)
            }
            group_order.append(m.group)
        g = groups[m.group]
        item = {"key": m.key, "title": m.title}
        if m.subgroup:
            sg = g["_subgroup_index"].get(m.subgroup)
            if sg is None:
                sg = {
                    "key": m.subgroup,
                    "title": SUBGROUP_TITLES.get(m.subgroup, m.subgroup),
                    "items": [],
                }
                g["_subgroup_index"][m.subgroup] = sg
                g["subgroups"].append(sg)
            sg["items"].append(item)
        else:
            g["items"].append(item)

    # 清理内部字段,并按是否有子分组决定输出形态
    ordered: List[dict] = []
    for gkey in group_order:
        g = groups[gkey]
        g.pop("_subgroup_index", None)
        if g["subgroups"]:
            g.pop("items", None)
        else:
            g.pop("subgroups", None)
        ordered.append(g)
    return ordered
