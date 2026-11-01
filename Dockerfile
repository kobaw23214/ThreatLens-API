# ─────────────────────────────────────────────────────────────────────────────
# 仅包含「系统依赖 + Python 依赖」的运行时镜像（不含业务代码 / 前端产物 / 模型）
#
# 设计目标：代码与镜像解耦。
#   · 后端代码 api/        → 运行时挂载到 /app/api
#   · 前端产物 web/dist    → 运行时挂载到 /app/web/dist
#   · 配置     .env        → 运行时挂载到 /app/api/.env
#   · 模型     models/     → 运行时挂载到 /app/models
#
# 因此：只改代码 / 前端 / 配置 → 不需要重打镜像，同步文件 + 重启容器即可。
#       只有 requirements.txt 变化（新增/升级 Python 依赖）才需要重新 build 本镜像。
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim

# APT_MIRROR（可选 build-arg）：deb.debian.org 直连经常不通/中途断连时换国内源，例如
#   --build-arg APT_MIRROR=https://mirrors.aliyun.com
# 值为「源基地址」(含协议,不含 /debian 路径)。留空则用官方源。
# 同时兼容 deb822(.sources) 与经典(.list) 两种格式。
ARG APT_MIRROR=""
RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|http://deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
        sed -i "s|http://deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list || true; \
    fi

# 系统依赖
#   libgomp1/libpq5/gcc/g++/git …… Python 依赖编译与运行
#   xvfb/x11vnc/novnc/websockify … TI 人工登录「网页远程桌面」（无显示器容器内跑有头浏览器）
#   fonts-noto-cjk …………………… 远程桌面里平台登录页的中文渲染（缺了全是方框）
# Acquire::Retries：国内源偶发中途断连时自动重试
RUN apt-get update -o Acquire::Retries=5 \
    && apt-get install -y --no-install-recommends -o Acquire::Retries=5 \
        libgomp1 \
        libpq5 \
        gcc \
        g++ \
        git \
        xvfb \
        x11vnc \
        novnc \
        websockify \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 安装 Python 依赖
# 统一使用 PyTorch CPU 源安装 torch（x86_64 与 aarch64 该源均提供 CPU-only wheel）。
# 注意：自 PyTorch 2.11 起，PyPI 上的 aarch64 torch 默认携带 CUDA 依赖（cudnn/nccl 等
# 合计 1.5GB+），对纯 CPU 的离线 ARM 部署是无用的巨大体积负担。改用 whl/cpu 源可彻底避免，
# 镜像显著变小、离线分发更轻。
COPY requirements.txt .
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt

# TI 联网情报·采集/登录浏览器：Chromium 及系统依赖打入镜像（离线内网不现下载）。
# 放独立路径并固定 PLAYWRIGHT_BROWSERS_PATH：/app/ti_data 运行时被宿主目录挂载
# （会话种子/留证持久化），浏览器不能被挂载层遮蔽。
# 镜像因此增大约 400MB。
#
# PLAYWRIGHT_DOWNLOAD_HOST（可选 build-arg）：官方源 cdn.playwright.dev 实际指向
# Azure（playwright.download.prss.microsoft.com），国内直连经常「握手成功但传输卡在 0%」。
# 需要时传镜像源，例如：
#   --build-arg PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright
# 留空则用官方源（含官方自带的多源回退）。另一种办法是给 build 传代理，见 prepare.sh --proxy。
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
ARG PLAYWRIGHT_DOWNLOAD_HOST=""
RUN apt-get update \
    && if [ -z "$PLAYWRIGHT_DOWNLOAD_HOST" ]; then unset PLAYWRIGHT_DOWNLOAD_HOST; fi \
    && python -m playwright install chromium --with-deps \
    && rm -rf /var/lib/apt/lists/*

# ─── 运行时挂载说明（均不打入镜像）──────────────────────────────────────────
#   docker run ... \
#     -v <w_deploy>/api:/app/api \
#     -v <w_deploy>/web/dist:/app/web/dist \
#     -v <w_deploy>/.env:/app/api/.env \
#     -v <models>:/app/models \
#     -v <w_deploy>/ti_data:/app/ti_data
# 详见 w_deploy/deploy.sh
# ─────────────────────────────────────────────────────────────────────────────

# 暴露端口
EXPOSE 8036

# 启动命令（api/ 在运行时挂载到 /app/api）
CMD ["python", "-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8036"]
