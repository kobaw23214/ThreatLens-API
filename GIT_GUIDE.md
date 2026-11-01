# 提交到本地 GitLab

**仓库地址**：http://192.168.100.169:8929/root/ner-api

---

## 本次提交范围

只提交以下内容，其余一律不提交：

| 提交 ✅ | 说明 |
|--------|------|
| `GIT_GUIDE.md` | 本提交指南 |
| `requirements.txt` | Python 依赖声明 |
| `提取标签API手册-v26_06_11.md` | 接口使用手册 |
| `api/` | 后端源码（FastAPI 服务、DB 层、SQL schema 等） |
| `web/` | 前端源码（Vue 工程，**排除依赖与构建产物**） |
| `docs/` | 工具手册文档（ioc-finder / iocextract） |

依赖、构建产物、敏感信息均已由 `.gitignore` 自动排除，无需手动处理：

| 自动排除 ❌ | 规则来源 |
|-----------|----------|
| `web/node_modules/` | 前端依赖，`npm install` 自动还原 |
| `web/dist/` | 前端构建产物，`npm run build` 自动生成 |
| `api/.env` | 含数据库密码、JWT 密钥、LLM API Key 等敏感信息 |
| `api/**/__pycache__/` | Python 字节码缓存 |
| `.venv/` `models/` `results/` 等 | 虚拟环境 / 模型 / 临时数据 |

---

## 首次推送（已完成，仅供参考）

```bash
git init
git config user.name "root"
git config user.email "root@gitlab.local"

git remote add origin http://root:<密码>@192.168.100.169:8929/root/ner-api.git
```

---

## 本次提交操作

```bash
# 只添加约定范围内的内容（.gitignore 会自动跳过依赖 / 产物 / .env）
git add GIT_GUIDE.md requirements.txt "提取标签API手册-v26_06_11.md" api/ web/ docs/

# 确认暂存内容里没有 node_modules / dist / .env
git status

git commit -m "feat: 缓存复用 TTL 策略 + 缓存管理页 + 后台定时清理"
git push
```

> 若是首次推送到空仓库，最后一步用：`git push -u origin main`

---

## 日常提交

```bash
git add <修改的文件或目录>
git commit -m "说明本次改动"
git push
```

提交前务必执行 `git status` 复核暂存列表，确保没有把 `api/.env`、`web/node_modules/`、`web/dist/` 误加入。

---

## GitLab 信息

| 项目 | 值 |
|------|----|
| Web 地址 | http://192.168.100.169:8929 |
| 用户名 | root |
| 密码 | `D7K6NSwQAt+00/KY1+r7tgj+yJLZekZvakQE0oik86s=` |
| 仓库 | http://192.168.100.169:8929/root/ner-api |
