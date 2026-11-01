"""
Property-based tests: ChunkingService 核心不变量。

覆盖 Design Properties:
  P1  labels_token_count / effective_max_tokens 的公式一致性
  P2  每个 Chunk 的 token_count <= effective_max_tokens
  P4  Chunk 列表三合一完整性 (chunk_index 单调 / text 切片等式 / 字符区间覆盖)
  P6  labels 过长 → ChunkingCapacityError
  P12 _sentence_split 确定性

对应 Requirements: 1.2 / 1.3 / 1.4 / 1.6 / 1.8 / 1.11 / 2.1 / 2.2 / 2.3 / 2.6 / 10.4 / 10.5
"""
from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from api.chunking import ChunkingService, ChunkingCapacityError

from tests.conftest import mixed_text_strategy, labels_strategy


# ─── P1: labels_token_count 公式一致性 ────────────────────────────────────

@given(labels=labels_strategy(min_size=1, max_size=10, max_label_len=15))
@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_labels_token_count_formula(multi_tokenizer, multi_model_max_tokens, labels):
    """
    P1: compute_labels_token_count(labels) 必须等于对拼接后的
        <<ENT>>label1<<SEP>><<ENT>>label2<<SEP>>...<<ENT>>labelN<<SEP>> 字符串
        用 tokenizer.encode(add_special_tokens=False) 的长度。

    Feature: long-text-chunking-ner
    Property: P1 (labels_token_count 公式一致性)
    _Requirements: 1.2, 2.1, 2.2_
    """
    service = ChunkingService(
        tokenizer=multi_tokenizer,
        model_max_tokens=multi_model_max_tokens,
        safety_margin=16,
        overlap=32,
    )
    expected_prompt = "".join(f"<<ENT>>{lb}<<SEP>>" for lb in labels)
    expected_tokens = len(
        multi_tokenizer.encode(expected_prompt, add_special_tokens=False)
    )
    assert service.compute_labels_token_count(labels) == expected_tokens

    effective, labels_tok = service.compute_effective_max_tokens(labels)
    assert labels_tok == expected_tokens
    assert effective == multi_model_max_tokens - expected_tokens - 16


# ─── P2: 每个 Chunk token_count <= effective_max_tokens ──────────────────

@given(
    text=mixed_text_strategy(min_size=0, max_size=1500),
    labels=labels_strategy(min_size=1, max_size=5, max_label_len=10),
)
@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_all_chunks_fit_in_effective(multi_tokenizer, multi_model_max_tokens, text, labels):
    """
    P2: split 返回的每个 Chunk 用真实 tokenizer 重新 encode 后的长度
        必须 <= effective_max_tokens。

    Feature: long-text-chunking-ner
    Property: P2 (Chunk token 上限严格遵守)
    _Requirements: 1.3, 1.4, 1.6, 2.3, 10.5_
    """
    service = ChunkingService(
        tokenizer=multi_tokenizer,
        model_max_tokens=multi_model_max_tokens,
        safety_margin=16,
        overlap=32,
    )
    effective, _ = service.compute_effective_max_tokens(labels)
    if effective < 50:
        # 小于 50 时走降级路径(不分块), 不在本属性覆盖范围
        return

    chunks, metadata = service.split(text, labels)
    for ch in chunks:
        recount = len(multi_tokenizer.encode(ch.text, add_special_tokens=False))
        assert recount <= effective, (
            f"chunk {ch.chunk_index} token_count={recount} > effective={effective}, "
            f"text_len={len(text)}, labels={labels}"
        )


# ─── P4: Chunk 三合一完整性 ───────────────────────────────────────────────

@given(
    text=mixed_text_strategy(min_size=1, max_size=1500),
    labels=labels_strategy(min_size=1, max_size=5, max_label_len=10),
)
@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_chunk_integrity_three_in_one(multi_tokenizer, multi_model_max_tokens, text, labels):
    """
    P4: split 返回的 chunks 同时满足:
      (a) chunk_index 从 0 起单调递增 1
      (b) 对每个 chunk: text[char_start:char_end] == chunk.text
      (c) chunks 的字符区间并集覆盖 [0, len(text))

    Feature: long-text-chunking-ner
    Property: P4 (Chunk 列表完整性)
    _Requirements: 1.8_
    """
    service = ChunkingService(
        tokenizer=multi_tokenizer,
        model_max_tokens=multi_model_max_tokens,
        safety_margin=16,
        overlap=32,
    )
    effective, _ = service.compute_effective_max_tokens(labels)
    if effective <= 0:
        return  # 会抛 ChunkingCapacityError,属于 P6 范畴
    chunks, _ = service.split(text, labels)
    if not chunks:
        assert text == ""
        return

    # (a) chunk_index 单调递增
    indices = [c.chunk_index for c in chunks]
    assert indices == list(range(len(chunks)))

    # (b) text 切片等式
    for c in chunks:
        assert text[c.char_start:c.char_end] == c.text

    # (c) 字符区间并集覆盖 [0, len(text))
    covered = sorted([(c.char_start, c.char_end) for c in chunks])
    # 并集 = 逐段取并,检查是否覆盖 [0, len(text))
    merged = [list(covered[0])]
    for s, e in covered[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    assert merged[0][0] == 0, f"覆盖起点应为 0, 实际 {merged[0][0]}"
    assert merged[-1][1] == len(text), f"覆盖终点应为 {len(text)}, 实际 {merged[-1][1]}"
    assert len(merged) == 1, f"并集不连续: {merged}"


# ─── P6: labels 过长 → ChunkingCapacityError ──────────────────────────────

@given(
    n_labels=st.integers(min_value=40, max_value=60),
    label_char=st.sampled_from("abcdefghij"),
    text=mixed_text_strategy(min_size=1, max_size=200),
)
@settings(max_examples=50, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_oversized_labels_raise_capacity_error(
    multi_tokenizer, multi_model_max_tokens, n_labels, label_char, text
):
    """
    P6: 构造 labels 总 token 数 >= model_max_tokens - safety_margin
        (即 effective_max_tokens <= 0) 时,split 必须抛 ChunkingCapacityError。

    Feature: long-text-chunking-ner
    Property: P6 (labels 爆容错误)
    _Requirements: 1.11, 2.6_
    """
    # 构造若干个足够长的 label, 让 labels_token_count 爆到 model_max_tokens 以上
    long_label = label_char * 30
    labels = [long_label + str(i) for i in range(n_labels)]

    service = ChunkingService(
        tokenizer=multi_tokenizer,
        model_max_tokens=multi_model_max_tokens,
        safety_margin=16,
        overlap=32,
    )
    effective, _ = service.compute_effective_max_tokens(labels)
    if effective > 0:
        return  # 随机构造可能恰好未爆,跳过此次迭代

    with pytest.raises(ChunkingCapacityError):
        service.split(text, labels)


# ─── P12: _sentence_split 确定性 ──────────────────────────────────────────

@given(text=mixed_text_strategy(min_size=0, max_size=800))
@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_sentence_split_deterministic(multi_tokenizer, multi_model_max_tokens, text):
    """
    P12: 同一文本多次调 _sentence_split 返回完全相等的结果 (数量、起止位置、文本全部一致)。

    Feature: long-text-chunking-ner
    Property: P12 (分句确定性)
    _Requirements: 10.4_
    """
    service = ChunkingService(
        tokenizer=multi_tokenizer,
        model_max_tokens=multi_model_max_tokens,
        safety_margin=16,
        overlap=32,
    )
    a = service._sentence_split(text)
    b = service._sentence_split(text)
    assert a == b
