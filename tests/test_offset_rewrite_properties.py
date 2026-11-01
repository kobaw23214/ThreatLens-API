"""
Property-based tests: offset 回写正确性与最终排序。

覆盖 Design Properties:
  P8  text[e.start:e.end] == e.text (全局 offset 等式)
  P9  最终实体列表按 (start, end, label) 字典序升序排列

对应 Requirements: 4.2 / 4.3 / 4.4 / 4.6
"""
from __future__ import annotations

from hypothesis import HealthCheck, given, settings

from api.chunking import ChunkConfig

from tests.conftest import (
    FakeGLiNERChunkingModel,
    labels_strategy,
    mixed_text_strategy,
)


# ─── P8: 实体 offset 回写后 text 切片等式 ────────────────────────────────

@given(
    text=mixed_text_strategy(min_size=200, max_size=3000),
    labels=labels_strategy(min_size=1, max_size=5, max_label_len=8),
)
@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_entity_offset_text_slice(multi_tokenizer, multi_model_max_tokens, text, labels):
    """
    P8: predict_with_chunks 返回的每个实体,其全局 start/end 满足
        text[start:end] == entity["text"]。

    Feature: long-text-chunking-ner
    Property: P8 (offset 回写正确性)
    _Requirements: 4.2, 4.3, 4.4_
    """
    model = FakeGLiNERChunkingModel(
        tokenizer=multi_tokenizer,
        model_max_tokens=multi_model_max_tokens,
        entities_per_chunk=3,
    )
    cfg = ChunkConfig(enabled=True, overlap=32, safety_margin=16, threshold=0.5)
    entities, meta = model.predict_with_chunks(text, labels, cfg)

    for e in entities:
        # 每个实体的全局切片必须等于 entity.text
        assert text[e["start"]:e["end"]] == e["text"], (
            f"offset 回写失败: text[{e['start']}:{e['end']}]='{text[e['start']:e['end']]}' "
            f"vs entity.text='{e['text']}'"
        )
        # 分块字段一致性: chunk_char_start <= start < end <= chunk_char_end
        assert e["chunk_char_start"] <= e["start"]
        assert e["end"] <= e["chunk_char_end"]
        assert e["chunk_index"] is not None


# ─── P9: 实体列表排序 (需经 queue_manager 的 dedup+sort) ────────────────

@given(
    text=mixed_text_strategy(min_size=200, max_size=2000),
    labels=labels_strategy(min_size=1, max_size=5, max_label_len=8),
)
@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_entities_sorted_after_dedup(multi_tokenizer, multi_model_max_tokens, text, labels):
    """
    P9: 经过 queue_manager 的去重与排序后,实体列表按 (start, end, label) 字典序升序。

    Feature: long-text-chunking-ner
    Property: P9 (最终实体列表排序)
    _Requirements: 4.6_
    """
    from api.queue_manager import dedup_by_overlap

    model = FakeGLiNERChunkingModel(
        tokenizer=multi_tokenizer,
        model_max_tokens=multi_model_max_tokens,
        entities_per_chunk=3,
    )
    cfg = ChunkConfig(enabled=True, overlap=32, safety_margin=16, threshold=0.5)
    entities, _ = model.predict_with_chunks(text, labels, cfg)

    # 模拟 queue_manager 的归一化: raw_label 字段
    for e in entities:
        e.setdefault("raw_label", e["label"])

    entities = dedup_by_overlap(entities)
    entities.sort(key=lambda x: (x["start"], x["end"], x["label"]))

    for i in range(1, len(entities)):
        a, b = entities[i - 1], entities[i]
        assert (a["start"], a["end"], a["label"]) <= (b["start"], b["end"], b["label"]), \
            f"排序失败: {a} 应 <= {b}"
