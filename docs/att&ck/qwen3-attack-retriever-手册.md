# qwen3-attack-retriever 模型手册

> 中文 CTI → MITRE ATT&CK 技术候选检索器  
> `model_name`: **`qwen3-attack-retriever`** · `type`: `attack`

## 1. 概述

该模型以 MLX 完成 `Qwen3-Embedding-4B` LoRA 工程训练，发布时已将 adapter 直接融合进基座并导出为标准 Hugging Face SafeTensors。生产 API 不依赖 MLX 或 PEFT，而是使用 PyTorch/Transformers：先把威胁情报文本切成保留原文 offset 的语义滑动窗口，再逐窗口生成 embedding，与固定 ATT&CK 技术描述计算余弦相似度，最后合并为全篇 Top-K 技术候选。

- ATT&CK 版本：Enterprise ATT&CK **v19.1**。
- 标签范围：Phase-1 固定 **50 个常见技术/子技术**。
- 语言：针对中文 CTI 做过工程训练，也支持英文输入。
- 推理：完全本地，不调用外部 API。
- 平台：标准 PyTorch 后端，支持 macOS（Apple MPS/CPU）、Linux x86_64/ARM64（CUDA/CPU）、Windows（CUDA/CPU）及对应 Docker 镜像。
- 质量边界：AI-only engineering 候选，未经正式人工 dev/test 验证，不是 release-grade 模型。

它适合内部辅助标注和 Top-K 候选召回，不应把 Top-1 当作无需复核的最终结论，也不具备可靠的 `NONE`/拒识能力。

## 2. 支持的 50 个 ATT&CK 标签

下表直接生成自运行时冻结语料 `retriever_corpus.jsonl`。外层“父技术族”仅用于展示层级，**只有内层表格列出的编号才属于模型支持范围**；例如 `T1003` 用于归组，但模型只支持其中的 `T1003.001`。

