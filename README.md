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

## 运行前提

- Windows PowerShell 5.1 或 PowerShell 7
- Python 3.11
- Node.js 22.22 或更高版本
- FFmpeg 与 ffprobe 已加入 `PATH`（Mock 视频生成与可播放性校验需要）

## 一键启动

```powershell
cd D:\LyProject\Other\ai-comic-platform
.\setup.ps1
.\build.ps1
```

首次安装和构建后，双击根目录的 `一键启动.cmd`。以后每次双击都会先安全停止
本项目之前启动的进程，再运行数据库迁移并启动新的生产进程，健康检查通过后打开浏览器。
如果新实例的构建产物、FFmpeg 或配置预检失败，旧实例仍会按重启语义先被停止，
脚本随后返回非零退出码且不会启动新进程。
唯一的安全前置是后端虚拟环境必须存在：若 `.venv\Scripts\python.exe` 缺失，
脚本无法核验旧进程身份，会在停止旧实例前退出，请先运行 `.\setup.ps1`。
批处理优先使用 PowerShell 7，未安装时自动回退到 Windows PowerShell 5.1。

命令行等价操作：

```powershell
cd D:\LyProject\Other\ai-comic-platform
.\start.ps1
# 或在自动化环境中禁止打开浏览器
.\restart.ps1 -NoBrowser

# 停止本项目受控进程
.\stop.ps1
```

默认访问 `http://127.0.0.1:8000`。启动状态和日志位于 `.run`；
启动失败会返回非零退出码并保留日志。端口若被其他程序占用，脚本只报告占用进程，
不会强制结束非本项目进程。

## 开发运行

```powershell
cd D:\LyProject\Other\ai-comic-platform
.\start-dev.ps1
```

浏览器访问 `http://127.0.0.1:3000`，运行 `.\stop.ps1` 同时停止前后端。
生产和开发启动都会读取 `.env` 中的 `APP_HOST`、`APP_PORT`；开发前端代理会同步
使用该后端地址。

## 验证

```powershell
cd D:\LyProject\Other\ai-comic-platform
.\check.ps1

cd .\frontend
npm.cmd run test:e2e
```

## 数据与密钥

- 结构化数据：`workspace/platform.db`
- 项目媒体根目录：在“平台设置”页选择，默认 `workspace/projects/`
- 新建项目：`<项目媒体根目录>/<项目名>/`，同名时自动追加 ` (2)`、` (3)`
- API Key 推荐在“接口配置”页直接填写，平台使用 Windows DPAPI 加密保存，重启后仍可用
- 数据库只保存 API Key 密文，不保存明文；接口列表和详情不会返回明文
- 已有旧 Provider 仍兼容从项目 `.env` 读取 `TEXT_API_KEY`、`IMAGE_OPENAI_API_KEY`、
  `VIDEO_VOLC_API_KEY` 等环境变量
- 修改旧 Provider 的 `.env` 后需要重启平台；密钥不要填写到 Provider 参数或自定义请求头中

如需改到其他磁盘，可在“平台设置”页点击“选择目录”，无需重启平台；也可在尚未通过
页面保存设置时使用 `.env` 中的 `APP_PROJECTS_DIR=D:/AIComicProjects` 作为默认值。
请使用专用且较短的项目目录，不要指向磁盘根目录、平台源码或已有资料目录。
新目录只影响之后创建的项目，已有项目继续使用各自原目录，平台不会自动迁移历史素材。

新建“项目1”后会自动创建并分类保存媒体：

```text
workspace/projects/项目1/
├── 角色/
├── 物品/
├── 场景/
├── 分镜关键帧/
└── 分镜视频/
```
# ai-comic-platform
# ai-comic-platform
