# -*- coding: utf-8 -*-
"""
TRAM (SciBERT) ATT&CK 技术识别伪模型

把 MITRE TRAM 的 SciBERT 分类模型封装成与 GLiNER / UIE / IOC 完全相同的「伪模型」,
实现 predict_with_chunks(text, labels, chunk_cfg) 接口,从而复用现有的任务队列、
缓存、回调、去重等全部基础设施,无需改动 queue_manager。

与 NER 模型的区别:
  - TRAM 是「句子级多标签分类器」而非「span 抽取器」: 它把一段文本切成句子,
    对每个句子判断它命中了哪些 ATT&CK 技术(50 类,见 bert_model/classes.txt)。
  - 返回的实体 label 即技术编号(如 "T1059.003"),句子文本作为实体 text、句子在
    原文中的区间作为实体 offset,并用 models/attack/techniques.json 富化出技术
    名称 / 战术 / 官方 URL 等附加字段。
  - labels 语义:默认「全量模式」——无需指定,直接返回全部命中阈值的技术
    (填 "full"/"全量"/"ATT&CK"/"TTP" 等哨兵词或任意语义词都走全量)。若 labels
    里包含技术编号(T1059 / T1059.003,父编号匹配其全部子技术)或战术 shortname,
    则进入「过滤模式」只返回匹配项。详见 _parse_filters。
  - 不接入通用 ChunkingService(model_max_tokens=None)，由本模块执行安全句界切分、
    短片段合并和超长句 token 滑窗；正常句保持逐句分类，仅对低置信度句补充一个
    相邻句上下文。同一技术按证据类型与分数融合，优先返回直接句子证据。

推理逻辑:
  direct_probs  = sigmoid(bert(安全句子或超长句 token 窗口))
  context_probs = sigmoid(bert(低置信度句子 + 一个相邻句))
  最终候选       = 按技术融合，direct 证据优先，其次比较概率

阈值复用请求的 chunk_cfg.threshold(默认取 .env 的 CHUNK_THRESHOLD)。

相关环境变量(见 api/.env):
  TRAM_MAX_LENGTH / TRAM_BATCH_SIZE / TRAM_MIN_SENT_CHARS
  TRAM_LONG_OVERLAP_TOKENS / TRAM_CONTEXT_ENABLED
  TRAM_CONTEXT_TRIGGER_THRESHOLD / TRAM_DEVICE
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import attack_common as ac

logger = logging.getLogger(__name__)

# ─── 路径解析(与 api/models.py 的 MODELS_ROOT 保持一致的独立推导)──────────────
_DEFAULT_MODELS_ROOT = Path(__file__).parent.parent / "models"
MODELS_ROOT = Path(os.getenv("MODELS_ROOT", str(_DEFAULT_MODELS_ROOT)))

# ─── 可调参数(.env 覆盖)────────────────────────────────────────────────────
_MAX_LENGTH = int(os.getenv("TRAM_MAX_LENGTH", "256"))
_BATCH_SIZE = int(os.getenv("TRAM_BATCH_SIZE", "16"))
_MIN_SENT_CHARS = int(os.getenv("TRAM_MIN_SENT_CHARS", "3"))
_LONG_OVERLAP_TOKENS = int(os.getenv("TRAM_LONG_OVERLAP_TOKENS", "32"))
_CONTEXT_ENABLED = os.getenv("TRAM_CONTEXT_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}
_CONTEXT_TRIGGER_THRESHOLD = float(os.getenv("TRAM_CONTEXT_TRIGGER_THRESHOLD", "0.45"))
_FORCE_DEVICE = os.getenv("TRAM_DEVICE", "").strip().lower()

Span = Tuple[int, int]
Window = Dict[str, Any]
_STRONG_TERMINATORS = frozenset("!?;。！？；\n\r")
_CLOSING_PUNCTUATION = frozenset("'\"”’)]}）】》")


def _load_classes(path: Path) -> List[str]:
    """读取 classes.txt: 每行一个技术编号,行号(0-based)对应模型输出下标。"""
    text = path.read_text(encoding="utf-8")
    return [line.strip() for line in text.split() if line.strip()]


def _trim_span(text: str, start: int, end: int) -> Optional[Span]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return (start, end) if end > start else None


def _period_is_boundary(text: str, index: int) -> bool:
    previous = text[index - 1] if index else ""
    following = text[index + 1] if index + 1 < len(text) else ""
    if previous.isdigit() and following.isdigit():
        return False
    if previous.isalnum() and following.isalnum():
        return False
    cursor = index + 1
    while cursor < len(text) and text[cursor] in _CLOSING_PUNCTUATION:
        cursor += 1
    return cursor >= len(text) or text[cursor].isspace()


def split_sentences(text: str, min_chars: int = _MIN_SENT_CHARS) -> List[Tuple[str, int, int]]:
    """按安全句界切分并合并短片段，始终保留精确原文 offset。"""
    if not text or not text.strip():
        return []

    raw: List[Span] = []
    start = 0
    index = 0
    while index < len(text):
        char = text[index]
        boundary = char in _STRONG_TERMINATORS or (char == "." and _period_is_boundary(text, index))
        if not boundary:
            index += 1
            continue
        end = index + 1
        while end < len(text):
            following = text[end]
            if following in _STRONG_TERMINATORS or (following == "." and _period_is_boundary(text, end)):
                end += 1
            else:
                break
        span = _trim_span(text, start, end)
        if span:
            raw.append(span)
        start = end
        index = end
    tail = _trim_span(text, start, len(text))
    if tail:
        raw.append(tail)

    merged: List[Span] = []
    pending: Optional[Span] = None
    for span in raw:
        non_space = sum(not char.isspace() for char in text[span[0]:span[1]])
        if non_space < min_chars:
            pending = (pending[0], span[1]) if pending else span
            continue
        if pending:
            combined = _trim_span(text, pending[0], span[1])
            if combined:
                merged.append(combined)
            pending = None
        else:
            merged.append(span)
    if pending:
        combined = _trim_span(text, merged[-1][0] if merged else pending[0], pending[1])
        if combined:
            if merged:
                merged[-1] = combined
            else:
                merged.append(combined)
    return [(text[start:end], start, end) for start, end in merged]


def _fallback_metadata(
    text: str, chunk_cfg, status: str = "done", error: Optional[str] = None,
) -> Dict[str, Any]:
    chunks = []
    if text and status == "failed":
        chunks.append({
            "chunk_index": 0, "char_start": 0, "char_end": len(text),
            "char_length": len(text), "token_count": None,
            "evidence_kind": None, "status": status, "error": error,
        })
    return {
        "chunking_enabled": True,
        "chunking_strategy": "tram_sentence_context_windows",
        "model_max_tokens": _MAX_LENGTH,
        "labels_token_count": None,
        "effective_max_tokens": None,
        "overlap": _LONG_OVERLAP_TOKENS,
        "overlap_unit": "tokens",
        "safety_margin": getattr(chunk_cfg, "safety_margin", 0),
        "threshold": getattr(chunk_cfg, "threshold", 0.5),
        "chunk_count": len(chunks),
        "direct_window_count": 0,
        "context_window_count": 0,
        "context_enabled": _CONTEXT_ENABLED,
        "context_trigger_threshold": _CONTEXT_TRIGGER_THRESHOLD,
        "chunks": chunks,
    }


class _DefaultCfg:
    """predict() 兼容用的极简 chunk_cfg,提供默认阈值。"""
    threshold = 0.5
    overlap = 0
    safety_margin = 0
    enabled = False


# ─── 伪模型:TRAM SciBERT ATT&CK 技术分类 ────────────────────────────────────

class TRAMModel:
    """
    TRAM SciBERT ATT&CK 技术识别伪模型。

    接口与 NER / IOC 模型同构:load() / predict() / predict_with_chunks()。
    model_type = "attack" 供前端区分(与 ner / ioc / llm / auto 并列)。
    """
    model_max_tokens: Optional[int] = None  # 不接入通用 ChunkingService(自行切句)
    model_type: str = "attack"

    def __init__(self, name: str = "tram", models_root: Optional[Path] = None) -> None:
        self.name = name
        self.model = None
        self.tokenizer = None
        self.classes: List[str] = []
        self.attack_meta: Dict[str, Dict[str, Any]] = {}
        self._known_tactics: set = set()  # 归一化后的战术 shortname 集合(过滤用)
        self._device = None
        self._payload_max_tokens: Optional[int] = None
        # 推理会串行占用较多内存,加锁保护
        self._lock = threading.Lock()

        # 允许调用方(models._make_tram_model)注入 MODELS_ROOT;缺省用模块级推导。
        root = Path(models_root) if models_root else MODELS_ROOT
        self._tram_dir = root / "tram"
        self._bert_dir = self._tram_dir / "bert_model"
        self._tokenizer_dir = self._tram_dir / "scibert_tokenizer"
        self._classes_path = self._bert_dir / "classes.txt"
        self._attack_path = root / "attack" / "techniques.json"

    # ── 设备选择 ────────────────────────────────────────────────────────
    def _resolve_device(self):
        import torch
        if _FORCE_DEVICE:
            if _FORCE_DEVICE == "cuda" and not torch.cuda.is_available():
                raise ValueError("TRAM_DEVICE=cuda, but CUDA is unavailable")
            if _FORCE_DEVICE == "mps" and not torch.backends.mps.is_available():
                raise ValueError("TRAM_DEVICE=mps, but MPS is unavailable")
            if _FORCE_DEVICE not in {"cpu", "cuda", "mps"}:
                raise ValueError(f"unsupported TRAM_DEVICE: {_FORCE_DEVICE}")
            return torch.device(_FORCE_DEVICE)
        if torch.cuda.is_available():
            return torch.device("cuda")
        if torch.backends.mps.is_available():
            return torch.device("mps")
        return torch.device("cpu")

    # ── 加载 ────────────────────────────────────────────────────────────
    def load(self) -> bool:
        try:
            from transformers import AutoTokenizer, BertForSequenceClassification

            if not self._bert_dir.is_dir():
                print(f"✗ TRAM 模型目录不存在: {self._bert_dir}")
                print("  请先运行 benchmark/download_tram.py 下载模型")
                return False

            self.classes = _load_classes(self._classes_path)
            if not self.classes:
                print(f"✗ TRAM classes.txt 为空或不存在: {self._classes_path}")
                return False

            tok_source = self._tokenizer_dir if self._tokenizer_dir.is_dir() else self._bert_dir
            self.tokenizer = AutoTokenizer.from_pretrained(str(tok_source))
            if not getattr(self.tokenizer, "is_fast", False):
                raise ValueError("TRAM requires a fast tokenizer for offset-preserving token windows")
            special_tokens = self.tokenizer.num_special_tokens_to_add(pair=False)
            self._payload_max_tokens = _MAX_LENGTH - special_tokens
            if self._payload_max_tokens <= 0:
                raise ValueError("TRAM_MAX_LENGTH leaves no room for content tokens")
            if not 0 <= _LONG_OVERLAP_TOKENS < self._payload_max_tokens:
                raise ValueError("TRAM_LONG_OVERLAP_TOKENS must be smaller than the content-token limit")
            if not 0.0 <= _CONTEXT_TRIGGER_THRESHOLD <= 1.0:
                raise ValueError("TRAM_CONTEXT_TRIGGER_THRESHOLD must be between 0 and 1")
            model = BertForSequenceClassification.from_pretrained(str(self._bert_dir))
            model.eval()

            self._device = self._resolve_device()
            model.to(self._device)
            self.model = model

            # classes.txt 行数应与模型输出维度一致
            n_labels = int(getattr(model.config, "num_labels", len(self.classes)))
            if n_labels != len(self.classes):
                logger.warning(
                    "TRAM: classes.txt 行数(%d)与模型 num_labels(%d)不一致,"
                    "可能导致技术编号错位", len(self.classes), n_labels,
                )

            self.attack_meta = ac.load_attack_metadata(self._attack_path)
            # 预计算已知战术 shortname 集合(供按战术过滤)
            self._known_tactics = ac.build_known_tactics(self.attack_meta)

            print(
                f"✓ {self.name} 加载成功 (SciBERT, {len(self.classes)} 类技术, "
                f"device={self._device}, 富化元数据 {len(self.attack_meta)} 条)"
            )
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"✗ {self.name} 加载失败: {exc}")
            return False

    # ── 窗口构造与批量分类 ──────────────────────────────────────────────
    def _token_count(self, text: str) -> int:
        return len(self.tokenizer.encode(text, add_special_tokens=False))

    def _classify(self, passages: List[str]) -> List[Dict[str, float]]:
        """批量分类已限长的句子/窗口，禁止静默截断。"""
        import torch

        results: List[Dict[str, float]] = []
        for index in range(0, len(passages), _BATCH_SIZE):
            batch = passages[index:index + _BATCH_SIZE]
            enc = self.tokenizer(batch, return_tensors="pt", padding=True, truncation=False)
            if int(enc["input_ids"].shape[1]) > _MAX_LENGTH:
                raise ValueError("TRAM window exceeds TRAM_MAX_LENGTH")
            enc = {key: value.to(self._device) for key, value in enc.items()}
            with torch.no_grad():
                logits = self.model(**enc).logits
            probs = logits.sigmoid().to("cpu")
            for row in probs:
                results.append({self.classes[j]: float(row[j]) for j in range(len(self.classes))})
        return results

    def _split_long_sentence(
        self, text: str, sentence: Tuple[str, int, int], source_index: int,
    ) -> List[Window]:
        content, start, end = sentence
        encoded = self.tokenizer(content, add_special_tokens=False, return_offsets_mapping=True)
        offsets = encoded["offset_mapping"]
        payload_max = int(self._payload_max_tokens or 0)
        if len(offsets) <= payload_max:
            return [{
                "text": content, "start": start, "end": end,
                "token_count": len(offsets), "kind": "direct",
                "source_index": source_index, "context_eligible": True,
            }]

        windows: List[Window] = []
        step = payload_max - _LONG_OVERLAP_TOKENS
        token_start = 0
        while token_start < len(offsets):
            token_end = min(len(offsets), token_start + payload_max)
            span = _trim_span(
                text, start + offsets[token_start][0], start + offsets[token_end - 1][1],
            )
            if span:
                windows.append({
                    "text": text[span[0]:span[1]], "start": span[0], "end": span[1],
                    "token_count": token_end - token_start, "kind": "direct",
                    "source_index": source_index, "context_eligible": False,
                })
            if token_end == len(offsets):
                break
            token_start += step
        return windows

    def _build_direct_windows(
        self, text: str, sentences: List[Tuple[str, int, int]],
    ) -> List[Window]:
        windows: List[Window] = []
        for source_index, sentence in enumerate(sentences):
            windows.extend(self._split_long_sentence(text, sentence, source_index))
        return windows

    def _build_context_windows(
        self, text: str, sentences: List[Tuple[str, int, int]],
        direct_windows: List[Window], distributions: List[Dict[str, float]],
        technique_ids: set, tactics: set, threshold: float,
    ) -> List[Window]:
        if not _CONTEXT_ENABLED or len(sentences) < 2:
            return []
        trigger = max(threshold, _CONTEXT_TRIGGER_THRESHOLD)
        contexts: List[Window] = []
        seen: set = set()
        for window, distribution in zip(direct_windows, distributions):
            if not window["context_eligible"]:
                continue
            best_allowed = max(
                (score for technique, score in distribution.items()
                 if self._passes_filter(technique, technique_ids, tactics)),
                default=0.0,
            )
            if best_allowed >= trigger:
                continue
            source_index = window["source_index"]
            neighbors = []
            if source_index > 0:
                neighbors.append(source_index - 1)
            if source_index + 1 < len(sentences):
                neighbors.append(source_index + 1)
            for neighbor_index in neighbors:
                start = min(sentences[source_index][1], sentences[neighbor_index][1])
                end = max(sentences[source_index][2], sentences[neighbor_index][2])
                key = (start, end)
                if key in seen:
                    break
                passage = text[start:end]
                token_count = self._token_count(passage)
                if token_count > int(self._payload_max_tokens or 0):
                    continue
                seen.add(key)
                contexts.append({
                    "text": passage, "start": start, "end": end,
                    "token_count": token_count, "kind": "context",
                    "source_index": source_index, "context_eligible": False,
                })
                break
        return contexts

    def _metadata(
        self, text: str, chunk_cfg, windows: Optional[List[Window]] = None,
        status: str = "done", error: Optional[str] = None,
    ) -> Dict[str, Any]:
        rows = windows or []
        chunks = [{
            "chunk_index": index,
            "char_start": row["start"], "char_end": row["end"],
            "char_length": row["end"] - row["start"],
            "token_count": row["token_count"], "evidence_kind": row["kind"],
            "status": status, "error": error,
        } for index, row in enumerate(rows)]
        if not chunks and text and status == "failed":
            return _fallback_metadata(text, chunk_cfg, status=status, error=error)
        return {
            "chunking_enabled": True,
            "chunking_strategy": "tram_sentence_context_windows",
            "model_max_tokens": _MAX_LENGTH,
            "labels_token_count": None,
            "effective_max_tokens": self._payload_max_tokens,
            "overlap": _LONG_OVERLAP_TOKENS,
            "overlap_unit": "tokens",
            "safety_margin": getattr(chunk_cfg, "safety_margin", 0),
            "threshold": getattr(chunk_cfg, "threshold", 0.5),
            "chunk_count": len(chunks),
            "direct_window_count": sum(row["kind"] == "direct" for row in rows),
            "context_window_count": sum(row["kind"] == "context" for row in rows),
            "context_enabled": _CONTEXT_ENABLED,
            "context_trigger_threshold": _CONTEXT_TRIGGER_THRESHOLD,
            "chunks": chunks,
        }

    @staticmethod
    def _prefer_candidate(candidate: Dict[str, Any], previous: Dict[str, Any]) -> bool:
        candidate_direct = candidate["_evidence_kind"] == "direct"
        previous_direct = previous["_evidence_kind"] == "direct"
        if candidate_direct != previous_direct:
            return candidate_direct
        if candidate["score"] != previous["score"]:
            return candidate["score"] > previous["score"]
        candidate_length = candidate["end"] - candidate["start"]
        previous_length = previous["end"] - previous["start"]
        if candidate_length != previous_length:
            return candidate_length < previous_length
        return candidate["chunk_index"] < previous["chunk_index"]

    # ── 过滤 / 富化(委托 attack_common,与 attack-llm 保持一致语义)──────
    def _enrich(self, technique: str) -> Dict[str, Any]:
        return ac.enrich(technique, self.attack_meta)

    def _parse_filters(self, labels: List[str]) -> Tuple[set, set]:
        return ac.parse_filters(labels, self._known_tactics)

    def _passes_filter(self, technique: str, technique_ids: set, tactics: set) -> bool:
        return ac.passes_filter(technique, technique_ids, tactics, self.attack_meta)

    # ── 向后兼容的单次调用 ──────────────────────────────────────────────
    def predict(self, text: str, labels: List[str]) -> List[Tuple]:
        """返回 [(text, label, start, end, score), ...]。labels 作过滤条件(见 _parse_filters)。"""
        ents, _ = self.predict_with_chunks(text, labels, _DefaultCfg())
        return [(e["text"], e["label"], e["start"], e["end"], e["score"]) for e in ents]

    # ── 主路径 ──────────────────────────────────────────────────────────
    def predict_with_chunks(
        self, text: str, labels: List[str], chunk_cfg,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """按安全句子/token 窗口分类，并对低置信度句补充一个相邻句上下文。"""
        if self.model is None:
            logger.error("%s 未加载,predict_with_chunks 返回空结果", self.name)
            return [], _fallback_metadata(text, chunk_cfg, status="failed", error="model not loaded")

        threshold = float(getattr(chunk_cfg, "threshold", 0.5) or 0.0)
        technique_ids, tactics = self._parse_filters(labels)
        if not technique_ids and not tactics:
            logger.info("%s: 全量模式,返回全部命中阈值(%.2f)的 ATT&CK 技术", self.name, threshold)
        else:
            logger.info(
                "%s: 过滤模式 technique_ids=%s tactics=%s",
                self.name, sorted(technique_ids), sorted(tactics),
            )

        sentences = split_sentences(text)
        if not sentences:
            return [], self._metadata(text, chunk_cfg, windows=[])

        direct_windows: List[Window] = []
        all_windows: List[Window] = []
        try:
            direct_windows = self._build_direct_windows(text, sentences)
            with self._lock:
                direct_distributions = self._classify([window["text"] for window in direct_windows])
                context_windows = self._build_context_windows(
                    text, sentences, direct_windows, direct_distributions,
                    technique_ids, tactics, threshold,
                )
                context_distributions = self._classify(
                    [window["text"] for window in context_windows]
                ) if context_windows else []
            all_windows = direct_windows + context_windows
            all_distributions = direct_distributions + context_distributions
        except Exception as exc:  # noqa: BLE001
            logger.exception("%s: 推理失败", self.name)
            failed_windows = all_windows or direct_windows
            return [], self._metadata(
                text, chunk_cfg, failed_windows, status="failed", error=str(exc)[:200],
            )

        best_by_technique: Dict[str, Dict[str, Any]] = {}
        for chunk_index, (window, distribution) in enumerate(zip(all_windows, all_distributions)):
            for technique, confidence in distribution.items():
                if confidence < threshold:
                    continue
                if not self._passes_filter(technique, technique_ids, tactics):
                    continue
                candidate = {
                    "text": window["text"], "label": technique,
                    "start": window["start"], "end": window["end"],
                    "score": confidence, "chunk_index": chunk_index,
                    "chunk_char_start": window["start"],
                    "chunk_char_end": window["end"], "model_name": self.name,
                    "_evidence_kind": window["kind"],
                }
                candidate.update(self._enrich(technique))
                previous = best_by_technique.get(technique)
                if previous is None or self._prefer_candidate(candidate, previous):
                    best_by_technique[technique] = candidate

        entities = list(best_by_technique.values())
        for entity in entities:
            entity.pop("_evidence_kind", None)
        entities.sort(key=lambda entity: (entity["start"], entity["end"], entity["label"]))
        return entities, self._metadata(text, chunk_cfg, all_windows)
