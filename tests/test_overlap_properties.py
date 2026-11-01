"""
Property-based tests: overlap 约束 与 "不分块" 场景单 Chunk 行为。

覆盖 Design Properties:
  P3  overlap 落在合法区间 (不超过 chunk_length 的一半、overlap=0 时无重叠)
  P5  所有"不分块"场景都产出单块 (chunk_enabled=False / UIE / effective<50 / 短文本)

对应 Requirements: 1.7 / 1.9 / 2.5 / 2.6 / 3.7
"""
from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from api.chunking import ChunkConfig, ChunkingService

from tests.conftest import (
    FakeGLiNERChunkingModel,
    labels_strategy,
    mixed_text_strategy,
)


# ─── P3: overlap 合法性 ──────────────────────────────────────────────────

@given(
    text=mixed_text_strategy(min_size=400, max_size=3000),
    overlap=st.integers(min_value=0, max_value=512),
    labels=labels_strategy(min_size=1, max_size=3, max_label_len=8),
)
@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_overlap_bounds(multi_tokenizer, multi_model_max_tokens, text, overlap, labels):
    """
    P3: 对 split 返回的 chunks (chunk_count > 1 时),
        每对相邻 chunk 的重叠字符数必须 <= 配置的 overlap,
        同时 <= 任一相邻 chunk 字符长度的一半。
        overlap=0 时,相邻 chunk 之间重叠必须严格为 0。

    Feature: long-text-chunking-ner
    Property: P3 (overlap 区间合法性)
    _Requirements: 1.7_
    """
    service = ChunkingService(
        tokenizer=multi_tokenizer,
        model_max_tokens=multi_model_max_tokens,
        safety_margin=16,
        overlap=overlap,
    )
    effective, _ = service.compute_effective_max_tokens(labels)
    if effective < 50:
        return

    chunks, meta = service.split(text, labels)
    if len(chunks) < 2:
        return  # 单块不在 P3 覆盖范围内

    for i in range(1, len(chunks)):
        prev, cur = chunks[i - 1], chunks[i]
        actual_overlap = prev.char_end - cur.char_start
        assert actual_overlap >= 0, f"chunks 出现倒序或空洞: {prev} / {cur}"
        assert actual_overlap <= overlap, (
            f"实际重叠 {actual_overlap} 超过配置 overlap={overlap}: "
            f"prev=[{prev.char_start},{prev.char_end}), cur=[{cur.char_start},{cur.char_end})"
        )
        if actual_overlap > 0:
            assert actual_overlap <= prev.char_length // 2, \
                f"重叠 {actual_overlap} 超过前块长度一半 {prev.char_length // 2}"
            assert actual_overlap <= cur.char_length // 2, \
                f"重叠 {actual_overlap} 超过后块长度一半 {cur.char_length // 2}"

        if overlap == 0:
            assert actual_overlap == 0, f"overlap=0 时不应有任何重叠,实际 {actual_overlap}"


# ─── P5: 不分块场景 → 单块 ────────────────────────────────────────────────

