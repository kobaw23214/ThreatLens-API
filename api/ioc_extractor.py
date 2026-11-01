# -*- coding: utf-8 -*-
"""
IOC 提取器（确定性实体抽取）

把 ioc-finder / iocextract 两个库封装成与 GLiNER / UIE / LLM 完全相同的
"伪模型"，实现 predict_with_chunks(text, labels, chunk_cfg) 接口，从而复用
现有的任务队列、缓存、回调、label_mapping、去重等全部基础设施，
无需改动 queue_manager。

注册三个伪模型:
  - "ioc-finder"  : 纯 ioc-finder（类型最全，自动处理 [.] 类 defang）
  - "iocextract"  : 纯 iocextract（类型少，但能 refang hxxp:// 类 scheme defang）
  - "auto"        : 智能分流。IOC 类标签 → 确定性引擎(ioc-finder ∪ iocextract)；
                    语义类标签 → 真正的 NER 模型；合并时 IOC 区间优先。

设计要点:
  1. 库本身只返回去重后的"值列表"，不带位置。本模块用 _locate_value 把每个
     值回填到原文 offset，且对被 defang 的 IOC 用宽松正则匹配。
  2. 同一个值多次出现 → 每个出现位置生成一个独立实体(角色判定需要按位置区分)。
  3. 返回实体的 label 用"用户原始标签"，保持 raw_label / label_mapping 流程一致。
  4. IOC 提取是正则,无 token 上限,不分块。返回 chunking_enabled=False 的单块
     metadata,与 UIE / LLM 的降级路径格式一致。
"""
from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ─── 标签 → canonical IOC 类型 映射 ──────────────────────────────────────────
# canonical 类型名沿用 ioc-finder 的命名(复数)。键统一做小写 + 去空格归一化。
# 一个标签可映射到多个 canonical 类型(如 "哈希" → 所有 hash 类型)。

_HASH_ALL = {"md5s", "sha1s", "sha256s", "sha512s"}

_LABEL_ALIASES: Dict[str, Set[str]] = {
    # IPv4
    "ip": {"ipv4s"}, "ip地址": {"ipv4s"}, "ipv4": {"ipv4s"},
    "ipaddress": {"ipv4s"}, "ip address": {"ipv4s"}, "ipaddr": {"ipv4s"},
    # IPv6
    "ipv6": {"ipv6s"}, "ipv6地址": {"ipv6s"},
    # IP(不分版本)
    "ipboth": {"ipv4s", "ipv6s"},
    # 域名
    "域名": {"domains"}, "domain": {"domains"}, "domains": {"domains"},
    "hostname": {"domains"}, "主机名": {"domains"},
    # URL
    "url": {"urls"}, "urls": {"urls"}, "网址": {"urls"}, "链接": {"urls"},
    # 邮箱
    "邮箱": {"email_addresses"}, "邮件": {"email_addresses"},
    "邮件地址": {"email_addresses"}, "email": {"email_addresses"},
    "邮箱地址": {"email_addresses"}, "e-mail": {"email_addresses"},
    # 哈希
    "md5": {"md5s"}, "sha1": {"sha1s"}, "sha256": {"sha256s"}, "sha512": {"sha512s"},
    "哈希": set(_HASH_ALL), "hash": set(_HASH_ALL), "哈希值": set(_HASH_ALL),
    "文件哈希": set(_HASH_ALL), "样本哈希": set(_HASH_ALL),
    "filehash": set(_HASH_ALL),
    # CVE / 漏洞编号
    "cve": {"cves"}, "cve编号": {"cves"}, "漏洞编号": {"cves"}, "漏洞": {"cves"},
    "cves": {"cves"},
    # 文件路径
    "文件路径": {"file_paths"}, "路径": {"file_paths"},
    "filepath": {"file_paths"}, "file path": {"file_paths"}, "file_path": {"file_paths"},
    # 注册表
    "注册表": {"registry_key_paths"}, "注册表键": {"registry_key_paths"},
    "registry": {"registry_key_paths"}, "registrykey": {"registry_key_paths"},
    "registry key": {"registry_key_paths"},
    # MAC
    "mac": {"mac_addresses"}, "mac地址": {"mac_addresses"},
    "macaddress": {"mac_addresses"}, "物理地址": {"mac_addresses"},
    # 比特币地址
    "比特币": {"bitcoin_addresses"}, "比特币地址": {"bitcoin_addresses"},
    "bitcoin": {"bitcoin_addresses"}, "btc": {"bitcoin_addresses"},
    # ASN
    "asn": {"asns"}, "asns": {"asns"}, "自治系统": {"asns"},
    # CIDR
    "cidr": {"ipv4_cidrs", "ipv6_cidrs"},
    "网段": {"ipv4_cidrs", "ipv6_cidrs"}, "ip段": {"ipv4_cidrs", "ipv6_cidrs"},
    # user-agent
    "ua": {"user_agents"}, "useragent": {"user_agents"},
    "user-agent": {"user_agents"}, "user agent": {"user_agents"}, "浏览器标识": {"user_agents"},
    # ── 以下仅 ioc-finder 支持(iocextract 无对应函数) ──────────────────
    # 模糊哈希
    "ssdeep": {"ssdeeps"}, "模糊哈希": {"ssdeeps"},
    # imphash / authentihash (PE 文件哈希)
    "imphash": {"imphashes"}, "authentihash": {"authentihashes"},
    # 门罗币
    "monero": {"monero_addresses"}, "门罗币": {"monero_addresses"}, "xmr": {"monero_addresses"},
    # XMPP / Jabber
    "xmpp": {"xmpp_addresses"}, "jabber": {"xmpp_addresses"},
    # TLP 标记
    "tlp": {"tlp_labels"}, "tlp标记": {"tlp_labels"},
    # Google 跟踪 ID
    "adsense": {"google_adsense_publisher_ids"},
    "googleadsense": {"google_adsense_publisher_ids"},
    "analytics": {"google_analytics_tracker_ids"},
    "googleanalytics": {"google_analytics_tracker_ids"},
    # MITRE ATT&CK
    "attack": {"attack_tactics", "attack_techniques", "attack_mitigations"},
    "att&ck": {"attack_tactics", "attack_techniques", "attack_mitigations"},
    "attacktactic": {"attack_tactics"}, "战术": {"attack_tactics"},
    "attacktechnique": {"attack_techniques"}, "技术": {"attack_techniques"},
    "ttp": {"attack_tactics", "attack_techniques"},
    "attackmitigation": {"attack_mitigations"}, "缓解措施": {"attack_mitigations"},
}


