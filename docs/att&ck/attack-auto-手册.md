# ATT&CK Auto 模型路由手册

> 模型名：`attack-auto` · 类型：`attack`

## 1. 用途

`attack-auto` 不直接执行 ATT&CK 识别，而是按输入文本的中英文字符占比选择一个已加载的子模型：

1. 英文占比达到阈值（默认 95%）→ `tram`；
2. 否则中文占比达到阈值（默认 60%）→ `qwen3-attack-retriever`；
3. 两个条件均未命中 → `qwen3-attack-retriever` 兜底。

英文规则固定优先于中文规则。比较使用 `>=`，因此恰好 95% 或 60% 会命中。

## 2. 占比算法

只统计 ASCII 字母 `A-Z/a-z` 和 Unicode 汉字，分母为：

```text
英文字符数 + 中文字符数
```

数字、标点、空白、URL 符号、IP 和哈希不进入分母，避免 IOC 内容稀释语言比例。没有可判定语言字符时直接走兜底模型。

## 3. 启用与调用

`api/.env` 中必须同时启用两个底层模型和 `auto`：

```dotenv
ATTACK_MODELS_ENABLED=tram,glm-5,qwen3-attack-retriever,auto
```

清单中的 `auto` 会注册为稳定运行时名称 `attack-auto`，与 NER 调用台的 `auto` 相互独立。调用 `/extract` 时指定：

```json
{"model_name":"attack-auto","text":"...","labels":["full"]}
```

## 4. 管理配置

管理员页面选择“ATT&CK Auto模型路由”，通过两个 0～100% 滑动条调整英文和中文阈值。保存后写入 `api/attack_auto_routing.json` 并热重载，无需重启。

配置保存采用原子替换，服务端固定校验英文→TRAM、中文→自研模型、兜底→自研模型。阈值变化会更新配置 hash；该 hash 纳入任务缓存键，避免复用旧阈值产生的缓存结果。

结果的 `chunk_metadata.attack_auto_routing` 会记录字符计数、两种占比、命中规则、实际子模型和配置 hash。实际实体及窗口 metadata 由被选中的子模型生成。

## 5. 注意事项

- `tram` 更适合高纯度英文报告；中文或混合文本默认交给自研 Retriever。
- 路由只负责选模，不会同时运行或合并两个模型。
- 任一底层模型加载失败时，`attack-auto` 拒绝注册，避免静默选择不可用模型。
- 调整阈值只改变模型选择，不改变 TRAM 或 Retriever 自身的推理阈值。
