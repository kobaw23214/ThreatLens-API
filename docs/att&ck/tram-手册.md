# tram 模型手册

> MITRE TRAM SciBERT 句子级 ATT&CK 技术分类器
> 模型名 `model_name`: **`tram`** · 类型 `type`: `attack`

---

## 1. 概述

`tram` 是把 [MITRE TRAM（Threat Report ATT&CK Mapper）](https://github.com/center-for-threat-informed-defense/tram) 的 **SciBERT 分类模型**封装成的「伪模型」，与 GLiNER / IOC 引擎同构，实现统一的 `predict_with_chunks` 接口，因此复用本服务全部的任务队列、缓存、回调、去重机制，提交任务时指定 `model_name: "tram"` 即可。

**它的工作方式**：不是 span 抽取，而是**句子级多标签分类** —— 把文本切成句子，对每个句子用 SciBERT 判断它命中了哪些 ATT&CK 技术，输出「句子 → 技术编号 + 置信度」。

**核心特点**

- **确定性强**：同一输入永远得到同一输出，适合需要可复现结果的场景。
- **本地纯推理**：CPU 即可运行（也支持 GPU/MPS），无外部 API 依赖，数据不出本地。
- **官方微调权重**：直接使用 TRAM 发布的 SciBERT 权重，行为与上游一致。
- **自动富化**：命中技术编号后，用 `models/attack/techniques.json` 补全技术名称 / 战术 / 官方 URL。

**适用场景**：英文威胁报告的 TTP 快速标注、需要稳定可复现分数的批处理流水线。

**局限**

- **仅覆盖 50 类技术**（见 `bert_model/classes.txt`），无法识别其余 550+ 技术。想要全覆盖请用 `attack-llm`。
- **中文效果弱**：SciBERT 是英文科技语料预训练，中文文本准确率明显低于英文。中文场景推荐 `attack-llm`。

---

## 2. 如何使用：labels 是过滤器

TRAM 的技术类别是**模型固定的**（50 类），所以 `labels` 不用来指定"抽什么"，而是当**过滤器**用：

| labels | 效果 |
|---|---|
| `["full"]`（或 `全量`/`ATT&CK`/`TTP` 等） | **全量模式**（默认）：返回全部命中阈值的技术 |
| `["T1059"]` | 只返回 `T1059` 及其全部子技术 |
| `["T1059.003"]` | 只返回该子技术 |
| `["execution"]` | 只返回属于 `execution` 战术的技术 |

规则与 `attack-llm` 完全一致（共用 `attack_common.parse_filters`），详见 [ATT&CK 集成手册第 5 节](ATT&CK集成手册.md#5-labels-全量与过滤模式)。`labels` 至少要有 1 项，全量时传 `["full"]` 占位。

---

## 3. 推理与分块逻辑

```text
原文
  → 安全句界切分并保留字符 offset
  → 短片段与相邻句合并
  → 超长句按 tokenizer offset 切成重叠 token 子窗口
  → 正常句/子窗口直接进行 TRAM 分类
  → 仅低置信度正常句补充一个相邻句上下文
  → 按 Technique ID 融合，优先直接句子证据
```

### 3.1 安全句界与短片段

强句界包括 `!?;。！？；` 和换行。英文句点仅在安全位置切分，因此不会拆开 `rundll32.exe`、`example.com`、`192.168.1.10`、版本号或小数。连续终止符作为同一结尾处理，所有窗口都直接截取原文，保证 `original_text[start:end] == entity.text`。

非空白字符少于 `TRAM_MIN_SENT_CHARS` 的标题或碎片不再丢弃：优先并入右侧正常句，文末碎片并入左侧句子。

### 3.2 超长句 token 滑窗

`TRAM_MAX_LENGTH=256` 包含 SciBERT 的特殊 token，因此实际正文上限由 tokenizer 动态计算，当前为 254 tokens。超过上限的单句按 offset mapping 切成子窗口，默认重叠 32 tokens；后端在送入模型前再次断言总长度不超过 256，不再静默截断句尾。

### 3.3 选择性上下文回退

正常句仍保持 TRAM 原本的逐句分类输入分布。只有该句在当前 `labels` 过滤范围内的最高分低于 `max(请求 threshold, TRAM_CONTEXT_TRIGGER_THRESHOLD)` 时，才补充一个相邻句：优先前一句，没有可用前句时尝试后一句。上下文总长度必须不超过正文 token 上限；超长句子窗口不做上下文扩展。

每个低置信度句最多增加一次上下文推理，不会把全文默认改成 2～3 句重叠窗口。这可以补充代词、主语等跨句信息，同时控制输入分布偏移、误报和推理成本。

### 3.4 候选融合与 metadata

每个窗口仍以 `sigmoid >= threshold` 产生候选。同一技术在多个窗口命中时全篇只保留一条，优先级为：直接句子/token 子窗口优先于上下文窗口；同类型再按分数高、证据短、窗口靠前比较。因此上下文只补充直接分类未命中的技术，不会用宽泛窗口覆盖已有精确句子证据。

返回的 `chunk_metadata` 会列出所有真实执行的 direct/context 窗口及其 offset、token 数和 `evidence_kind`，并提供 `direct_window_count`、`context_window_count`、正文 token 上限及 token overlap。

---

## 4. 输出示例

请求：

```json
{ "text": "The malware creates a scheduled task and loads a DLL via rundll32.exe.",
  "labels": ["full"], "model_name": "tram" }
```

结果 `entities`（节选）：

```json
[
  {
    "text": "The malware creates a scheduled task and loads a DLL via rundll32.exe.",
    "label": "T1053.005",
    "start": 0, "end": 69, "score": 0.88,
    "technique_id": "T1053.005",
    "technique_name": "Scheduled Task",
    "tactics": ["execution", "persistence", "privilege-escalation"],
    "technique_url": "https://attack.mitre.org/techniques/T1053/005"
  }
]
```

> 同一句可能命中多个技术，各产出一条实体（label 不同、句子 offset 相同）。

---

## 5. 配置与调优（api/.env）

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `TRAM_MAX_LENGTH` | `256` | 含 SciBERT 特殊 token 的模型总长度上限 |
| `TRAM_BATCH_SIZE` | `16` | 窗口批量推理大小；显存/内存紧张可调小 |
| `TRAM_MIN_SENT_CHARS` | `3` | 低于该非空白字符数的片段并入相邻句，不会丢弃 |
| `TRAM_LONG_OVERLAP_TOKENS` | `32` | 超长单句 token 子窗口的重叠量，必须小于正文 token 上限 |
| `TRAM_CONTEXT_ENABLED` | `true` | 是否启用低置信度句子的单邻句上下文回退 |
| `TRAM_CONTEXT_TRIGGER_THRESHOLD` | `0.45` | 上下文触发分数线；实际使用它与请求 threshold 的较大值 |
| `TRAM_DEVICE` | 空（自动） | 强制 `cuda`/`mps`/`cpu`；留空按 CUDA → MPS → CPU 自动选择 |

**阈值调优**：请求里的 `threshold` 直接控制召回/精度。阈值调高（如 0.7）→ 精度升、召回降，但也会让更多句子进入上下文回退；阈值调低（如 0.3）→ 召回升、误报增。可用 `benchmark/attack_benchmark.py` 在标注集上联合比较 threshold、上下文开关和触发线。

---

## 6. 模型文件与获取

```
models/tram/
├── bert_model/
│   ├── pytorch_model.bin     # SciBERT 微调权重(~440MB)
│   ├── config.json
│   └── classes.txt           # 50 个技术编号，行号对应模型输出下标
└── scibert_tokenizer/        # tokenizer(缺失时回退用 bert_model/)
models/attack/techniques.json # 富化元数据(编号→名称/战术/URL)
```

获取方式：

```bash
python benchmark/download_tram.py          # 下载 SciBERT 权重与 classes.txt
python benchmark/download_attack_data.py   # 生成 techniques.json 富化元数据
```

未下载模型时 `tram` 不会注册（`load()` 返回 False 并打印提示）。

---

## 7. 评测

```bash
# 在标注集上评测 TRAM（Top-1 / micro P/R/F1）
python benchmark/attack_benchmark.py \
  --dataset 'datasets/att&ck/attack_tram_bootstrap.json' --threshold 0.5
```

> `attack_tram_bootstrap.json` 是 TRAM 官方 bootstrap 训练数据，评测分数会偏乐观（训练/测试重叠），仅供相对比较。

---

## 8. 相关文档

- [`ATT&CK集成手册.md`](ATT&CK集成手册.md) —— API 集成、labels 语义、输出字段
- [`attack-llm-手册.md`](attack-llm-手册.md) —— 大模型映射方案（全覆盖、中英文）
