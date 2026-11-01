# -*- coding: utf-8 -*-
"""
生成 datasets/msg/ 下的 .msg 测试样本。

环境里没有 Outlook,直接用 extract-msg 自带的 OleWriter 手工构造
最小合法 MSG(OLE2) 文件:顶层字符串属性流 + recipient 存储区 + 附件存储区,
可被 extract_msg.openMsg / Outlook 正常打开。

用法: .venv/bin/python datasets/msg/generate_samples.py
"""
from __future__ import annotations

import struct
from pathlib import Path

from extract_msg.ole_writer import OleWriter

OUT_DIR = Path(__file__).parent

PT_UNICODE = 0x001F
PT_LONG = 0x0003
PT_BINARY = 0x0102
PROP_FLAGS = 0x02 | 0x04  # READABLE | WRITEABLE


def _props_stream(header: bytes, entries: list) -> bytes:
    """entries: [(ptype, pid, value)] value 为内联整数或流字节数(字符串/二进制)。"""
    body = b""
    for ptype, pid, val in entries:
        body += struct.pack("<HHI", ptype, pid, PROP_FLAGS)
        if ptype == PT_LONG:
            body += struct.pack("<Q", val)
        else:
            body += struct.pack("<4sI", b"\x00" * 4, val)
    return header + body


def _add_str(writer, entries, prefix, pid, text):
    data = text.encode("utf-16-le") + b"\x00\x00"
    writer.addEntry(f"{prefix}__substg1.0_{pid:04X}{PT_UNICODE:04X}", data=data)
    entries.append((PT_UNICODE, pid, len(data)))


def build_msg(path: Path, props: dict, recipients: list, attachments: list):
    """props: {pid: str};recipients: [{name, email}];attachments: [{filename, data(bytes), mime}]."""
    writer = OleWriter()
    entries = []
    props = dict(props)
    if 0x0C1F in props and 0x5D01 not in props:
        props[0x5D01] = props[0x0C1F]  # extract-msg 的 sender 属性从 0x5D01 取发件人邮箱
    for pid, text in props.items():
        _add_str(writer, entries, "", pid, text)

    for idx, r in enumerate(recipients):
        rdir = f"__recip_version1.0_#{idx:08X}"
        rentries = []
        for pid, text in {0x3001: r["name"], 0x39FE: r["email"], 0x3003: r["email"], 0x3002: "SMTP"}.items():
            _add_str(writer, rentries, rdir + "/", pid, text)
        rentries.append((PT_LONG, 0x0C15, 1))  # PR_RECIPIENT_TYPE = To
        writer.addEntry(rdir + "/__properties_version1.0",
                        data=_props_stream(struct.pack("<8s", b"\x00" * 8), rentries))

    for idx, a in enumerate(attachments):
        adir = f"__attach_version1.0_#{idx:08X}"
        aentries = []
        _add_str(writer, aentries, adir + "/", 0x3707, a["filename"])          # PR_ATTACH_LONG_FILENAME
        if a.get("mime"):
            _add_str(writer, aentries, adir + "/", 0x370E, a["mime"])          # PR_ATTACH_MIME_TAG
        writer.addEntry(f"{adir}/__substg1.0_3701{PT_BINARY:04X}", data=a["data"])  # PR_ATTACH_DATA_BIN
        aentries.append((PT_BINARY, 0x3701, len(a["data"])))
        aentries.append((PT_LONG, 0x0E20, len(a["data"])))                     # PR_ATTACH_SIZE
        aentries.append((PT_LONG, 0x3705, 1))                                  # PR_ATTACH_METHOD = by value
        writer.addEntry(adir + "/__properties_version1.0",
                        data=_props_stream(struct.pack("<8s", b"\x00" * 8), aentries))

    # 附件初始化会访问 named properties;没有 named 属性也要给出空的三个流
    writer.addEntry("__nameid_version1.0/__substg1.0_00020102", data=b"")  # guid stream
    writer.addEntry("__nameid_version1.0/__substg1.0_00030102", data=b"")  # entry stream
    writer.addEntry("__nameid_version1.0/__substg1.0_00040102", data=b"")  # string stream

    header = struct.pack("<8sIIII8s", b"\x00" * 8, len(recipients), len(attachments),
                         len(recipients), len(attachments), b"\x00" * 8)
    writer.addEntry("__properties_version1.0", data=_props_stream(header, entries))
    writer.write(str(path))


