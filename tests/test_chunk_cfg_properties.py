"""
Property-based tests: build_chunk_cfg 的合并优先级。

覆盖 Design Properties:
  P7 请求字段显式提供时用请求值,否则用环境变量默认值

对应 Requirements: 3.1 / 3.3
"""
from __future__ import annotations

import os

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from api.main import build_chunk_cfg
from api.schemas import ExtractRequest


# 使用 pytest fixture 的 monkeypatch 不能直接用在 @given 里,所以手动保存/恢复 env

_CHUNK_ENV_KEYS = (
    "CHUNK_ENABLED",
    "CHUNK_OVERLAP",
    "CHUNK_SAFETY_MARGIN",
    "CHUNK_THRESHOLD",
)


@pytest.fixture
def saved_env():
    saved = {k: os.environ.get(k) for k in _CHUNK_ENV_KEYS}
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ─── P7: 合并优先级 (请求字段非 None 覆盖 env 默认值) ────────────────────

@given(
    env_enabled=st.booleans(),
    env_overlap=st.integers(min_value=0, max_value=512),
    env_margin=st.integers(min_value=0, max_value=64),
    env_threshold=st.floats(min_value=0.0, max_value=1.0, allow_nan=False,
                            allow_infinity=False).map(lambda x: round(x, 4)),
    req_enabled=st.one_of(st.none(), st.booleans()),
    req_overlap=st.one_of(st.none(), st.integers(min_value=0, max_value=512)),
    req_margin=st.one_of(st.none(), st.integers(min_value=0, max_value=64)),
    req_threshold=st.one_of(st.none(), st.floats(min_value=0.0, max_value=1.0,
                                                  allow_nan=False,
                                                  allow_infinity=False).map(lambda x: round(x, 4))),
)
@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_build_chunk_cfg_priority(saved_env, env_enabled, env_overlap, env_margin, env_threshold,
                                   req_enabled, req_overlap, req_margin, req_threshold):
    """
    P7: build_chunk_cfg 的每个字段 f 满足
        cfg.f == req.f   if req.f is not None
        cfg.f == env.f   otherwise

    Feature: long-text-chunking-ner
    Property: P7 (chunk_cfg 合并优先级)
    _Requirements: 3.1, 3.3_
    """
    os.environ["CHUNK_ENABLED"] = "true" if env_enabled else "false"
    os.environ["CHUNK_OVERLAP"] = str(env_overlap)
    os.environ["CHUNK_SAFETY_MARGIN"] = str(env_margin)
    os.environ["CHUNK_THRESHOLD"] = str(env_threshold)

    req = ExtractRequest(
        text="dummy",
        labels=["X"],
        chunk_enabled=req_enabled,
        chunk_overlap=req_overlap,
        chunk_safety_margin=req_margin,
        threshold=req_threshold,
    )
    cfg = build_chunk_cfg(req)

    assert cfg.enabled == (req_enabled if req_enabled is not None else env_enabled)
    assert cfg.overlap == (req_overlap if req_overlap is not None else env_overlap)
    assert cfg.safety_margin == (req_margin if req_margin is not None else env_margin)
    assert cfg.threshold == (req_threshold if req_threshold is not None else env_threshold)


def test_build_chunk_cfg_fallback_defaults_when_env_missing(saved_env):
    """
    P7 额外: env 也不存在时,build_chunk_cfg 必须回落到代码硬编码默认值
    (true / 32 / 16 / 0.5)。

    Feature: long-text-chunking-ner
    Property: P7 (无 env 时使用代码默认值)
    _Requirements: 3.1_
    """
    for k in _CHUNK_ENV_KEYS:
        os.environ.pop(k, None)

    req = ExtractRequest(text="x", labels=["X"])
    cfg = build_chunk_cfg(req)
    assert cfg.enabled is True
    assert cfg.overlap == 32
    assert cfg.safety_margin == 16
    assert cfg.threshold == 0.5
