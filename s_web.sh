#!/bin/bash
# ─── NER Web 前端管理脚本 ────────────────────────────────────────────────────

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT/web"

PID_FILE="$PROJECT_ROOT/.web.pid"
LOG_FILE="$PROJECT_ROOT/.web.log"
WEB_PORT=5466

_is_running() {
    [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

do_start() {
    if _is_running; then
        echo "⚠️  前端已在运行中 (PID $(cat "$PID_FILE")，端口 $WEB_PORT)"
        exit 0
    fi
    echo "🌐 启动 NER 前端 (端口 $WEB_PORT)..."
    nohup npm run dev >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    sleep 2
    if _is_running; then
        echo "✅ 启动成功 (PID $(cat "$PID_FILE"))  http://localhost:$WEB_PORT"
        echo "   日志: $LOG_FILE"
    else
        echo "❌ 启动失败，请查看日志: $LOG_FILE"
        rm -f "$PID_FILE"
        exit 1
    fi
}

do_stop() {
    if ! _is_running; then
        echo "⚠️  前端未在运行"
        rm -f "$PID_FILE"
        return
    fi
    local pid
    pid=$(cat "$PID_FILE")
    echo "🛑 停止前端 (PID $pid)..."
    # vite 会派生子进程，一并终止整个进程组
    kill -- -"$(ps -o pgid= -p "$pid" | tr -d ' ')" 2>/dev/null || kill "$pid" 2>/dev/null
    local i=0
    while kill -0 "$pid" 2>/dev/null && [ $i -lt 10 ]; do
        sleep 1; i=$((i+1))
    done
    if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null
    fi
    rm -f "$PID_FILE"
    echo "✅ 已停止"
}

do_install() {
    echo "📦 安装前端依赖 (npm install)..."
    npm install
    echo "✅ 依赖安装完成"
}

do_restart() {
    echo "🔄 重启前端..."
    do_stop
    sleep 1
    do_start
}

do_status() {
    if _is_running; then
        local pid
        pid=$(cat "$PID_FILE")
        echo "✅ 前端运行中 (PID $pid，端口 $WEB_PORT)"
        echo "   访问地址: http://localhost:$WEB_PORT"
        echo "   日志文件: $LOG_FILE"
        echo "   最后 5 行日志:"
        tail -5 "$LOG_FILE" 2>/dev/null | sed 's/^/   /'
    else
        echo "⏹  前端未运行"
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
    *)
        echo "用法: $0 {start|stop|restart|status|install}"
        echo "  start   启动前端（默认）"
        echo "  stop    停止前端"
        echo "  restart 重启前端"
        echo "  status  查看运行状态及最近日志"
        echo "  install 安装/更新前端依赖 (npm install)"
        exit 1
        ;;
esac
