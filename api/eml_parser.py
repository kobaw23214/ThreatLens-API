# -*- coding: utf-8 -*-
"""
邮件解析模块(EML / MSG)

.eml 调用 mail-parser (SpamScope) 解析,.msg (Outlook OLE2) 调用 extract-msg
解析;两者输出同一结构的邮件 JSON,并把关键字段展平为纯文本,
供现有 IOC 提取管线复用。

业务链路:
  POST /extract/eml 上传 .eml / .msg
    → parse_eml() / parse_msg()  生成邮件 JSON(附件 payload 剥离,保留 sha256/size)
    → flatten_eml_text()         展平为文本(解码字段 + 全量头部 + Received 逐跳 + 正文 + 附件名)
    → 复用 /extract 异步队列做 IOC 提取(默认 ioc-finder 模型)
    → 任务结果的 information_json.eml 中可回读完整邮件 JSON
"""
from __future__ import annotations

import base64
import binascii
import email.utils
import hashlib
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# EML 任务的默认 IOC 标签集(与 auto_routing.json / 内置别名表对齐)
DEFAULT_EML_LABELS: List[str] = [
    "邮箱", "域名", "URL", "IP", "IPv6",
    "MD5", "SHA1", "SHA256", "SHA512",
    "CVE", "比特币地址", "MAC地址", "文件路径", "CIDR", "ASN",
]

# EML 任务的默认提取模型(确定性 IOC 引擎,类型覆盖最全)
DEFAULT_EML_MODEL = "ioc-finder"


def _fmt_addrs(pairs: Any) -> str:
    """mail-parser 的地址字段是 [[显示名, 地址], ...],格式化为可读串。"""
    out: List[str] = []
    for item in pairs or []:
        try:
            disp, addr = item[0], item[1]
        except (TypeError, IndexError):
            continue
        out.append(f"{disp} <{addr}>".strip() if disp else str(addr))
    return ", ".join(out)


def _attachment_meta(att: Dict[str, Any]) -> Dict[str, Any]:
    """
    把 mail-parser 的附件 dict 转成元数据(剥离 base64 payload)。
    payload 按 base64 解码后计算 size / sha256;解码失败时按原始文本兜底。
    """
    payload_b64 = (att.get("payload") or "").strip()
    raw = b""
    if payload_b64:
        try:
            raw = base64.b64decode(payload_b64, validate=True)
        except (binascii.Error, ValueError):
            # 非 base64(如纯文本附件直接内联)→ 按原文字节计算
            raw = payload_b64.encode("utf-8", "replace")
    return {
        "filename": att.get("filename") or "",
        "mail_content_type": att.get("mail_content_type") or "",
        "content_transfer_encoding": att.get("content_transfer_encoding") or "",
        "content_disposition": att.get("content-disposition") or "",
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest() if raw else None,
    }


