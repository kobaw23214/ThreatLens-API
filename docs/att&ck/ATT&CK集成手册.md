# ATT&CK 提取集成手册

> 面向友商 / 第三方系统的 MITRE ATT&CK 技战术提取集成指南
> 适用「ATT&CK 调用台」：`tram`、`qwen3-attack-retriever` 与 `attack-llm`

---

## 1. 这是什么

本服务在原有 NER / IOC 抽取能力之外，提供 **ATT&CK 技战术提取**：输入中文或英文威胁情报文本，输出行为对应的 **MITRE ATT&CK 技术编号**（如 `T1059.003`），并富化技术名称、所属战术和官方 URL。

与 NER 抽取显式实体不同，ATT&CK 模型从行为叙述中推断或检索 TTP。集成完全复用现有 `/extract` 任务体系：**同一套 API、任务队列、缓存、回调和结果查询**，只需切换 `model_name`。

---

## 2. 三种引擎怎么选

| 维度 | `tram` | `qwen3-attack-retriever` | `attack-llm` |
|---|---|---|---|
| 原理 | SciBERT 句子级分类 | Qwen3-Embedding-4B + MLX LoRA 向量检索 | 大模型语义映射 |
| 覆盖技术数 | 固定 50 类 | ATT&CK v19.1 固定 50 类 | 全部 600+ 技术 |
| 语言 | 英文最佳，中文较弱 | **中文优先**，兼容英文 | 中英文皆可 |
| 运行位置 | 本地 CPU/GPU | 本地 Apple Silicon | 外部或本地 LLM API |
| 输出性质 | 阈值以上分类结果 | Top-K 相似候选 | 技术判断 + 证据 |
| 主要限制 | 中文较弱 | 无可靠 NONE、无精确证据、非正式发布模型 | 延迟、成本和一定波动 |

**选型建议**：英文句子分类用 `tram`；中文报告需要本地 Top-K 候选用 `qwen3-attack-retriever`；需要完整标签覆盖与证据片段用 `attack-llm`。三者输出结构一致，可交叉验证。

模型细节见 [`tram-手册.md`](tram-手册.md)、[`qwen3-attack-retriever-手册.md`](qwen3-attack-retriever-手册.md) 与 [`attack-llm-手册.md`](attack-llm-手册.md)。

---

## 3. 快速开始

### 3.1 提交提取任务

```http
POST /extract
Content-Type: application/json
Authorization: Bearer <API_KEY>
```

```json
{
  "text": "攻击者远程投放 procdump.exe 并转储 LSASS 进程内存，随后使用 Mimikatz 抓取凭据。",
  "labels": ["full"],
  "model_name": "attack-llm",
  "reuse_cache": true
}
```

