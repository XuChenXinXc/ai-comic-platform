#!/bin/bash

# AI 漫剧平台 - 停止脚本

echo "🛑 停止 AI 漫剧平台..."
echo ""

# 读取 PID
if [ -f .backend.pid ]; then
    BACKEND_PID=$(cat .backend.pid)
    if kill -0 $BACKEND_PID 2>/dev/null; then
        echo "   停止后端 (PID: $BACKEND_PID)"
        kill $BACKEND_PID
    fi
    rm .backend.pid
fi

if [ -f .frontend.pid ]; then
    FRONTEND_PID=$(cat .frontend.pid)
    if kill -0 $FRONTEND_PID 2>/dev/null; then
        echo "   停止前端 (PID: $FRONTEND_PID)"
        kill $FRONTEND_PID
    fi
    rm .frontend.pid
fi

# 清理可能残留的进程
pkill -f "uvicorn app.main:app" 2>/dev/null || true
pkill -f "vite.*3000" 2>/dev/null || true

echo ""
echo "✅ 服务已停止"
