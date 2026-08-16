#!/bin/bash
# AI 漫剧平台 - macOS 停止脚本

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "🛑 停止 AI 漫剧平台..."

# 1. 从 PID 文件读取进程 ID
BACKEND_PID=""
FRONTEND_PID=""

if [ -f ".run/backend.pid" ]; then
    BACKEND_PID=$(cat .run/backend.pid)
fi

if [ -f ".run/frontend.pid" ]; then
    FRONTEND_PID=$(cat .run/frontend.pid)
fi

# 2. 停止后端服务
if [ -n "$BACKEND_PID" ] && kill -0 $BACKEND_PID 2>/dev/null; then
    echo "🔧 停止后端服务 (PID: $BACKEND_PID)..."
    kill $BACKEND_PID
    # 等待进程退出
    for i in {1..10}; do
        if ! kill -0 $BACKEND_PID 2>/dev/null; then
            break
        fi
        sleep 0.5
    done
    # 强制杀死（如果还在运行）
    if kill -0 $BACKEND_PID 2>/dev/null; then
        echo -e "${YELLOW}⚠️  后端服务未响应，强制停止...${NC}"
        kill -9 $BACKEND_PID 2>/dev/null || true
    fi
    echo -e "${GREEN}✅ 后端服务已停止${NC}"
else
    echo -e "${YELLOW}⚠️  后端服务未运行${NC}"
fi

# 3. 停止前端服务
if [ -n "$FRONTEND_PID" ] && kill -0 $FRONTEND_PID 2>/dev/null; then
    echo "🎨 停止前端服务 (PID: $FRONTEND_PID)..."
    kill $FRONTEND_PID
    # 等待进程退出
    for i in {1..10}; do
        if ! kill -0 $FRONTEND_PID 2>/dev/null; then
            break
        fi
        sleep 0.5
    done
    # 强制杀死（如果还在运行）
    if kill -0 $FRONTEND_PID 2>/dev/null; then
        echo -e "${YELLOW}⚠️  前端服务未响应，强制停止...${NC}"
        kill -9 $FRONTEND_PID 2>/dev/null || true
    fi
    echo -e "${GREEN}✅ 前端服务已停止${NC}"
else
    echo -e "${YELLOW}⚠️  前端服务未运行${NC}"
fi

# 4. 清理残留进程（通过端口查找）
echo "🧹 清理残留进程..."

# 查找占用 8012 端口的进程（后端）
BACKEND_PORT_PID=$(lsof -ti:8012 2>/dev/null || true)
if [ -n "$BACKEND_PORT_PID" ]; then
    echo -e "${YELLOW}⚠️  发现占用端口 8012 的进程 (PID: $BACKEND_PORT_PID)，正在停止...${NC}"
    kill $BACKEND_PORT_PID 2>/dev/null || true
    sleep 1
    if kill -0 $BACKEND_PORT_PID 2>/dev/null; then
        kill -9 $BACKEND_PORT_PID 2>/dev/null || true
    fi
    echo -e "${GREEN}✅ 端口 8012 已释放${NC}"
fi

# 查找占用 3000 端口的进程（前端）
FRONTEND_PORT_PID=$(lsof -ti:3000 2>/dev/null || true)
if [ -n "$FRONTEND_PORT_PID" ]; then
    echo -e "${YELLOW}⚠️  发现占用端口 3000 的进程 (PID: $FRONTEND_PORT_PID)，正在停止...${NC}"
    kill $FRONTEND_PORT_PID 2>/dev/null || true
    sleep 1
    if kill -0 $FRONTEND_PORT_PID 2>/dev/null; then
        kill -9 $FRONTEND_PORT_PID 2>/dev/null || true
    fi
    echo -e "${GREEN}✅ 端口 3000 已释放${NC}"
fi

# 5. 删除 PID 文件
rm -f .run/backend.pid .run/frontend.pid

echo ""
echo -e "${GREEN}✅ AI 漫剧平台已完全停止${NC}"