# 由 auto_routing.json 解析得到的别名映射(归一化别名 → canonical IOC 类型集合)。
# 这是「配置驱动」的别名表,优先级高于下方内置 _LABEL_ALIASES 兜底表。
# 由 load_auto_routing() 在加载/热重载路由配置时刷新。
_CONFIG_ALIASES: Dict[str, Set[str]] = {}

# 由 auto_routing.json 解析得到的「transform(派生)类型」注册表。
# 结构: {canonical类型: {"input": 真实引擎类型, "regex": 已编译正则, "raw_regx": 原始串}}
# 例如 port → {"input": "socket_addresses", "regex": re.compile(r":(\d{1,5})$"), ...}
# 提取时先用 input 类型(socket_addresses)从引擎拿到值,再用正则截取目标片段(端口)。
# 由 load_auto_routing() 在加载/热重载路由配置时刷新。
_TRANSFORM_TYPES: Dict[str, Dict[str, Any]] = {}

# 已知的确定性 IOC 伪模型名(其 route 的 labels 键被视为 canonical IOC 类型)。
_IOC_MODEL_NAMES = {"ioc-finder", "iocextract"}


def _normalize_label(label: str) -> str:
    """标签归一化:小写 + 去除空白/下划线/连字符,便于别名匹配。"""
    return re.sub(r"[\s_\-]+", "", label.strip().lower())


def map_label_to_ioc_types(label: str) -> Set[str]:
    """
    把单个用户标签映射为 canonical IOC 类型集合;映射不到返回空集合。

    优先查 auto_routing.json 配置驱动的别名表(_CONFIG_ALIASES);
    配置里没有时回退到内置 _LABEL_ALIASES 兜底表(保证配置缺失/损坏时仍可用)。
    """
    key = _normalize_label(label)
    if key in _CONFIG_ALIASES:
        return set(_CONFIG_ALIASES[key])
    if key in _LABEL_ALIASES:
        return set(_LABEL_ALIASES[key])
    return set()


def split_labels(labels: List[str]) -> Tuple[Dict[str, Set[str]], List[str]]:
    """
    把用户标签分成 IOC 标签 与 语义(NER)标签。

    Returns:
        (ioc_label_map, ner_labels)
        ioc_label_map: {用户原始标签: {canonical类型,...}}  —— 仅含能映射到 IOC 的标签
        ner_labels:    无法映射到 IOC 类型的标签(交给 NER)
    """
    ioc_label_map: Dict[str, Set[str]] = {}
    ner_labels: List[str] = []
    for lb in labels:
        types = map_label_to_ioc_types(lb)
        if types:
            ioc_label_map[lb] = types
        else:
            ner_labels.append(lb)
    return ioc_label_map, ner_labels