def _base(body, extra):
    return {0x001A: "IPM.Note", 0x1000: body, **extra}


SAMPLES = {
    # 钓鱼邮件:URL + IP + 邮箱,IOC 丰富
    "sample_phishing.msg": dict(
        props=_base(
            "Dear user,\n\nYour account has been locked. Verify immediately at "
            "http://secure-login.evil-example.com/verify or contact support@evil-example.com.\n"
            "Our server: 45.155.30.10\n\nRegards,\nIT Desk",
            
            {0x0037: "Urgent: Account Verification Required",
               0x0C1A: "IT Desk", 0x0C1F: "itdesk@evil-example.com",
               0x0E04: "Zhang Wei"},
        ),
        recipients=[{"name": "Zhang Wei", "email": "zhangwei@target-example.com"}],
        attachments=[],
    ),
    # 带附件的发票邮件:附件名(双扩展名)也是 IOC 信号
    "sample_invoice_attachment.msg": dict(
        props=_base(
            "Hi,\n\nPlease find the invoice attached. Transfer to account at "
            "https://pay.fake-bank-example.net/invoice INV-2024-0712.\nSHA256 of original: "
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n\nThanks",
            
            {0x0037: "Invoice INV-2024-0712 overdue",
               0x0C1A: "Billing Dept", 0x0C1F: "billing@fake-bank-example.net",
               0x0E04: "Li Na"},
        ),
        recipients=[{"name": "Li Na", "email": "lina@target-example.com"}],
        attachments=[{
            "filename": "invoice_INV-2024-0712.pdf.exe",
            "data": b"MZ\x90\x00 fake executable payload for testing \x00\x01\x02",
            "mime": "application/octet-stream",
        }],
    ),
    # 中文正文 + CVE + 哈希
    "sample_chinese.msg": dict(
        props=_base(
            "各位同事:\n\n近期监测到针对 CVE-2024-3400 的在野利用,请立即排查。"
            "恶意样本 MD5: 44d88612fea8a8f36de82e1278abb02f,"
            "C2 域名: c2.malicious-example.xyz,回连 IP 10.20.30.40。\n\n安全团队",
            
            {0x0037: "【安全通告】CVE-2024-3400 紧急排查",
               0x0C1A: "安全团队", 0x0C1F: "secops@corp-example.cn",
               0x0E04: "全体员工"},
        ),
        recipients=[{"name": "全体员工", "email": "all@corp-example.cn"}],
        attachments=[],
    ),
    # 普通商务邮件:无 IOC,作为阴性样本
    "sample_benign.msg": dict(
        props=_base(
            "Hi team,\n\nThe quarterly review meeting is moved to Friday 3pm. "
            "Agenda: Q2 results, hiring plan, and budget.\n\nBest,\nWang Fang",
            
            {0x0037: "Quarterly review rescheduled",
               0x0C1A: "Wang Fang", 0x0C1F: "wangfang@corp-example.cn",
               0x0E04: "Team"},
        ),
        recipients=[{"name": "Team", "email": "team@corp-example.cn"}],
        attachments=[],
    ),
}


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, spec in SAMPLES.items():
        path = OUT_DIR / name
        build_msg(path, spec["props"], spec["recipients"], spec["attachments"])
        print(f"written: {path} ({path.stat().st_size} bytes)")

    # 自检:生成的文件必须能被 extract-msg 重新解析
    import extract_msg
    for name in SAMPLES:
        m = extract_msg.openMsg(str(OUT_DIR / name))
        assert m.subject, f"{name}: subject 为空"
        print(f"verified: {name} | subject={m.subject!r} to={m.to!r} attachments={len(m.attachments)}")
        m.close()


if __name__ == "__main__":
    main()
