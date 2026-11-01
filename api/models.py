"""
NER 模型封装层
提取自 ner_benchmark.py，支持从 .env 配置 MODELS_ROOT。

long-text-chunking-ner 新增:
  - 类属性 model_max_tokens: 384 (gliner_multi-v2.1) / 1024 (gliner-x-large) / None (UIE)
  - 方法 predict_with_chunks(text, labels, chunk_cfg)
      → (entities: List[Dict], chunk_metadata_dict: Dict)
    内部调 ChunkingService 切块、逐块调原生 predict_entities,单块异常只影响本块,
    把 local offset 加上 char_start 得到全局 offset,并记录 chunk_index / chunk_char_*
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .chunking import (
    Chunk,
    ChunkConfig,
    ChunkMetadata,
    ChunkingService,
)
from .features import attack_enabled, ner_enabled

logger = logging.getLogger(__name__)

# ─── Paddle 环境变量必须在任何 paddle/paddlenlp import 之前设置 ───────────────
_DEFAULT_MODELS_ROOT = Path(__file__).parent.parent / "models"
MODELS_ROOT = Path(os.getenv("MODELS_ROOT", str(_DEFAULT_MODELS_ROOT)))

os.environ.setdefault("PADDLENLP_HOME", str(MODELS_ROOT / "uie"))
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("FLAGS_enable_pir_api", "0")

# Stanza 资源目录 (gliner-x-large 的 words_splitter_type=stanza 需要中文分词模型)
# 优先使用项目本地目录,避免 Windows 用户 Cache 的权限问题
_stanza_dir = MODELS_ROOT / "stanza_resources"
if _stanza_dir.is_dir():
    os.environ.setdefault("STANZA_RESOURCES_DIR", str(_stanza_dir))

MODEL_DIRS = {
    "gliner-x-large":    MODELS_ROOT / "gliner-x-large",
    "gliner_multi-v2.1": MODELS_ROOT / "gliner_multi-v2.1",
    "uie":               MODELS_ROOT / "uie",
    "tram":              MODELS_ROOT / "tram",
}


# ─── Mixin:GLiNER 系模型共享的分块推理逻辑 ──────────────────────────────────

class _GLiNERChunkingMixin:
    """
    为 GLiNERModel / GLiNERMultiModel 提供 predict_with_chunks 的共同实现。

    依赖子类提供:
      - self.model          GLiNER 实例
      - self.name           str,用于日志
      - self.model_max_tokens  int,tokenizer 硬上限
      - self._lock          threading.Lock,串行保护 predict_entities 调用
    """

    model_max_tokens: int  # 子类覆盖

    def _get_tokenizer(self):
        """
        通过 GLiNER 对象的 data_processor.transformer_tokenizer 获取真实 tokenizer。
        """
        if self.model is None:
            raise RuntimeError(f"{self.name} 未加载,无法获取 tokenizer")
        data_processor = getattr(self.model, "data_processor", None)
        if data_processor is None:
            raise RuntimeError(
                f"{self.name}: 模型对象上没有 data_processor 属性,"
                f"gliner 版本可能不兼容"
            )
        tokenizer = getattr(data_processor, "transformer_tokenizer", None)
        if tokenizer is None:
            raise RuntimeError(
                f"{self.name}: data_processor 上没有 transformer_tokenizer 属性,"
                f"gliner 版本可能不兼容"
            )
        return tokenizer

    def _single_chunk_metadata(
        self, text: str, chunk_cfg: ChunkConfig, chunking_enabled: bool,
    ) -> Tuple[List[Chunk], ChunkMetadata]:
        """
        构造"不分块"的降级数据结构:单 Chunk 覆盖整段文本,token_count 仍然用
        真实 tokenizer 计算以便前端展示。
        """
        try:
            tokenizer = self._get_tokenizer()
            token_count = len(tokenizer.encode(text, add_special_tokens=False)) if text else 0
        except Exception as exc:
            logger.warning(f"{self.name}: 无法计算单块 token_count, err={exc}")
            token_count = None

        only = Chunk(
            chunk_index=0,
            char_start=0,
            char_end=len(text),
            text=text,
            token_count=token_count if token_count is not None else 0,
        )
        metadata = ChunkMetadata(
            chunking_enabled=chunking_enabled,
            model_max_tokens=self.model_max_tokens,
            labels_token_count=None,
            effective_max_tokens=None,
            overlap=chunk_cfg.overlap,
            safety_margin=chunk_cfg.safety_margin,
            threshold=chunk_cfg.threshold,
            chunk_count=1,
            chunks=[only],
        )
        return [only], metadata

    def predict_with_chunks(
        self, text: str, labels: List[str], chunk_cfg: ChunkConfig,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """
        分块推理 + offset 回写。返回 (entities, chunk_metadata_dict)。

        entities 的每个元素形如:
          {"text", "label", "start"(全局), "end"(全局), "score",
           "chunk_index", "chunk_char_start", "chunk_char_end"}
        label_mapping 的应用由上层 queue_manager 负责(保留 raw_label)。

        单个 Chunk 的 predict_entities 异常只会把该 Chunk 的 status 标记为 failed,
        不会中断其他 Chunk 的推理。ChunkingCapacityError 会原样向上抛出,由上层
        转为 HTTP 500 / task failed。
        """
        if self.model is None:
            logger.error(f"{self.name} 未加载,predict_with_chunks 返回空结果")
            _, empty_meta = self._single_chunk_metadata(text, chunk_cfg, chunking_enabled=False)
            empty_meta.chunks[0].status = "failed"
            empty_meta.chunks[0].error = "model not loaded"
            return [], empty_meta.to_dict()

        # ── 1. 分块 ────────────────────────────────────────────────────
        if not chunk_cfg.enabled:
            # 降级:整段作为单 Chunk,跳过 ChunkingService 的切分流程
            chunks, metadata = self._single_chunk_metadata(text, chunk_cfg, chunking_enabled=False)
        else:
            tokenizer = self._get_tokenizer()
            service = ChunkingService(
                tokenizer=tokenizer,
                model_max_tokens=self.model_max_tokens,
                safety_margin=chunk_cfg.safety_margin,
                overlap=chunk_cfg.overlap,
            )
            # 让 ChunkingCapacityError 直接抛到外层
            chunks, metadata = service.split(text, labels)
            # 把真实 threshold 回写到 metadata(ChunkingService.split 内部填 0.0)
            metadata.threshold = chunk_cfg.threshold

        # ── 2. 逐块推理 ────────────────────────────────────────────────
        merged: List[Dict[str, Any]] = []
        with self._lock:
            for ch in chunks:
                if not ch.text:
                    ch.status = "done"
                    continue
                try:
                    raw = self.model.predict_entities(
                        ch.text, labels, threshold=chunk_cfg.threshold,
                    )
                    for e in raw:
                        local_start = int(e["start"])
                        local_end = int(e["end"])
                        merged.append({
                            "text":  e["text"],
                            "label": e["label"],
                            "start": ch.char_start + local_start,
                            "end":   ch.char_start + local_end,
                            "score": float(e["score"]),
                            "chunk_index":      ch.chunk_index,
                            "chunk_char_start": ch.char_start,
                            "chunk_char_end":   ch.char_end,
                        })
                    ch.status = "done"
                except Exception as exc:  # noqa: BLE001
                    # Requirement 4.5: 单块失败不整体失败
                    ch.status = "failed"
                    ch.error = str(exc)[:200]
                    logger.error(
                        f"{self.name}: chunk {ch.chunk_index} "
                        f"[{ch.char_start},{ch.char_end}) 推理失败: {exc}"
                    )

        return merged, metadata.to_dict()


# ─── GLiNER 模型 ─────────────────────────────────────────────────────────────

class GLiNERModel(_GLiNERChunkingMixin):
    MODEL_ID = "knowledgator/gliner-x-large"
    model_max_tokens = 1024  # mT5-large 的 tokenizer 硬上限

    def __init__(self) -> None:
        self.name = "gliner-x-large"
        self.model = None
        self.local_dir = MODEL_DIRS["gliner-x-large"]
        self._lock = threading.Lock()

    def load(self) -> bool:
        try:
            from gliner import GLiNER
            config_file = self.local_dir / "gliner_config.json"
            if config_file.exists():
                print(f"  从本地加载: {self.local_dir}")
                self.model = GLiNER.from_pretrained(str(self.local_dir))
            else:
                print(f"  本地未找到模型，正在下载 {self.MODEL_ID} ...")
                self.model = GLiNER.from_pretrained(self.MODEL_ID)
                self.model.save_pretrained(str(self.local_dir))
            print(f"✓ {self.name} 加载成功")
            return True
        except Exception as e:
            print(f"✗ {self.name} 加载失败: {e}")
            return False

    def predict(self, text: str, labels: List[str]) -> List[Tuple]:
        """
        向后兼容的单次调用:不分块、不做 offset 调整。
        新代码应使用 predict_with_chunks。
        """
        if not self.model:
            return []
        with self._lock:
            try:
                entities = self.model.predict_entities(text, labels, threshold=0.5)
                return [(e["text"], e["label"], e["start"], e["end"], e["score"]) for e in entities]
            except Exception as e:
                print(f"  {self.name} 预测错误: {e}")
                return []


class GLiNERMultiModel(_GLiNERChunkingMixin):
    MODEL_ID = "urchade/gliner_multi-v2.1"
    model_max_tokens = 384  # mDeBERTa-v3-base 的 tokenizer 硬上限

    def __init__(self) -> None:
        self.name = "gliner_multi-v2.1"
        self.model = None
        self.local_dir = MODEL_DIRS["gliner_multi-v2.1"]
        self._lock = threading.Lock()

    def load(self) -> bool:
        try:
            from gliner import GLiNER
            config_file = self.local_dir / "gliner_config.json"
            if config_file.exists():
                print(f"  从本地加载: {self.local_dir}")
                self.model = GLiNER.from_pretrained(str(self.local_dir))
            else:
                print(f"  本地未找到模型，正在下载 {self.MODEL_ID} ...")
                self.model = GLiNER.from_pretrained(self.MODEL_ID)
                self.model.save_pretrained(str(self.local_dir))
            print(f"✓ {self.name} 加载成功")
            return True
        except Exception as e:
            print(f"✗ {self.name} 加载失败: {e}")
            return False

    def predict(self, text: str, labels: List[str]) -> List[Tuple]:
        """向后兼容的单次调用。新代码应使用 predict_with_chunks。"""
        if not self.model:
            return []
        with self._lock:
            try:
                entities = self.model.predict_entities(text, labels, threshold=0.5)
                return [(e["text"], e["label"], e["start"], e["end"], e["score"]) for e in entities]
            except Exception as e:
                print(f"  {self.name} 预测错误: {e}")
                return []


# ─── UIE 模型:不支持分块,走降级路径 ─────────────────────────────────────────

class UIEModel:
    model_max_tokens: Optional[int] = None  # 不支持分块

    def __init__(self, model_size: str = "base") -> None:
        self.name = f"UIE-{model_size}"
        self.model = None
        self.model_size = model_size
        self.local_dir = MODEL_DIRS["uie"]
        # set_schema 修改模型状态，多线程必须串行
        self._lock = threading.Lock()

    def load(self) -> bool:
        try:
            from paddlenlp import Taskflow
            model_name = "uie-base" if self.model_size == "base" else "uie-micro"
            model_dir = self.local_dir / "taskflow" / "information_extraction" / model_name
            static_dir = model_dir / "static"
            model_file = static_dir / "inference.pdmodel"
            json_file  = static_dir / "inference.json"

            if json_file.exists() and not model_file.exists():
                import shutil
                print(f"  检测到旧 PIR 格式文件，正在清理...")
                shutil.rmtree(str(static_dir))

            if model_file.exists():
                print(f"  从本地加载 {model_name}: {model_dir}")
            else:
                print(f"  本地未找到 {model_name}，正在下载...")

            self.model = Taskflow(
                "information_extraction",
                model=model_name,
                schema=[],
                device="cpu",
                precision="fp32",
                use_fast=False,
                home_path=str(self.local_dir),
            )
            print(f"✓ {self.name} 加载成功")
            return True
        except Exception as e:
            print(f"✗ {self.name} 加载失败: {e}")
            return False

    def predict(self, text: str, labels: List[str]) -> List[Tuple]:
        """UIE 原生推理,已有调用者使用。predict_with_chunks 内部会复用它。"""
        if not self.model:
            return []
        with self._lock:
            try:
                self.model.set_schema(labels)
                results = self.model(text)
                entities: List[Tuple] = []
                if results:
                    for label in labels:
                        for item in results[0].get(label, []):
                            if isinstance(item, dict) and "text" in item:
                                entity_text = item["text"]
                                prob  = item.get("probability", 1.0)
                                start = item.get("start", text.find(entity_text))
                                if start != -1:
                                    end = item.get("end", start + len(entity_text))
                                    entities.append((entity_text, label, start, end, prob))
                return entities
            except Exception as e:
                print(f"  {self.name} 预测错误: {e}")
                return []

    def predict_with_chunks(
        self, text: str, labels: List[str], chunk_cfg: ChunkConfig,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """
        UIE 不支持分块,走降级路径:整段调用 predict,chunk_metadata.chunking_enabled=False。
        """
        # 一次性日志:每个 UIE 实例只打一次 "chunking skipped: unsupported model"
        if not getattr(self, "_chunking_skipped_logged", False):
            logger.info(f"chunking skipped: unsupported model {self.name}")
            self._chunking_skipped_logged = True

        tuples = self.predict(text, labels)
        entities: List[Dict[str, Any]] = []
        for t in tuples:
            entities.append({
                "text":  t[0],
                "label": t[1],
                "start": int(t[2]),
                "end":   int(t[3]),
                "score": float(t[4]),
                "chunk_index":      0,
                "chunk_char_start": 0,
                "chunk_char_end":   len(text),
            })

        metadata_dict = {
            "chunking_enabled":    False,
            "model_max_tokens":    None,
            "labels_token_count":  None,
            "effective_max_tokens": None,
            "overlap":             chunk_cfg.overlap,
            "safety_margin":       chunk_cfg.safety_margin,
            "threshold":           chunk_cfg.threshold,
            "chunk_count":         1,
            "chunks": [{
                "chunk_index": 0,
                "char_start":  0,
                "char_end":    len(text),
                "char_length": len(text),
                "token_count": None,
                "status":      "done",
                "error":       None,
            }],
        }
        return entities, metadata_dict


# ─── LLM 大模型:通过 OpenAI 兼容 API 进行实体提取 ─────────────────────────

class LLMModel:
    """
    通过 OpenAI 兼容的 /v1/chat/completions 接口进行实体提取。

    与 NER 模型（GLiNER / UIE）实现相同的 predict_with_chunks 接口,
    复用现有的任务队列、数据库、缓存、回调等基础设施。

    不支持分块,走 UIE 相同的降级路径（整段文本一次请求）。
    """
    model_max_tokens: Optional[int] = None  # 不支持分块
    model_type: str = "llm"  # 用于前端区分

    # ── Prompt 模板 ───────────────────────────────────────────────────────
    _SYSTEM_PROMPT = (
        "你是一个专业的命名实体识别(NER)助手。"
        "你的任务是从给定文本中精确提取指定类型的实体。"
        "你必须只输出一个 JSON 数组,不要包含任何其他文字、解释或 markdown 格式。"
    )

    _USER_PROMPT_TEMPLATE = (
        "请从下面的文本中提取以下类型的实体: {labels}\n"
        "\n"
        "文本:\n"
        "---\n"
        "{text}\n"
        "---\n"
        "\n"
        "请以 JSON 数组格式输出所有提取到的实体,每个实体包含:\n"
        '- "text": 实体文本（必须是原文中的原始片段,不要修改）\n'
        '- "label": 实体类型（必须是上面指定的类型之一）\n'
        '- "start": 实体在原文中的起始字符位置（从0开始计数）\n'
        '- "end": 实体在原文中的结束字符位置（不包含该位置的字符）\n'
        "\n"
        "要求:\n"
        "1. start 和 end 必须精确,text[start:end] 必须等于实体文本\n"
        "2. 如果没有找到任何实体,返回空数组 []\n"
        "3. 只输出 JSON 数组,不要包含 ```json 等 markdown 标记或任何其他文字\n"
    )

    def __init__(
        self,
        model_name: str,
        base_url: str,
        api_key: str = "none",
        temperature: float = 0.1,
        timeout: int = 120,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.name = model_name
        self.model = model_name  # API 请求中的 model 字段
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.headers = headers or {}
        self._lock = threading.Lock()

    def load(self) -> bool:
        """
        验证 LLM API 可达性。

        发送一个极简请求来测试连通性,失败时仅打印警告而非阻止启动
        (LLM 服务可能稍后才上线)。
        """
        import httpx

        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key and self.api_key.lower() != "none":
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.headers:
            headers.update(self.headers)

        try:
            with httpx.Client(timeout=10) as client:
                resp = client.post(
                    url,
                    headers=headers,
                    json={
                        "model": self.model,
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 5,
                        "stream": False,
                    },
                )
                # 任何 2xx / 4xx 都说明 API 可达(4xx 可能是模型名错误但服务在线)
                if resp.status_code < 500:
                    print(f"✓ LLM {self.name} 连通测试成功 (HTTP {resp.status_code})")
                    return True
                else:
                    print(f"✗ LLM {self.name} 连通测试收到 HTTP {resp.status_code}，不注册该模型")
                    return False
        except Exception as e:
            print(f"✗ LLM {self.name} 连通测试失败: {e}，不注册该模型")
            return False

    def _build_messages(self, text: str, labels: List[str]) -> List[Dict[str, str]]:
        """构建 chat messages。"""
        labels_str = "、".join(f'"{l}"' for l in labels)
        user_content = self._USER_PROMPT_TEMPLATE.format(
            labels=labels_str, text=text,
        )
        return [
            {"role": "system", "content": self._SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

    def _call_api(self, messages: List[Dict[str, str]]) -> str:
        """
        调用 OpenAI 兼容 API,返回模型生成的文本。

        优先使用 stream 模式(支持大文本长时间生成),
        拼接所有 chunk 后返回完整文本。
        """
        import httpx

        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key and self.api_key.lower() != "none":
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.headers:
            headers.update(self.headers)

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "stream": True,
            "chat_template_kwargs": {"enable_thinking": False},
        }

        # 打印发送给 LLM 的完整提示词
        import json as _json_log
        logger.info(
            "[LLM] %s — 发送提示词:\n%s",
            self.name,
            _json_log.dumps(messages, ensure_ascii=False, indent=2),
        )

        try:
            with httpx.Client(timeout=self.timeout) as client:
                with client.stream("POST", url, headers=headers, json=payload) as resp:
                    if resp.status_code != 200:
                        body = resp.read().decode("utf-8", errors="replace")
                        raise RuntimeError(
                            f"LLM API 返回 HTTP {resp.status_code}: {body[:500]}"
                        )

                    # 拼接 SSE stream 内容
                    import json as _json
                    collected: List[str] = []
                    for line in resp.iter_lines():
                        if not line or not line.startswith("data: "):
                            continue
                        data_str = line[len("data: "):]
                        if data_str.strip() == "[DONE]":
                            break
                        try:
                            chunk = _json.loads(data_str)
                            # 有的服务(如部分代理)首个 chunk 的 choices 为空数组(仅带 role/usage),
                            # 需跳过,避免 choices[0] 触发 IndexError
                            choices = chunk.get("choices") or []
                            if not choices:
                                continue
                            delta = choices[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                collected.append(content)
                        except _json.JSONDecodeError:
                            continue

                    full_text = "".join(collected)
                    if not full_text.strip():
                        raise RuntimeError("LLM 返回了空内容")
                    return full_text

        except httpx.TimeoutException as exc:
            raise RuntimeError(
                f"LLM API 请求超时 ({self.timeout}s),请检查模型服务是否正常或增大 LLM_TIMEOUT"
            ) from exc
        except httpx.ConnectError as exc:
            raise RuntimeError(
                f"无法连接 LLM API ({self.base_url}),请确认服务已启动"
            ) from exc

    def _parse_entities(
        self, raw_text: str, original_text: str, labels: List[str],
    ) -> List[Dict[str, Any]]:
        """
        从大模型回复中解析实体 JSON。

        支持处理模型输出中可能包含的 markdown 代码块标记。
        会验证并修正 start/end 偏移量。
        """
        import json as _json
        import re as _re

        content = raw_text.strip()

        # 去除可能的 markdown 代码块包裹
        md_match = _re.search(r"```(?:json)?\s*\n?(.*?)```", content, _re.DOTALL)
        if md_match:
            content = md_match.group(1).strip()

        # 尝试提取 JSON 数组（模型可能在数组前后附加了文字）
        arr_match = _re.search(r"\[.*\]", content, _re.DOTALL)
        if arr_match:
            content = arr_match.group(0)

        try:
            parsed = _json.loads(content)
        except _json.JSONDecodeError as exc:
            try:
                import json_repair
                parsed = json_repair.loads(content)
                logger.info("LLM 输出 JSON 解析失败，但使用 json-repair 成功修复并解析")
            except Exception as repair_exc:
                logger.error(
                    f"LLM 输出 JSON 解析且尝试修复失败: {exc} (修复错误: {repair_exc})\n"
                    f"原始输出:\n{raw_text}\n"
                    f"尝试解析的文本:\n{content}"
                )
                raise RuntimeError(f"LLM 输出的 JSON 格式不正确，且修复失败: {exc}") from exc

        if not isinstance(parsed, list):
            logger.error(
                f"LLM 输出不是 JSON 数组, 而是 {type(parsed).__name__}\n"
                f"原始输出:\n{raw_text}\n"
                f"解析得到的对象:\n{parsed}"
            )
            raise RuntimeError(
                f"LLM 输出不是 JSON 数组,而是 {type(parsed).__name__}"
            )

        valid_labels = set(labels)
        entities: List[Dict[str, Any]] = []

        for item in parsed:
            if not isinstance(item, dict):
                continue

            text = item.get("text", "")
            label = item.get("label", "")
            start = item.get("start")
            end = item.get("end")

            if not text or not label:
                continue
            if label not in valid_labels:
                logger.warning(f"LLM 输出了未知标签 '{label}',跳过")
                continue

            # 验证并修正 start/end
            if start is not None and end is not None:
                try:
                    start = int(start)
                    end = int(end)
                except (ValueError, TypeError):
                    start, end = None, None

            # 如果 start/end 不正确或缺失,尝试在原文中查找
            if (
                start is None
                or end is None
                or start < 0
                or end > len(original_text)
                or original_text[start:end] != text
            ):
                # 回退: 在原文中搜索实体文本
                idx = original_text.find(text)
                if idx >= 0:
                    start = idx
                    end = idx + len(text)
                else:
                    # 实体文本不在原文中,跳过
                    logger.warning(
                        f"LLM 提取的实体 '{text}' 不在原文中,跳过"
                    )
                    continue

            entities.append({
                "text": text,
                "label": label,
                "start": start,
                "end": end,
                "score": 1.0,  # LLM 不提供置信度,统一设为 1.0
            })

        return entities

    def predict(self, text: str, labels: List[str]) -> List[Tuple]:
        """向后兼容的单次调用。新代码应使用 predict_with_chunks。"""
        if not text or not labels:
            return []
        with self._lock:
            try:
                messages = self._build_messages(text, labels)
                raw = self._call_api(messages)
                entities = self._parse_entities(raw, text, labels)
                return [
                    (e["text"], e["label"], e["start"], e["end"], e["score"])
                    for e in entities
                ]
            except Exception as e:
                print(f"  {self.name} 预测错误: {e}")
                return []

    def predict_with_chunks(
        self, text: str, labels: List[str], chunk_cfg: ChunkConfig,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """
        LLM 不支持分块,走降级路径:整段调用,chunk_metadata.chunking_enabled=False。
        与 UIEModel.predict_with_chunks 保持一致的返回格式。
        """
        if not getattr(self, "_chunking_skipped_logged", False):
            logger.info(f"chunking skipped: LLM model {self.name}")
            self._chunking_skipped_logged = True

        with self._lock:
            try:
                messages = self._build_messages(text, labels)
                raw = self._call_api(messages)
                parsed_entities = self._parse_entities(raw, text, labels)
            except Exception as exc:
                logger.error(f"{self.name}: LLM 推理失败: {exc}")
                # 构造 failed chunk metadata
                return [], {
                    "chunking_enabled": False,
                    "model_max_tokens": None,
                    "labels_token_count": None,
                    "effective_max_tokens": None,
                    "overlap": chunk_cfg.overlap,
                    "safety_margin": chunk_cfg.safety_margin,
                    "threshold": chunk_cfg.threshold,
                    "chunk_count": 1,
                    "chunks": [{
                        "chunk_index": 0,
                        "char_start": 0,
                        "char_end": len(text),
                        "char_length": len(text),
                        "token_count": None,
                        "status": "failed",
                        "error": str(exc)[:200],
                    }],
                }

        entities: List[Dict[str, Any]] = []
        for e in parsed_entities:
            entities.append({
                "text": e["text"],
                "label": e["label"],
                "start": e["start"],
                "end": e["end"],
                "score": e["score"],
                "chunk_index": 0,
                "chunk_char_start": 0,
                "chunk_char_end": len(text),
            })

        metadata_dict = {
            "chunking_enabled": False,
            "model_max_tokens": None,
            "labels_token_count": None,
            "effective_max_tokens": None,
            "overlap": chunk_cfg.overlap,
            "safety_margin": chunk_cfg.safety_margin,
            "threshold": chunk_cfg.threshold,
            "chunk_count": 1,
            "chunks": [{
                "chunk_index": 0,
                "char_start": 0,
                "char_end": len(text),
                "char_length": len(text),
                "token_count": None,
                "status": "done",
                "error": None,
            }],
        }
        return entities, metadata_dict


# ─── 注册表 ────────────────────────────────────────────────────────────────────
MODEL_REGISTRY: Dict[str, Any] = {
    "gliner-x-large":    lambda: GLiNERModel(),
    "gliner_multi-v2.1": lambda: GLiNERMultiModel(),
    "UIE-base":          lambda: UIEModel("base"),
    "UIE-micro":         lambda: UIEModel("micro"),
    # IOC 确定性提取器伪模型(与 NER 模型并列,通过 MODELS_ENABLED 启用)
    "ioc-finder":        lambda: _make_ioc_model("ioc-finder", "ioc_finder"),
    "iocextract":        lambda: _make_ioc_model("iocextract", "iocextract"),
    # ATT&CK 技术识别模型
    "tram":                   lambda: _make_tram_model("tram"),
    "qwen3-attack-retriever": lambda: _make_qwen3_attack_retriever("qwen3-attack-retriever"),
}


def _make_tram_model(name: str):
    """延迟构造 TRAMModel,避免顶部导入 torch/transformers 拖慢启动。"""
    from .tram_extractor import TRAMModel
    # TRAMModel 内部会从 env MODELS_ROOT 解析模型目录;兼容带/不带 models_root 的构造签名。
    try:
        return TRAMModel(name, models_root=MODELS_ROOT)
    except TypeError:
        return TRAMModel(name)


def _make_qwen3_attack_retriever(name: str):
    """延迟构造跨平台 PyTorch Retriever，仅启用时加载 4B 融合模型。"""
    from .torch_attack_retriever import Qwen3AttackRetrieverModel
    return Qwen3AttackRetrieverModel(name, models_root=MODELS_ROOT)


def _make_ioc_model(name: str, backend: str):
    """延迟构造 IOCExtractorModel,避免顶部循环导入。"""
    from .ioc_extractor import IOCExtractorModel
    return IOCExtractorModel(name, backend=backend)


# 归入 ATT&CK 调用台的专有(注册表)模型
_ATTACK_REGISTRY_MODELS = {"tram", "qwen3-attack-retriever"}


# ─── 功能模块开关过滤(NER_ENABLED / ATTACK_ENABLED)──────────────────────────
def _roster_entry_console(kind: str, name: str, console: str) -> str:
    """
    判定一个清单条目最终归属的调用台(ner / attack),供功能开关过滤。

    - 注册表模型以自身类型为准: tram / qwen3-attack-retriever 恒属 ATT&CK,
      其余(GLiNER / UIE / IOC)恒属 NER,与被误配进哪份清单无关;
    - auto / LLM 以所在清单为准: ATT&CK 清单里的 auto 即 attack-auto,
      LLM 会包装成 AttackLLMModel;NER 清单里的 LLM 是 NER 抽取器(LLMModel)。
    """
    if kind == "registry":
        return "attack" if name in _ATTACK_REGISTRY_MODELS else "ner"
    return console


def _feature_allows_entry(kind: str, name: str, console: str) -> bool:
    """功能开关是否允许加载该条目;被关闭的调用台对应模型一律不加载(零开销)。"""
    if _roster_entry_console(kind, name, console) == "attack":
        return attack_enabled()
    return ner_enabled()

# 匹配 LLM 组的 models 变量名: LLM_MODELS(第1组) 或 LLM_MODELS_<N>(第N组)
_LLM_GROUP_KEY_RE = re.compile(r"^LLM_MODELS(?:_(\d+))?$")
_LLM_DEFAULT_URL = "http://localhost:8000/v1"


def _discover_llm_groups() -> List[dict]:
    """
    动态发现 .env 中的所有 LLM 连接组(不写死组数)。

    扫描环境变量里形如 `LLM_MODELS` / `LLM_MODELS_<N>` 的键,每个键对应一组连接:
      LLM_MODELS[_N] / LLM_BASE_URL[_N] / LLM_API_KEY[_N] /
      LLM_TEMPERATURE[_N] / LLM_TIMEOUT[_N] / LLM_HEADERS[_N]
    组序: 无后缀的 `LLM_MODELS` 视为第 1 组(别名同时含 `LLM_MODELS` 与 `LLM_MODELS_1`);
          `LLM_MODELS_<N>` 为第 N 组(别名 `LLM_MODELS_<N>`)。按组序升序返回。

    返回 [{"aliases": {别名小写,...}, "conn": {base_url/api_key/temperature/timeout/headers},
           "models": [名字,...]}, ...];同名模型跨组时后面的组覆盖前面的(组序大者优先)。
    """
    # index(组序,int) → suffix(连接变量后缀: "" 或 "_N")
    found: Dict[int, str] = {}
    for key in os.environ:
        m = _LLM_GROUP_KEY_RE.match(key)
        if not m:
            continue
        num = m.group(1)
        if num is None:
            found[1] = ""                       # 无后缀 LLM_MODELS 恒为第 1 组、连接变量无后缀
        else:
            found.setdefault(int(num), f"_{num}")  # LLM_MODELS_N;若第1组已由无后缀占位则不覆盖

    groups: List[dict] = []
    for idx in sorted(found):
        suffix = found[idx]
        models_raw = os.environ.get(f"LLM_MODELS{suffix}", "").strip()
        model_names = [x.strip() for x in models_raw.split(",") if x.strip()]
        try:
            temperature = float(os.environ.get(f"LLM_TEMPERATURE{suffix}", "0.1"))
        except (TypeError, ValueError):
            temperature = 0.1
        try:
            timeout = int(os.environ.get(f"LLM_TIMEOUT{suffix}", "120"))
        except (TypeError, ValueError):
            timeout = 120
        headers: Dict[str, str] = {}
        hraw = os.environ.get(f"LLM_HEADERS{suffix}", "").strip()
        if hraw:
            try:
                headers = json.loads(hraw)
            except Exception as e:  # noqa: BLE001
                print(f"⚠ 解析 LLM_HEADERS{suffix} 失败: {e}，需为合法 JSON 字符串")
        aliases = {f"llm_models{suffix}".lower()}
        if suffix == "":
            aliases.add("llm_models_1")  # 第 1 组也可用 LLM_MODELS_1 引用
        groups.append({
            "aliases": aliases,
            "conn": {
                "base_url": os.environ.get(f"LLM_BASE_URL{suffix}", _LLM_DEFAULT_URL),
                "api_key": os.environ.get(f"LLM_API_KEY{suffix}", "none"),
                "temperature": temperature,
                "timeout": timeout,
                "headers": headers,
            },
            "models": model_names,
        })
    return groups


def _parse_llm_groups() -> Tuple[Dict[str, dict], Dict[str, dict]]:
    """
    汇总所有 LLM 连接组,返回 (by_alias, by_model):
      by_alias: {组别名(小写): group}  —— 用于按组引用(LLM_MODELS_1 / _2 / ...)
      by_model: {模型名: group}        —— 用于按模型名引用(glm-5 等)
    """
    by_alias: Dict[str, dict] = {}
    by_model: Dict[str, dict] = {}
    for group in _discover_llm_groups():
        for a in group["aliases"]:
            by_alias[a] = group
        for name in group["models"]:
            by_model[name] = group
    return by_alias, by_model


def _resolve_roster_entry(
    entry: str, by_alias: Dict[str, dict], by_model: Dict[str, dict],
) -> List[Tuple[str, str, Optional[dict]]]:
    """
    把一个清单条目解析为 [(kind, name, conn), ...]。
      kind: "auto" | "registry" | "llm" | "unknown"
    条目可为: 'auto' / 专有模型名(在 MODEL_REGISTRY) / LLM 组别名(LLM_MODELS_1..) / 具体 LLM 模型名。
    """
    e = entry.strip()
    if not e:
        return []
    if e == "auto":
        return [("auto", "auto", None)]
    if e in MODEL_REGISTRY:
        return [("registry", e, None)]
    alias = e.lower()
    if alias in by_alias:
        grp = by_alias[alias]
        return [("llm", m, grp["conn"]) for m in grp["models"]]
    if e in by_model:
        return [("llm", e, by_model[e]["conn"])]
    return [("unknown", e, None)]


def load_models(names: List[str]) -> Dict[str, Any]:
    """
    加载模型入口。

    - 新配置(推荐):设置 `NER_MODELS_ENABLED` / `ATTACK_MODELS_ENABLED` 两份「按调用台
      组织」的清单;每份都可混选 专有模型(gliner/ioc/uie/auto/tram) 与 LLM(按组别名
      LLM_MODELS_1.. 或具体模型名引用,连接自动从对应 LLM_* 组解析)。NER 清单里的 LLM
      包成 NER 抽取器(LLMModel);ATT&CK 清单里的 LLM 包成 ATT&CK 映射器(AttackLLMModel)。
      同一个 LLM 同时进两个台时,ATT&CK 实例自动登记为 `<name>-attack`(缓存天然隔离)。
    - 旧配置(向后兼容):未设置上述两个变量时,回退到 `MODELS_ENABLED` + `LLM_MODELS*`
      (全部当 NER) 的历史逻辑(旧路径不支持 attack-llm,如需请改用两份清单)。
    """
    ner_roster = os.environ.get("NER_MODELS_ENABLED", "").strip()
    attack_roster = os.environ.get("ATTACK_MODELS_ENABLED", "").strip()
    if ner_roster or attack_roster:
        return _load_models_rosters(ner_roster, attack_roster)
    return _load_models_legacy(names)


def _load_models_rosters(ner_roster_raw: str, attack_roster_raw: str) -> Dict[str, Any]:
    """新配置路径:按两份调用台清单加载模型。"""
    loaded: Dict[str, Any] = {}
    ner_auto_enabled = False
    attack_auto_enabled = False
    by_alias, by_model = _parse_llm_groups()

    def _load_registry(name: str, console: str) -> None:
        if name in loaded:
            return
        # 类型 / 台 归属校验(仅告警,不阻断)
        if console == "ner" and name in _ATTACK_REGISTRY_MODELS:
            print(f"⚠ '{name}' 是 ATT&CK 模型,却配在 NER 清单,它仍会归入 ATT&CK 调用台")
        if console == "attack" and name not in _ATTACK_REGISTRY_MODELS:
            print(f"⚠ '{name}' 不是 ATT&CK 专有模型,却配在 ATT&CK 清单,它仍按自身类型归台")
        model = MODEL_REGISTRY[name]()
        if model.load():
            loaded[name] = model

    def _load_llm(name: str, conn: dict, console: str) -> None:
        if console == "attack":
            from .attack_mapper import AttackLLMModel
            inst = AttackLLMModel(
                model_name=name, base_url=conn["base_url"], api_key=conn["api_key"],
                temperature=conn["temperature"], timeout=conn["timeout"],
                headers=conn["headers"], models_root=MODELS_ROOT,
            )
            key = name if name not in loaded else f"{name}-attack"
            inst.name = key  # 展示/调用名唯一;inst.model 仍是真实 API 模型名
        else:
            inst = LLMModel(
                model_name=name, base_url=conn["base_url"], api_key=conn["api_key"],
                temperature=conn["temperature"], timeout=conn["timeout"],
                headers=conn["headers"],
            )
            key = name
        if key in loaded:
            print(f"⚠ 模型键 '{key}' 冲突,跳过")
            return
        if inst.load():
            loaded[key] = inst

    def _load_roster(raw: str, console: str) -> None:
        nonlocal ner_auto_enabled, attack_auto_enabled
        for entry in [x.strip() for x in raw.split(",") if x.strip()]:
            for kind, name, conn in _resolve_roster_entry(entry, by_alias, by_model):
                # 功能模块开关:被关闭调用台的模型不加载
                if not _feature_allows_entry(kind, name, console):
                    print(f"  功能开关已关闭,跳过 {console} 清单条目 '{name}'")
                    continue
                if kind == "auto":
                    if console == "ner":
                        ner_auto_enabled = True
                    else:
                        attack_auto_enabled = True
                elif kind == "registry":
                    _load_registry(name, console)
                elif kind == "llm":
                    _load_llm(name, conn, console)
                else:
                    print(f"⚠ {console} 清单: 无法解析 '{name}'(不在模型注册表,也不匹配任何 LLM 组别名/模型名),跳过")

    _load_roster(ner_roster_raw, "ner")
    _load_roster(attack_roster_raw, "attack")

    if ner_auto_enabled and "auto" not in loaded:
        from .ioc_extractor import AutoExtractorModel
        auto = AutoExtractorModel("auto")
        if auto.load():
            auto.set_registry(loaded)
            loaded["auto"] = auto
            print(f"  auto 兜底模型: {auto._fallback_models or '(无, 未匹配标签将被忽略)'}")

    if attack_auto_enabled and "attack-auto" not in loaded:
        from .attack_auto_router import AttackAutoModel
        attack_auto = AttackAutoModel("attack-auto")
        if attack_auto.load():
            try:
                attack_auto.set_registry(loaded)
            except ValueError as exc:
                print(f"✗ attack-auto 未注册: {exc}")
            else:
                loaded["attack-auto"] = attack_auto

    return loaded


def _load_models_legacy(names: List[str]) -> Dict[str, Any]:
    """
    旧配置路径(向后兼容):MODELS_ENABLED + LLM_MODELS*(全部当 NER)。
    不支持 attack-llm(如需请改用 NER_MODELS_ENABLED / ATTACK_MODELS_ENABLED 两份清单)。
    """
    loaded: Dict[str, Any] = {}
    # "auto" 需要在其它模型加载完成后再创建并注入引用,这里先记下是否启用。
    auto_enabled = False

    # ── 1. 加载 NER / IOC 模型（GLiNER / UIE / ioc-finder / iocextract）──
    for raw in names:
        name = raw.strip()
        if not name:
            continue
        # 功能模块开关:被关闭调用台的模型不加载(注册表模型按自身类型归台)
        if name == "auto":
            auto_enabled = ner_enabled()
            if not auto_enabled:
                print("  功能开关已关闭,跳过 NER 清单条目 'auto'")
            continue
        if name not in MODEL_REGISTRY:
            print(f"⚠ 未知模型名称: {name}，跳过")
            continue
        if not _feature_allows_entry("registry", name, "ner"):
            print(f"  功能开关已关闭,跳过模型 '{name}'")
            continue
        model = MODEL_REGISTRY[name]()
        if model.load():
            loaded[name] = model

    # ── 2. 加载 LLM 模型（动态发现所有 LLM_MODELS[_N] 组，全部当 NER）────
    for group in _discover_llm_groups() if ner_enabled() else []:
        conn = group["conn"]
        for name in group["models"]:
            if name in loaded:
                print(f"⚠ LLM 模型名称 '{name}' 与已加载的 NER 模型冲突，跳过")
                continue
            model = LLMModel(
                model_name=name,
                base_url=conn["base_url"],
                api_key=conn["api_key"],
                temperature=conn["temperature"],
                timeout=conn["timeout"],
                headers=conn["headers"],
            )
            if model.load():
                loaded[name] = model

    # 说明:attack-llm(大模型 ATT&CK 映射)统一走 ATTACK_MODELS_ENABLED(新配置路径),
    #      旧路径不再支持;如需在旧配置下使用 attack-llm,请改用两份清单配置。

    # ── 3. 加载 auto 智能分流伪模型（基于 api/auto_routing.json）─────────
    #    ioc-finder / iocextract 已在第 1 步随 MODELS_ENABLED 加载。
    #    auto 需引用其它已加载模型,因此在最后用 set_registry 注入。
    #    兜底模型与优先级全部来自 auto_routing.json(labels 为空的 route 即兜底),
    #    不再依赖 .env 的 IOC_AUTO_DEFAULT_MODEL。
    if auto_enabled and "auto" not in loaded:
        from .ioc_extractor import AutoExtractorModel
        auto = AutoExtractorModel("auto")
        if auto.load():
            auto.set_registry(loaded)
            loaded["auto"] = auto
            fb = auto._fallback_models
            print(f"  auto 兜底模型: {fb or '(无, 未匹配标签将被忽略)'}")

    return loaded


