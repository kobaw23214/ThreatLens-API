#!/bin/bash
# ─── 构建 / 打包部署包 (在开发机 / 联网机器上运行) ────────────────────────────
#
# 用法：
#   ./build_deploy.sh build [--no-image]   构建并归集部署内容到 w_deploy/
#   ./build_deploy.sh zip   [--model]      把 w_deploy/ 打包成 w_deploy.zip
#
# build 子命令做的事：
#   1. 构建前端静态产物 (web/dist)            —— 现场无需 Node
#   2. 把 后端代码 / 前端产物 / 文档 / 配置 归集到 w_deploy/
#   3. 构建「仅含依赖」的运行时 Docker 镜像
#   4. docker save 导出镜像为 w_deploy/ner-api.tar
#   （--no-image：仅同步代码/前端/配置，跳过第 3、4 步镜像构建与导出）
#
# zip 子命令做的事：
#   把整个 w_deploy/ 打包成 w_deploy.zip，方便发给现场同事。
#   默认排除 w_deploy/models/（模型体积大，通常单独提供）；
#   加 --model 则把 models/ 一并打进压缩包。
#
# 现场更新规则：
#   · 只改了 api/ 代码或 web/ 前端 → 重新跑 build（或只同步对应目录），无需关心镜像
#   · 改了 requirements.txt（新增/升级 Python 依赖）→ 必须重新 build 生成新 ner-api.tar
# ─────────────────────────────────────────────────────────────────────────────
set -e

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT"

DEPLOY_DIR="$PROJECT_ROOT/w_deploy"
IMAGE="ner-api:runtime"
TAR_FILE="$DEPLOY_DIR/ner-api.tar"
ZIP_FILE="$PROJECT_ROOT/w_deploy.zip"

# ─── build：构建并归集部署内容 ────────────────────────────────────────────────
do_build() {
    local skip_image=0
    for arg in "$@"; do
        case "$arg" in
            --no-image) skip_image=1; echo "ℹ️  --no-image：本次仅同步代码/前端/配置，跳过镜像构建与导出" ;;
            *) echo "⚠️  build 未知参数: $arg（忽略）" ;;
        esac
    done

    mkdir -p "$DEPLOY_DIR"

    # ── 1. 构建前端 ──────────────────────────────────────────────────────────
    echo ""
    echo "🌐 [1/4] 构建前端静态产物..."
    if [ ! -d "web/node_modules" ]; then
        echo "   📦 未检测到 node_modules，执行 npm install..."
        ( cd web && npm install )
    fi
    ( cd web && npm run build )
    echo "   ✅ 前端构建完成: web/dist"

    # ── 2. 归集挂载内容到 w_deploy/ ──────────────────────────────────────────
    echo ""
    echo "📁 [2/4] 归集挂载内容到 w_deploy/ ..."

    # 后端代码（排除缓存与本地 .env，.env 单独放到顶层）
    rsync -a --delete \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='.env' \
        api/ "$DEPLOY_DIR/api/"
    echo "   ✅ 后端代码 -> w_deploy/api/"

    # 前端产物
    mkdir -p "$DEPLOY_DIR/web"
    rsync -a --delete web/dist/ "$DEPLOY_DIR/web/dist/"
    echo "   ✅ 前端产物 -> w_deploy/web/dist/"

    # 手册文档
    rsync -a --delete docs/ "$DEPLOY_DIR/docs/"
    echo "   ✅ 手册文档 -> w_deploy/docs/"

    # 配置文件（.env 运行时挂载进容器；始终用 api/.env 同步覆盖 w_deploy/.env）
    if [ -f "api/.env" ]; then
        cp api/.env "$DEPLOY_DIR/.env"
        echo "   ✅ 配置文件 -> w_deploy/.env （已用 api/.env 同步覆盖）"
    else
        echo "   ⚠️  未找到 api/.env，无法同步 w_deploy/.env，请手动放置"
    fi

    if [ "$skip_image" -eq 1 ]; then
        echo ""
        echo "✅ 完成（已跳过镜像）。w_deploy/ 内代码/前端/配置已更新。"
        return 0
    fi

    # ── 3. 构建运行时镜像 ────────────────────────────────────────────────────
    echo ""
    echo "🐳 [3/4] 构建运行时镜像 $IMAGE （架构: $(uname -m)）..."
    docker build -t "$IMAGE" .
    echo "   ✅ 镜像构建完成"

    # ── 4. 导出镜像为 tar ────────────────────────────────────────────────────
    echo ""
    echo "💾 [4/4] 导出镜像 -> $TAR_FILE ..."
    [ -f "$TAR_FILE" ] && rm -f "$TAR_FILE"
    docker save -o "$TAR_FILE" "$IMAGE"
    echo "   ✅ 导出完成，大小: $(du -sh "$TAR_FILE" | cut -f1)"

    echo ""
    echo "🎉 build 完成！部署内容已就绪: $DEPLOY_DIR"
    echo "   下一步：打包整个 w_deploy/ 发给现场："
    echo "     ./build_deploy.sh zip            # 默认不含模型"
    echo "     ./build_deploy.sh zip --model    # 含模型"
}

# ─── zip：把 w_deploy/ 打包成 w_deploy.zip ────────────────────────────────────
do_zip() {
    local include_models=0
    for arg in "$@"; do
        case "$arg" in
            --model|--models) include_models=1 ;;
            *) echo "⚠️  zip 未知参数: $arg（忽略）" ;;
        esac
    done

    if [ ! -d "$DEPLOY_DIR" ]; then
        echo "❌ 未找到 $DEPLOY_DIR，请先执行: ./build_deploy.sh build"
        exit 1
    fi
    if ! command -v zip >/dev/null 2>&1; then
        echo "❌ 未检测到 zip 命令，请先安装（macOS 自带；Linux: apt/yum install zip）"
        exit 1
    fi

    [ -f "$ZIP_FILE" ] && rm -f "$ZIP_FILE"

    local exclude_opts=()
    if [ "$include_models" -eq 1 ]; then
        echo "📦 正在打包 w_deploy/ （含 models）-> $ZIP_FILE ..."
    else
        echo "📦 正在打包 w_deploy/ （已排除 models）-> $ZIP_FILE ..."
        exclude_opts=("-x" "w_deploy/models/*")
    fi

    # 从工程根目录打包，使压缩包内顶层目录为 w_deploy/
    ( cd "$PROJECT_ROOT" && zip -r -q "$ZIP_FILE" w_deploy "${exclude_opts[@]}" )

    echo "   ✅ 打包完成: $ZIP_FILE （大小: $(du -sh "$ZIP_FILE" | cut -f1)）"
    echo "   现场操作：解压后  cd w_deploy && ./deploy.sh start"
}

# ─── 入口命令解析 ─────────────────────────────────────────────────────────────
CMD="${1:-usage}"
shift 2>/dev/null || true

case "$CMD" in
    build) do_build "$@" ;;
    zip)   do_zip   "$@" ;;
    *)
        echo "用法: $0 {build|zip} [选项]"
        echo "  build [--no-image]  : 构建前端 + 归集到 w_deploy/ + 构建并导出运行时镜像"
        echo "                        --no-image 仅同步代码/前端/配置，跳过镜像构建与导出"
        echo "  zip   [--model]     : 把 w_deploy/ 打包成 w_deploy.zip"
        echo "                        默认排除 models/；加 --model 则一并打包模型"
        exit 1
        ;;
esac
