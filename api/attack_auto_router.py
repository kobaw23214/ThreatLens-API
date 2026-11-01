# -*- coding: utf-8 -*-
"""Language-ratio router for the ATT&CK console."""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)
CONFIG_PATH = Path(__file__).with_name("attack_auto_routing.json")
ATTACK_AUTO_NAME = "attack-auto"
DEFAULT_CONFIG: Dict[str, Any] = {
    "version": 1,
    "routes": [
        {
            "model": "tram",
            "metric": "english_ratio",
            "operator": "gte",
            "threshold": 0.95,
        },
        {
            "model": "qwen3-attack-retriever",
            "metric": "chinese_ratio",
            "operator": "gte",
            "threshold": 0.60,
        },
    ],
    "fallback_model": "qwen3-attack-retriever",
}
_ALLOWED_METRICS = {"english_ratio", "chinese_ratio"}


def _is_han(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x323AF
    )

def language_ratios(text: str) -> Dict[str, Any]:
    """Count only ASCII letters and Han characters; ignore numbers/punctuation/IOCs."""
    english = sum(character.isascii() and character.isalpha() for character in text)
    chinese = sum(_is_han(character) for character in text)
    denominator = english + chinese
    return {
        "english_characters": english,
        "chinese_characters": chinese,
        "language_characters": denominator,
        "english_ratio": english / denominator if denominator else 0.0,
        "chinese_ratio": chinese / denominator if denominator else 0.0,
    }