- **`labels`**：ATT&CK 提取里 `labels` 是**过滤器**，不是要抽取的实体类型。
  - 默认走**全量模式**：填 `["full"]`（推荐哨兵词，也可写 `全量`/`ATT&CK`/`TTP` 等任意语义词），返回文本中识别到的**所有**技术。
  - 需要**只关心特定范围**时，填技术编号或战术名做过滤（见 [第 5 节](#5-labels-全量与过滤模式)）。
  - 注意 `labels` 不能为空数组（schema 要求至少 1 项），所以全量模式也要传 `["full"]` 占位。
- **`model_name`**：`tram`、`qwen3-attack-retriever` 或已配置的 ATT&CK LLM 名称（同一 LLM 同时配在两个调用台时可能带 `-attack` 后缀，以 `/models` 返回为准）。
- 其余字段（`reuse_cache` / `callback` / `information_json` 等）与 NER 完全一致。

**响应**（Stage 1，仅返回任务号）：

```json
{ "code": 200, "status": "成功", "task_id": "a1b2c3...", "cached": false }
```

### 3.2 查询结果

```http
GET /tasks/{task_id}/result
Authorization: Bearer <API_KEY>
```

`entities` 数组里每个元素即一个命中的技术：

```json
{
  "text": "转储 LSASS 进程内存",
  "label": "T1003.001",
  "start": 14,
  "end": 24,
  "score": 0.91,
  "technique_id": "T1003.001",
  "technique_name": "LSASS Memory",
  "tactics": ["credential-access"],
  "technique_url": "https://attack.mitre.org/techniques/T1003/001",
  "is_subtechnique": true,
  "parent_technique": "T1003"
}
```

---

## 4. 输出字段说明

ATT&CK 类模型在标准实体字段基础上，附加了一组 ATT&CK 富化字段（NER / IOC 模型这些字段为 `null`）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `text` | string | 支撑判断的证据片段；Retriever 当前返回完整输入 |
| `label` | string | 技术编号，等同 `technique_id`（如 `T1059.003`） |
| `start` / `end` | int | 证据在原文中的字符区间；Retriever 使用完整输入区间 |
| `score` | float | `tram` 为 sigmoid 概率；Retriever 为余弦相似度；LLM 为模型自评置信度 |
| `technique_id` | string | ATT&CK 技术编号 |
| `technique_name` | string | 技术名称（来自 `techniques.json`） |
| `tactics` | string[] | 所属战术 shortname，如 `["execution"]` |
| `technique_url` | string | 官方技术页面 URL |
| `is_subtechnique` | bool | 是否为子技术（带 `.xxx`） |
| `parent_technique` | string | 父技术编号（子技术才有，如 `T1059.003` → `T1059`） |

> 富化字段来源于本地打包的 `models/attack/techniques.json`（ATT&CK 官方 STIX 蒸馏），编号一律经它校验：不存在或已废弃的编号会被丢弃，杜绝大模型幻觉编号流出。

---

## 5. labels 全量与过滤模式

`tram`、`qwen3-attack-retriever` 与 ATT&CK LLM **共用同一套 labels 过滤语义**（由 `attack_common.parse_filters` 实现）：

| labels 内容 | 模式 | 效果 |
|---|---|---|
| `["full"]` / `["全量"]` / `["ATT&CK"]` / `["TTP"]` / 任意语义词 | **全量** | 返回全部命中技术（默认） |
| `["T1059"]`（父编号） | 过滤 | 只返回 `T1059` 及其**全部子技术**（T1059.001…） |
| `["T1059.003"]`（具体子技术） | 过滤 | 只返回该子技术 |
| `["execution"]`（战术 shortname） | 过滤 | 只返回属于该战术的技术 |
| `["T1566", "credential-access"]`（混合） | 过滤 | 编号 + 战术并集 |

规则要点：
- 技术编号匹配 `^T\d{4}(\.\d{3})?$`，**大小写不敏感**；父编号自动匹配其所有子技术。
- 战术名匹配忽略大小写 / 空格 / 连字符（`credential-access` = `Credential Access`）。
- 无法识别为编号或战术的词（含 `full` 等哨兵）一律忽略 → 等价全量，**不会报错**。
- 只要出现**任一具体编号或战术**，即进入过滤模式；否则全量。

---

## 6. 集成注意事项

1. **异步任务模型**：`/extract` 返回 `task_id`，结果需轮询 `/tasks/{id}/result` 或配置 `callback` 回调。ATT&CK 与 NER 在这一点上无差异。
2. **`attack-llm` 有网络依赖**：它每次调用后端 LLM API，延迟取决于模型（实测单段中文报告若干秒级）。建议开启 `reuse_cache` 对重复文本命中缓存。
3. **`tram` 需先下载模型**：约 440MB SciBERT 权重，通过 `python benchmark/download_tram.py` 获取，未下载时该模型不会注册。
4. **本地 Retriever 的边界**：`qwen3-attack-retriever` 仅覆盖固定 50 标签，score 是余弦相似度而非概率，且没有可靠 NONE/拒识能力；应把结果当候选并人工复核。
5. **技术编号是语言无关的**：各模型输出的 `T1059` 与语言无关，便于跨语言归一化统计。
6. **证据 offset**：`tram` 精确对应命中句子；Retriever 返回完整输入区间；ATT&CK LLM 依赖模型证据定位，个别情况下可能回退为 `0,0`。
7. **可用模型以 `/models` 为准**：调用前拉取 `/models`，按返回的 `type === "attack"` 过滤出 ATT&CK 模型及其真实注册名。

---

## 7. 端到端示例（curl）

```bash
# 1. 提交（全量模式，中文，attack-llm）
curl -sS -X POST http://<host>/extract \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
        "text": "该木马通过计划任务实现持久化，并利用 rundll32 加载恶意 DLL。",
        "labels": ["full"],
        "model_name": "attack-llm"
      }'
# → {"task_id":"...","code":200,...}

# 2. 查询结果
curl -sS http://<host>/tasks/<task_id>/result \
  -H "Authorization: Bearer $API_KEY"
```

---

## 8. 相关文档

- [`tram-手册.md`](tram-手册.md) —— TRAM SciBERT 模型细节、阈值、参数调优
- [`qwen3-attack-retriever-手册.md`](qwen3-attack-retriever-手册.md) —— 中文本地 Retriever、模型资产、分数与质量边界
- [`attack-llm-手册.md`](attack-llm-手册.md) —— 大模型映射的提示词、校验富化、配置
- [`../提取标签API手册-v26_06_11.md`](../提取标签API手册-v26_06_11.md) —— 通用 `/extract` API 完整字段与鉴权
