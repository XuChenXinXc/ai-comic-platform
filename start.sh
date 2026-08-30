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

# 加载 .env 配置（如果存在）。使用 `set -a` 让 source 的变量自动 export，
# 比 `xargs` 解析对含空格 / $ / 引号的值更安全。
if [ -f ".env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "./.env"
    set +a
fi

# 从环境变量读取配置，提供安全默认值。与 restart.ps1 / .env 保持一致。
APP_HOST="${APP_HOST:-127.0.0.1}"
APP_PORT="${APP_PORT:-8012}"
FRONTEND_HOST="${FRONTEND_HOST:-127.0.0.1}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"

# 检查 Python 环境。venv 的可执行文件目录随平台不同：
# POSIX 是 .venv/bin，Windows（含 Git Bash 下创建的 venv）是 .venv/Scripts。
if [ -x "backend/.venv/bin/python" ]; then
    VENV_PYTHON=".venv/bin/python"
elif [ -x "backend/.venv/Scripts/python.exe" ]; then
    VENV_PYTHON=".venv/Scripts/python.exe"
else
    echo "❌ 错误：后端虚拟环境不存在"
    echo "   请先运行: cd backend && python -m venv .venv"
    echo "   然后:     .venv/bin/pip install -e .      # macOS / Linux"
    echo "   或:       .venv/Scripts/pip install -e .  # Windows"
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
echo "🔧 启动后端服务器 (http://${APP_HOST}:${APP_PORT})..."
cd backend
"${VENV_PYTHON}" -m uvicorn app.main:app --reload --host "${APP_HOST}" --port "${APP_PORT}" > ../backend.log 2>&1 &
BACKEND_PID=$!
cd ..

echo "   后端 PID: $BACKEND_PID"
echo "   日志: backend.log"
echo ""

# 等待后端启动
sleep 3

# 检查后端是否启动成功
if curl -s "http://${APP_HOST}:${APP_PORT}/api/health" > /dev/null; then
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
echo "   后端 API: http://localhost:${APP_PORT}"
echo "   API 文档: http://localhost:${APP_PORT}/docs"
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
