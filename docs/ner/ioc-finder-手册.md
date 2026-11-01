# ioc-finder 模型手册

> 确定性网安实体提取引擎 · 基于 [fhightower/ioc-finder](https://github.com/fhightower/ioc-finder) 7.0.0
> 模型名 `model_name`: **`ioc-finder`** · 类型 `type`: `ioc`

---

## 1. 概述

`ioc-finder` 是一个基于 **pyparsing 语法（grammar）** 而非纯正则的 IOC（失陷指标，Indicator of Compromise）解析库。它从文本中提取 IP、域名、URL、哈希、CVE、文件路径等网络安全可观测对象（observables）。

在本 NER 服务中，它被封装成一个与 GLiNER / UIE 同构的「伪模型」，提交任务时指定 `model_name: "ioc-finder"` 即可使用，复用相同的任务队列、缓存、回调、`label_mapping` 等全部机制。

**核心特点**

- **零样本、零推理成本**：纯 CPU 正则/语法匹配，毫秒级响应，不需要 GPU，不占用模型显存。
- **类型最全**：支持 29 类指标，覆盖网络型、文件型、主机型、情报型 IOC。
- **自带 defang 还原**：内置 `ioc-fanger`，能自动识别并还原 `evil[.]com`、`1[.]2[.]3[.]4`、`example(dot)com` 这类「去武器化」写法。
- **结果去重**：同一值在文档中多次出现，库本身只返回一次；本服务在回填位置时会**还原每一次出现的位置**（见下文「位置信息」）。

**适用场景**：威胁情报报告、应急响应记录、安全告警、日志等含有大量结构化 IOC 的文本。

**不适用场景**：人物、组织、地点、恶意软件家族名等**语义型实体**——这些请用 GLiNER / UIE，或使用 `auto` 模型自动分流。

---

## 2. 如何使用：写哪些标签（labels）

本服务通过 `labels` 字段接收「你想提取的实体类型」。`ioc-finder` 模型会把你的每个标签**映射**到它内部支持的 IOC 类型；**映射不到的标签会被静默忽略**（不会报错，只是不产出该类实体）。

> 例如你传 `labels: ["IP地址", "域名", "人物"]`，`ioc-finder` 会提取 IP 和域名，`人物` 因无法映射到 IOC 类型而被忽略。

### 2.1. 标签映射表（推荐写法 → 实际提取的类型）

标签**大小写不敏感**，且会忽略空格、下划线、连字符。例如 `IP地址`、`ip地址`、`IP_地址` 等价。

| 推荐标签（任写其一） | 别名 | 实际提取的 IOC 类型 | 说明 |
|---|---|---|---|
| `IP地址` | `IP` `IPv4` `ip address` | IPv4 地址 | 形如 `192.168.1.1` |
| `IPv6` | `IPv6地址` | IPv6 地址 | 形如 `2001:db8::1` |
| `域名` | `domain` `主机名` `hostname` | 域名 | 形如 `evil.example.com`；URL/邮箱中的域名也会被拆出 |
| `URL` | `网址` `链接` | URL | 形如 `https://x.com/a` |
| `邮箱` | `email` `邮件地址` `邮箱地址` | 邮件地址 | 形如 `admin@victim.org` |
| `MD5` | — | MD5 哈希 | 32 位十六进制 |
| `SHA1` | — | SHA1 哈希 | 40 位十六进制 |
| `SHA256` | — | SHA256 哈希 | 64 位十六进制 |
| `SHA512` | — | SHA512 哈希 | 128 位十六进制 |
| `哈希` | `hash` `哈希值` `文件哈希` `样本哈希` | MD5+SHA1+SHA256+SHA512 | 一次性提取全部哈希类型 |
| `CVE` | `漏洞编号` `CVE编号` `漏洞` | CVE 编号 | 形如 `CVE-2021-44228` |
| `文件路径` | `路径` `file path` | 文件路径 | Windows/Unix 路径，形如 `C:\temp\evil.exe` |
| `注册表` | `注册表键` `registry` | 注册表键路径 | 形如 `HKLM\Software\Bad` |
| `MAC` | `MAC地址` `物理地址` | MAC 地址 | 形如 `00:1B:44:11:3A:B7` |
| `CIDR` | `网段` `IP段` | IPv4/IPv6 CIDR | 形如 `10.0.0.0/8` |
| `ASN` | `自治系统` | 自治系统号 | 形如 `ASN123` / `AS123` |
| `比特币` | `bitcoin` `BTC` `比特币地址` | 比特币地址 | — |
| `门罗币` | `monero` `XMR` | 门罗币地址 | — |
| `User-Agent` | `UA` `浏览器标识` | User-Agent 字符串 | 形如 `Mozilla/5.0 (...)` |
| `ssdeep` | `模糊哈希` | ssdeep 模糊哈希 | — |
| `imphash` | — | PE 导入哈希 | — |
| `authentihash` | — | PE Authenticode 哈希 | — |
| `XMPP` | `jabber` | XMPP/Jabber 地址 | — |
| `TLP` | `TLP标记` | TLP 标记 | 形如 `TLP:RED` |
| `adsense` | `googleadsense` | Google AdSense 发布者 ID | — |
| `analytics` | `googleanalytics` | Google Analytics 跟踪 ID | — |
| `战术` | `attacktactic` | MITRE ATT&CK 战术 | 形如 `TA0001` |
| `技术` | `attacktechnique` `TTP` | MITRE ATT&CK 技术 | 形如 `T1059` |
| `缓解措施` | `attackmitigation` | MITRE ATT&CK 缓解措施 | 形如 `M1049` |
| `ATT&CK` | `attack` | 战术+技术+缓解措施 | 一次性提取三类 |

> **建议**：网安文档最常用的标签是 `IP地址`、`域名`、`URL`、`邮箱`、`哈希`、`CVE`、`文件路径`。其余按需添加。

---

## 3. 位置信息（start / end）

`ioc-finder` 库本身**只返回去重后的值列表**，不提供字符位置。本服务做了 **offset 回填**：

1. 拿到规范值后，先在原文精确查找；
2. 若文中是 **defang 写法**（如 `evil[.]com`），用容忍 defang 的宽松匹配重新定位；
3. **同一个值的每一次出现都会生成一个独立实体**，各自带正确的 `start` / `end`。

因此返回结果与 NER 模型完全同构，`text[start:end]` 必定等于实体在原文中的真实片段（保留 defang 原貌）。这一点对后续做「同一 IP 在不同位置扮演不同角色」的分析至关重要。

返回实体字段：

| 字段 | 说明 |
|---|---|
| `text` | 原文真实片段（若被 defang 则保留 `evil[.]com` 原貌） |
| `label` | 你传入的**原始标签**（如 `IP地址`），不是内部类型名 |
| `start` / `end` | 在原文中的字符偏移，`text[start:end] == text` |
| `score` | 恒为 `1.0`（确定性匹配，无置信度概念） |

---

## 4. 请求示例

```bash
curl -X POST "http://localhost:8036/extract" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ner_your_api_key" \
  -d '{
    "text": "受害主机 192.168.1.100 被 8.8.8.8 入侵，C2 域名 evil[.]example[.]com，样本 MD5 d41d8cd98f00b204e9800998ecf8427e，漏洞 CVE-2021-44228。",
    "labels": ["IP地址", "域名", "MD5", "CVE"],
    "model_name": "ioc-finder"
  }'
```

提取结果（查询 `/tasks/{task_id}/result`）：

| text | label | start | end |
|---|---|---|---|
| `192.168.1.100` | IP地址 | 5 | 18 |
| `8.8.8.8` | IP地址 | 21 | 28 |
| `evil[.]example[.]com` | 域名 | 38 | 58 |
| `d41d8cd98f00b204e9800998ecf8427e` | MD5 | 67 | 99 |
| `CVE-2021-44228` | CVE | 104 | 118 |

---

## 5. 与 iocextract 的对比

| 维度 | **ioc-finder** | iocextract |
|---|---|---|
| 类型覆盖 | **29 类（最全）** | 4 类（IP/URL/hash/email） |
| `[.]` 类 defang | ✅ 自动还原 | ✅ |
| `hxxp://` 类 scheme defang | ⚠️ 可能漏抓 | ✅ **强项** |
| ATT&CK / CVE / 注册表 / 文件路径 | ✅ | ❌ |
| 实现方式 | pyparsing 语法 | 正则 |

> **两者互补**：`ioc-finder` 类型全但对 `hxxp://` 这类协议头 defang 较弱；`iocextract` 类型少但能还原协议头 defang。如需两者优势叠加，请使用 **`auto`** 模型（内部默认 `ioc-finder ∪ iocextract`）。

---

## 6. 局限与注意事项

- **不提取语义实体**：人物、组织、地点、恶意软件家族名等需用 NER 模型或 `auto`。
- **域名会从 URL / 邮箱中拆出**：传 `域名` 时，`https://x.com/a` 会额外产出 `x.com`，`a@b.com` 会额外产出 `b.com`。如不需要可只传 `URL` / `邮箱`。
- **User-Agent 边界**：UA 字符串形态多样，复杂 UA 的尾部边界可能不完全精确。
- **不分块**：IOC 提取是正则匹配，无 token 上限，整段处理，`chunk_metadata.chunking_enabled` 恒为 `false`。
- **许可证**：ioc-finder 采用 **LGPL-3.0**，作为独立服务调用通常无碍；若修改其源码并分发产品需留意开源义务。
