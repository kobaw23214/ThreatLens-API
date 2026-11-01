"""
Property-based tests: compute_task_hash 对分块参数敏感。

覆盖 Design Properties:
  P11 compute_task_hash 对 (text, sorted(labels), model_name, chunk_cfg 四参数) 都敏感

对应 Requirements: 9.3
"""
from __future__ import annotations

from hypothesis import given, settings, strategies as st

from api.chunking import ChunkConfig
from api.main import compute_task_hash


_TEXT_STRAT = st.text(min_size=1, max_size=300)
_LABELS_STRAT = st.lists(
    st.text(min_size=1, max_size=10),
    min_size=1, max_size=10, unique=True,
)
_MODEL_NAMES = ["gliner-x-large", "gliner_multi-v2.1", "UIE-base"]
_CFG_STRAT = st.builds(
    ChunkConfig,
    enabled=st.booleans(),
    overlap=st.integers(min_value=0, max_value=512),
    safety_margin=st.integers(min_value=0, max_value=64),
    threshold=st.floats(min_value=0.0, max_value=1.0, allow_nan=False,
                        allow_infinity=False).map(lambda x: round(x, 4)),
)


# ─── P11: 四参数敏感性 + labels 顺序无关 ────────────────────────────────

@given(text=_TEXT_STRAT, labels=_LABELS_STRAT,
       model_name=st.sampled_from(_MODEL_NAMES),
       cfg=_CFG_STRAT)
@settings(max_examples=100)
def test_hash_labels_order_invariant(text, labels, model_name, cfg):
    """
    P11 (a): labels 元素相同但顺序不同时,hash 必须相等。

    Feature: long-text-chunking-ner
    Property: P11 (labels 顺序无关)
    _Requirements: 9.3_
    """
    import random
    shuffled = labels[:]
    random.Random(hash(tuple(labels)) & 0xFFFF).shuffle(shuffled)
    h1 = compute_task_hash(text, labels, model_name, cfg)
    h2 = compute_task_hash(text, shuffled, model_name, cfg)
    assert h1 == h2


@given(text=_TEXT_STRAT, labels=_LABELS_STRAT,
       model_a=st.sampled_from(_MODEL_NAMES),
       cfg=_CFG_STRAT)
@settings(max_examples=100)
def test_hash_sensitive_to_model_name(text, labels, model_a, cfg):
    """
    P11 (b): model_name 不同 → hash 不同。

    Feature: long-text-chunking-ner
    Property: P11 (model_name 敏感)
    _Requirements: 9.3_
    """
    # 随便挑一个不同的 model
    others = [m for m in _MODEL_NAMES if m != model_a]
    if not others:
        return
    model_b = others[0]
    h1 = compute_task_hash(text, labels, model_a, cfg)
    h2 = compute_task_hash(text, labels, model_b, cfg)
    assert h1 != h2


@given(text=_TEXT_STRAT, labels=_LABELS_STRAT,
       model_name=st.sampled_from(_MODEL_NAMES),
       cfg_a=_CFG_STRAT, cfg_b=_CFG_STRAT)
@settings(max_examples=100)
def test_hash_sensitive_to_chunk_cfg(text, labels, model_name, cfg_a, cfg_b):
    """
    P11 (c): 只要 (enabled, overlap, safety_margin, threshold) 任一不同,hash 必须不同。
             全部相同时,hash 必须相等。

    Feature: long-text-chunking-ner
    Property: P11 (chunk_cfg 敏感)
    _Requirements: 9.3_
    """
    h1 = compute_task_hash(text, labels, model_name, cfg_a)
    h2 = compute_task_hash(text, labels, model_name, cfg_b)

    same = (
        cfg_a.enabled == cfg_b.enabled
        and cfg_a.overlap == cfg_b.overlap
        and cfg_a.safety_margin == cfg_b.safety_margin
        and cfg_a.threshold == cfg_b.threshold
    )
    if same:
        assert h1 == h2, "cfg 相同 hash 应该相等"
    else:
        assert h1 != h2, f"cfg 不同 hash 应该不同: {cfg_a} vs {cfg_b}"


@given(text=_TEXT_STRAT, labels=_LABELS_STRAT,
       model_name=st.sampled_from(_MODEL_NAMES),
       cfg=_CFG_STRAT)
@settings(max_examples=100)
def test_hash_sensitive_to_text(text, labels, model_name, cfg):
    """
    P11 (d): text 不同 → hash 不同。

    Feature: long-text-chunking-ner
    Property: P11 (text 敏感)
    _Requirements: 9.3_
    """
    h1 = compute_task_hash(text, labels, model_name, cfg)
    h2 = compute_task_hash(text + "x", labels, model_name, cfg)
    assert h1 != h2


def test_hash_legacy_two_args_preserved():
    """
    回归: 两参数 compute_task_hash(text, labels) 仍可用,与四参数结果不同,
    对 labels 顺序仍保持无关。

    Feature: long-text-chunking-ner
    Property: P11 (向后兼容)
    _Requirements: 9.3_
    """
    h_legacy_1 = compute_task_hash("x", ["A", "B"])
    h_legacy_2 = compute_task_hash("x", ["B", "A"])
    assert h_legacy_1 == h_legacy_2

    cfg = ChunkConfig(enabled=True, overlap=32, safety_margin=16, threshold=0.5)
    h_full = compute_task_hash("x", ["A", "B"], "gliner-x-large", cfg)
    assert h_legacy_1 != h_full
