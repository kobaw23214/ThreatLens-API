"""
共享 fixtures 与生成器 (long-text-chunking-ner 的 property-based tests)。

设计原则:
  - 真实 tokenizer 从本地 gliner_multi-v2.1 模型目录加载;若不可用则标记 skip。
    PBT 属性必须用真实 tokenizer 才能验证"token 数 ≤ effective_max_tokens"等不变量。
  - 不加载真实 GLiNER 模型。需要测试 predict_with_chunks 的用例使用 FakeGLiNERModel,
    在每个 Chunk 文本中生成伪随机实体,保证 offset 回写与去重测试 100+ 次迭代成本可控。
"""
from __future__ import annotations

import os
import random
import string
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

import pytest

# 允许 tests/ 下直接 `from api...` import
sys.path.insert(0, str(Path(__file__).parent.parent))

# 在单元测试环境中默认禁用认证，使已有的 API 单元测试正常运行
os.environ["AUTH_ENABLED"] = "false"
# 自动启用测试所需的模型
os.environ["MODELS_ENABLED"] = "gliner-x-large,gliner_multi-v2.1"

MODEL_DIR_MULTI = Path(__file__).parent.parent / "models" / "gliner_multi-v2.1"
MODEL_DIR_XLARGE = Path(__file__).parent.parent / "models" / "gliner-x-large"


# ─── 真实 tokenizer fixture ───────────────────────────────────────────────

@pytest.fixture(scope="session")
def multi_tokenizer():
    """gliner_multi-v2.1 的 tokenizer (mDeBERTa-v3-base)。session 范围,只加载一次。"""
    if not MODEL_DIR_MULTI.exists():
        pytest.skip(f"本地模型目录不存在: {MODEL_DIR_MULTI}")
    try:
        from transformers import AutoTokenizer
    except ImportError:
        pytest.skip("transformers 未安装")
    return AutoTokenizer.from_pretrained(str(MODEL_DIR_MULTI))


@pytest.fixture(scope="session")
def xlarge_tokenizer():
    """gliner-x-large 的 tokenizer (mT5-large)。session 范围。"""
    if not MODEL_DIR_XLARGE.exists():
        pytest.skip(f"本地模型目录不存在: {MODEL_DIR_XLARGE}")
    try:
        from transformers import AutoTokenizer
    except ImportError:
        pytest.skip("transformers 未安装")
    return AutoTokenizer.from_pretrained(str(MODEL_DIR_XLARGE))


@pytest.fixture(scope="session")
def multi_model_max_tokens():
    return 384


@pytest.fixture(scope="session")
def xlarge_model_max_tokens():
    return 1024


# ─── FakeGLiNERModel: 仿真 predict_entities 行为 ─────────────────────────

@dataclass
class _FakeDataProcessor:
    transformer_tokenizer: object


class FakeGLiNERModel:
    """
    Fake GLiNER 推理对象,供 offset 回写 / 去重相关的 PBT 使用。
    - data_processor.transformer_tokenizer: 真实 tokenizer 传入
    - predict_entities(text, labels, threshold): 对输入文本按每 k 个字符确定性地
      产出一个实体,start/end 是局部偏移,与 GLiNER 原生输出格式一致。
    """

    def __init__(self, tokenizer, entities_per_chunk: int = 3, seed: int = 12345):
        self.data_processor = _FakeDataProcessor(transformer_tokenizer=tokenizer)
        self._entities_per_chunk = entities_per_chunk
        self._seed = seed

    def predict_entities(self, text: str, labels: List[str], threshold: float = 0.5):
        """
        基于 (text, labels, threshold) 的确定性伪随机实体生成。同一输入多次调用结果相同。
        """
        if not text or not labels:
            return []
        rng = random.Random((hash(text[:50]) ^ hash(tuple(labels)) ^ self._seed) & 0xFFFFFFFF)
        out = []
        n = len(text)
        for _ in range(self._entities_per_chunk):
            if n < 2:
                break
            start = rng.randrange(0, n - 1)
            end = min(n, start + rng.randint(1, 6))
            label = rng.choice(labels)
            score = round(rng.uniform(threshold, 1.0), 3)
            out.append({
                "text": text[start:end],
                "label": label,
                "start": start,
                "end": end,
                "score": score,
            })
        return out