<table>
<thead><tr><th style="width:28%">父技术族（仅分组）</th><th>模型实际支持的技术/子技术</th></tr></thead>
<tbody>
<tr><td><code>T1003</code><br>OS Credential Dumping</td><td><table><tr><th>ID</th><th>类型</th><th>名称</th><th>战术</th></tr><tr><td><code>T1003.001</code></td><td>子技术</td><td>LSASS Memory</td><td>credential-access</td></tr></table></td></tr>
<tr><td><code>T1016</code><br>System Network Configuration Discovery</td><td><table><tr><th>ID</th><th>类型</th><th>名称</th><th>战术</th></tr><tr><td><code>T1016</code></td><td>技术</td><td>System Network Configuration Discovery</td><td>discovery</td></tr></table></td></tr>
<tr><td><code>T1021</code><br>Remote Services</td><td><table><tr><th>ID</th><th>类型</th><th>名称</th><th>战术</th></tr><tr><td><code>T1021.004</code></td><td>子技术</td><td>SSH</td><td>lateral-movement</td></tr></table></td></tr>
<tr><td><code>T1027</code><br>Obfuscated Files or Information</td><td><table><tr><th>ID</th><th>类型</th><th>名称</th><th>战术</th></tr><tr><td><code>T1027</code></td><td>技术</td><td>Obfuscated Files or Information</td><td>stealth</td></tr><tr><td><code>T1027.010</code></td><td>子技术</td><td>Command Obfuscation</td><td>stealth</td></tr><tr><td><code>T1027.013</code></td><td>子技术</td><td>Encrypted/Encoded File</td><td>stealth</td></tr></table></td></tr>
<tr><td><code>T1036</code><br>Masquerading</td><td><table><tr><th>ID</th><th>类型</th><th>名称</th><th>战术</th></tr><tr><td><code>T1036.005</code></td><td>子技术</td><td>Match Legitimate Resource Name or Location</td><td>stealth</td></tr></table></td></tr>
<tr><td><code>T1040</code><br>Network Sniffing</td><td><table><tr><th>ID</th><th>类型</th><th>名称</th><th>战术</th></tr><tr><td><code>T1040</code></td><td>技术</td><td>Network Sniffing</td><td>credential-access, discovery</td></tr></table></td></tr>
<tr><td><code>T1041</code><br>Exfiltration Over C2 Channel</td><td><table><tr><th>ID</th><th>类型</th><th>名称</th><th>战术</th></tr><tr><td><code>T1041</code></td><td>技术</td><td>Exfiltration Over C2 Channel</td><td>exfiltration</td></tr></table></td></tr>
<tr><td><code>T1055</code><br>Process Injection</td><td><table><tr><th>ID</th><th>类型</th><th>名称</th><th>战术</th></tr><tr><td><code>T1055</code></td><td>技术</td><td>Process Injection</td><td>privilege-escalation, stealth</td></tr><tr><td><code>T1055.001</code></td><td>子技术</td><td>Dynamic-link Library Injection</td><td>privilege-escalation, stealth</td></tr></table></td></tr>
<tr><td><code>T1056</code><br>Input Capture</td><td><table><tr><th>ID</th><th>类型</th><th>名称</th><th>战术</th></tr><tr><td><code>T1056.001</code></td><td>子技术</td><td>Keylogging</td><td>collection, credential-access</td></tr></table></td></tr>
<tr><td><code>T1059</code><br>Command and Scripting Interpreter</td><td><table><tr><th>ID</th><th>类型</th><th>名称</th><th>战术</th></tr><tr><td><code>T1059</code></td><td>技术</td><td>Command and Scripting Interpreter</td><td>execution</td></tr><tr><td><code>T1059.001</code></td><td>子技术</td><td>PowerShell</td><td>execution</td></tr><tr><td><code>T1059.003</code></td><td>子技术</td><td>Windows Command Shell</td><td>execution</td></tr></table></td></tr>
<tr><td><code>T1070</code><br>Indicator Removal</td><td><table><tr><th>ID</th><th>类型</th><th>名称</th><th>战术</th></tr><tr><td><code>T1070.004</code></td><td>子技术</td><td>File Deletion</td><td>stealth</td></tr></table></td></tr>
<tr><td><code>T1071</code><br>Application Layer Protocol</td><td><table><tr><th>ID</th><th>类型</th><th>名称</th><th>战术</th></tr><tr><td><code>T1071</code></td><td>技术</td><td>Application Layer Protocol</td><td>command-and-control</td></tr><tr><td><code>T1071.001</code></td><td>子技术</td><td>Web Protocols</td><td>command-and-control</td></tr><tr><td><code>T1071.004</code></td><td>子技术</td><td>DNS</td><td>command-and-control</td></tr></table></td></tr>
<tr><td><code>T1078</code><br>Valid Accounts</td><td><table><tr><th>ID</th><th>类型</th><th>名称</th><th>战术</th></tr><tr><td><code>T1078</code></td><td>技术</td><td>Valid Accounts</td><td>initial-access, persistence, privilege-escalation, stealth</td></tr><tr><td><code>T1078.004</code></td><td>子技术</td><td>Cloud Accounts</td><td>initial-access, persistence, privilege-escalation, stealth</td></tr></table></td></tr>
<tr><td><code>T1090</code><br>Proxy</td><td><table><tr><th>ID</th><th>类型</th><th>名称</th><th>战术</th></tr><tr><td><code>T1090</code></td><td>技术</td><td>Proxy</td><td>command-and-control</td></tr><tr><td><code>T1090.001</code></td><td>子技术</td><td>Internal Proxy</td><td>command-and-control</td></tr><tr><td><code>T1090.003</code></td><td>子技术</td><td>Multi-hop Proxy</td><td>command-and-control</td></tr></table></td></tr>
<tr><td><code>T1091</code><br>Replication Through Removable Media</td><td><table><tr><th>ID</th><th>类型</th><th>名称</th><th>战术</th></tr><tr><td><code>T1091</code></td><td>技术</td><td>Replication Through Removable Media</td><td>initial-access, lateral-movement</td></tr></table></td></tr>
<tr><td><code>T1102</code><br>Web Service</td><td><table><tr><th>ID</th><th>类型</th><th>名称</th><th>战术</th></tr><tr><td><code>T1102</code></td><td>技术</td><td>Web Service</td><td>command-and-control</td></tr><tr><td><code>T1102.002</code></td><td>子技术</td><td>Bidirectional Communication</td><td>command-and-control</td></tr></table></td></tr>
<tr><td><code>T1105</code><br>Ingress Tool Transfer</td><td><table><tr><th>ID</th><th>类型</th><th>名称</th><th>战术</th></tr><tr><td><code>T1105</code></td><td>技术</td><td>Ingress Tool Transfer</td><td>command-and-control</td></tr></table></td></tr>
<tr><td><code>T1112</code><br>Modify Registry</td><td><table><tr><th>ID</th><th>类型</th><th>名称</th><th>战术</th></tr><tr><td><code>T1112</code></td><td>技术</td><td>Modify Registry</td><td>defense-impairment, persistence</td></tr></table></td></tr>
<tr><td><code>T1485</code><br>Data Destruction</td><td><table><tr><th>ID</th><th>类型</th><th>名称</th><th>战术</th></tr><tr><td><code>T1485</code></td><td>技术</td><td>Data Destruction</td><td>impact</td></tr></table></td></tr>
<tr><td><code>T1486</code><br>Data Encrypted for Impact</td><td><table><tr><th>ID</th><th>类型</th><th>名称</th><th>战术</th></tr><tr><td><code>T1486</code></td><td>技术</td><td>Data Encrypted for Impact</td><td>impact</td></tr></table></td></tr>
<tr><td><code>T1530</code><br>Data from Cloud Storage</td><td><table><tr><th>ID</th><th>类型</th><th>名称</th><th>战术</th></tr><tr><td><code>T1530</code></td><td>技术</td><td>Data from Cloud Storage</td><td>collection</td></tr></table></td></tr>
<tr><td><code>T1560</code><br>Archive Collected Data</td><td><table><tr><th>ID</th><th>类型</th><th>名称</th><th>战术</th></tr><tr><td><code>T1560.001</code></td><td>子技术</td><td>Archive via Utility</td><td>collection</td></tr></table></td></tr>
<tr><td><code>T1566</code><br>Phishing</td><td><table><tr><th>ID</th><th>类型</th><th>名称</th><th>战术</th></tr><tr><td><code>T1566.001</code></td><td>子技术</td><td>Spearphishing Attachment</td><td>initial-access</td></tr><tr><td><code>T1566.002</code></td><td>子技术</td><td>Spearphishing Link</td><td>initial-access</td></tr></table></td></tr>
<tr><td><code>T1567</code><br>Exfiltration Over Web Service</td><td><table><tr><th>ID</th><th>类型</th><th>名称</th><th>战术</th></tr><tr><td><code>T1567</code></td><td>技术</td><td>Exfiltration Over Web Service</td><td>exfiltration</td></tr></table></td></tr>
<tr><td><code>T1570</code><br>Lateral Tool Transfer</td><td><table><tr><th>ID</th><th>类型</th><th>名称</th><th>战术</th></tr><tr><td><code>T1570</code></td><td>技术</td><td>Lateral Tool Transfer</td><td>lateral-movement</td></tr></table></td></tr>
<tr><td><code>T1574</code><br>Hijack Execution Flow</td><td><table><tr><th>ID</th><th>类型</th><th>名称</th><th>战术</th></tr><tr><td><code>T1574.006</code></td><td>子技术</td><td>Dynamic Linker Hijacking</td><td>execution, stealth</td></tr></table></td></tr>
<tr><td><code>T1583</code><br>Acquire Infrastructure</td><td><table><tr><th>ID</th><th>类型</th><th>名称</th><th>战术</th></tr><tr><td><code>T1583.001</code></td><td>子技术</td><td>Domains</td><td>resource-development</td></tr><tr><td><code>T1583.003</code></td><td>子技术</td><td>Virtual Private Server</td><td>resource-development</td></tr><tr><td><code>T1583.006</code></td><td>子技术</td><td>Web Services</td><td>resource-development</td></tr></table></td></tr>
<tr><td><code>T1584</code><br>Compromise Infrastructure</td><td><table><tr><th>ID</th><th>类型</th><th>名称</th><th>战术</th></tr><tr><td><code>T1584.004</code></td><td>子技术</td><td>Server</td><td>resource-development</td></tr></table></td></tr>
<tr><td><code>T1585</code><br>Establish Accounts</td><td><table><tr><th>ID</th><th>类型</th><th>名称</th><th>战术</th></tr><tr><td><code>T1585.002</code></td><td>子技术</td><td>Email Accounts</td><td>resource-development</td></tr></table></td></tr>
<tr><td><code>T1587</code><br>Develop Capabilities</td><td><table><tr><th>ID</th><th>类型</th><th>名称</th><th>战术</th></tr><tr><td><code>T1587.001</code></td><td>子技术</td><td>Malware</td><td>resource-development</td></tr></table></td></tr>
<tr><td><code>T1588</code><br>Obtain Capabilities</td><td><table><tr><th>ID</th><th>类型</th><th>名称</th><th>战术</th></tr><tr><td><code>T1588.001</code></td><td>子技术</td><td>Malware</td><td>resource-development</td></tr><tr><td><code>T1588.002</code></td><td>子技术</td><td>Tool</td><td>resource-development</td></tr></table></td></tr>
<tr><td><code>T1589</code><br>Gather Victim Identity Information</td><td><table><tr><th>ID</th><th>类型</th><th>名称</th><th>战术</th></tr><tr><td><code>T1589</code></td><td>技术</td><td>Gather Victim Identity Information</td><td>reconnaissance</td></tr><tr><td><code>T1589.002</code></td><td>子技术</td><td>Email Addresses</td><td>reconnaissance</td></tr></table></td></tr>
<tr><td><code>T1608</code><br>Stage Capabilities</td><td><table><tr><th>ID</th><th>类型</th><th>名称</th><th>战术</th></tr><tr><td><code>T1608.001</code></td><td>子技术</td><td>Upload Malware</td><td>resource-development</td></tr></table></td></tr>
<tr><td><code>T1685</code><br>Disable or Modify Tools</td><td><table><tr><th>ID</th><th>类型</th><th>名称</th><th>战术</th></tr><tr><td><code>T1685</code></td><td>技术</td><td>Disable or Modify Tools</td><td>defense-impairment</td></tr></table></td></tr>
</tbody>
</table>

