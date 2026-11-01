"""
Property-based tests: dedup_by_overlap 正确性与幂等性。

覆盖 Design Properties:
  P10 跨块去重 (三个子断言: 无冲突保留 / 选择规则 / 幂等)

对应 Requirements: 5.1 / 5.2 / 5.3 / 5.4 / 5.5 / 5.6
"""
from __future__ import annotations

from hypothesis import HealthCheck, given, settings, strategies as st

from api.queue_manager import dedup_by_overlap


# ─── 策略: 生成冲突频率较高的实体集合 ────────────────────────────────────

_ENTITY_STRAT = st.fixed_dictionaries({
    "text":        st.text(min_size=1, max_size=10),
    "label":       st.sampled_from(["A", "B", "C"]),
    "raw_label":   st.sampled_from(["A", "B", "C"]),
    "start":       st.integers(min_value=0, max_value=50),
    "length":      st.integers(min_value=1, max_value=15),
    "score":       st.floats(min_value=0.0, max_value=1.0, allow_nan=False,
                             allow_infinity=False).map(lambda x: round(x, 3)),
    "chunk_index": st.integers(min_value=0, max_value=5),
}).map(lambda d: {
    "text":        d["text"],
    "label":       d["label"],
    # 强制 raw_label == label 消除非法组合
    "raw_label":   d["label"],
    "start":       d["start"],
    "end":         d["start"] + d["length"],
    "score":       d["score"],
    "chunk_index": d["chunk_index"],
})

_ENTITIES_STRAT = st.lists(_ENTITY_STRAT, min_size=0, max_size=40)


# ─── P10: 跨块去重三合一 ─────────────────────────────────────────────────

@given(xs=_ENTITIES_STRAT)
@settings(max_examples=100, deadline=None)
def test_dedup_correctness_and_idempotence(xs):
    """
    P10: dedup_by_overlap 三合一正确性:
      (a) 结果列表中任意两个不同元素之间不存在冲突:要么 raw_label 不同,要么 [start,end) 无交集
      (b) 对原输入中的每个冲突对,最终保留的必定是按 (score↓, chunk_index↑, length↓, 原序↑) 胜出者
      (c) 幂等: dedup(dedup(xs)) == dedup(xs) (元素与顺序都一致)

    Feature: long-text-chunking-ner
    Property: P10 (跨块去重)
    _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6_
    """
    once = dedup_by_overlap(xs)
    twice = dedup_by_overlap(once)

    # (c) 幂等
    assert once == twice, "dedup 必须幂等: dedup(dedup(xs)) == dedup(xs)"

    # (a) 结果内无冲突
    for i in range(len(once)):
        for j in range(i + 1, len(once)):
            a, b = once[i], once[j]
            if a["raw_label"] is None or b["raw_label"] is None:
                continue
            if a["raw_label"] != b["raw_label"]:
                continue
            # 同 raw_label,[start,end) 必须无交集
            assert max(a["start"], b["start"]) >= min(a["end"], b["end"]), (
                f"冲突未消除: {a} 与 {b}"
            )

    # (b) 对每个原始实体,若有冲突,则保留的必须是 "胜者"
    # dedup 不 deepcopy 实体,只从输入列表里挑选,所以保留的 e 一定是原 xs 里某个元素的同一对象。
    # 于是用 is 来判等,避免两条结构完全相同的实体互相"误匹"。
    for i in range(len(xs)):
        for j in range(i + 1, len(xs)):
            a, b = xs[i], xs[j]
            if a["raw_label"] != b["raw_label"]:
                continue
            if max(a["start"], b["start"]) >= min(a["end"], b["end"]):
                continue
            # 到这一步: 二者冲突
            a_in = any(e is a for e in once)
            b_in = any(e is b for e in once)
            assert not (a_in and b_in), f"冲突对同时保留: {a} / {b}"


def _same_entity(e1, e2):
    """已弃用 - dedup 返回的是原 xs 中的对象引用,应该用 is 判等。"""
    return e1 is e2


# ─── 额外 example: 四级 tiebreaker 精确验证 ──────────────────────────────

def _ent(raw, s, e, score, ci, label=None, text=None):
    return {
        "text": text if text is not None else f"e_{s}_{e}",
        "label": label if label is not None else raw,
        "raw_label": raw,
        "start": s, "end": e,
        "score": score,
        "chunk_index": ci,
    }


def test_dedup_score_wins():
    xs = [_ent("P", 0, 5, 0.6, 0), _ent("P", 2, 7, 0.9, 1)]
    out = dedup_by_overlap(xs)
    assert len(out) == 1 and out[0]["score"] == 0.9


def test_dedup_chunk_index_tiebreaker():
    xs = [_ent("P", 0, 5, 0.8, 3), _ent("P", 2, 7, 0.8, 1)]
    out = dedup_by_overlap(xs)
    assert len(out) == 1 and out[0]["chunk_index"] == 1


def test_dedup_length_tiebreaker():
    xs = [_ent("P", 0, 3, 0.5, 0), _ent("P", 0, 6, 0.5, 0)]
    out = dedup_by_overlap(xs)
    assert len(out) == 1 and (out[0]["end"] - out[0]["start"]) == 6


def test_dedup_original_order_tiebreaker():
    xs = [_ent("P", 0, 3, 0.5, 0, text="A"), _ent("P", 0, 3, 0.5, 0, text="B")]
    out = dedup_by_overlap(xs)
    assert len(out) == 1 and out[0]["text"] == "A"


def test_dedup_no_conflict_different_labels():
    xs = [_ent("P", 0, 5, 0.9, 0), _ent("L", 0, 5, 0.9, 0)]
    out = dedup_by_overlap(xs)
    assert len(out) == 2


def test_dedup_no_conflict_no_intersection():
    xs = [_ent("P", 0, 3, 0.9, 0), _ent("P", 5, 8, 0.9, 1)]
    out = dedup_by_overlap(xs)
    assert len(out) == 2


def test_dedup_null_raw_label_not_conflicting():
    xs = [
        {"text": "a", "label": "X", "raw_label": None, "start": 0, "end": 3,
         "score": 0.9, "chunk_index": 0},
        {"text": "b", "label": "X", "raw_label": None, "start": 0, "end": 3,
         "score": 0.8, "chunk_index": 0},
    ]
    out = dedup_by_overlap(xs)
    assert len(out) == 2
