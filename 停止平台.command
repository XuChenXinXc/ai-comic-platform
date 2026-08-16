#!/bin/bash
# AI 漫剧平台 - macOS 双击停止文件
# 使用方法：双击此文件即可停止平台

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 检查停止脚本是否存在
if [ ! -f "stop-mac.sh" ]; then
    echo "❌ 找不到 stop-mac.sh 停止脚本"
    echo "请确保此文件在项目根目录"
    echo ""
    echo "按任意键退出..."
    read -n 1
    exit 1
fi

# 设置执行权限
chmod +x stop-mac.sh

# 运行停止脚本
./stop-mac.sh

# 保持终端窗口打开以显示结果
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "💡 提示："
echo "   - 平台已停止"
echo "   - 双击 '启动平台.command' 可以重新启动"
echo ""
echo "按任意键关闭此终端窗口..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
read -n 1
