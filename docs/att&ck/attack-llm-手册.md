# attack-llm 模型手册

> 大模型 ATT&CK 技战术映射引擎（中英文通吃、覆盖全部 600+ 技术）
> 模型名 `model_name`: **`attack-llm`**（或 `<模型名>` / `<模型名>-attack`） · 类型 `type`: `attack`

---

## 1. 概述

`attack-llm` 复用本服务的 `LLMModel`（OpenAI 兼容 API 调用 / SSE 流式 / 连通测试）能力，换上一套 **ATT&CK 映射提示词**：让大模型阅读威胁报告文本，从对手的**行为叙述**中推断出对应的 MITRE ATT&CK 技术编号，并给出原文证据片段。

相比 `tram`：

- **覆盖全部 600+ 技术**，而非固定 50 类。
- **中英文通吃**：技术编号与语言无关，中文报告同样能映射出 `T1059` 等编号。
- **保留原文证据 offset**：模型回传证据片段，服务定位回原文字符区间。
- **防幻觉**：所有编号用 `models/attack/techniques.json` 官方清单校验，不存在/已废弃的一律丢弃。

**适用场景**：中文报告、需要全技术覆盖、需要证据片段定位、已有可用大模型后端。

**代价**：每次调用一次 LLM API，有网络延迟与费用；输出受温度影响有一定波动（可用 `reuse_cache` 缓解重复文本）。

---

## 2. 提示词构成

发送给大模型的提示词分五层，逐层收窄任务、提高准确率：

1. **System 角色**：定位为资深 CTI 分析师，强调透过工具名 / 命令 / 注册表键 / 系统调用等细节**理解对手意图**，做语义映射而非字面匹配；要求只输出 JSON 数组。
2. **Scope（范围）**：
   - 全量模式 → 提示"识别文本中描述的所有技术，根据行为推断编号"。
   - 过滤模式 → 生成一份**带名称 + 战术的候选技术清单**（如 `- T1003.001  OS Credential Dumping: LSASS Memory  [credential-access]`），让模型从"带标签的菜单"里选，而不是凭记忆回想编号，显著提升准确率。
3. **Few-shot 示例**：4 个"行为描述 → 技术编号"通用范例，示范如何把短行为片段映射到技术、如何用 confidence 表达不确定性。
4. **分析要点**：行为优先、透过细节看意图、优先具体子技术、**短句也要给最可能项并调低 confidence 而非空手而归**、无可识别行为才输出空数组。
5. **输出契约**：JSON 数组，每项含 `technique_id` / `evidence`（逐字原文）/ `confidence`。

> 提示词由 `AttackLLMModel._build_messages` 构造，候选清单由 `attack_common.format_candidate_catalog` 生成，与 `tram` 共用同一套 labels 过滤语义。

---

## 3. 解析、校验与富化

模型返回文本后，`_parse_attack` 依次处理：

1. **抽取 JSON**：剥离 ```` ```json ```` 代码块标记，必要时用 `json-repair` 修复；非数组直接报错。
2. **编号校验**：每个 `technique_id` 经 `attack_common.is_valid_technique` 校验 —— 格式合法、在 `techniques.json` 中存在且未废弃，否则丢弃（防幻觉，如 `T9999` 被剔除）。
3. **过滤**：按 labels 的全量/过滤模式筛选（见下节）。
4. **定位 offset**：在原文里 `find(evidence)` 定位证据片段的 `start/end`；找不到则回退 `0,0`（技术仍保留）。
5. **去重**：`(technique_id, start, end)` 相同的只留一条。
6. **富化**：补 `technique_name` / `tactics` / `technique_url` / `is_subtechnique` / `parent_technique`。

---

## 4. labels 全量与过滤模式

与 `tram` **完全一致**（共用 `attack_common.parse_filters`）：

| labels | 模式 | 效果 |
|---|---|---|
| `["full"]` / 任意语义词 | 全量 | 识别文本中所有技术（默认） |
| `["T1059"]` | 过滤 | 只要 `T1059` 及其子技术，候选清单只列该范围 |
| `["T1059.003"]` | 过滤 | 只要该子技术 |
| `["execution"]` | 过滤 | 只要该战术下的技术 |

过滤模式下，提示词会把范围内的技术连同名称、战术一并列给模型，因此过滤模式往往比全量模式更准。详见 [ATT&CK 集成手册第 5 节](ATT&CK集成手册.md#5-labels-全量与过滤模式)。

---

## 5. 输出示例

请求：

```json
{ "text": "攻击者使用 Mimikatz 抓取凭据，并在注册表 Run 键写入自启动项。",
  "labels": ["full"], "model_name": "attack-llm" }
```

结果 `entities`：

```json
[
  {
    "text": "Mimikatz 抓取凭据",
    "label": "T1003.001", "start": 6, "end": 19, "score": 0.9,
    "technique_id": "T1003.001", "technique_name": "LSASS Memory",
    "tactics": ["credential-access"],
    "technique_url": "https://attack.mitre.org/techniques/T1003/001"
  },
  {
    "text": "注册表 Run 键写入自启动项",
    "label": "T1547.001", "start": 22, "end": 37, "score": 0.8,
    "technique_id": "T1547.001",
    "technique_name": "Registry Run Keys / Startup Folder",
    "tactics": ["persistence", "privilege-escalation"],
    "technique_url": "https://attack.mitre.org/techniques/T1547/001"
  }
]
```

---

## 6. 配置（api/.env）

`attack-llm` 不是独立后端，而是**把某个已配置的 LLM 包成 ATT&CK 映射器**。在 `ATTACK_MODELS_ENABLED` 里引用一个 LLM 即可：

```dotenv
# ATT&CK 调用台可选模型：tram + 某个 LLM
ATTACK_MODELS_ENABLED=tram,glm-5

# 该 LLM 的连接来自对应的 LLM_* 组（与 NER 台共用同一份连接配置）
LLM_MODELS=glm-5
LLM_BASE_URL=https://api.example.com/v1
LLM_API_KEY=sk-...
LLM_TEMPERATURE=0.1
LLM_TIMEOUT=120
```

引用方式两种：
- **按模型名**：直接写 `glm-5`，自动找到它所在的 `LLM_*` 组连接。
- **按组别名**：写 `LLM_MODELS_1` / `_2` / `_3`，展开为该组全部模型。

**同一 LLM 同时进两个调用台**时，ATT&CK 实例会自动登记为 `<模型名>-attack`（如 `glm-5-attack`），与 NER 台的 `glm-5` 缓存天然隔离；前端各台下拉各显示各的。实际可调用名以 `/models` 返回为准。

> 低温度（如 `LLM_TEMPERATURE=0.1`）能减少输出波动、提升可复现性，推荐。

---

## 7. 评测

```bash
# 约束到 TRAM 50 类做公平对比（默认）
python benchmark/attack_llm_benchmark.py \
  --dataset 'datasets/att&ck/attack_tram_bootstrap_chinese.json' --model glm-5 --limit 30 --verbose

# 放开为全部 600+ 技术
python benchmark/attack_llm_benchmark.py --dataset ... --open
```

`--verbose` 会打印每条错判的 `GT / 预测 / 原文`，便于定位提示词改进点。

---

## 8. 相关文档

- [`ATT&CK集成手册.md`](ATT&CK集成手册.md) —— API 集成、labels 语义、输出字段
- [`tram-手册.md`](tram-手册.md) —— 确定性 SciBERT 方案（英文、50 类）
