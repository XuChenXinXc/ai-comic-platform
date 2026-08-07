#!/bin/bash

# AI 漫剧平台 - 快速启动脚本

set -e

echo "🎬 AI 漫剧平台 - 启动中..."
echo ""

# 检查是否在项目根目录
if [ ! -d "backend" ] || [ ! -d "frontend" ]; then
    echo "❌ 错误：请在项目根目录运行此脚本"
    exit 1
fi

# 加载 .env 配置（如果存在）
if [ -f ".env" ]; then
    export $(grep -v '^#' .env | xargs)
fi

# 从环境变量读取配置，提供安全默认值
BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_HOST="${FRONTEND_HOST:-127.0.0.1}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"

# 检查 Python 环境
if [ ! -d "backend/.venv" ]; then
    echo "❌ 错误：后端虚拟环境不存在"
    echo "   请先运行: cd backend && python -m venv .venv && .venv/Scripts/pip install -e ."
    exit 1
fi

# 检查前端依赖
if [ ! -d "frontend/node_modules" ]; then
    echo "❌ 错误：前端依赖未安装"
    echo "   请先运行: cd frontend && npm install"
    exit 1
fi

# 检查 FFmpeg
if ! command -v ffmpeg &> /dev/null; then
    echo "⚠️  警告：未检测到 FFmpeg，视频合成功能将不可用"
    echo "   Windows: 下载 FFmpeg 并添加到 PATH"
    echo "   Linux: sudo apt install ffmpeg"
    echo "   macOS: brew install ffmpeg"
    echo ""
fi

# 启动后端
echo "🔧 启动后端服务器 (http://${BACKEND_HOST}:${BACKEND_PORT})..."
cd backend
.venv/Scripts/python -m uvicorn app.main:app --reload --host "${BACKEND_HOST}" --port "${BACKEND_PORT}" > ../backend.log 2>&1 &
BACKEND_PID=$!
cd ..

echo "   后端 PID: $BACKEND_PID"
echo "   日志: backend.log"
echo ""

# 等待后端启动
sleep 3

# 检查后端是否启动成功
if curl -s "http://${BACKEND_HOST}:${BACKEND_PORT}/api/health" > /dev/null; then
    echo "✅ 后端启动成功"
else
    echo "❌ 后端启动失败，请查看 backend.log"
    kill $BACKEND_PID 2>/dev/null || true
    exit 1
fi

# 启动前端
echo ""
echo "🎨 启动前端服务器 (http://${FRONTEND_HOST}:${FRONTEND_PORT})..."
cd frontend
# 前端开发服务器的 host/port 配置通过 vite.config.ts 或环境变量控制
npm run dev > ../frontend.log 2>&1 &
FRONTEND_PID=$!
cd ..

echo "   前端 PID: $FRONTEND_PID"
echo "   日志: frontend.log"
echo ""

# 等待前端启动
sleep 5

# 检查前端是否启动成功
if curl -s http://127.0.0.1:3000 > /dev/null; then
    echo "✅ 前端启动成功"
else
    echo "❌ 前端启动失败，请查看 frontend.log"
    kill $BACKEND_PID $FRONTEND_PID 2>/dev/null || true
    exit 1
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🎉 AI 漫剧平台启动完成！"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "📍 访问地址："
echo "   前端界面: http://127.0.0.1:3000"
echo "   后端 API: http://localhost:8000"
echo "   API 文档: http://localhost:8000/docs"
echo ""
echo "📋 进程信息："
echo "   后端 PID: $BACKEND_PID"
echo "   前端 PID: $FRONTEND_PID"
echo ""
echo "🛑 停止服务："
echo "   kill $BACKEND_PID $FRONTEND_PID"
echo "   或者运行: ./stop.sh"
echo ""
echo "📖 使用指南: USAGE_GUIDE.md"
echo ""
echo "按 Ctrl+C 查看日志，或直接打开浏览器访问..."

# 保存 PID 到文件
echo $BACKEND_PID > .backend.pid
echo $FRONTEND_PID > .frontend.pid

# 跟踪日志
tail -f backend.log frontend.log
