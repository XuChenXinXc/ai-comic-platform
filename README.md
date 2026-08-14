# AI 漫剧本地平台

依据 `AI漫剧平台-V1核心设计文档` 从零实现的本地个人漫剧生产工作台。

## V1 能力

- 小说分析与可编辑剧本
- 角色、场景、物品提取与提示词
- 素材参考图生成与多版本管理
- 从小说或剧本提取分镜
- 分镜图像提示词、视频提示词与负向提示词
- 关键帧与单镜视频生成、多版本、上传替换和导出
- Text / Image / Video Provider 配置
- 持久化任务、批量部分成功、失败重试与异步视频恢复
- 无外部密钥时可用 Mock Provider 完整验收

V1 不包含配音、字幕、BGM 和最终视频合成。

## 平台支持

| 平台 | 状态 | 说明 |
|------|------|------|
| Windows 10/11 | ✅ 完全支持 | 推荐平台，所有功能可用 |
| macOS | ⚠️ 部分支持 | 需使用环境变量配置 API Key，详见 [macOS 兼容性指南](MACOS_COMPATIBILITY.md) |
| Linux | ⚠️ 未测试 | 理论上可用（与 macOS 类似限制） |

## 运行前提

### Windows

- Windows PowerShell 5.1 或 PowerShell 7
- Python 3.11
- Node.js 20.0 或更高版本
- FFmpeg 与 ffprobe 已加入 `PATH`（Mock 视频生成与可播放性校验需要）

### macOS

- macOS 10.15 (Catalina) 或更高版本
- Python 3.11（通过 Homebrew 安装）
- Node.js 20.0 或更高版本
- FFmpeg 与 ffprobe（通过 Homebrew 安装）

**安装依赖:**

```bash
# macOS
brew install python@3.11 node ffmpeg

# 验证安装
python3.11 --version
node --version
ffmpeg -version
```

## 快速开始

### Windows 一键启动

```powershell
cd D:\LyProject\Other\ai-comic-platform
.\setup.ps1
.\build.ps1
```

首次安装和构建后，双击根目录的 `一键启动.cmd`。以后每次双击都会先安全停止
本项目之前启动的进程，再运行数据库迁移并启动新的生产进程，健康检查通过后打开浏览器。

命令行等价操作：

```powershell
cd D:\LyProject\Other\ai-comic-platform
.\start.ps1
# 或在自动化环境中禁止打开浏览器
.\restart.ps1 -NoBrowser

# 停止本项目受控进程
.\stop.ps1
```

### macOS 启动

```bash
cd ~/Projects/ai-comic-platform

# 首次运行：安装依赖
cd backend
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
cd ..

cd frontend
npm install
cd ..

# 配置环境变量（重要！）
cp .env.example .env
nano .env  # 添加 TEXT_API_KEY 等配置

# 启动平台
chmod +x start-mac.sh
./start-mac.sh

# 停止平台
./stop-mac.sh
```

**⚠️ macOS 重要提示:**

macOS 不支持 Windows DPAPI 加密，必须使用环境变量配置 API Key：

1. 在 `.env` 文件中配置：
   ```env
   TEXT_API_KEY=sk-...
   IMAGE_API_KEY=sk-...
   VIDEO_API_KEY=sk-...
   ```

2. 在 Web UI 创建 Provider 时：
   - **不要填写** “API Key” 字段
   - 在 “API Key 环境变量” 字段填写 `TEXT_API_KEY` 等变量名

3. 修改 API Key 后需要重启平台

详细说明请参考 [MACOS_COMPATIBILITY.md](MACOS_COMPATIBILITY.md)

## 开发运行

### Windows

```powershell
cd D:\LyProject\Other\ai-comic-platform
.\start-dev.ps1
```

浏览器访问 `http://127.0.0.1:3000`，运行 `.\stop.ps1` 同时停止前后端。

### macOS

