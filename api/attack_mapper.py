# -*- coding: utf-8 -*-
"""
attack-llm: 大模型 ATT&CK 技战术映射伪模型

复用 LLMModel 的 OpenAI 兼容 API 调用 / SSE 流式 / 连通测试能力,换成 ATT&CK 映射
提示词:让大模型读威胁情报报告(中/英文均可),从「行为叙述」推断出对应的 MITRE
ATT&CK 技术编号,并给出原文证据片段。与 TRAM 相比:
  - TRAM: 固定 50 类 SciBERT 分类器,仅英文效果好,覆盖有限但确定性强;
  - attack-llm: 大模型推断,中英文通吃、覆盖全部 600+ 技术,但需校验编号防幻觉。

与 TRAM 共用 attack_common 的 full / 过滤语义(默认全量;labels 含技术编号/战术
时按其过滤),保证两个模型行为一致。大模型输出的编号统一用 techniques.json 校验
(丢弃不存在/已废弃的幻觉编号)并富化技术名 / 战术 / URL。

model_type = "attack",与 TRAM 一起归入前端「ATT&CK 调用台」。

启用方式(见 api/.env):在 ATTACK_MODELS_ENABLED 里引用某个 LLM —— 组别名
(LLM_MODELS_1 / _2 / _3)或具体模型名(如 glm-5),连接自动从对应 LLM_* 组解析。
load_models 会把 ATT&CK 清单里的 LLM 包成本类(AttackLLMModel)。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import attack_common as ac
from .models import LLMModel

logger = logging.getLogger(__name__)


def _single_chunk_metadata_dict(
    text: str, chunk_cfg, status: str = "done", error: Optional[str] = None,
) -> Dict[str, Any]:
    """「不分块」的 chunk_metadata,与 UIE / LLM / IOC / TRAM 降级路径一致。"""
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


class AttackLLMModel(LLMModel):
    """大模型 ATT&CK 技战术映射伪模型(复用 LLMModel 的 API 调用能力)。"""

    model_type: str = "attack"

    _SYSTEM_PROMPT = (
        "你是一名资深网络威胁情报(CTI)分析师,精通 MITRE ATT&CK 框架。"
        "你的任务是阅读威胁报告文本(可能是中文或英文),从对手的行为描述中识别出"
        "对应的 ATT&CK 技术(technique),即使原文没有显式写出技术编号也要能推断出来。"
        "你擅长透过工具名、命令、系统调用、注册表键、文件操作等细节,理解其背后的"
        "对手意图,并映射到最贴切的技术。你必须只输出一个 JSON 数组,不要包含任何"
        "解释、说明或 markdown 代码块标记。"
    )

    # few-shot 示例:示范「短行为描述 → 技术编号」的映射思路(通用例子,不取自测试集)。
    # 这些例子帮助模型在句子较短/为片段时也能给出最匹配技术,而不是空手而归。
    _FEWSHOT = (
        "下面是几个「行为描述 → ATT&CK 技术」的映射示例,请学习这种映射思路"
        "(注意:这些只是示范,不要把它们的编号带入正式输出):\n"
        "示例1 文本:「木马运行 whoami 获取当前登录用户」→ "
        '[{"technique_id": "T1033", "evidence": "运行 whoami 获取当前登录用户", "confidence": 0.9}]\n'
        "示例2 文本:「在注册表 Run 键写入自身路径以实现开机自启」→ "
        '[{"technique_id": "T1547.001", "evidence": "在注册表 Run 键写入自身路径", "confidence": 0.9}]\n'
        "示例3 文本:「从远程服务器下载后续 payload 到本地」→ "
        '[{"technique_id": "T1105", "evidence": "从远程服务器下载后续 payload", "confidence": 0.85}]\n'
        "示例4 文本:「通过 HTTP 请求与 C2 服务器通信」→ "
        '[{"technique_id": "T1071.001", "evidence": "通过 HTTP 请求与 C2 服务器通信", "confidence": 0.85}]\n'
    )

    def __init__(
        self,
        model_name: str,
        base_url: str,
        api_key: str = "none",
        temperature: float = 0.1,
        timeout: int = 120,
        headers: Optional[Dict[str, str]] = None,
        models_root: Optional[Path] = None,
    ) -> None:
        super().__init__(model_name, base_url, api_key, temperature, timeout, headers)
        root = Path(models_root) if models_root else (Path(__file__).parent.parent / "models")
        self.attack_meta = ac.load_attack_metadata(root / "attack" / "techniques.json")
        self._known_tactics = ac.build_known_tactics(self.attack_meta)

    # ── 提示词(覆盖 LLMModel)────────────────────────────────────────────
    def _build_messages(self, text: str, labels: List[str]) -> List[Dict[str, str]]:
        technique_ids, tactics = ac.parse_filters(labels, self._known_tactics)
        if technique_ids or tactics:
            catalog = ac.format_candidate_catalog(technique_ids, tactics, self.attack_meta)
            if catalog:
                scope = (
                    "本次只能从下面这份「候选技术清单」中选择技术,清单之外的一律不要输出。\n"
                    "请仔细阅读每个技术的名称和所属战术,再根据文本描述的对手行为,"
                    "选出语义最匹配的技术(而不是靠关键词字面匹配)。\n"
                    "候选技术清单(格式:编号  名称  [战术]):\n"
                    f"{catalog}\n"
                )
            else:
                scope_parts = []
                if technique_ids:
                    scope_parts.append("技术编号 " + ", ".join(sorted(technique_ids)))
                if tactics:
                    scope_parts.append("战术 " + ", ".join(sorted(tactics)))
                scope = (
                    "本次只需识别以下范围内的技术,范围之外的一律忽略:"
                    + ";".join(scope_parts)
                    + "。\n"
                )
        else:
            scope = (
                "请识别文本中描述的所有 ATT&CK 技术。根据对手的行为语义推断技术编号,"
                "即使原文没有显式写出编号。\n"
            )

        user_content = (
            f"{scope}"
            f"\n{self._FEWSHOT}"
            "\n分析要点:\n"
            "- 关注文本描述的对手「做了什么行为」,把行为映射到对应的 ATT&CK 技术;\n"
            "- 透过工具名/命令/系统调用/注册表键/文件操作等细节,理解其背后的对手意图;\n"
            "- 优先选择更具体的子技术(如 T1059.003)而不是笼统的父技术,除非文本信息不足;\n"
            "- 即使句子较短或为片段,只要能看出对手的某个行为,也应给出最匹配的技术,"
            "而不是空手而归;把握不大时可调低 confidence(如 0.4~0.6),但仍要给出最可能的一项;\n"
            "- 只有当文本完全没有任何可识别的对手行为时,才输出空数组;\n"
            "- 同一行为若同时符合多个技术,可都列出,按相关度用 confidence 区分。\n"
            "\n威胁报告文本:\n---\n"
            f"{text}\n"
            "---\n\n"
            "请以 JSON 数组输出识别到的每一个 ATT&CK 技术,每个元素包含:\n"
            '- "technique_id": ATT&CK 官方技术编号(如 T1059 或子技术 T1059.003)\n'
            '- "evidence": 支撑该判断的原文片段(必须逐字复制自上面的文本,不要改写/翻译)\n'
            '- "confidence": 置信度,0 到 1 之间的小数\n'
            "\n要求:\n"
            "1. technique_id 必须是真实存在的 ATT&CK 编号,不要杜撰\n"
            "2. evidence 必须是原文中的连续片段,能在原文里被精确找到\n"
            "3. 同一技术若在多处出现,可分别输出多条\n"
            "4. 没有识别到任何技术时输出空数组 []\n"
            "5. 只输出 JSON 数组,不要包含 ```json 等标记或任何其它文字\n"
        )
        return [
            {"role": "system", "content": self._SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

    # ── 解析大模型输出 → 校验 + 定位 + 富化 ───────────────────────────────
    def _parse_attack(
        self, raw_text: str, original_text: str, technique_ids: set, tactics: set,
    ) -> List[Dict[str, Any]]:
        content = raw_text.strip()
        md = re.search(r"```(?:json)?\s*\n?(.*?)```", content, re.DOTALL)
        if md:
            content = md.group(1).strip()
        arr = re.search(r"\[.*\]", content, re.DOTALL)
        if arr:
            content = arr.group(0)

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            try:
                import json_repair
                parsed = json_repair.loads(content)
                logger.info("attack-llm: JSON 解析失败,已用 json-repair 修复")
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"attack-llm 输出的 JSON 无法解析: {exc}") from exc

        if not isinstance(parsed, list):
            raise RuntimeError(
                f"attack-llm 输出不是 JSON 数组,而是 {type(parsed).__name__}"
            )

        entities: List[Dict[str, Any]] = []
        seen: set = set()
        for item in parsed:
            if not isinstance(item, dict):
                continue
            tid = str(item.get("technique_id") or item.get("technique") or item.get("id") or "").strip().upper()
            evidence = str(item.get("evidence") or item.get("text") or "").strip()
            try:
                conf = float(item.get("confidence", 1.0))
            except (TypeError, ValueError):
                conf = 1.0

            # 校验编号(丢弃幻觉/废弃)
            if not ac.is_valid_technique(tid, self.attack_meta):
                logger.info("attack-llm: 丢弃无效/幻觉技术编号 %r", tid)
                continue
            # full/过滤
            if not ac.passes_filter(tid, technique_ids, tactics, self.attack_meta):
                continue

            # 定位证据片段 → offset(找不到则回退 0,0,技术仍保留)
            start, end, span_text = 0, 0, evidence
            if evidence:
                idx = original_text.find(evidence)
                if idx >= 0:
                    start, end, span_text = idx, idx + len(evidence), evidence

            dedup_key = (tid, start, end)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            entity: Dict[str, Any] = {
                "text": span_text,
                "label": tid,
                "start": start,
                "end": end,
                "score": conf,
                "chunk_index": 0,
                "chunk_char_start": 0,
                "chunk_char_end": len(original_text),
                "model_name": self.name,
            }
            entity.update(ac.enrich(tid, self.attack_meta))
            entities.append(entity)

        entities.sort(key=lambda e: (e["start"], e["end"], e["label"]))
        return entities

    # ── 向后兼容单次调用 ─────────────────────────────────────────────────
    def predict(self, text: str, labels: List[str]) -> List[Tuple]:
        ents, _ = self.predict_with_chunks(text, labels, _DefaultCfg())
        return [(e["text"], e["label"], e["start"], e["end"], e["score"]) for e in ents]

    # ── 主路径(覆盖 LLMModel:整段一次请求,不分块)──────────────────────
    def predict_with_chunks(
        self, text: str, labels: List[str], chunk_cfg,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        technique_ids, tactics = ac.parse_filters(labels, self._known_tactics)
        if not technique_ids and not tactics:
            logger.info("%s: 全量模式,识别文本中的全部 ATT&CK 技术", self.name)
        else:
            logger.info(
                "%s: 过滤模式 technique_ids=%s tactics=%s",
                self.name, sorted(technique_ids), sorted(tactics),
            )

        with self._lock:
            try:
                messages = self._build_messages(text, labels)
                raw = self._call_api(messages)
                entities = self._parse_attack(raw, text, technique_ids, tactics)
            except Exception as exc:  # noqa: BLE001
                logger.error("%s: 推理失败: %s", self.name, exc)
                return [], _single_chunk_metadata_dict(
                    text, chunk_cfg, status="failed", error=str(exc)[:200],
                )

        return entities, _single_chunk_metadata_dict(text, chunk_cfg)


class _DefaultCfg:
    """predict() 兼容用的极简 chunk_cfg。"""
    threshold = 0.5
    overlap = 0
    safety_margin = 0
    enabled = False