@given(
    text=mixed_text_strategy(min_size=1, max_size=400),
    labels=labels_strategy(min_size=1, max_size=5, max_label_len=8),
)
@settings(max_examples=60, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_no_chunking_chunk_enabled_false(multi_tokenizer, multi_model_max_tokens, text, labels):
    """
    P5 (a): chunk_enabled=False 时,无论文本长度,都产出 1 个 Chunk 且
            chunking_enabled=False, chunk 覆盖整段文本。

    Feature: long-text-chunking-ner
    Property: P5 (不分块场景单块行为)
    _Requirements: 3.7_
    """
    model = FakeGLiNERChunkingModel(multi_tokenizer, model_max_tokens=multi_model_max_tokens)
    cfg = ChunkConfig(enabled=False, overlap=32, safety_margin=16, threshold=0.5)
    entities, meta = model.predict_with_chunks(text, labels, cfg)

    assert meta["chunking_enabled"] is False
    assert meta["chunk_count"] == 1
    assert len(meta["chunks"]) == 1
    c = meta["chunks"][0]
    assert c["char_start"] == 0
    assert c["char_end"] == len(text)


@given(
    text=mixed_text_strategy(min_size=1, max_size=400),
    labels=labels_strategy(min_size=1, max_size=5, max_label_len=8),
)
@settings(max_examples=60, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_no_chunking_short_text(multi_tokenizer, multi_model_max_tokens, text, labels):
    """
    P5 (d): 短文本 (全文 token 数 <= effective_max_tokens) 应只产一个 Chunk,
            且 chunking_enabled=True (分块流程生效,只是文本刚好装得下)。

    Feature: long-text-chunking-ner
    Property: P5 (短文本单块行为)
    _Requirements: 1.9_
    """
    service = ChunkingService(
        tokenizer=multi_tokenizer,
        model_max_tokens=multi_model_max_tokens,
        safety_margin=16,
        overlap=32,
    )
    effective, _ = service.compute_effective_max_tokens(labels)
    if effective <= 0:
        return

    full_tokens = len(multi_tokenizer.encode(text, add_special_tokens=False))
    if full_tokens > effective:
        return  # 只验证短文本

    chunks, meta = service.split(text, labels)
    assert meta.chunking_enabled is True
    assert meta.chunk_count == 1
    assert len(chunks) == 1
    c = chunks[0]
    assert c.char_start == 0
    assert c.char_end == len(text)
    assert c.chunk_index == 0


def test_no_chunking_effective_too_small(multi_tokenizer):
    """
    P5 (c): effective_max_tokens < 50 时走降级路径,单 Chunk + chunking_enabled=False。

    Feature: long-text-chunking-ner
    Property: P5 (窗口过小单块行为)
    _Requirements: 2.6_
    """
    # 用较小的 model_max_tokens 与最大 safety_margin,方便让 effective 落到 (0, 50)
    service = ChunkingService(
        tokenizer=multi_tokenizer,
        model_max_tokens=80,     # 仅用于本属性验证,与真实模型无关
        safety_margin=40,
        overlap=16,
    )
    labels = ["a"]  # labels_token_count 极小, 让 effective = 80 - ~1 - 40 ≈ 39
    effective, _ = service.compute_effective_max_tokens(labels)
    assert 0 < effective < 50, f"期望 0 < effective < 50, 实际 {effective}"

    text = "这是一段测试文本" * 10
    chunks, meta = service.split(text, labels)
    assert meta.chunking_enabled is False, "effective<50 应走降级路径"
    assert meta.chunk_count == 1
    assert chunks[0].char_start == 0
    assert chunks[0].char_end == len(text)


def test_no_chunking_uie_downgrade():
    """
    P5 (b): UIE 模型走 predict_with_chunks 降级路径,总是 chunk_count=1,
            chunking_enabled=False, model_max_tokens=None。

    Feature: long-text-chunking-ner
    Property: P5 (UIE 降级单块)
    _Requirements: 2.5_
    """
    from api.models import UIEModel
    from api.chunking import ChunkConfig

    # 不真正加载 UIE 模型, 只构造实例并打 stub 的 self.predict
    uie = UIEModel("micro")

    def fake_predict(text, labels):
        # 返回 1 个实体的 tuple, 便于生成 chunk_metadata
        if not text:
            return []
        return [("x", labels[0], 0, 1, 0.9)]
    uie.predict = fake_predict
    uie.model = object()  # 骗过 "model is None" 判断

    cfg = ChunkConfig(enabled=True, overlap=32, safety_margin=16, threshold=0.5)
    entities, meta = uie.predict_with_chunks("长文本" * 100, ["X"], cfg)

    assert meta["chunking_enabled"] is False
    assert meta["chunk_count"] == 1
    assert meta["model_max_tokens"] is None