def validate_config(config: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(config, dict):
        raise ValueError("ATT&CK Auto config must be an object")
    version = config.get("version", 1)
    if not isinstance(version, int) or version < 1:
        raise ValueError("version must be a positive integer")
    routes = config.get("routes")
    if not isinstance(routes, list) or not routes:
        raise ValueError("routes must be a non-empty array")
    normalized_routes = []
    seen_metrics = set()
    for route in routes:
        if not isinstance(route, dict):
            raise ValueError("each route must be an object")
        model = route.get("model")
        metric = route.get("metric")
        operator = route.get("operator", "gte")
        threshold = route.get("threshold")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("route model must be a non-empty string")
        if metric not in _ALLOWED_METRICS or metric in seen_metrics:
            raise ValueError(f"invalid or duplicate route metric: {metric}")
        if operator != "gte":
            raise ValueError("only the gte operator is supported")
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise ValueError(f"threshold for {metric} must be numeric")
        threshold = float(threshold)
        if not 0.0 <= threshold <= 1.0:
            raise ValueError(f"threshold for {metric} must be between 0 and 1")
        seen_metrics.add(metric)
        normalized_routes.append({
            "model": model.strip(), "metric": metric,
            "operator": "gte", "threshold": threshold,
        })
    if seen_metrics != _ALLOWED_METRICS:
        raise ValueError("routes must contain exactly english_ratio and chinese_ratio")
    normalized_routes.sort(key=lambda route: 0 if route["metric"] == "english_ratio" else 1)
    fallback = config.get("fallback_model")
    if not isinstance(fallback, str) or not fallback.strip():
        raise ValueError("fallback_model must be a non-empty string")
    return {"version": version, "routes": normalized_routes, "fallback_model": fallback.strip()}


def config_hash(config: Dict[str, Any]) -> str:
    payload = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_config(path: Path = CONFIG_PATH) -> Dict[str, Any]:
    if not path.is_file():
        logger.warning("ATT&CK Auto config is missing; using built-in defaults: %s", path)
        return validate_config(DEFAULT_CONFIG)
    return validate_config(json.loads(path.read_text(encoding="utf-8")))


class AttackAutoModel:
    """Select exactly one loaded ATT&CK model from configured language ratios."""

    model_type = "attack"
    model_max_tokens: Optional[int] = None

    def __init__(self, name: str = ATTACK_AUTO_NAME, config_path: Path = CONFIG_PATH) -> None:
        self.name = name
        self._config_path = config_path
        self._config = validate_config(DEFAULT_CONFIG)
        self._config_hash = config_hash(self._config)
        self._models: Dict[str, Any] = {}
        self._lock = threading.RLock()

    @property
    def cache_version(self) -> str:
        with self._lock:
            return self._config_hash

    def load(self) -> bool:
        try:
            config = load_config(self._config_path)
            with self._lock:
                self._config = config
                self._config_hash = config_hash(config)
            print(
                "✓ attack-auto ATT&CK 语言路由就绪 "
                f"(English {config['routes'][0]['threshold']:.0%}, "
                f"中文 {config['routes'][1]['threshold']:.0%}, "
                f"兜底 {config['fallback_model']})"
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.exception("attack-auto config load failed")
            print(f"✗ attack-auto 加载失败: {exc}")
            return False

    @staticmethod
    def _target_names(config: Dict[str, Any]) -> set:
        return {route["model"] for route in config["routes"]} | {config["fallback_model"]}

    @classmethod
    def validate_targets(cls, config: Dict[str, Any], models: Dict[str, Any]) -> None:
        for name in cls._target_names(config):
            model = models.get(name)
            if model is None:
                raise ValueError(f"ATT&CK Auto target model is not loaded: {name}")
            if getattr(model, "model_type", "") != "attack":
                raise ValueError(f"ATT&CK Auto target is not an ATT&CK model: {name}")
            if name == ATTACK_AUTO_NAME or isinstance(model, cls):
                raise ValueError("ATT&CK Auto cannot route to itself")

    def set_registry(self, models: Dict[str, Any]) -> None:
        with self._lock:
            config = self._config
        self.validate_targets(config, models)
        with self._lock:
            self._models = models

    def apply_config(self, config: Dict[str, Any], models: Dict[str, Any]) -> str:
        normalized = validate_config(config)
        self.validate_targets(normalized, models)
        fingerprint = config_hash(normalized)
        with self._lock:
            self._config = normalized
            self._config_hash = fingerprint
            self._models = models
        logger.info("attack-auto routing hot-reloaded: %s", fingerprint[:12])
        return fingerprint

    def reload_routing(self, models: Dict[str, Any]) -> str:
        return self.apply_config(load_config(self._config_path), models)

    def snapshot(self) -> Tuple[Dict[str, Any], str]:
        with self._lock:
            return json.loads(json.dumps(self._config)), self._config_hash

    def select_model(self, text: str) -> Tuple[str, Dict[str, Any]]:
        ratios = language_ratios(text)
        with self._lock:
            config = self._config
        selected = config["fallback_model"]
        matched = "fallback"
        for route in config["routes"]:
            if ratios[route["metric"]] >= route["threshold"]:
                selected = route["model"]
                matched = route["metric"]
                break
        return selected, {**ratios, "matched_route": matched, "selected_model": selected}

    def predict(self, text: str, labels: List[str]) -> List[Tuple]:
        entities, _ = self.predict_with_chunks(text, labels, type(
            "Cfg", (), {"threshold": 0.5, "overlap": 0, "safety_margin": 0},
        )())
        return [(item["text"], item["label"], item["start"], item["end"], item["score"])
                for item in entities]

    def predict_with_chunks(
        self, text: str, labels: List[str], chunk_cfg,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        selected, decision = self.select_model(text)
        with self._lock:
            model = self._models.get(selected)
            fingerprint = self._config_hash
        if model is None:
            raise RuntimeError(f"ATT&CK Auto selected unavailable model: {selected}")
        entities, metadata = model.predict_with_chunks(text, labels, chunk_cfg)
        metadata = dict(metadata or {})
        metadata["attack_auto_routing"] = {
            **decision,
            "config_hash": fingerprint,
        }
        for entity in entities:
            entity.setdefault("model_name", selected)
        return entities, metadata