# ─── offset 回填:把规范值定位回(可能被 defang 的)原文 ──────────────────────

# 常见 defang 形式的 "." 替代
_DOT_ALT = r"(?:\s*[\[\(\{]\s*\.\s*[\]\)\}]\s*|\s*[\[\(]\s*dot\s*[\]\)]\s*|\sdot\s|\.)"
# 常见 defang 形式的 "@" 替代
_AT_ALT = r"(?:[\[\(\{]\s*@\s*[\]\)\}]|[\[\(]\s*at\s*[\]\)]|\sat\s|@)"


def _build_loose_pattern(value: str) -> Optional[re.Pattern]:
    """
    把一个规范化 IOC 值转成"容忍 defang"的正则,用于在原文中重新定位。
    返回 None 表示构造失败(应回退到精确匹配)。
    """
    try:
        escaped = re.escape(value)
        # http/https → 允许 hxxp/hxxps (escape 后仍是字面 http)
        escaped = re.sub(r"^https", r"h[tx]{2}ps", escaped)
        escaped = re.sub(r"^http", r"h[tx]{2}p", escaped)
        # "." (escape 后为 \.) → defang 选择组
        escaped = escaped.replace(r"\.", _DOT_ALT)
        # "@" → defang 选择组
        escaped = escaped.replace("@", _AT_ALT)
        return re.compile(escaped, re.IGNORECASE)
    except re.error as exc:
        logger.debug("loose pattern 构造失败 value=%r err=%s", value, exc)
        return None


def _locate_value(value: str, text: str) -> List[Tuple[int, int, str]]:
    """
    在原文 text 中定位规范值 value 的所有出现位置。

    返回 [(start, end, actual_text), ...]。actual_text 是原文真实片段(保留 defang
    原貌);若文本里被 defang 了,actual_text 会和 value 不同。

    策略:先精确匹配(快路径,覆盖未 defang 的多数情况),无命中再用宽松正则。
    """
    spans: List[Tuple[int, int, str]] = []

    # 快路径:精确子串
    if value and value in text:
        start = text.find(value)
        while start != -1:
            spans.append((start, start + len(value), value))
            start = text.find(value, start + 1)
        return spans

    # 慢路径:宽松(容忍 defang)
    pat = _build_loose_pattern(value)
    if pat is None:
        return spans
    for m in pat.finditer(text):
        spans.append((m.start(), m.end(), m.group(0)))
    return spans


# ─── 两个引擎的原始抽取(返回 {canonical_type: [value,...]}) ────────────────

def _extract_with_ioc_finder(text: str, wanted: Set[str]) -> Dict[str, List[str]]:
    """用 ioc-finder 抽取。一次 find_iocs 拿全量,再筛 wanted 类型。

    注意: ioc-finder 9.x 的 find_iocs() 不传 included_ioc_types 时只解析 9 种
    DEFAULT_IOC_TYPES(ipv4s/ipv6s/domains/urls/email_addresses/cves/md5s/sha1s/
    sha256s),其余类型(sha512s/file_paths/mac/asn/socket_addresses/...)会被静默
    跳过。因此必须显式把需要的类型传给 included_ioc_types,否则路由配置里声明的
    大部分类型实际抽不到。对不支持该参数的旧版本(如 7.x)做兼容回退。
    """
    import ioc_finder

    if not wanted:
        return {}
    try:
        result = ioc_finder.find_iocs(text, included_ioc_types=list(wanted))
    except TypeError:
        # 兼容不支持 included_ioc_types 参数的旧版本(默认解析全部类型)
        result = ioc_finder.find_iocs(text)
    out: Dict[str, List[str]] = {}
    for ctype in wanted:
        val = result.get(ctype)
        if not val:
            continue
        if isinstance(val, dict):
            # attack_* 等嵌套结构:摊平
            flat: List[str] = []
            for sub in val.values():
                flat.extend(sub)
            if flat:
                out[ctype] = flat
        else:
            out[ctype] = list(val)
    return out