```bash
# 终端 1: 启动后端
cd ~/Projects/ai-comic-platform/backend
source .venv/bin/activate
uvicorn app.main:app --reload --host 127.0.0.1 --port 8012

# 终端 2: 启动前端
cd ~/Projects/ai-comic-platform/frontend
npm run dev
```

访问 `http://127.0.0.1:3000`

## 验证

### Windows

```powershell
cd D:\LyProject\Other\ai-comic-platform
.\check.ps1

cd .\frontend
npm.cmd run test:e2e
```

### macOS

```bash
cd ~/Projects/ai-comic-platform

# 后端测试
cd backend
source .venv/bin/activate
pytest

# 前端测试
cd ../frontend
npm run test
npm run test:e2e
```

## 数据与密钥

- 结构化数据：`workspace/platform.db`
- 项目媒体根目录：在”平台设置”页选择，默认 `workspace/projects/`
- 新建项目：`<项目媒体根目录>/<项目名>/`，同名时自动追加 ` (2)`、` (3)`

### Windows

- API Key 推荐在”接口配置”页直接填写，平台使用 Windows DPAPI 加密保存，重启后仍可用
- 数据库只保存 API Key 密文，不保存明文；接口列表和详情不会返回明文

### macOS / Linux

- API Key 必须通过环境变量配置（`.env` 文件）
- 在 Web UI 创建 Provider 时，”API Key 环境变量” 字段填写变量名
- 修改 API Key 需要重启平台

### 兼容旧配置

已有旧 Provider 仍兼容从项目 `.env` 读取 `TEXT_API_KEY`、`IMAGE_OPENAI_API_KEY`、
`VIDEO_VOLC_API_KEY` 等环境变量。修改旧 Provider 的 `.env` 后需要重启平台；
密钥不要填写到 Provider 参数或自定义请求头中。

### 项目存储

如需改到其他磁盘，可在”平台设置”页点击”选择目录”，无需重启平台；也可在尚未通过
页面保存设置时使用 `.env` 中的 `APP_PROJECTS_DIR=D:/AIComicProjects` 作为默认值。
请使用专用且较短的项目目录，不要指向磁盘根目录、平台源码或已有资料目录。
新目录只影响之后创建的项目，已有项目继续使用各自原目录，平台不会自动迁移历史素材。

新建”项目1”后会自动创建并分类保存媒体：

```text
workspace/projects/项目1/
├── 角色/
├── 物品/
├── 场景/
├── 分镜关键帧/
└── 分镜视频/
```

## 文档

- [macOS 兼容性指南](MACOS_COMPATIBILITY.md) - macOS 使用说明与跨平台适配方案
- [代码审查报告](CODE_REVIEW_REPORT.md) - 代码质量分析
- [修复报告](FIX_REPORT.md) - 问题修复记录
- [功能追踪](FEATURE_TRACKING.md) - 待实现功能
- [测试指南](backend/tests/README.md) - 测试环境配置

## 故障排查

### Windows 端口占用

如果启动失败，检查端口占用：

```powershell
# 查看占用端口的进程
netstat -ano | findstr :8012
netstat -ano | findstr :3000

# 结束进程（将 PID 替换为实际值）
taskkill /PID <PID> /F
```

### macOS 端口占用

```bash
# 查看占用端口的进程
lsof -ti:8012
lsof -ti:3000

# 结束进程
kill $(lsof -ti:8012)
kill $(lsof -ti:3000)
```

### FFmpeg 未找到

**Windows:**
1. 下载 FFmpeg: https://ffmpeg.org/download.html
2. 解压到 `C:\ffmpeg`
3. 添加 `C:\ffmpeg\bin` 到系统 PATH

**macOS:**
```bash
brew install ffmpeg
```

### 数据库迁移失败

```bash
# 删除数据库重新初始化（会丢失数据）
rm workspace/platform.db

# 重新运行迁移
cd backend
alembic upgrade head
```

## 许可证

见 [LICENSE](LICENSE) 文件。
