# iocextract 模型手册

> 确定性网安实体提取引擎 · 基于 [InQuestLabs/python-iocextract](https://github.com/InQuest/python-iocextract) 1.16.1
> 模型名 `model_name`: **`iocextract`** · 类型 `type`: `ioc`

---

## 1. 概述

`iocextract` 是 InQuest Labs 出品的 **高级 IOC 提取器**，基于正则实现。它最大的特点是**对 defang（去武器化）写法的还原能力**——尤其是 `hxxp://`、`hXXps://` 这类**协议头被改写**的 URL，以及各种编码/混淆的指标。

在本 NER 服务中，它被封装成与 GLiNER / UIE 同构的「伪模型」，提交任务时指定 `model_name: "iocextract"` 即可使用，复用相同的任务队列、缓存、回调、`label_mapping` 等全部机制。

**核心特点**

- **零样本、零推理成本**：纯 CPU 正则匹配，毫秒级响应，不需要 GPU。
- **defang 还原是看家本领**：`refang=True` 能把 `hxxp://malware[.]test/x[.]exe` 还原为 `http://malware.test/x.exe`。
- **支持编码 URL / YARA 规则**：能提取 base64、十六进制等编码形式的 URL，以及 YARA 规则片段。

**类型覆盖较少**：只支持 IP、URL、邮箱、哈希四大类。需要 CVE、文件路径、注册表、ATT&CK 等更多类型时，请用 `ioc-finder` 或 `auto`。

**适用场景**：含有大量 defang 写法的威胁情报报告、社区共享的 IOC 列表、邮件钓鱼分析。

---

## 2. 如何使用：写哪些标签（labels）

本服务通过 `labels` 字段接收「你想提取的实体类型」。`iocextract` 模型会把你的每个标签**映射**到它支持的类型；**映射不到或它不支持的标签会被静默忽略**（不报错）。

> `iocextract` 支持的类型是 `ioc-finder` 的子集。如果你传了 `CVE`、`文件路径` 这类它不支持的标签，它不会产出，请改用 `ioc-finder` 或 `auto`。

### 2.1. 标签映射表

标签**大小写不敏感**，忽略空格、下划线、连字符。

| 推荐标签（任写其一） | 别名 | 实际提取的类型 | 说明 |
|---|---|---|---|
| `IP地址` | `IP` `IPv4` `ip address` | IPv4 地址 | 自动 refang `1[.]2[.]3[.]4` |
| `IPv6` | `IPv6地址` | IPv6 地址 | — |
| `URL` | `网址` `链接` | URL | 自动 refang `hxxp://...`、编码 URL |
| `邮箱` | `email` `邮件地址` `邮箱地址` | 邮件地址 | 自动 refang `a[at]b[.]com` |
| `MD5` | — | MD5 哈希 | 32 位十六进制 |
| `SHA1` | — | SHA1 哈希 | 40 位十六进制 |
| `SHA256` | — | SHA256 哈希 | 64 位十六进制 |
| `SHA512` | — | SHA512 哈希 | 128 位十六进制 |
| `哈希` | `hash` `哈希值` `文件哈希` `样本哈希` | MD5+SHA1+SHA256+SHA512 | 一次性提取全部哈希 |

> **不支持的标签**（传了会被忽略，请改用 `ioc-finder` / `auto`）：
> `CVE`、`域名`（独立域名，非 URL 内）、`文件路径`、`注册表`、`MAC`、`CIDR`、`ASN`、`比特币`、`门罗币`、`User-Agent`、`ssdeep`、`ATT&CK` 等。

---

## 3. 位置信息（start / end）

`iocextract` 库本身**不返回字符位置**，且 `refang=True` 时返回的是**还原后的规范值**（与原文 defang 写法不同）。本服务做了 **offset 回填**：

1. 先用还原后的规范值在原文精确查找；
2. 找不到（说明原文是 defang 写法）则用容忍 defang 的宽松匹配定位；
3. **每一次出现都生成独立实体**，带正确的 `start` / `end`。

返回实体字段与 `ioc-finder`、NER 模型完全一致：`text`（原文真实片段，保留 defang 原貌）、`label`（你的原始标签）、`start` / `end`（`text[start:end]` 必等于该片段）、`score`（恒为 `1.0`）。

---

## 4. 请求示例

```bash
curl -X POST "http://localhost:8036/extract" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ner_your_api_key" \
  -d '{
    "text": "下载 hxxp://malware[.]test/x[.]exe，回连 8[.]8[.]8[.]8，样本 d41d8cd98f00b204e9800998ecf8427e。",
    "labels": ["URL", "IP地址", "MD5"],
    "model_name": "iocextract"
  }'
```

提取结果：`text` 字段保留原文的 defang 写法（如 `hxxp://malware[.]test/x[.]exe`），但能被正确定位和归类。

---

## 5. 与 ioc-finder 的对比

| 维度 | ioc-finder | **iocextract** |
|---|---|---|
| 类型覆盖 | 29 类（最全） | **4 类（IP/URL/hash/email）** |
| `[.]` 类 defang | ✅ | ✅ |
| `hxxp://` 类 scheme defang | ⚠️ 可能漏抓 | ✅ **强项** |
| 编码 URL / YARA | ❌ | ✅ |
| 实现方式 | pyparsing 语法 | 正则 |

> **两者互补**：如需「类型全 + scheme defang 还原」兼得，请使用 **`auto`** 模型，它内部默认对 IOC 类标签同时跑 `ioc-finder` 和 `iocextract` 并取并集。

---

## 6. 局限与注意事项

- **类型少**：仅 IP / URL / 邮箱 / 哈希。其余类型请用 `ioc-finder` 或 `auto`。
- **不提取语义实体**：人物、组织、地点等需用 NER 模型或 `auto`。
- **不分块**：正则匹配整段处理，`chunk_metadata.chunking_enabled` 恒为 `false`。
- **许可证**：iocextract 采用 **BSD** 宽松许可，商用无负担。
