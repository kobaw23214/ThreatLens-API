"""
集成测试: 真实 GLiNER / UIE 模型的长文本分块端到端。

标记为 slow(真实模型加载需要数秒到数十秒)。运行方式:
  pytest tests/test_integration_long_text.py -v

对应 Requirements: 2.4 / 2.5 / 4.4 / 7.5
"""
from __future__ import annotations

from pathlib import Path

import pytest

MODEL_DIR_MULTI = Path(__file__).parent.parent / "models" / "gliner_multi-v2.1"
MODEL_DIR_XLARGE = Path(__file__).parent.parent / "models" / "gliner-x-large"


pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def multi_model():
    if not MODEL_DIR_MULTI.exists():
        pytest.skip(f"本地模型不存在: {MODEL_DIR_MULTI}")
    from api.models import GLiNERMultiModel
    m = GLiNERMultiModel()
    if not m.load():
        pytest.skip("gliner_multi-v2.1 加载失败")
    return m


@pytest.fixture(scope="module")
def xlarge_model():
    if not MODEL_DIR_XLARGE.exists():
        pytest.skip(f"本地模型不存在: {MODEL_DIR_XLARGE}")
    from api.models import GLiNERModel
    m = GLiNERModel()
    if not m.load():
        pytest.skip("gliner-x-large 加载失败")
    return m


# ─── 用例 1: gliner_multi-v2.1 中文长文本 ────────────────────────────────

def test_multi_long_chinese_text_chunk_count_and_entities(multi_model):
    """
    2000+ 字中文长文本,gliner_multi-v2.1 应切成 ≥ 2 块,
    且最后一块(最大 chunk_index)应有实体,证明不再丢失尾部。

    _Requirements: 2.4, 4.4_
    """
    from api.chunking import ChunkConfig

    paragraph = (
        "乔治·华盛顿是美国第一任总统,他于1789年在纽约就任。"
        "他出生于弗吉尼亚州威克菲尔德,后来成为大陆军总司令,"
        "带领军队在独立战争中击败了英国。"
        "他在1797年卸任,回到芒特弗农的庄园过着退休生活。"
    )
    long_text = paragraph * 20  # 约 2680 字
    labels = ["人物", "地点", "时间"]
    cfg = ChunkConfig(enabled=True, overlap=32, safety_margin=16, threshold=0.5)
    entities, meta = multi_model.predict_with_chunks(long_text, labels, cfg)

    assert meta["chunking_enabled"] is True
    assert meta["chunk_count"] >= 2, f"长文本应 ≥ 2 块, 实际 {meta['chunk_count']}"
    assert meta["model_max_tokens"] == 384

    # 每个实体的 offset 回写正确
    for e in entities:
        assert long_text[e["start"]:e["end"]] == e["text"]
        assert e["chunk_index"] is not None

    # 最大 chunk_index(最后一块)必须有至少一个实体
    max_ci = meta["chunk_count"] - 1
    last_chunk_entities = [e for e in entities if e["chunk_index"] == max_ci]
    assert len(last_chunk_entities) > 0, \
        f"最后一块(chunk_index={max_ci})应该有实体, 但实际没有"


# ─── 用例 2: gliner-x-large 英文长文本 ───────────────────────────────────

def test_xlarge_long_english_text(xlarge_model):
    """
    5000+ 字英文长文本,gliner-x-large (max_tokens=1024) 应能处理。

    _Requirements: 2.4, 4.4_
    """
    from api.chunking import ChunkConfig

    paragraph = (
        "Barack Obama served as the 44th President of the United States, "
        "from January 2009 to January 2017. He was born in Honolulu, Hawaii, "
        "and graduated from Columbia University and Harvard Law School. "
        "Before his presidency he was a United States Senator from Illinois "
        "and a community organizer in Chicago. "
    )
    long_text = paragraph * 15  # 约 4500 字符(英文字符)
    labels = ["person", "place", "date"]
    cfg = ChunkConfig(enabled=True, overlap=32, safety_margin=16, threshold=0.5)
    entities, meta = xlarge_model.predict_with_chunks(long_text, labels, cfg)

    assert meta["chunking_enabled"] is True
    assert meta["model_max_tokens"] == 1024
    assert meta["effective_max_tokens"] > 900, \
        f"effective 应 > 900, 实际 {meta['effective_max_tokens']}"

    # offset 回写正确
    for e in entities:
        assert long_text[e["start"]:e["end"]] == e["text"]


# ─── 用例 3: UIE 降级路径 (没有 gliner tokenizer 也不会崩) ───────────────

def test_uie_predict_with_chunks_downgrade():
    """
    UIE 模型通过 predict_with_chunks 应走降级路径:
    整段不分块,chunking_enabled=False, chunk_count=1, model_max_tokens=None。

    _Requirements: 2.5_
    """
    from api.models import UIEModel
    from api.chunking import ChunkConfig

    # 不真正加载 UIE (避免 paddle 依赖),用 stub self.predict
    uie = UIEModel("micro")

    def fake_predict(text, labels):
        if not text:
            return []
        return [("alice", labels[0], 0, 5, 0.9)]
    uie.predict = fake_predict
    uie.model = object()

    text = "长文本" * 100  # 用字符数足够大的内容
    cfg = ChunkConfig(enabled=True, overlap=32, safety_margin=16, threshold=0.5)
    entities, meta = uie.predict_with_chunks(text, ["X"], cfg)

    assert meta["chunking_enabled"] is False
    assert meta["chunk_count"] == 1
    assert meta["model_max_tokens"] is None
    assert meta["labels_token_count"] is None
    # 实体也该加上 chunk_* 字段
    for e in entities:
        assert e["chunk_index"] == 0
        assert e["chunk_char_start"] == 0
        assert e["chunk_char_end"] == len(text)