def _strip_nul(obj: Any) -> Any:
    """
    递归移除字符串中的 NUL(0x00) 字符。

    内嵌二进制附件的邮件(inline binary 且无文件名)会被 mail-parser 解码进
    body/text_html,其中的 NUL 字节会导致 PostgreSQL 写入失败
    (text/jsonb 均不允许 NUL,psycopg2 抛 ValueError)。
    """
    if isinstance(obj, str):
        return obj.replace("\x00", "") if "\x00" in obj else obj
    if isinstance(obj, dict):
        return {k: _strip_nul(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_nul(x) for x in obj]
    if isinstance(obj, tuple):
        return tuple(_strip_nul(x) for x in obj)
    return obj


def parse_eml(raw: bytes, filename: str = "") -> Dict[str, Any]:
    """
    解析 EML 字节流,返回结构化邮件 JSON。失败抛 ValueError。

    返回字段:
      filename / eml_sha256               来源文件信息
      subject / from / to / cc / reply_to / date / timezone / message_id
      headers                             全量头部(dict)
      received                            Received 逐跳(含 from/by/with/date_utc)
      to_domains                          收件方域名列表
      body / text_plain / text_html       正文
      attachments                         [{filename, content_type, size, sha256, ...}] (payload 已剥离)
      has_defects / defects               RFC 合规缺陷(可作为可疑度信号)
    """
    if not raw or not raw.strip():
        raise ValueError("文件为空")

    try:
        import mailparser
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的兜底提示
        raise ValueError(
            "服务端未安装 EML 解析库 mail-parser,无法处理 .eml。"
            "请在部署环境执行 pip install mail-parser"
        ) from exc

    try:
        m = mailparser.parse_from_bytes(raw)
    except Exception as exc:  # noqa: BLE001 - mail-parser 异常体系较杂,统一收敛
        raise ValueError(f"EML 解析失败,文件可能已损坏或不是合法的 EML: {exc}") from exc

    # 基本 sanity:既没有任何头部也没有正文 → 大概率不是 EML
    if not m.headers and not (m.body or "").strip():
        raise ValueError("未能从文件中解析出邮件内容,请确认是合法的 EML 文件")

    # 入库前统一剥离 NUL 字符(内嵌二进制会混入 body/text_html)
    return _strip_nul({
        "format": "eml",
        "filename": filename,
        "eml_sha256": hashlib.sha256(raw).hexdigest(),
        "subject": m.subject or "",
        "from": m.from_ or [],
        "to": m.to or [],
        "cc": m.cc or [],
        "reply_to": m.reply_to or [],
        "date": m.date.isoformat() if m.date else None,
        "timezone": m.timezone,
        "message_id": m.message_id or "",
        "headers": dict(m.headers),
        "received": m.received or [],
        "to_domains": m.to_domains or [],
        "body": m.body or "",
        "text_plain": list(m.text_plain or []),
        "text_html": list(m.text_html or []),
        "attachments": [_attachment_meta(a) for a in m.attachments],
        "has_defects": bool(m.defects),
        "defects": [str(d) for d in m.defects],
    })


def _addr_pairs(text: str) -> List[List[str]]:
    """
    把 'Alice <a@x.com>; Bob <b@y.com>' 解析成 mail-parser 风格的 [[显示名, 地址], ...]。
    纯显示名(无 <> 邮箱,常见于 Outlook 本地 MSG 的发件人)整体作为地址保留。
    """
    text = (text or "").replace("\x00", "").strip()
    if not text:
        return []
    # 纯显示名(无 <> / @,常见于 Outlook 本地 MSG 的发件人)整体保留,
    # 避免 getaddresses 把 "Wang Fang" 按空白拆开
    if "<" not in text and "@" not in text:
        return [["", text]]
    pairs = [[disp, addr] for disp, addr in email.utils.getaddresses([text]) if addr]
    if not pairs:
        return [["", text]]
    return pairs


def parse_msg(raw: bytes, filename: str = "") -> Dict[str, Any]:
    """
    解析 MSG (Outlook OLE2) 字节流,返回与 parse_eml() 同结构的邮件 JSON。
    失败抛 ValueError。

    与 EML 的差异:MSG 由 Outlook 生成,通常没有 RFC822 传输头部;若内嵌了
    传输头部(PR_TRANSPORT_MESSAGE_HEADERS)则照常提取 headers/received。
    """
    if not raw or not raw.strip():
        raise ValueError("文件为空")

    try:
        import extract_msg
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的兜底提示
        raise ValueError(
            "服务端未安装 MSG 解析库 extract-msg,无法处理 .msg。"
            "请在部署环境执行 pip install extract-msg"
        ) from exc

    try:
        m = extract_msg.openMsg(raw)
    except Exception as exc:  # noqa: BLE001 - extract-msg 异常体系较杂,统一收敛
        raise ValueError(f"MSG 解析失败,文件可能已损坏或不是合法的 MSG: {exc}") from exc

    try:
        if not (m.subject or "").strip() and not (m.body or "").strip() and not m.sender:
            raise ValueError("未能从文件中解析出邮件内容,请确认是合法的 MSG 文件")

        # 传输头部(可选,Outlook 本地保存的 MSG 往往没有;只有真实存在
        # PR_TRANSPORT_MESSAGE_HEADERS 流时才提取,避免 extract-msg 合成的
        # 头部与上面的解码字段重复)
        headers: Dict[str, Any] = {}
        received: List[Dict[str, Any]] = []
        header_text = m.headerText
        if header_text:
            if header_text.startswith("Microsoft Mail Internet Headers Version 2.0"):
                header_text = header_text[43:].lstrip()
            from email.parser import HeaderParser
            from email import policy
            hdr = HeaderParser(policy=policy.compat32).parsestr(header_text)
            headers = {k: str(v) for k, v in hdr.items()}
            received = [{"raw": str(h)} for h in hdr.get_all("Received") or []]

        html_body = m.htmlBody
        if isinstance(html_body, bytes):
            html_body = html_body.decode("utf-8", "replace")
        body = m.body or ""

        to_pairs = _addr_pairs(m.to or "")
        cc_pairs = _addr_pairs(m.cc or "")
        to_domains = sorted({a.split("@", 1)[1].lower() for _, a in to_pairs + cc_pairs if "@" in a})

        attachments: List[Dict[str, Any]] = []
        for att in m.attachments:
            name = att.longFilename or att.shortFilename or ""
            data = att.data
            payload = data if isinstance(data, bytes) else b""  # 内嵌 MSG 等非常规附件不取 payload
            attachments.append({
                "filename": name,
                "mail_content_type": getattr(att, "mimetype", None) or "",
                "content_transfer_encoding": "",
                "content_disposition": "",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest() if payload else None,
            })

        return _strip_nul({
            "format": "msg",
            "filename": filename,
            "msg_sha256": hashlib.sha256(raw).hexdigest(),
            "subject": m.subject or "",
            "from": _addr_pairs(m.sender or ""),
            "to": to_pairs,
            "cc": cc_pairs,
            "reply_to": [],
            "date": m.date.isoformat() if m.date else None,
            "timezone": None,
            "message_id": m.messageId or "",
            "headers": headers,
            "received": received,
            "to_domains": to_domains,
            "body": body,
            "text_plain": [body] if body else [],
            "text_html": [html_body] if html_body else [],
            "attachments": attachments,
            "has_defects": False,
            "defects": [],
        })
    finally:
        m.close()


def parse_mail(raw: bytes, filename: str = "", ext: str = "") -> Dict[str, Any]:
    """按扩展名分派到 parse_eml() / parse_msg()。ext 需为小写带点扩展名。"""
    if ext == ".msg":
        return parse_msg(raw, filename=filename)
    return parse_eml(raw, filename=filename)


def flatten_eml_text(eml: Dict[str, Any]) -> str:
    """
    把 parse_eml() 的邮件 JSON 展平为纯文本,作为 IOC 提取的输入。

    布局(每段都可能含 IOC,全部保留原文):
      1. 解码后的关键字段(Subject/From/To/... 已处理 encoded-word)
      2. 全量原始头部(Received / Authentication-Results / Return-Path / X-* 等)
      3. Received 逐跳汇总(headers dict 会折叠重复 Received 头,这里保留完整链路)
      4. 正文(text_plain + text_html 原文;HTML 中的 URL 一并保留)
      5. 附件文件名列表(双扩展名等可疑文件名也是 IOC 信号)
    """
    lines: List[str] = []

    # ── 1. 解码后的关键字段 ─────────────────────────────────────────────
    lines.append(f"Subject: {eml.get('subject', '')}")
    if eml.get("from"):
        lines.append(f"From: {_fmt_addrs(eml['from'])}")
    if eml.get("to"):
        lines.append(f"To: {_fmt_addrs(eml['to'])}")
    if eml.get("cc"):
        lines.append(f"Cc: {_fmt_addrs(eml['cc'])}")
    if eml.get("reply_to"):
        lines.append(f"Reply-To: {_fmt_addrs(eml['reply_to'])}")
    if eml.get("date"):
        lines.append(f"Date: {eml['date']}")
    if eml.get("message_id"):
        lines.append(f"Message-ID: {eml['message_id']}")

    # ── 2. 全量原始头部 ─────────────────────────────────────────────────
    for key, value in (eml.get("headers") or {}).items():
        lines.append(f"{key}: {value}")

    # ── 3. Received 逐跳汇总 ────────────────────────────────────────────
    for hop in eml.get("received") or []:
        if not isinstance(hop, dict):
            continue
        parts = [f"{k}={v}" for k, v in hop.items() if v not in (None, "", [])]
        if parts:
            lines.append("Received-Hop: " + " ".join(parts))

    # ── 4. 正文 ─────────────────────────────────────────────────────────
    body = eml.get("body") or ""
    lines.append("")
    lines.append(body)

    # ── 5. 附件文件名 ───────────────────────────────────────────────────
    names = [a.get("filename") or "" for a in eml.get("attachments") or []]
    names = [n for n in names if n]
    if names:
        lines.append("")
        lines.append("Attachments: " + ", ".join(names))

    return "\n".join(lines)