> 共计 **50 个模型标签**。父技术族名称来自本地 `models/attack/techniques.json`，标签编号、类型、英文名称和战术字段来自冻结的 ATT&CK v19.1 Retriever corpus。

## 3. 调用方法

```json
{
  "text": "攻击者使用 mimikatz 从 lsass.exe 进程内存中导出登录凭据和明文密码。",
  "labels": ["full"],
  "model_name": "qwen3-attack-retriever",
  "threshold": 0.5,
  "reuse_cache": true
}
```

请求仍通过 `POST /extract` 提交，再通过 `GET /tasks/{task_id}/result` 查询。

`labels` 沿用 ATT&CK 调用台的过滤语义：

| labels | 效果 |
|---|---|
| `["full"]` | 在模型固定的 50 标签中全量检索 |
| `["T1003"]` | 只保留该父技术及其子技术 |
| `["T1003.001"]` | 只保留指定子技术 |
| `["credential-access"]` | 只保留该战术下的候选 |

## 4. 细粒度语义窗口识别

该模型采用“借鉴 TRAM 细粒度、但不直接逐句分类”的混合策略：

```text
原文
  → 安全句界切分并保留字符 offset
  → 短片段与相邻句合并
  → 超长句 token 滑窗
  → 相邻句组装语义窗口
  → 每窗口 Retriever Top-3
  → 按 Technique ID 去重并保留最高分证据
  → 全篇 Top-10
```

