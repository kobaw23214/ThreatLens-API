# -*- coding: utf-8 -*-
"""Portable PyTorch Qwen3 ATT&CK Phase-1 Retriever."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from . import attack_common as ac

logger = logging.getLogger(__name__)
MODELS_ROOT = Path(os.getenv("MODELS_ROOT", Path(__file__).parent.parent / "models"))
MODEL_DIR_NAME = "qwen3-attack-retriever"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


Span = Tuple[int, int]
Window = Dict[str, Any]
_STRONG_TERMINATORS = frozenset("!?;。！？；\n\r")
_CLOSING_PUNCTUATION = frozenset("'\"”’)]}）】》")


class _DefaultCfg:
    threshold = 0.5
    overlap = 0
    safety_margin = 0


class Qwen3AttackRetrieverModel:
    """Local 50-label ATT&CK retriever using fused HF SafeTensors."""

    model_type = "attack"
    model_max_tokens: Optional[int] = None

    def __init__(self, name: str = MODEL_DIR_NAME, models_root: Optional[Path] = None) -> None:
        self.name = name
        root = Path(models_root) if models_root else MODELS_ROOT
        self._dir = root / "attack" / MODEL_DIR_NAME
        self._merged = self._dir / "merged"
        self._config_path = self._dir / "training_config.json"
        self._manifest_path = self._dir / "training_manifest.json"
        self._runtime_manifest_path = self._dir / "runtime_manifest.json"
        self._corpus_path = self._dir / "retriever_corpus.jsonl"
        self._attack_path = root / "attack" / "techniques.json"
        self.model = None
        self.tokenizer = None
        self.device = None
        self.dtype = None
        self.config: Dict[str, Any] = {}
        self.corpus: List[Dict[str, Any]] = []
        self.document_vectors: Optional[np.ndarray] = None
        self.attack_meta: Dict[str, Dict[str, Any]] = {}
        self._known_tactics: set = set()
        self._lock = threading.Lock()
        self.device_name = os.getenv("QWEN3_ATTACK_DEVICE", "auto").strip().lower() or "auto"
        self.dtype_name = os.getenv("QWEN3_ATTACK_DTYPE", "auto").strip().lower() or "auto"
        self.top_k = max(1, min(50, int(os.getenv("QWEN3_ATTACK_TOP_K", "10"))))
        self.window_top_k = max(1, min(50, int(os.getenv("QWEN3_ATTACK_WINDOW_TOP_K", "3"))))
        self.window_target_tokens = max(32, int(os.getenv("QWEN3_ATTACK_WINDOW_TARGET_TOKENS", "256")))
        self.window_max_tokens = max(64, int(os.getenv("QWEN3_ATTACK_WINDOW_MAX_TOKENS", "320")))
        self.window_overlap_sentences = max(0, int(os.getenv("QWEN3_ATTACK_WINDOW_OVERLAP_SENTENCES", "1")))
        self.window_max_sentences = max(1, int(os.getenv("QWEN3_ATTACK_WINDOW_MAX_SENTENCES", "3")))
        self.long_overlap_tokens = max(0, int(os.getenv("QWEN3_ATTACK_LONG_OVERLAP_TOKENS", "48")))
        self.min_fragment_chars = max(1, int(os.getenv("QWEN3_ATTACK_MIN_FRAGMENT_CHARS", "12")))
        self.max_windows = max(1, int(os.getenv("QWEN3_ATTACK_MAX_WINDOWS", "200")))
        _require(self.window_target_tokens <= self.window_max_tokens, "window target exceeds maximum")
        _require(self.long_overlap_tokens < self.window_max_tokens, "long-window overlap must be smaller than maximum")
        self.model_max_tokens = self.window_max_tokens

    def _verify_assets(self) -> Dict[str, Any]:
        required = (
            self._merged, self._merged / "config.json", self._merged / "model.safetensors.index.json",
            self._config_path, self._manifest_path, self._runtime_manifest_path, self._corpus_path,
        )
        for path in required:
            _require(path.exists(), f"missing model asset: {path}")
        config = _read_json(self._config_path)
        training_manifest = _read_json(self._manifest_path)
        runtime = _read_json(self._runtime_manifest_path)
        _require(training_manifest["status"] == "engineering_training_completed", "training is incomplete")
        _require(runtime["backend"] == "torch", "runtime package is not a PyTorch export")
        _require(runtime["format"] == "huggingface_safetensors_fused_lora", "unexpected runtime format")
        _require(_sha256(self._config_path) == runtime["sources"]["training_config_sha256"], "config hash mismatch")
        _require(_sha256(self._manifest_path) == runtime["sources"]["training_manifest_sha256"], "manifest hash mismatch")
        _require(_sha256(self._corpus_path) == runtime["sources"]["corpus_sha256"], "corpus hash mismatch")
        for item in runtime["files"]:
            path = self._dir / item["path"]
            _require(path.is_file(), f"missing exported file: {item['path']}")
            _require(path.stat().st_size == item["bytes"], f"exported file size mismatch: {item['path']}")
            _require(_sha256(path) == item["sha256"], f"exported file hash mismatch: {item['path']}")
        return config

    def _resolve_runtime(self):
        import torch

        if self.device_name == "auto":
            device_name = "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"
        else:
            device_name = self.device_name
        _require(device_name in {"cpu", "cuda", "mps"}, f"unsupported device: {device_name}")
        if device_name == "cuda":
            _require(torch.cuda.is_available(), "CUDA was requested but is unavailable")
        if device_name == "mps":
            _require(torch.backends.mps.is_available(), "MPS was requested but is unavailable")
        if self.dtype_name == "auto":
            dtype_name = "float32" if device_name == "cpu" else "float16"
        else:
            dtype_name = self.dtype_name
        _require(dtype_name in {"float32", "float16", "bfloat16"}, f"unsupported dtype: {dtype_name}")
        if device_name == "mps":
            _require(dtype_name != "bfloat16", "bfloat16 is not enabled for the MPS backend")
        return torch.device(device_name), getattr(torch, dtype_name), dtype_name

    def _load_model(self) -> None:
        from transformers import AutoModel, AutoTokenizer

        self.device, self.dtype, resolved_dtype = self._resolve_runtime()
        self.dtype_name = resolved_dtype
        self.tokenizer = AutoTokenizer.from_pretrained(self._merged, local_files_only=True)
        self.model = AutoModel.from_pretrained(
            self._merged,
            local_files_only=True,
            torch_dtype=self.dtype,
            low_cpu_mem_usage=True,
            attn_implementation="eager",
        ).to(self.device)
        self.model.eval()

    def _prepare_text(self, text: str, kind: str) -> str:
        if kind == "query":
            return f"Instruct: {self.config['encoding']['query_instruction']}\nQuery:{text}"
        _require(kind == "document", f"unsupported encoding kind: {kind}")
        return text

    def _encode(self, text: str, kind: str) -> np.ndarray:
        import torch

        maximum = self.config["encoding"][f"{kind}_max_length"]
        token_ids = self.tokenizer.encode(self._prepare_text(text, kind), add_special_tokens=True)[:maximum]
        _require(bool(token_ids), "tokenizer returned an empty sequence")
        inputs = torch.tensor([token_ids], dtype=torch.long, device=self.device)
        with torch.inference_mode():
            hidden = self.model(input_ids=inputs, use_cache=False).last_hidden_state[0, -1].float()
            hidden = torch.nn.functional.normalize(hidden, dim=0)
        return hidden.cpu().numpy()

    @property
    def _raw_tokenizer(self):
        return self.tokenizer

    def _token_count(self, text: str) -> int:
        return len(self._raw_tokenizer.encode(text, add_special_tokens=False))

    @staticmethod
    def _trim_span(text: str, start: int, end: int) -> Optional[Span]:
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        return (start, end) if end > start else None

    @staticmethod
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

    def _split_base_spans(self, text: str) -> List[Span]:
        raw: List[Span] = []
        start = 0
        index = 0
        while index < len(text):
            char = text[index]
            boundary = char in _STRONG_TERMINATORS or (char == "." and self._period_is_boundary(text, index))
            if not boundary:
                index += 1
                continue
            end = index + 1
            while end < len(text):
                next_char = text[end]
                if next_char in _STRONG_TERMINATORS or (next_char == "." and self._period_is_boundary(text, end)):
                    end += 1
                else:
                    break
            span = self._trim_span(text, start, end)
            if span:
                raw.append(span)
            start = end
            index = end
        tail = self._trim_span(text, start, len(text))
        if tail:
            raw.append(tail)

        merged: List[Span] = []
        pending: Optional[Span] = None
        for span in raw:
            non_space = sum(not char.isspace() for char in text[span[0]:span[1]])
            if non_space < self.min_fragment_chars:
                pending = (pending[0], span[1]) if pending else span
                continue
            if pending:
                combined = self._trim_span(text, pending[0], span[1])
                if combined:
                    merged.append(combined)
                pending = None
            else:
                merged.append(span)
        if pending:
            combined = self._trim_span(text, merged[-1][0] if merged else pending[0], pending[1])
            if combined:
                if merged:
                    merged[-1] = combined
                else:
                    merged.append(combined)
        return merged

    def _split_long_span(self, text: str, span: Span) -> List[Span]:
        content = text[span[0]:span[1]]
        encoded = self._raw_tokenizer(
            content, add_special_tokens=False, return_offsets_mapping=True,
        )
        offsets = encoded["offset_mapping"]
        if len(offsets) <= self.window_max_tokens:
            return [span]
        step = self.window_max_tokens - self.long_overlap_tokens
        parts: List[Span] = []
        token_start = 0
        while token_start < len(offsets):
            token_end = min(len(offsets), token_start + self.window_max_tokens)
            relative_start = offsets[token_start][0]
            relative_end = offsets[token_end - 1][1]
            part = self._trim_span(text, span[0] + relative_start, span[0] + relative_end)
            if part and (not parts or part != parts[-1]):
                parts.append(part)
            if token_end == len(offsets):
                break
            token_start += step
        return parts

    def _build_windows(self, text: str) -> Tuple[List[Window], bool, int]:
        spans: List[Span] = []
        for span in self._split_base_spans(text):
            spans.extend(self._split_long_span(text, span))
        windows: List[Window] = []
        start = 0
        while start < len(spans):
            end = start
            accepted_end = start
            accepted_tokens = 0
            while end < len(spans):
                if end - start >= self.window_max_sentences:
                    break
                candidate_text = text[spans[start][0]:spans[end][1]]
                candidate_tokens = self._token_count(candidate_text)
                if end > start and candidate_tokens > self.window_target_tokens:
                    break
                _require(candidate_tokens <= self.window_max_tokens, "window exceeds hard token limit")
                accepted_end = end + 1
                accepted_tokens = candidate_tokens
                end += 1
                if candidate_tokens >= self.window_target_tokens:
                    break
            _require(accepted_end > start, "window builder made no progress")
            char_start, char_end = spans[start][0], spans[accepted_end - 1][1]
            windows.append({
                "text": text[char_start:char_end], "start": char_start,
                "end": char_end, "token_count": accepted_tokens,
            })
            if accepted_end >= len(spans):
                break
            start = max(start + 1, accepted_end - self.window_overlap_sentences)

        unique: List[Window] = []
        seen = set()
        for window in windows:
            key = (window["start"], window["end"])
            if key not in seen:
                seen.add(key)
                unique.append(window)
        total = len(unique)
        if total <= self.max_windows:
            return unique, False, total
        if self.max_windows == 1:
            return [unique[0]], True, total
        indexes = sorted({round(i * (total - 1) / (self.max_windows - 1)) for i in range(self.max_windows)})
        return [unique[index] for index in indexes], True, total

    def _metadata(
        self, text: str, chunk_cfg, windows: Optional[List[Window]] = None,
        status: str = "done", error: Optional[str] = None,
        sampled: bool = False, total_windows: Optional[int] = None,
    ) -> Dict[str, Any]:
        rows = windows or []
        chunks = [{
            "chunk_index": index, "char_start": row["start"], "char_end": row["end"],
            "char_length": row["end"] - row["start"], "token_count": row["token_count"],
            "status": status, "error": error,
        } for index, row in enumerate(rows)]
        if not chunks and text and status == "failed":
            chunks = [{"chunk_index": 0, "char_start": 0, "char_end": len(text),
                       "char_length": len(text), "token_count": None,
                       "status": status, "error": error}]
        return {
            "chunking_enabled": True, "chunking_strategy": "semantic_sentence_windows",
            "model_max_tokens": self.window_max_tokens,
            "labels_token_count": None, "effective_max_tokens": self.window_max_tokens,
            "overlap": self.window_overlap_sentences,
            "safety_margin": getattr(chunk_cfg, "safety_margin", 0),
            "threshold": getattr(chunk_cfg, "threshold", 0.5),
            "chunk_count": len(chunks), "window_target_tokens": self.window_target_tokens,
            "window_max_sentences": self.window_max_sentences,
            "window_top_k": self.window_top_k, "windows_sampled": sampled,
            "total_windows_before_sampling": total_windows if total_windows is not None else len(rows),
            "chunks": chunks,
        }

    def load(self) -> bool:
        try:
            self.config = self._verify_assets()
            self._load_model()
            instruction_tokens = self._token_count(self._prepare_text("", "query"))
            _require(
                self.window_max_tokens + instruction_tokens <= self.config["encoding"]["query_max_length"],
                "window maximum leaves no room for the query instruction",
            )
            with self._corpus_path.open(encoding="utf-8") as stream:
                self.corpus = [json.loads(line) for line in stream if line.strip()]
            _require(len(self.corpus) == 50, "expected frozen 50-label corpus")
            _require(len({row["technique_id"] for row in self.corpus}) == 50, "duplicate corpus technique")
            _require(all(row["attack_version"] == "19.1" for row in self.corpus), "ATT&CK version mismatch")
            self.document_vectors = np.stack([
                self._encode(row["retrieval_text_en"], "document") for row in self.corpus
            ])
            self.attack_meta = ac.load_attack_metadata(self._attack_path)
            self._known_tactics = ac.build_known_tactics(self.attack_meta)
            print(
                f"✓ {self.name} 加载成功 (Qwen3-Embedding-4B fused SafeTensors, "
                f"PyTorch {self.device}/{self.dtype_name}, 50 labels, "
                f"window={self.window_target_tokens}/{self.window_max_tokens} tokens, "
                f"window_top_k={self.window_top_k}, final_top_k={self.top_k})"
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.exception("%s 加载失败", self.name)
            print(f"✗ {self.name} 加载失败: {exc}")
            self.model = None
            return False

    def predict(self, text: str, labels: List[str]) -> List[Tuple]:
        entities, _ = self.predict_with_chunks(text, labels, _DefaultCfg())
        return [(e["text"], e["label"], e["start"], e["end"], e["score"]) for e in entities]

    def predict_with_chunks(
        self, text: str, labels: List[str], chunk_cfg,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        if self.model is None or self.document_vectors is None:
            return [], self._metadata(text, chunk_cfg, status="failed", error="model not loaded")
        if not text.strip():
            return [], self._metadata(text, chunk_cfg, windows=[])
        technique_ids, tactics = ac.parse_filters(labels, self._known_tactics)
        threshold = float(getattr(chunk_cfg, "threshold", 0.5) or 0.0)
        windows: List[Window] = []
        sampled = False
        total_windows = 0
        try:
            windows, sampled, total_windows = self._build_windows(text)
            allowed = [
                index for index, row in enumerate(self.corpus)
                if ac.passes_filter(row["technique_id"], technique_ids, tactics, self.attack_meta)
            ]
            best_by_technique: Dict[str, Dict[str, Any]] = {}
            with self._lock:
                for chunk_index, window in enumerate(windows):
                    query = np.array(self._encode(window["text"], "query"))
                    scores = query @ self.document_vectors.T
                    order = sorted(
                        allowed, key=lambda index: float(scores[index]), reverse=True,
                    )[:self.window_top_k]
                    for index in order:
                        score = float(scores[index])
                        if score < threshold:
                            continue
                        technique = self.corpus[index]["technique_id"]
                        previous = best_by_technique.get(technique)
                        if previous is not None and previous["score"] >= score:
                            continue
                        entity = {
                            "text": window["text"], "label": technique,
                            "start": window["start"], "end": window["end"],
                            "score": score, "chunk_index": chunk_index,
                            "chunk_char_start": window["start"],
                            "chunk_char_end": window["end"], "model_name": self.name,
                        }
                        entity.update(ac.enrich(technique, self.attack_meta))
                        best_by_technique[technique] = entity
            entities = sorted(
                best_by_technique.values(), key=lambda entity: (-entity["score"], entity["label"]),
            )[:self.top_k]
            return entities, self._metadata(
                text, chunk_cfg, windows, sampled=sampled, total_windows=total_windows,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("%s 推理失败", self.name)
            return [], self._metadata(
                text, chunk_cfg, windows, status="failed", error=str(exc)[:200],
                sampled=sampled, total_windows=total_windows,
            )
