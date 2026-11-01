# -*- coding: utf-8 -*-
"""
EML 解析模块测试 (api/eml_parser.py)

样本来源: datasets/eml/(GOVCERT-LU/eml_parser 仓库 samples 目录,
覆盖 MIME 多部分 / 附件 / 内嵌 HTML / 畸形头部 / 无 scheme URL 等边界)。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from api.eml_parser import DEFAULT_EML_LABELS, flatten_eml_text, parse_eml
from api.ioc_extractor import extract_ioc_entities, split_labels

SAMPLES_DIR = Path(__file__).parent.parent / "datasets" / "eml"
SAMPLE_FILES = sorted(SAMPLES_DIR.glob("*.eml"))

EXPECTED_KEYS = {
    "filename", "eml_sha256", "subject", "from", "to", "cc", "reply_to",
    "date", "timezone", "message_id", "headers", "received", "to_domains",
    "body", "text_plain", "text_html", "attachments", "has_defects", "defects",
}


def _sample(name: str) -> bytes:
    return (SAMPLES_DIR / name).read_bytes()


# ─── 样本回归:全部样本都必须能解析(覆盖 mail-parser 版本回归风险) ──────────

@pytest.mark.parametrize("path", SAMPLE_FILES, ids=[p.name for p in SAMPLE_FILES])
def test_parse_all_samples(path):
    eml = parse_eml(path.read_bytes(), filename=path.name)
    assert EXPECTED_KEYS <= set(eml.keys())
    assert eml["filename"] == path.name
    assert len(eml["eml_sha256"]) == 64
    # 每个样本都至少有主题或正文
    assert eml["subject"] or eml["body"]


# ─── 入库安全:邮件 JSON 必须可 JSON 序列化且不含 NUL ─────────────────────────
# (内嵌二进制邮件的 body/text_html 会混入 NUL,PostgreSQL text/jsonb 均不允许,
#  曾在 /extract/eml 提交时引发 insert_task ValueError → 500)

def _iter_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_strings(v)
    elif isinstance(obj, (list, tuple)):
        for x in obj:
            yield from _iter_strings(x)


@pytest.mark.parametrize("path", SAMPLE_FILES, ids=[p.name for p in SAMPLE_FILES])
def test_eml_json_serializable_and_nul_free(path):
    import json
    eml = parse_eml(path.read_bytes(), filename=path.name)
    # 必须能被 json.dumps(否则 insert_task 写 JSONB 时会 500)
    json.dumps(eml)
    # 所有字符串字段不得含 NUL
    for s in _iter_strings(eml):
        assert "\x00" not in s
    # 展平文本同样不得含 NUL
    assert "\x00" not in flatten_eml_text(eml)


# ─── 字段正确性 ────────────────────────────────────────────────────────────

def test_parse_headers_and_received():
    eml = parse_eml(_sample("github_issue_48.eml"), filename="github_issue_48.eml")
    assert eml["subject"] == "[Limited Time Offer] - Free Welcome Package"
    assert eml["from"] == [("Giovanna", "email1@example.com")]
    # 完整 Received 链路 4 跳,含发信源 IP
    assert len(eml["received"]) == 4
    hop1 = eml["received"][0]
    assert "192.168.0.4" in hop1.get("from", "")
    assert eml["has_defects"] is False


def test_parse_attachments_payload_stripped():
    eml = parse_eml(_sample("sample_attachments.eml"), filename="sample_attachments.eml")
    atts = eml["attachments"]
    assert [a["filename"] for a in atts] == ["text.txt", "document.pdf", "test.csv"]
    for a in atts:
        assert "payload" not in a           # payload 必须剥离
        assert a["size"] > 0
        assert a["sha256"] and len(a["sha256"]) == 64
        assert a["mail_content_type"]


def test_parse_invalid_bytes_raises():
    with pytest.raises(ValueError):
        parse_eml(b"", filename="x.eml")
    with pytest.raises(ValueError):
        parse_eml(b"\x00\x01\x02\x03" * 64, filename="x.eml")


# ─── 展平文本 ──────────────────────────────────────────────────────────────

def test_flatten_contains_key_sections():
    eml = parse_eml(_sample("github_issue_48.eml"))
    text = flatten_eml_text(eml)
    # 解码字段
    assert "Subject: [Limited Time Offer]" in text
    assert "From: Giovanna <email1@example.com>" in text
    # Received 逐跳(headers dict 折叠后仍保留完整链路)
    assert text.count("Received-Hop:") == 4
    assert "192.168.0.4" in text
    # 正文中的 URL
    assert "http://example2.com/spl.html" in text


def test_flatten_attachment_filenames():
    eml = parse_eml(_sample("sample_attachments.eml"))
    text = flatten_eml_text(eml)
    assert "Attachments: text.txt, document.pdf, test.csv" in text


# ─── 端到端(模块级):展平文本 → 默认标签 → IOC 实体 ─────────────────────────

def test_flattened_text_yields_iocs():
    eml = parse_eml(_sample("github_issue_48.eml"))
    text = flatten_eml_text(eml)
    ioc_label_map, ner_labels = split_labels(DEFAULT_EML_LABELS)
    assert not ner_labels, f"默认标签应全部映射到 IOC 类型: {ner_labels}"

    entities = extract_ioc_entities(text, ioc_label_map, "ioc_finder")
    values = {e["text"] for e in entities}
    labels = {e["label"] for e in entities}

    # 头部邮箱 / 发信源 IP / 正文 URL 与域名 都应被抽出
    assert "email1@example.com" in values
    assert "192.168.0.4" in values
    assert any("example2.com/spl.html" in v for v in values)
    assert {"邮箱", "IP", "URL"} <= labels
    # offset 必须指回原文真实片段
    for e in entities:
        assert text[e["start"]:e["end"]] == e["text"]