融合前后必须保持训练时的编码合同：query 精确拼接为 `Instruct: {instruction}\nQuery:{text}`，query/document 上限分别为 `384/512` tokens；实现先调用 tokenizer 完整 `encode`，再直接取前 N 个 token，而不是使用会为尾部特殊 token 预留位置的 `truncation=True`。模型取最后一个实际 token 的 hidden state，转 float32 后做 L2 normalize。50 条 document embedding 在启动时形成 `50 × 2560` 内存矩阵，窗口 query 与其做余弦点积；这不是外部向量数据库。导出校验曾据此发现并阻止长 document 截断语义漂移，因此后端迁移时不可改成 mean pooling 或另一种截断方式。

### 4.1 基础切分和 offset

- 强边界：中文 `。！？；`、英文 `!?;`、换行 `\n/\r`。
- 英文句点 `.` 仅在后面是空白或文本结尾时作为边界；数字小数/IP，以及前后均为字母数字的域名、文件名、版本号不会在该点切开。
- 连续终止符作为同一结尾处理。
- 每个窗口始终直接截取自原文，保证 `text[start:end] == entity.text`。
- 不修改或规范化原文后再计算 offset。

### 4.2 短片段合并

非空白字符少于 `12` 的片段不会直接丢弃：标题或短句优先与下一条正常句合并，末尾短片段与前一条合并。例如“凭据窃取”会与其后具体行为一起进入模型；纯碎片也不会单独触发一次昂贵的 4B 推理。