class FakeGLiNERChunkingModel:
    """
    带 _GLiNERChunkingMixin 能力的完整 fake 模型,用于 predict_with_chunks PBT。
    - 复用真实 tokenizer 保证 chunking 精度
    - predict_entities 走 FakeGLiNERModel 的确定性逻辑
    """

    def __init__(self, tokenizer, model_max_tokens: int = 384,
                 entities_per_chunk: int = 3):
        # 这些属性名必须与 api.models._GLiNERChunkingMixin 期望的一致
        self.name = "fake-gliner"
        self.model = FakeGLiNERModel(tokenizer, entities_per_chunk=entities_per_chunk)
        self.model_max_tokens = model_max_tokens
        self._lock = threading.Lock()
        # 注入 mixin 的方法 (运行时借用)
        from api.models import _GLiNERChunkingMixin
        for attr in ("_get_tokenizer", "_single_chunk_metadata", "predict_with_chunks"):
            setattr(
                self,
                attr,
                _GLiNERChunkingMixin.__dict__[attr].__get__(self, FakeGLiNERChunkingModel),
            )


# ─── Hypothesis 策略工厂 ─────────────────────────────────────────────────

def mixed_text_strategy(min_size: int = 0, max_size: int = 1500):
    """
    生成中英混合文本策略,覆盖常见标点 + 字母 + 汉字 + 空白。
    默认 max_size 控制在 1500,保证每次迭代 tokenizer encode 开销可控。
    """
    from hypothesis import strategies as st

    alphabet_blocks = (
        st.characters(min_codepoint=0x4E00, max_codepoint=0x9FA5),  # 常用汉字
        st.characters(whitelist_categories=("Ll", "Lu", "Nd")),     # 英文字母 + 数字
        st.sampled_from(list(" 。？！；…. ?!;\n\t, ")),                  # 标点 + 空白
    )
    return st.text(
        alphabet=st.one_of(*alphabet_blocks),
        min_size=min_size,
        max_size=max_size,
    )


def labels_strategy(min_size: int = 1, max_size: int = 20,
                    max_label_len: int = 20):
    """生成 labels 列表策略: 1-20 个 label,每个不超过 max_label_len 字符。"""
    from hypothesis import strategies as st
    return st.lists(
        st.text(
            alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Lo", "Nd")),
            min_size=1,
            max_size=max_label_len,
        ),
        min_size=min_size,
        max_size=max_size,
        unique=True,
    )


def entities_strategy(min_size: int = 0, max_size: int = 30):
    """
    生成实体列表策略,字段: raw_label / start / end / score / chunk_index / text。
    raw_label 从小集合取 (制造冲突),区间随机,score / chunk_index 随机。
    """
    from hypothesis import strategies as st

    def _one_entity(draw):
        raw_label = draw(st.sampled_from(["A", "B", "C"]))
        start = draw(st.integers(min_value=0, max_value=200))
        length = draw(st.integers(min_value=1, max_value=20))
        end = start + length
        score = draw(st.floats(min_value=0.0, max_value=1.0, allow_nan=False,
                               allow_infinity=False).map(lambda x: round(x, 3)))
        chunk_index = draw(st.integers(min_value=0, max_value=5))
        return {
            "text": f"e_{start}_{end}",
            "label": raw_label,  # 无 mapping 的情况下 label == raw_label
            "raw_label": raw_label,
            "start": start,
            "end": end,
            "score": score,
            "chunk_index": chunk_index,
        }

    return st.lists(st.composite(_one_entity)(), min_size=min_size, max_size=max_size)
