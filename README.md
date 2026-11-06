# ThreatLens-API

[![Stars](https://img.shields.io/github/stars/kobaw23214/ThreatLens-API?style=social)](https://github.com/kobaw23214/ThreatLens-API/stargazers)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Demo](https://img.shields.io/badge/NER%20Demo-demo%2F-ff69b4)](#-ner-在线演示)

> 威胁情报实体提取平台 —— NER / ATT&CK / 联网情报,一站式提取与调度
>
> Threat Intelligence Entity Extraction Platform — NER · MITRE ATT&CK Mapping · TI Enrichment

<div align="center">

**NER 实体提取** · **ATT&CK 技术识别** · **联网情报扩线(微步/奇安信)**

Entity Extraction · Technique Mapping · Threat Intelligence Enrichment

</div>

---

## ✨ 功能

| 模块 | 说明 |
|---|---|
| **NER 提取** | gliner-x-large / ioc-finder / iocextract / auto 智能分流,支持自定义标签、分块、缓存复用 |
| **ATT&CK 识别** | tram(SciBERT)/ attack-auto(中英语言路由)/ qwen3 检索,输出技术编号与战术映射 |
| **联网情报** | 微步在线、奇安信 TI 平台自动扩线,Playwright 会话管理与登录态持久化 |
| **任务调度** | 异步队列、Worker 状态监控、结果缓存 TTL、Webhook 回调(成功/失败/重试) |
| **管理后台** | 用户与限额、模型统计、缓存管理、API 手册 |

## 🏗️ 架构与技术栈

FastAPI + Vue3 + PostgreSQL,模型与会话运行时挂载、代码与镜像解耦,单 compose 一键起。

```
web/   Vue3 前端(仪表盘/调用台/统计)
api/   FastAPI 后端(队列/模型/回调)
docs/  手册与截图    tests/  测试
```

**Backend** Python · FastAPI · GLiNER · Transformers · SciBERT(TRAM)· Playwright · PostgreSQL · Docker
**Frontend** Vue 3 · ECharts · xterm.js
**Domain** Threat Intelligence (CTI) · NER / Information Extraction · MITRE ATT&CK · IOC Extraction · Threat Hunting

## 🚀 快速开始

```bash
# 1. 配置(数据库、模型、平台密钥)
cp api/.env api/.env.bak && vim api/.env

# 2. 启动(首次构建镜像约需数分钟)
docker compose up -d

# 3. 访问
open http://localhost:8036
```

> 详细部署与接口说明见 `docs/` 下各手册。改代码只需 `docker compose restart api`,无需重打镜像。

## 🖼️ 界面

| | |
|:---:|:---:|
| <img src="docs/images/parse-detail-ner-overview.png" width="380" alt="NER 解析总览"><br>**NER 解析总览** — 原文标注/类型分布/关系图 | <img src="docs/images/parse-detail-ner-entity-graph.png" width="380" alt="NER 实体图谱"><br>**NER 实体图谱** — 实体关系与置信度 |
| <img src="docs/images/parse-detail-attack-entities.png" width="380" alt="ATT&CK 实体列表"><br>**ATT&CK 识别** — 技术编号与战术映射 | <img src="docs/images/parse-detail-ti-qianxin.png" width="380" alt="联网情报详情"><br>**联网情报** — 奇安信 IP 情报/AI 研判 |
| <img src="docs/images/console-ner-playground.png" width="380" alt="NER 调用台"><br>**NER 调用台** — 标签配置/curl 预览 | <img src="docs/images/console-attack-playground.png" width="380" alt="ATT&CK 调用台"><br>**ATT&CK 调用台** — 模型选择/参数配置 |
| <img src="docs/images/stats-ner-overview.png" width="380" alt="NER 统计"><br>**NER 任务统计** — 调用趋势/成功率/延迟 | <img src="docs/images/stats-ti-overview.png" width="380" alt="联网情报统计"><br>**联网情报统计** — 平台状态/种子分布 |
| <img src="docs/images/queue-status.png" width="380" alt="队列状态"><br>**队列状态** — 三队列/Worker 监控 | <img src="docs/images/entity-detail-modal-ner.png" width="380" alt="实体详情弹窗"><br>**实体详情** — 原文定位/置信度 |

<details>
<summary>更多截图(分块明细、API 调用台、模型统计、手册与管理等)</summary>

| | |
|:---:|:---:|
| <img src="docs/images/parse-detail-ner-chunks.png" width="380" alt="NER 分块明细"><br>**NER 分块明细** — Chunk 级实体分布 | <img src="docs/images/parse-detail-attack-chunks.png" width="380" alt="ATT&CK 分块明细"><br>**ATT&CK 分块明细** — 长文本分块识别 |
| <img src="docs/images/entity-detail-modal-attack.png" width="380" alt="ATT&CK 实体弹窗"><br>**ATT&CK 实体详情** — 技术编号高亮定位 | <img src="docs/images/api-playground-ner.png" width="380" alt="API 调用台 NER"><br>**API 调用台** — NER 两步调用示例 |
| <img src="docs/images/api-playground-attack.png" width="380" alt="API 调用台 ATT&CK"><br>**API 调用台** — ATT&CK 两步调用示例 | <img src="docs/images/stats-ner-models.png" width="380" alt="NER 模型统计"><br>**NER 模型统计** — 模型对比/性能表 |
| <img src="docs/images/stats-attack-overview.png" width="380" alt="ATT&CK 统计"><br>**ATT&CK 任务统计** — 调用趋势/占比 | <img src="docs/images/stats-attack-models.png" width="380" alt="ATT&CK 模型统计"><br>**ATT&CK 模型统计** — tram/attack-auto 对比 |
| <img src="docs/images/manual-ner-api.png" width="380" alt="API 手册"><br>**API 手册** — 在线集成文档 | <img src="docs/images/admin-user-management.png" width="380" alt="用户管理"><br>**管理后台** — 用户/限额管理 |

</details>

---

<div align="center">

## 📬 联系我 / Contact

**微信扫码交流 / Scan to chat on WeChat**

<a href="docs/images/wechat-qrcode.jpg" target="_blank">
  <img src="docs/images/wechat-qrcode.jpg" width="200" alt="WeChat QR Code" title="点击查看大图" />
</a>

**邮箱 / Email**: [1477746214@qq.com](mailto:1477746214@qq.com)

</div>

---

<div align="center">

**如果这个项目帮到了你,欢迎点一个 ⭐ Star 支持一下!**

</div>

## 🎯 NER 在线演示

`demo/` 是自包含的实体抽取演示(**免 DB / 免 LLM / 免 TI 密钥**):GLiNER + FastAPI + 单文件演示页,可一键部署到 [Hugging Face Spaces](https://huggingface.co/spaces)(免费 Docker,面向 AI 受众的曝光渠道):

```bash
# 本地运行
cd demo && docker build -t threatlens-demo . && docker run -p 8092:7860 threatlens-demo
# 浏览器打开 http://localhost:8092 —— 内置 APT 报告示例,一键抽取实体
```

部署到 HF Spaces:新建 Docker Space → 上传 `demo/` 目录内容 → 访问 `https://<用户名>-threatlens-ner.hf.space`。

> 演示仅含 NER;生产版含 ATT&CK 识别、TI 扩线、任务调度等完整能力。
