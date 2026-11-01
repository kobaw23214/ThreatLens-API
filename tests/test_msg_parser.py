# -*- coding: utf-8 -*-
"""
MSG 解析模块测试 (api/eml_parser.py 的 parse_msg / parse_mail)

样本来源: datasets/msg/(由 datasets/msg/generate_samples.py 生成,
覆盖钓鱼邮件 / 带附件 / 中文正文 / 阴性样本)。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from api.eml_parser import DEFAULT_EML_LABELS, flatten_eml_text, parse_mail, parse_msg
from api.ioc_extractor import extract_ioc_entities, split_labels

SAMPLES_DIR = Path(__file__).parent.parent / "datasets" / "msg"
SAMPLE_FILES = sorted(SAMPLES_DIR.glob("*.msg"))


def _sample(name: str) -> bytes:
    return (SAMPLES_DIR / name).read_bytes()


# ─── 样本回归:全部样本都必须能解析 ─────────────────────────────────────────

@pytest.mark.parametrize("path", SAMPLE_FILES, ids=[p.name for p in SAMPLE_FILES])
def test_parse_all_msg_samples(path):
    msg = parse_msg(path.read_bytes(), filename=path.name)
    assert msg["format"] == "msg"
    assert msg["filename"] == path.name
    assert len(msg["msg_sha256"]) == 64
    assert msg["subject"] or msg["body"]
    # 展平文本可复用 EML 的展平函数
    assert flatten_eml_text(msg)


# ─── 入库安全:邮件 JSON 必须可 JSON 序列化且不含 NUL ─────────────────────────

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
def test_msg_json_serializable_and_nul_free(path):
    import json
    msg = parse_msg(path.read_bytes(), filename=path.name)
    json.dumps(msg)
    for s in _iter_strings(msg):
        assert "\x00" not in s
    assert "\x00" not in flatten_eml_text(msg)


# ─── 字段正确性 ────────────────────────────────────────────────────────────

def test_parse_msg_sender_and_recipient():
    msg = parse_msg(_sample("sample_phishing.msg"), filename="sample_phishing.msg")
    assert msg["subject"] == "Urgent: Account Verification Required"
    assert ["IT Desk", "itdesk@evil-example.com"] in msg["from"]
    assert ["Zhang Wei", "zhangwei@target-example.com"] in msg["to"]
    assert msg["to_domains"] == ["target-example.com"]


def test_parse_msg_attachment_payload_stripped():
    msg = parse_msg(_sample("sample_invoice_attachment.msg"), filename="sample_invoice_attachment.msg")
    atts = msg["attachments"]
    assert [a["filename"] for a in atts] == ["invoice_INV-2024-0712.pdf.exe"]
    for a in atts:
        assert "payload" not in a
        assert a["size"] > 0
        assert a["sha256"] and len(a["sha256"]) == 64


def test_parse_msg_invalid_bytes_raises():
    with pytest.raises(ValueError):
        parse_msg(b"", filename="x.msg")
    with pytest.raises(ValueError):
        parse_msg(b"\x00\x01\x02\x03" * 64, filename="x.msg")


def test_parse_mail_dispatches_by_ext():
    raw = _sample("sample_phishing.msg")
    assert parse_mail(raw, ext=".msg")["format"] == "msg"
    eml_raw = (SAMPLES_DIR.parent / "eml" / "github_issue_48.eml").read_bytes()
    assert parse_mail(eml_raw, ext=".eml")["format"] == "eml"


# ─── 展平文本 ──────────────────────────────────────────────────────────────

def test_msg_flatten_contains_key_sections():
    msg = parse_msg(_sample("sample_chinese.msg"))
    text = flatten_eml_text(msg)
    assert "Subject: 【安全通告】CVE-2024-3400 紧急排查" in text
    assert "From: 安全团队 <secops@corp-example.cn>" in text
    assert "CVE-2024-3400" in text


def test_msg_flatten_attachment_filenames():
    msg = parse_msg(_sample("sample_invoice_attachment.msg"))
    text = flatten_eml_text(msg)
    assert "Attachments: invoice_INV-2024-0712.pdf.exe" in text


# ─── 端到端(模块级):展平文本 → 默认标签 → IOC 实体 ─────────────────────────

def test_msg_flattened_text_yields_iocs():
    msg = parse_msg(_sample("sample_phishing.msg"))
    text = flatten_eml_text(msg)
    ioc_label_map, ner_labels = split_labels(DEFAULT_EML_LABELS)
    entities = extract_ioc_entities(text, ioc_label_map, "ioc_finder")
    values = {e["text"] for e in entities}

    # 发件人邮箱 / 正文 URL 与域名 / IP 都应被抽出
    assert "itdesk@evil-example.com" in values
    assert "45.155.30.10" in values
    assert any("evil-example.com" in v for v in values)
    for e in entities:
        assert text[e["start"]:e["end"]] == e["text"]
