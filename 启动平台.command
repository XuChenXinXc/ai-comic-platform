#!/bin/bash
# AI 漫剧平台 - macOS 双击启动文件
# 使用方法：双击此文件即可启动平台

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 检查启动脚本是否存在
if [ ! -f "start-mac.sh" ]; then
    echo "❌ 找不到 start-mac.sh 启动脚本"
    echo "请确保此文件在项目根目录"
    echo ""
    echo "按任意键退出..."
    read -n 1
    exit 1
fi

# 设置执行权限
chmod +x start-mac.sh stop-mac.sh

# 运行启动脚本
./start-mac.sh

# 保持终端窗口打开
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "💡 提示："
echo "   - 终端窗口保持打开状态时，平台持续运行"
echo "   - 双击 '停止平台.command' 停止服务"
echo "   - 或按 Ctrl+C 停止（需要手动运行 ./stop-mac.sh 清理）"
echo ""
echo "按任意键关闭此终端窗口..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
read -n 1