### 4.3 token 窗口

- 正常窗口最多合并 `3` 个相邻基础句，目标长度不超过 `256` 个内容 token。
- 硬上限：`320` 个内容 token；启动时额外校验它与 query instruction 合计不超过训练配置的 `query_max_length=384`。
- 相邻窗口重叠 `1` 个完整句子，保留代词、主语和前后行为上下文。
- 单个基础句超过 `320` tokens 时，按 tokenizer 的 offset mapping 切成 320-token 子窗口，并重叠 `48` tokens；因此超长命令、日志或无标点段落也不会静默截断。
- 默认最多处理 `200` 个窗口；超过时从全文首尾之间均匀采样 200 个窗口，而不是只保留报告开头。metadata 会返回 `windows_sampled` 和采样前总数。

### 4.4 候选与合并

每个窗口只保留相似度超过请求 `threshold` 的 Top-3 候选；随后按 `technique_id` 全篇去重。同一技术在多个窗口命中时，最终分数取最大值，并把最高分窗口作为 `text/start/end` 证据。所有去重结果按分数降序排列，最后最多返回全篇 Top-10。

这意味着 `score` 仍是窗口与技术描述的余弦相似度，不是概率；证据范围是一个包含 1～数句的语义窗口，不保证是最小短语 span。推理窗口在模型锁内串行编码，长报告的耗时约随窗口数线性增长。

## 5. 输出与分数

模型默认最多返回 10 个全篇去重候选，并复用统一 ATT&CK 输出字段：`technique_id`、`technique_name`、`tactics`、`technique_url`、`is_subtechnique` 和 `parent_technique`。`chunk_index` 标识产生最佳分数的语义窗口，`chunk_char_start/end` 与 `start/end` 指向该窗口的原文范围。

`score` 是归一化 query/document embedding 的**余弦相似度**，不是概率。请求中的 `threshold` 是每个窗口的最低相似度过滤线；平台默认 `0.5`。若结果过少可适当降低，但会增加误报。

Retriever 返回最高分语义窗口作为证据，因此能够定位到相关的 1～数句文本，但不会进一步抽取最小证据短语。需要更细的短语级证据时应使用 `attack-llm` 或人工复核。

## 6. 模型文件与一键导出

```text
models/attack/qwen3-attack-retriever/
├── merged/                         # 标准 HF Qwen3Model，融合后的 float16 SafeTensors
│   ├── config.json
│   ├── model-00001-of-00002.safetensors
│   ├── model-00002-of-00002.safetensors
│   ├── model.safetensors.index.json
│   └── tokenizer 等 HF/SentenceTransformers 资产
├── retriever_corpus.jsonl          # ATT&CK v19.1 Phase-1 50 标签语料
├── training_config.json            # 冻结训练配置
├── training_manifest.json          # seed 42 / epoch 1 训练谱系
└── runtime_manifest.json           # 源 hash、产物 hash 与跨框架一致性结果
```

从训练资产重新生成完整运行包：

```bash
cd TrainingModel
uv run python src/export_qwen3_attack_retriever.py
```

目标已存在时脚本默认拒绝覆盖；确认替换该模型包时加 `--force`。它只操作 `models/attack/qwen3-attack-retriever/`，不会删除共享的 `models/attack/techniques.json`。导出依次校验训练 config、seed 42/epoch 1 adapter、50-label corpus 与基座文件 hash，融合 8 个 LoRA 线性层，保存标准 SafeTensors，再比较 MLX 与 PyTorch 的 query/document embedding 和代表 query 的 Top-10 排名。全部通过后才替换目标目录；失败会清理临时目录。