# iocextract 支持的 canonical 类型 → 抽取函数
def _iocextract_funcs():
    import iocextract
    return {
        "ipv4s":  lambda t: iocextract.extract_ipv4s(t, refang=True),
        "ipv6s":  lambda t: iocextract.extract_ipv6s(t),
        "urls":   lambda t: iocextract.extract_urls(t, refang=True),
        "email_addresses": lambda t: iocextract.extract_emails(t, refang=True),
        "md5s":    lambda t: iocextract.extract_md5_hashes(t),
        "sha1s":   lambda t: iocextract.extract_sha1_hashes(t),
        "sha256s": lambda t: iocextract.extract_sha256_hashes(t),
        "sha512s": lambda t: iocextract.extract_sha512_hashes(t),
    }


def _extract_with_iocextract(text: str, wanted: Set[str]) -> Dict[str, List[str]]:
    """用 iocextract 抽取。仅支持部分类型,不支持的类型静默跳过。"""
    funcs = _iocextract_funcs()
    out: Dict[str, List[str]] = {}
    for ctype in wanted:
        fn = funcs.get(ctype)
        if fn is None:
            continue
        try:
            vals = [v for v in fn(text) if v]
            if vals:
                out[ctype] = vals
        except Exception as exc:  # noqa: BLE001
            logger.warning("iocextract 抽取 %s 失败: %s", ctype, exc)
    return out


def _extract_combined(text: str, wanted: Set[str]) -> Dict[str, List[str]]:
    """ioc-finder ∪ iocextract,按类型合并去重。auto 模式用,互补 defang 覆盖。"""
    a = _extract_with_ioc_finder(text, wanted)
    b = _extract_with_iocextract(text, wanted)
    out: Dict[str, List[str]] = {}
    for ctype in wanted:
        seen: Set[str] = set()
        merged: List[str] = []
        for v in a.get(ctype, []) + b.get(ctype, []):
            if v not in seen:
                seen.add(v)
                merged.append(v)
        if merged:
            out[ctype] = merged
    return out


_BACKENDS: Dict[str, Callable[[str, Set[str]], Dict[str, List[str]]]] = {
    "ioc_finder": _extract_with_ioc_finder,
    "iocextract": _extract_with_iocextract,
    "combined":   _extract_combined,
}


# ─── 核心:抽取 + offset 回填 → 实体列表 ─────────────────────────────────────

def extract_ioc_entities(
    text: str,
    ioc_label_map: Dict[str, Set[str]],
    backend: str,
) -> List[Dict[str, Any]]:
    """
    抽取 IOC 并回填 offset,返回与 GLiNER 实体同构的 dict 列表。

    Args:
        text:           原文
        ioc_label_map:  {用户原始标签: {canonical类型,...}}
        backend:        "ioc_finder" / "iocextract" / "combined"

    Returns:
        [{"text","label","start","end","score","chunk_index","chunk_char_start","chunk_char_end"}]
        label 为用户原始标签;score 恒为 1.0(确定性,无置信度概念)。
    """
    if not text or not ioc_label_map:
        return []

    extract_fn = _BACKENDS.get(backend, _extract_with_ioc_finder)

    # 需要抽取的全部 canonical 类型(可能含 transform 派生类型,如 port)
    wanted: Set[str] = set()
    for types in ioc_label_map.values():
        wanted |= types

    # 把 transform 派生类型解析为它依赖的「真实引擎类型」(input);
    # 例如 port → socket_addresses。真实类型保持原样。
    engine_wanted: Set[str] = set()
    for t in wanted:
        tdef = _TRANSFORM_TYPES.get(t)
        engine_wanted.add(tdef["input"] if tdef else t)

    raw = extract_fn(text, engine_wanted)  # {engine_type: [value,...]}

    # canonical 类型 → 哪些用户标签需要它(一个类型可能被多个标签共享)
    type_to_labels: Dict[str, List[str]] = {}
    for user_label, types in ioc_label_map.items():
        for t in types:
            type_to_labels.setdefault(t, []).append(user_label)

    entities: List[Dict[str, Any]] = []
    # 去重:同一 (start, end, label) 只保留一次
    seen_spans: Set[Tuple[int, int, str]] = set()

    def _emit(start: int, end: int, actual: str, user_labels: List[str]) -> None:
        for user_label in user_labels:
            key = (start, end, user_label)
            if key in seen_spans:
                continue
            seen_spans.add(key)
            entities.append({
                "text": actual,
                "label": user_label,
                "start": start,
                "end": end,
                "score": 1.0,
                "chunk_index": 0,
                "chunk_char_start": 0,
                "chunk_char_end": len(text),
            })

    for ctype, user_labels in type_to_labels.items():
        if not user_labels:
            continue
        tdef = _TRANSFORM_TYPES.get(ctype)
        if tdef is None:
            # 普通类型:直接回填引擎抽取到的值
            for value in raw.get(ctype, []):
                for (start, end, actual) in _locate_value(value, text):
                    _emit(start, end, actual, user_labels)
        else:
            # transform 类型:先取 input 引擎类型的值,再用正则在原文片段里截取目标
            # 片段(如端口)。优先取捕获组 1,没有捕获组则取整个匹配;并据匹配在
            # 片段内的相对位置换算出目标片段在原文中的绝对 offset。
            pattern = tdef["regex"]
            input_type = tdef["input"]
            for value in raw.get(input_type, []):
                for (start, end, actual) in _locate_value(value, text):
                    m = pattern.search(actual)
                    if not m:
                        continue
                    gi = 1 if m.lastindex else 0
                    sub = m.group(gi)
                    if not sub:
                        continue
                    _emit(start + m.start(gi), start + m.end(gi), sub, user_labels)

    entities.sort(key=lambda e: (e["start"], e["end"]))
    return entities


