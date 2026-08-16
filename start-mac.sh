#!/bin/bash
# AI 漫剧平台 - macOS 启动脚本

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "🚀 启动 AI 漫剧平台..."

# 1. 检查 Python 版本
if ! command -v python3.11 &> /dev/null; then
    echo -e "${RED}❌ Python 3.11 未安装${NC}"
    echo "请运行: brew install python@3.11"
    exit 1
fi

# 2. 检查 FFmpeg
if ! command -v ffmpeg &> /dev/null; then
    echo -e "${RED}❌ FFmpeg 未安装${NC}"
    echo "请运行: brew install ffmpeg"
    exit 1
fi

if ! command -v ffprobe &> /dev/null; then
    echo -e "${RED}❌ ffprobe 未安装${NC}"
    echo "请运行: brew install ffmpeg"
    exit 1
fi

# 3. 检查 Node.js
if ! command -v node &> /dev/null; then
    echo -e "${RED}❌ Node.js 未安装${NC}"
    echo "请运行: brew install node"
    exit 1
fi

NODE_VERSION=$(node --version | sed 's/v//' | cut -d. -f1)
if [ "$NODE_VERSION" -lt 20 ]; then
    echo -e "${YELLOW}⚠️  Node.js 版本过低 (需要 >= 20.0.0)${NC}"
    echo "当前版本: $(node --version)"
    echo "请运行: brew upgrade node"
    exit 1
fi

# 4. 检查后端虚拟环境
if [ ! -d "backend/.venv" ]; then
    echo -e "${YELLOW}⚠️  后端虚拟环境不存在，正在创建...${NC}"
    cd backend
    python3.11 -m venv .venv
    source .venv/bin/activate
    pip install --upgrade pip
    pip install -e .
    cd ..
    echo -e "${GREEN}✅ 后端虚拟环境创建完成${NC}"
fi

# 5. 激活虚拟环境
source backend/.venv/bin/activate

# 6. 检查 .env 文件
if [ ! -f ".env" ]; then
    echo -e "${YELLOW}⚠️  .env 文件不存在，正在从模板创建...${NC}"
    cp .env.example .env
    echo -e "${GREEN}✅ 已创建 .env 文件${NC}"
    echo -e "${YELLOW}请编辑 .env 文件配置 API Key（使用环境变量方式）${NC}"
    echo ""
    echo "示例："
    echo "  TEXT_API_KEY=sk-..."
    echo "  IMAGE_API_KEY=sk-..."
    echo "  VIDEO_API_KEY=sk-..."
    echo ""
    read -p "按回车键继续..."
fi

# 7. 检查前端依赖
if [ ! -d "frontend/node_modules" ]; then
    echo -e "${YELLOW}⚠️  前端依赖未安装，正在安装...${NC}"
    cd frontend
    npm install
    cd ..
    echo -e "${GREEN}✅ 前端依赖安装完成${NC}"
fi

# 8. 数据库迁移
echo "📦 执行数据库迁移..."
cd backend
if ! alembic upgrade head 2>&1; then
    echo -e "${RED}❌ 数据库迁移失败${NC}"
    exit 1
fi
cd ..
echo -e "${GREEN}✅ 数据库迁移完成${NC}"

# 9. 启动后端
echo "🔧 启动后端服务..."
cd backend
uvicorn app.main:app --host 127.0.0.1 --port 8012 > ../backend.log 2>&1 &
BACKEND_PID=$!
cd ..
echo -e "${GREEN}✅ 后端服务启动 (PID: $BACKEND_PID)${NC}"

# 等待后端健康检查
echo "⏳ 等待后端服务就绪..."
for i in {1..30}; do
    if curl -s http://127.0.0.1:8012/api/health > /dev/null 2>&1; then
        echo -e "${GREEN}✅ 后端服务就绪${NC}"
        break
    fi
    if [ $i -eq 30 ]; then
        echo -e "${RED}❌ 后端服务启动超时${NC}"
        echo "查看日志: tail -f backend.log"
        kill $BACKEND_PID 2>/dev/null || true
        exit 1
    fi
    sleep 1
done

# 10. 启动前端
echo "🎨 启动前端服务..."
cd frontend
npm run dev > ../frontend.log 2>&1 &
FRONTEND_PID=$!
cd ..
echo -e "${GREEN}✅ 前端服务启动 (PID: $FRONTEND_PID)${NC}"

# 等待前端就绪
echo "⏳ 等待前端服务就绪..."
for i in {1..30}; do
    if curl -s http://127.0.0.1:3000 > /dev/null 2>&1; then
        echo -e "${GREEN}✅ 前端服务就绪${NC}"
        break
    fi
    if [ $i -eq 30 ]; then
        echo -e "${YELLOW}⚠️  前端服务可能需要更长时间启动${NC}"
        break
    fi
    sleep 1
done

# 11. 显示信息
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "${GREEN}🎉 AI 漫剧平台启动成功！${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "🌐 访问地址:"
echo "   前端: http://127.0.0.1:3000"
echo "   后端: http://127.0.0.1:8012"
echo "   API 文档: http://127.0.0.1:8012/docs"
echo ""
echo "📝 日志文件:"
echo "   后端: tail -f backend.log"
echo "   前端: tail -f frontend.log"
echo ""
echo "⚠️  注意事项:"
echo "   1. API Key 配置方式："
echo "      - 推荐在 Web UI 创建 Provider 时直接填写 'API Key' 字段"
echo "        （使用 macOS Keychain 加密保存，重启后仍可用）"
echo "      - 也可以使用 'API Key 环境变量' 字段兼容旧配置"
echo ""
echo "   2. 修改 .env 环境变量后需要重启平台；直接填写 API Key 保存后即时生效"
echo ""
echo "🛑 停止服务:"
echo "   运行: ./stop-mac.sh"
echo "   或按 Ctrl+C 然后手动停止进程"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# 保存 PID 到文件
echo "$BACKEND_PID" > .run/backend.pid
echo "$FRONTEND_PID" > .run/frontend.pid
mkdir -p .run

# 12. 等待用户中断
trap cleanup EXIT INT TERM

cleanup() {
    echo ""
    echo "🛑 正在停止服务..."

    if [ -n "$BACKEND_PID" ] && kill -0 $BACKEND_PID 2>/dev/null; then
        kill $BACKEND_PID
        echo "✅ 后端服务已停止"
    fi

    if [ -n "$FRONTEND_PID" ] && kill -0 $FRONTEND_PID 2>/dev/null; then
        kill $FRONTEND_PID
        echo "✅ 前端服务已停止"
    fi

    rm -f .run/backend.pid .run/frontend.pid
    echo "👋 再见！"
}

# 保持脚本运行
wait
