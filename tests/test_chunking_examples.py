"""
Example-based 测试: ChunkingService 的中英混排分句与字符切分退化路径。

对应 Requirements: 1.11 / 10.1 / 10.2 / 10.3 / 10.5
"""
from __future__ import annotations

import pytest

from api.chunking import ChunkingService, ChunkingCapacityError


def _make_service(tokenizer, model_max_tokens=384, safety_margin=16, overlap=32):
    return ChunkingService(
        tokenizer=tokenizer,
        model_max_tokens=model_max_tokens,
        safety_margin=safety_margin,
        overlap=overlap,
    )


# ─── 用例 1: 中英混排 + 英文缩写保护 ─────────────────────────────────────

def test_mixed_punctuation_and_abbreviation(multi_tokenizer):
    """
    中英混合文本中,`Dr.` 作为英文缩写不应成为句边界。
    预期把 3 个真正的句子切出来,而不是 4 个。

    _Requirements: 10.1, 10.2_
    """
    service = _make_service(multi_tokenizer)
    text = "马云是阿里的创始人。He founded it in 1999. Dr. Smith confirmed."
    sents = service._sentence_split(text)
    assert len(sents) == 3, f"预期 3 句,实际 {len(sents)}: {sents}"

    reconstructed = "".join(s for _, _, s in sents)
    assert reconstructed == text, "分句结果字符应完整覆盖原文"


# ─── 用例 2: 英文版本号保护 ──────────────────────────────────────────────

def test_version_number_not_split(multi_tokenizer):
    """
    `1.2.3` 这种版本号中的点号不应当作句边界。

    _Requirements: 10.3_
    """
    service = _make_service(multi_tokenizer)
    text = "Version 1.2.3 is released."
    sents = service._sentence_split(text)
    assert len(sents) == 1, f"预期 1 句,实际 {len(sents)}: {sents}"


def test_multiple_abbreviations(multi_tokenizer):
    """
    更密集的缩写场景:`e.g.` / `i.e.` / `U.S.` 都不应成为边界。

    _Requirements: 10.2_
    """
    service = _make_service(multi_tokenizer)
    text = "Install python, e.g. Python 3.11, i.e. the latest version, in the U.S."
    sents = service._sentence_split(text)
    assert len(sents) == 1, f"预期 1 句(末尾句号),实际 {len(sents)}: {sents}"


# ─── 用例 3: ChunkingCapacityError ───────────────────────────────────────

def test_capacity_error_when_labels_fill_model(multi_tokenizer):
    """
    labels 占满 model_max_tokens 时,split 应抛 ChunkingCapacityError。

    _Requirements: 1.11, 2.6_
    """
    service = _make_service(multi_tokenizer, model_max_tokens=384, safety_margin=16)
    # 构造累计 token 数 >= 368 (= 384 - 16) 的 labels
    labels = [f"long_label_{i}" * 20 for i in range(80)]
    with pytest.raises(ChunkingCapacityError) as exc_info:
        service.split("短文本测试", labels)
    assert exc_info.value.effective_max_tokens <= 0


# ─── 用例 4: 无断句符 → 字符切分退化路径 ─────────────────────────────────

def test_no_punctuation_falls_back_to_char_split(multi_tokenizer):
    """
    文本完全由连续字符组成时, _sentence_split 返回空列表,
    上层 split 进入字符切分退化路径。每个产出的 Chunk token_count 必须 <= effective。

    _Requirements: 10.5_
    """
    service = _make_service(multi_tokenizer, safety_margin=16, overlap=32)
    # 纯汉字无标点,长度足够触发字符切分
    text = "一二三四五六七八九十" * 200  # 2000 字
    labels = ["人物", "地点"]

    # 先确认分句返回空(即触发字符切分)
    assert service._sentence_split(text) == []

    effective, _ = service.compute_effective_max_tokens(labels)
    chunks, meta = service.split(text, labels)
    assert meta.chunk_count >= 2, "长文本无标点也应被切分为多块"
    for c in chunks:
        recount = len(multi_tokenizer.encode(c.text, add_special_tokens=False))
        assert recount <= effective, f"chunk {c.chunk_index} 超限: {recount} > {effective}"
        # 切片等式
        assert text[c.char_start:c.char_end] == c.text


# ─── 用例 5: 连续标点合并 (Requirement 10.6 衍生) ────────────────────────

def test_consecutive_punctuation_merged(multi_tokenizer):
    """
    `!!!` / `。。。` / `?!` / `。\\n` 等连续标点序列应合并为单一边界,
    不产生空 Chunk / 空句子。
    """
    service = _make_service(multi_tokenizer)
    text = "他说什么?!!我不知道。。。然后呢"
    sents = service._sentence_split(text)
    # 不应有空文本
    for _, _, s in sents:
        assert s.strip() != "" or s  # 允许含空白但不应是空串
    # 重建等于原文
    reconstructed = "".join(s for _, _, s in sents)
    assert reconstructed == text


# ─── 用例 6: 空文本 ──────────────────────────────────────────────────────

def test_empty_text(multi_tokenizer):
    """
    空文本应返回 ([], chunk_count=0, chunking_enabled=False),不报错。

    _Requirements: 1.9 (衍生)_
    """
    service = _make_service(multi_tokenizer)
    chunks, meta = service.split("", ["人物"])
    assert chunks == []
    assert meta.chunk_count == 0
    assert meta.chunking_enabled is False