def _single_chunk_metadata_dict(text: str, chunk_cfg, status: str = "done",
                                error: Optional[str] = None) -> Dict[str, Any]:
    """构造"不分块"的 chunk_metadata,与 UIE / LLM 降级路径格式一致。"""
    return {
        "chunking_enabled": False,
        "model_max_tokens": None,
        "labels_token_count": None,
        "effective_max_tokens": None,
        "overlap": getattr(chunk_cfg, "overlap", 0),
        "safety_margin": getattr(chunk_cfg, "safety_margin", 0),
        "threshold": getattr(chunk_cfg, "threshold", 0.5),
        "chunk_count": 1,
        "chunks": [{
            "chunk_index": 0,
            "char_start": 0,
            "char_end": len(text),
            "char_length": len(text),
            "token_count": None,
            "status": status,
            "error": error,
        }],
    }


# ─── 伪模型:纯 IOC 提取 ─────────────────────────────────────────────────────

class IOCExtractorModel:
    """
    把单个 IOC 引擎封装成与 NER 模型同构的伪模型。

    backend: "ioc_finder" 或 "iocextract"
    """
    model_max_tokens: Optional[int] = None  # 不分块
    model_type: str = "ioc"

    def __init__(self, name: str, backend: str) -> None:
        self.name = name
        self.backend = backend
        self._lock = threading.Lock()

    def load(self) -> bool:
        try:
            if self.backend in ("ioc_finder", "combined"):
                import ioc_finder  # noqa: F401
            if self.backend in ("iocextract", "combined"):
                import iocextract  # noqa: F401
            print(f"✓ IOC 提取器 {self.name} (backend={self.backend}) 就绪")
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"✗ IOC 提取器 {self.name} 加载失败: {exc}")
            return False

    def predict(self, text: str, labels: List[str]) -> List[Tuple]:
        """向后兼容的单次调用。"""
        ioc_label_map, _ = split_labels(labels)
        ents = extract_ioc_entities(text, ioc_label_map, self.backend)
        return [(e["text"], e["label"], e["start"], e["end"], e["score"]) for e in ents]

    def predict_with_chunks(
        self, text: str, labels: List[str], chunk_cfg,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        ioc_label_map, ner_labels = split_labels(labels)
        if ner_labels:
            logger.info(
                "%s: 以下标签无法用 IOC 引擎提取,已忽略(如需提取请用 NER 模型或 auto): %s",
                self.name, ner_labels,
            )
        with self._lock:
            try:
                entities = extract_ioc_entities(text, ioc_label_map, self.backend)
            except Exception as exc:  # noqa: BLE001
                logger.error("%s: IOC 提取失败: %s", self.name, exc)
                return [], _single_chunk_metadata_dict(
                    text, chunk_cfg, status="failed", error=str(exc)[:200],
                )
        for e in entities:
            e["model_name"] = self.name
        return entities, _single_chunk_metadata_dict(text, chunk_cfg)


# ─── 伪模型:auto 智能分流 ───────────────────────────────────────────────────

def _overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return max(a_start, b_start) < min(a_end, b_end)


# auto 路由配置文件路径(与本模块同目录)
_ROUTING_PATH = Path(__file__).parent / "auto_routing.json"


def _iter_route_labels(labels_field: Any):
    """
    统一遍历一个 route 的 labels 字段,兼容三种形态:

      - 简写数组形态: {canonical类型: [别名,...]}
            → yield (canonical, [别名...], None)
      - 扩展对象形态: {canonical类型: {"type":"transform","mapping":[别名...],
                       "input": 真实引擎类型, "regx": 正则}}
            → yield (canonical, [别名...], transform_spec)
        (mapping 等价于简写里的别名数组;input 为本类型派生所依赖的真实引擎类型;
         regx 为从 input 抽取值里截取目标片段的正则。)
      - 旧版纯数组形态: [标签, ...]
            → yield (标签, [], None)  (canonical 未知)

    返回 (canonical_or_label, alias_list, transform_spec_or_None)。
    """
    if isinstance(labels_field, dict):
        for canonical, spec in labels_field.items():
            if not isinstance(canonical, str):
                continue
            if isinstance(spec, dict):
                # 扩展对象形态:别名来自 mapping
                alias_list = [a for a in (spec.get("mapping") or []) if isinstance(a, str)]
                yield canonical, alias_list, spec
            else:
                # 简写数组形态:spec 即别名数组
                alias_list = [a for a in (spec or []) if isinstance(a, str)]
                yield canonical, alias_list, None
    elif isinstance(labels_field, list):
        for lb in labels_field:
            if isinstance(lb, str):
                yield lb, [], None


def _compile_transform(canonical: str, spec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    把一个 transform 类型定义编译为 {"input", "regex", "raw_regx"};无效时返回 None。

    spec 形如 {"type":"transform","mapping":[...],"input":"socket_addresses","regx":"..."}。
    input 缺失 / regx 缺失或编译失败 → 记录警告并返回 None(该 transform 类型被忽略)。
    """
    input_type = spec.get("input")
    regx = spec.get("regx") or spec.get("regex")
    if not isinstance(input_type, str) or not input_type:
        logger.warning("transform 类型 '%s' 缺少有效 input,已忽略", canonical)
        return None
    if not isinstance(regx, str) or not regx:
        logger.warning("transform 类型 '%s' 缺少有效 regx,已忽略", canonical)
        return None
    try:
        pattern = re.compile(regx)
    except re.error as exc:
        logger.warning("transform 类型 '%s' 正则编译失败(%s),已忽略", canonical, exc)
        return None
    return {"input": input_type, "regex": pattern, "raw_regx": regx}


def load_auto_routing(
    path: Optional[Path] = None,
) -> tuple:
    """
    读取 auto 路由配置 JSON,并刷新全局别名表 _CONFIG_ALIASES(副作用)。

    Returns:
        (label_to_models, model_priorities, fallback_models)
        label_to_models : {归一化标签: [模型名,...]}  —— canonical 与每个别名都登记
        model_priorities: {模型名: 优先级int(0-100,越大越优先)}
        fallback_models : [labels 为空的模型名,...] —— 兜底:处理未匹配的标签

    同时构建 _CONFIG_ALIASES: {归一化别名/类型: {canonical类型,...}},
    仅针对确定性 IOC 模型(ioc-finder / iocextract)的 route。

    配置缺失或损坏时返回 ({}, {}, []),清空配置别名表,并打印警告,不阻断启动。
    """
    p = path or _ROUTING_PATH
    label_to_models: Dict[str, List[str]] = {}
    config_aliases: Dict[str, Set[str]] = {}
    transform_types: Dict[str, Dict[str, Any]] = {}
    model_priorities: Dict[str, int] = {}
    fallback_models: List[str] = []

    def _commit_aliases() -> None:
        """把本次解析得到的配置别名表 / transform 表提交到全局(失败时清空回退)。"""
        global _CONFIG_ALIASES, _TRANSFORM_TYPES
        _CONFIG_ALIASES = config_aliases
        _TRANSFORM_TYPES = transform_types

    try:
        with open(p, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        logger.warning("auto 路由配置不存在: %s,所有标签将走兜底模型", p)
        _commit_aliases()
        return label_to_models, model_priorities, fallback_models
    except Exception as exc:  # noqa: BLE001
        logger.error("auto 路由配置解析失败 (%s): %s,所有标签将走兜底模型", p, exc)
        _commit_aliases()
        return label_to_models, model_priorities, fallback_models

    seen_priorities: Dict[int, str] = {}
    for route in cfg.get("routes", []):
        model_name = route.get("model")
        if not model_name:
            continue
        # 优先级(0-100,越大越优先);各 route 间应唯一,重复时仅告警。
        try:
            priority = int(route.get("priority", 0))
        except (TypeError, ValueError):
            priority = 0
        if priority in seen_priorities and seen_priorities[priority] != model_name:
            logger.warning("auto 路由: 优先级 %d 同时用于 '%s' 和 '%s',建议保持唯一",
                           priority, seen_priorities[priority], model_name)
        seen_priorities[priority] = model_name
        model_priorities[model_name] = priority

        is_ioc_model = model_name in _IOC_MODEL_NAMES
        labels_field = route.get("labels")
        has_labels = False
        for canonical, alias_list, transform in _iter_route_labels(labels_field):
            has_labels = True
            # canonical 名本身也算一个可输入标签
            names = [canonical, *alias_list]
            for name in names:
                key = _normalize_label(name)
                if not key:
                    continue
                # 路由表: canonical 与每个别名都路由到该模型
                label_to_models.setdefault(key, [])
                if model_name not in label_to_models[key]:
                    label_to_models[key].append(model_name)
                # 别名表: 仅对 IOC 模型,且仅当 labels 为对象形态(canonical 已知)
                if is_ioc_model and isinstance(labels_field, dict):
                    config_aliases.setdefault(key, set()).add(canonical)
            # transform(派生)类型: 仅对 IOC 模型有意义,编译成功才登记
            if is_ioc_model and transform is not None:
                compiled = _compile_transform(canonical, transform)
                if compiled is not None:
                    transform_types[canonical] = compiled
        # labels 为空 → 兜底模型(处理所有未匹配标签)
        if not has_labels and model_name not in fallback_models:
            fallback_models.append(model_name)

    _commit_aliases()
    return label_to_models, model_priorities, fallback_models


class AutoExtractorModel:
    """
    智能分流伪模型(基于 api/auto_routing.json 配置):
      - 按路由配置把每个标签分派给声明处理它的模型;
      - 未在任何 route 中匹配到的标签 → 兜底模型(labels 为空 {} 的 route);
      - 各模型分别处理自己分到的标签,结果按「优先级」合并:
        每个 route 配有 priority(0-100,越大越优先,各 route 唯一)。当不同模型的
        实体区间重叠时,优先级高者保留,低者被丢弃;完全相同区间(start,end,label)
        的重复实体只保留优先级最高的一个。

    依赖注入:
      set_registry(models_dict)
        models_dict : load_models 返回的 {name: model} 引用
    兜底模型与优先级全部来自 auto_routing.json,不再依赖 .env。
    """
    model_max_tokens: Optional[int] = None
    model_type: str = "auto"

    def __init__(self, name: str = "auto",
                 routing: Optional[Dict[str, List[str]]] = None,
                 priorities: Optional[Dict[str, int]] = None,
                 fallback_models: Optional[List[str]] = None) -> None:
        self.name = name
        if routing is not None:
            self._routing = routing
            self._priorities: Dict[str, int] = priorities or {}
            self._fallback_models: List[str] = list(fallback_models or [])
        else:
            self._routing, self._priorities, self._fallback_models = load_auto_routing()
        self._models: Dict[str, Any] = {}
        self._lock = threading.Lock()

    def set_registry(self, models_dict: Dict[str, Any]) -> None:
        self._models = models_dict

    def reload_routing(self, models_dict: Dict[str, Any]) -> List[str]:
        """
        热重载路由配置（读取 auto_routing.json），无需重启服务。

        Args:
            models_dict: 当前已加载的模型字典（与 set_registry 保持一致）

        Returns:
            实际生效的兜底模型名列表（labels 为空的 route）
        """
        new_routing, new_priorities, new_fallback = load_auto_routing()
        with self._lock:
            self._routing = new_routing
            self._priorities = new_priorities
            self._fallback_models = new_fallback
            self._models = models_dict
            logger.info("auto 路由已热重载：%d 条标签规则，兜底模型: %s",
                        len(new_routing), new_fallback or "(无)")
        return new_fallback

    def load(self) -> bool:
        try:
            import ioc_finder  # noqa: F401
            import iocextract  # noqa: F401
            n_rules = len(self._routing)
            print(f"✓ auto 智能提取器就绪 (路由规则 {n_rules} 条)")
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"✗ auto 智能提取器加载失败: {exc}")
            return False

    def _route_labels(self, labels: List[str]) -> Dict[str, List[str]]:
        """
        把标签列表按路由配置分派到各模型。

        Returns:
            {模型名: [该模型要处理的原始标签,...]}。
            未匹配的标签归到所有兜底模型(labels 为空的 route);无兜底模型时忽略。
        """
        assignment: Dict[str, List[str]] = {}
        for lb in labels:
            key = _normalize_label(lb)
            targets = self._routing.get(key)
            if not targets:
                # 未匹配 → 兜底模型(可能多个)
                if self._fallback_models:
                    targets = list(self._fallback_models)
                else:
                    logger.warning("auto: 标签 '%s' 未匹配任何路由且无兜底模型,忽略", lb)
                    continue
            for model_name in targets:
                assignment.setdefault(model_name, []).append(lb)
        return assignment

    def predict(self, text: str, labels: List[str]) -> List[Tuple]:
        ents, _ = self.predict_with_chunks(text, labels, _DummyCfg())
        return [(e["text"], e["label"], e["start"], e["end"], e["score"]) for e in ents]

    def predict_with_chunks(
        self, text: str, labels: List[str], chunk_cfg,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        assignment = self._route_labels(labels)

        # 收集所有模型的实体,并记录各自来源模型的优先级,用于后续按优先级合并。
        scored_entities: List[Tuple[int, Dict[str, Any]]] = []  # (priority, entity)
        ner_metadata: Optional[Dict[str, Any]] = None

        for model_name, sub_labels in assignment.items():
            model = self._models.get(model_name)
            if model is None:
                logger.warning("auto: 路由到的模型 '%s' 未加载,跳过标签 %s", model_name, sub_labels)
                continue
            # 防递归:auto 不应路由到自身
            if model is self or getattr(model, "model_type", "") == "auto":
                logger.warning("auto: 路由目标 '%s' 是 auto 自身,跳过避免递归", model_name)
                continue
            try:
                ents, meta = model.predict_with_chunks(text, sub_labels, chunk_cfg)
            except Exception as exc:  # noqa: BLE001
                logger.error("auto: 模型 '%s' 处理标签 %s 失败: %s", model_name, sub_labels, exc)
                continue

            priority = self._priorities.get(model_name, 0)
            # 给每条实体注入来源模型名(如果子模型自身未注入)
            for e in ents:
                e.setdefault("model_name", model_name)
                scored_entities.append((priority, e))

            # 取第一个真实分块模型(非 IOC 伪模型)的 metadata 作为整体 chunk_metadata
            if (ner_metadata is None and meta is not None
                    and getattr(model, "model_type", "") not in ("ioc",)):
                ner_metadata = meta

        merged = self._merge_by_priority(scored_entities)

        metadata = ner_metadata if ner_metadata is not None else _single_chunk_metadata_dict(text, chunk_cfg)
        return merged, metadata

    @staticmethod
    def _merge_by_priority(
        scored_entities: List[Tuple[int, Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        """
        按优先级合并不同模型的实体。

        规则:
          - 按优先级从高到低处理(同优先级保持稳定顺序);
          - 完全相同区间且同标签(start,end,label)的重复实体只保留优先级最高的一个;
          - 若某实体区间与「优先级严格更高」的已保留实体重叠,则被丢弃;
          - 同优先级(同一模型或并列模型)之间的区间重叠互不丢弃(允许嵌套实体)。
        """
        # 稳定排序: 优先级降序
        ordered = sorted(scored_entities, key=lambda pe: -pe[0])
        kept: List[Dict[str, Any]] = []
        kept_spans: List[Tuple[int, int, int]] = []  # (start, end, priority)
        seen_exact: Set[Tuple[int, int, str]] = set()
        dropped = 0

        for pri, e in ordered:
            exact_key = (e["start"], e["end"], e["label"])
            if exact_key in seen_exact:
                dropped += 1
                continue
            # 与严格更高优先级的已保留实体重叠 → 丢弃
            overlapped = any(
                kp > pri and _overlaps(e["start"], e["end"], s, en)
                for (s, en, kp) in kept_spans
            )
            if overlapped:
                dropped += 1
                continue
            seen_exact.add(exact_key)
            kept.append(e)
            kept_spans.append((e["start"], e["end"], pri))

        if dropped:
            logger.info("auto: 按优先级合并丢弃了 %d 个重叠/重复实体", dropped)

        kept.sort(key=lambda e: (e["start"], e["end"]))
        return kept


class _DummyCfg:
    """predict() 兼容用的最小 chunk_cfg。"""
    enabled = True
    overlap = 32
    safety_margin = 16
    threshold = 0.5


# ─── 模块加载时预热配置别名表 ────────────────────────────────────────────────
# 让独立的 ioc-finder / iocextract 伪模型(不经过 AutoExtractorModel)也能用上
# auto_routing.json 里配置的别名映射。失败时静默回退到内置 _LABEL_ALIASES。
try:
    load_auto_routing()
except Exception as _exc:  # noqa: BLE001
    logger.warning("启动时预加载 auto_routing 别名表失败,回退内置别名: %s", _exc)
