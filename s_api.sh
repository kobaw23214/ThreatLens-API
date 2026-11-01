#!/bin/bash
# ─── NER API 后端管理脚本 ────────────────────────────────────────────────────

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT"

PID_FILE="$PROJECT_ROOT/.api.pid"
LOG_FILE="$PROJECT_ROOT/.api.log"

API_PORT=$(grep '^API_PORT=' api/.env 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')
API_PORT=${API_PORT:-8036}

_ensure_postgres() {
    if ! docker ps --filter "name=ner_postgres" --filter "status=running" --format "{{.Names}}" | grep -q ner_postgres; then
        echo "⚠️  ner_postgres 容器未运行，正在启动..."
        docker start ner_postgres 2>/dev/null || {
            echo "❌ 容器不存在，请先执行首次部署步骤（见 README.md）"
            exit 1
        }
        sleep 2
    fi
}

_is_running() {
    [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

do_start() {
    if _is_running; then
        echo "⚠️  API 已在运行中 (PID $(cat "$PID_FILE")，端口 $API_PORT)"
        exit 0
    fi
    # 每次启动前自动同步依赖（已安装的包直接跳过，不会重复安装）
    if [ -f ".venv/bin/pip" ] && [ -f "requirements.txt" ]; then
        echo "📦 同步依赖..."
        .venv/bin/pip install -q -r requirements.txt && echo "✅ 依赖就绪"
    fi
    _ensure_postgres
    echo "🚀 启动 NER API 后端 (端口 $API_PORT)..."
    nohup .venv/bin/python -m uvicorn api.main:app \
        --host 0.0.0.0 \
        --port "$API_PORT" \
        >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    sleep 2
    if _is_running; then
        echo "✅ 启动成功 (PID $(cat "$PID_FILE"))  日志: $LOG_FILE"
    else
        echo "❌ 启动失败，请查看日志: $LOG_FILE"
        rm -f "$PID_FILE"
        exit 1
    fi
}

do_stop() {
    if ! _is_running; then
        echo "⚠️  API 未在运行"
        rm -f "$PID_FILE"
        return
    fi
    local pid
    pid=$(cat "$PID_FILE")
    echo "🛑 停止 API (PID $pid)..."
    kill "$pid" 2>/dev/null
    # 等待进程退出，最多 10 秒
    local i=0
    while kill -0 "$pid" 2>/dev/null && [ $i -lt 10 ]; do
        sleep 1; i=$((i+1))
    done
    if kill -0 "$pid" 2>/dev/null; then
        echo "⚠️  进程未响应，强制终止..."
        kill -9 "$pid" 2>/dev/null
    fi
    rm -f "$PID_FILE"
    echo "✅ 已停止"
}

do_install() {
    echo "📦 安装后端依赖 (.venv/bin/pip install -r requirements.txt)..."
    if [ ! -f ".venv/bin/pip" ]; then
        echo "❌ 未找到 .venv/bin/pip，请先创建虚拟环境: python3 -m venv .venv"
        exit 1
    fi
    .venv/bin/pip install -r requirements.txt
    echo "✅ 依赖安装完成"
}

do_restart() {
    echo "🔄 重启 NER API..."
    do_stop
    sleep 1
    do_start
}

do_log() {
    local n="${1:-100}"
    if ! [[ "$n" =~ ^[0-9]+$ ]]; then
        echo "❌ 行数必须是数字，例如: $0 log 50"
        exit 1
    fi
    if [ ! -f "$LOG_FILE" ]; then
        echo "⚠️  日志文件不存在: $LOG_FILE"
        exit 1
    fi
    echo "📜 实时跟踪日志最后 $n 行 ($LOG_FILE)，按 Ctrl+C 退出..."
    tail -n "$n" -f "$LOG_FILE"
}

do_status() {
    if _is_running; then
        local pid
        pid=$(cat "$PID_FILE")
        echo "✅ API 运行中 (PID $pid，端口 $API_PORT)"
        echo "   日志文件: $LOG_FILE"
        echo "   最后 5 行日志:"
        tail -5 "$LOG_FILE" 2>/dev/null | sed 's/^/   /'
    else
        echo "⏹  API 未运行"
        rm -f "$PID_FILE"
    fi
}

# ─── 入口 ────────────────────────────────────────────────────────────────────
case "${1:-start}" in
    start)   do_start   ;;
    stop)    do_stop    ;;
    restart) do_restart ;;
    status)  do_status  ;;
    install) do_install ;;
    log)     do_log "$2" ;;
    *)
        echo "用法: $0 {start|stop|restart|status|install|log}"
        echo "  start   启动 API 后端（默认）"
        echo "  stop    停止 API 后端"
        echo "  restart 重启 API 后端"
        echo "  status  查看运行状态及最近日志"
        echo "  install 安装/更新后端依赖 (pip install -r requirements.txt)"
        echo "  log     实时跟踪日志 (tail -f)，默认 100 行，可指定行数如: $0 log 50"
        exit 1
        ;;
esac