API 启动时依据 `runtime_manifest.json` 校验 config、manifest、corpus 及 `merged/` 下全部产物的大小和 SHA-256；任一文件不匹配时模型拒绝注册。运行目录不再包含独立 base/adapter，也不需要 PEFT。

## 7. 配置

在 `api/.env` 中启用：

```dotenv
ATTACK_MODELS_ENABLED=tram,glm-5,qwen3-attack-retriever
```

可选环境变量：

| 环境变量 | 默认 | 说明 |
|---|---:|---|
| `QWEN3_ATTACK_DEVICE` | `auto` | `auto/cpu/cuda/mps`；自动顺序为 CUDA → Apple MPS → CPU |
| `QWEN3_ATTACK_DTYPE` | `auto` | `auto/float32/float16/bfloat16`；auto 在 CUDA/MPS 用 float16、CPU 用 float32 |
| `QWEN3_ATTACK_TOP_K` | `10` | 全篇去重后最多返回的技术数，范围 1～50 |
| `QWEN3_ATTACK_WINDOW_TOP_K` | `3` | 每个语义窗口最多保留的候选数 |
| `QWEN3_ATTACK_WINDOW_TARGET_TOKENS` | `256` | 相邻句组装窗口的目标 token 上限 |
| `QWEN3_ATTACK_WINDOW_MAX_TOKENS` | `320` | 单窗口硬上限；还需为 query instruction 预留空间 |
| `QWEN3_ATTACK_WINDOW_OVERLAP_SENTENCES` | `1` | 相邻正常窗口重叠的完整句子数 |
| `QWEN3_ATTACK_WINDOW_MAX_SENTENCES` | `3` | 一个正常窗口最多合并的基础句数量 |
| `QWEN3_ATTACK_LONG_OVERLAP_TOKENS` | `48` | 超长单句 token 子窗口的重叠量 |
| `QWEN3_ATTACK_MIN_FRAGMENT_CHARS` | `12` | 低于该非空白字符数的片段与相邻句合并 |
| `QWEN3_ATTACK_MAX_WINDOWS` | `200` | 单次请求最多实际推理的窗口数；超出后均匀采样 |
| `MODELS_ROOT` | 项目 `models/` | 模型根目录 |

生产运行时仅需要平台已有的 PyTorch、`transformers==4.53.2`、NumPy 和 SafeTensors 依赖，不需要 `mlx`、`mlx-lm` 或 PEFT。MLX 仅保留在 `TrainingModel/.venv` 中用于训练复现和一键导出。

模型权重约 8.04 GB。GPU/MPS 的 `float16` 通常需要约 8～10 GB 模型内存，另加窗口前向开销；CPU `auto` 默认采用兼容性最好的 `float32`，模型常驻内存可能超过 16 GB 且 4B 推理较慢。内存受限时可尝试 `QWEN3_ATTACK_DTYPE=float16`，但需由目标 CPU/PyTorch 版本确认半精度算子支持。Docker 当前从 PyTorch CPU wheel 源安装，适用于 Linux amd64 和 arm64；Windows 原生运行使用对应 Windows PyTorch wheel。SafeTensors 模型本身与操作系统和 CPU 架构无关。

## 8. 工程评测与限制

冻结候选：`qwen3-embedding-4b-mlx-lora-engineering-v2`，seed 42，最佳 epoch 1。

| 指标 | 未适配基座 | LoRA 候选 |
|---|---:|---:|
| Recall@10 | 0.5333 | 0.5355 |
| Recall@5 | 0.2815 | 0.3204 |

评测仅有 62 条 AI-only engineering dev query，且跨 seed 存在方差，因此这些数字不能作为正式生产效果声明。模型仅覆盖 50/697 标签；范围外行为可能被错误映射到最相近的范围内标签。

## 9. 选择建议

- 中文报告、希望本地且快速得到 50 标签候选：使用本模型。
- 英文句子级 50 类分类：使用 `tram`。
- 需要 600+ 标签覆盖、精确证据片段或更强语义判断：使用 `attack-llm`。
- 高风险自动化决策：对本模型结果进行人工复核或与其他模型交叉验证。
