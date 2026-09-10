#!/bin/bash

# AI 漫剧平台 - 停止脚本

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "🛑 停止 AI 漫剧平台..."
echo ""

# 读取 PID
if [ -f .backend.pid ]; then
    BACKEND_PID=$(cat .backend.pid)
    if kill -0 $BACKEND_PID 2>/dev/null; then
        CMDLINE=$(ps -p $BACKEND_PID -o args= 2>/dev/null || true)
        if echo "$CMDLINE" | grep -q "app.main:app"; then
            echo "   停止后端 (PID: $BACKEND_PID)"
            kill $BACKEND_PID 2>/dev/null || true
        fi
    fi
    rm -f .backend.pid
fi

if [ -f .frontend.pid ]; then
    FRONTEND_PID=$(cat .frontend.pid)
    if kill -0 $FRONTEND_PID 2>/dev/null; then
        echo "   停止前端 (PID: $FRONTEND_PID)"
        kill $FRONTEND_PID 2>/dev/null || true
    fi
    rm -f .frontend.pid
fi

# 仅清理属于本项目的残留进程
pkill -f "$PROJECT_ROOT/backend.*uvicorn app.main:app" 2>/dev/null || true
pkill -f "$PROJECT_ROOT/frontend.*vite" 2>/dev/null || true

echo ""
echo "✅ 服务已停止"
