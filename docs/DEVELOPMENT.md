# 开发指南

本指南面向希望参与 AI 漫剧平台开发的开发者。

## 目录

- [环境搭建](#环境搭建)
- [项目结构](#项目结构)
- [开发流程](#开发流程)
- [代码规范](#代码规范)
- [测试](#测试)
- [调试技巧](#调试技巧)
- [常见问题](#常见问题)

## 环境搭建

### 前置要求

- Python 3.11+
- Node.js 18+
- FFmpeg
- Git

### 克隆仓库

```bash
git clone <repository-url>
cd ai-comic-platform
```

### 后端设置

```bash
cd backend

# 创建虚拟环境
python -m venv .venv

# 激活虚拟环境
# Windows
.venv\Scripts\activate
# Linux/Mac
source .venv/bin/activate

# 安装依赖
pip install -e .

# 运行数据库迁移
alembic upgrade head

# 启动开发服务器
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

### 前端设置

```bash
cd frontend

# 安装依赖
npm install

# 启动开发服务器
npm run dev
```

### 环境变量

复制 `.env.example` 到 `.env` 并配置：

```bash
# 数据库
DATABASE_URL=sqlite:///../workspace/platform.db

# 存储
STORAGE_ROOT=../workspace

# FFmpeg 路径（Windows）
FFMPEG_PATH=C:/ffmpeg/bin/ffmpeg.exe

# API 密钥（根据需要）
OPENAI_API_KEY=sk-...
```

## 项目结构

```
ai-comic-platform/
├── backend/
│   ├── alembic/              # 数据库迁移
│   ├── app/
│   │   ├── api/              # API 路由
│   │   ├── jobs/             # 后台任务
│   │   ├── models/           # 数据模型
│   │   ├── providers/        # AI Provider 适配器
│   │   ├── services/         # 业务逻辑
│   │   ├── config.py         # 配置管理
│   │   ├── db.py             # 数据库连接
│   │   └── main.py           # FastAPI 应用
│   ├── tests/                # 测试
│   └── pyproject.toml        # Python 依赖
├── frontend/
│   ├── src/
│   │   ├── api/              # API 客户端
│   │   ├── components/       # React 组件
│   │   ├── hooks/            # 自定义 Hooks
│   │   ├── pages/            # 页面组件
│   │   ├── App.tsx           # 根组件
│   │   └── main.tsx          # 入口文件
│   ├── public/               # 静态资源
│   └── package.json          # Node.js 依赖
├── docs/                     # 文档
└── workspace/                # 数据存储
```

## 开发流程

### 添加新功能

1. **创建分支**
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. **后端开发**
   - 在 `app/api/` 添加路由
   - 在 `app/services/` 添加业务逻辑
   - 在 `app/models/tables.py` 添加数据表（如需要）
   - 创建数据库迁移（如需要）
     ```bash
     alembic revision -m "add new table"
     ```

3. **前端开发**
   - 在 `src/api/client.ts` 添加 API 方法
   - 在 `src/pages/` 创建页面
   - 在 `src/components/` 创建组件

4. **测试**
   ```bash
   # 后端测试
   cd backend
   pytest

   # 前端测试
   cd frontend
   npm test

   # 类型检查
   npm run type-check
   ```

5. **提交代码**
   ```bash
   git add .
   git commit -m "feat: add new feature"
   git push origin feature/your-feature-name
   ```

### 数据库迁移

创建新迁移：

```bash
cd backend
alembic revision -m "your migration message"
```

编辑 `alembic/versions/` 下的新文件，实现 `upgrade()` 和 `downgrade()` 函数。

应用迁移：

```bash
alembic upgrade head
```

回滚迁移：

```bash
alembic downgrade -1
```

## 代码规范

### Python 代码规范

遵循 PEP 8 和项目 `.ruff.toml` 配置。

**命名约定**：
- 函数/变量：`snake_case`
- 类：`PascalCase`
- 常量：`UPPER_CASE`
- 私有成员：`_leading_underscore`

**类型注解**：
```python
from __future__ import annotations

def create_project(
    name: str,
    storage_name: str | None = None,
) -> dict[str, Any]:
    ...
```

**导入顺序**：
1. 标准库
2. 第三方库
3. 本地模块

### TypeScript 代码规范

遵循项目 ESLint 和 Prettier 配置。

**命名约定**：
- 函数/变量：`camelCase`
- 组件/类/接口：`PascalCase`
- 常量：`UPPER_CASE`

**React 组件**：
```typescript
interface MyComponentProps {
  title: string;
  onSubmit?: () => void;
}

export function MyComponent({ title, onSubmit }: MyComponentProps) {
  // 组件逻辑
  return <div>{title}</div>;
}
```

### Git 提交信息

使用 [Conventional Commits](https://www.conventionalcommits.org/)：

```
feat: 添加新功能
fix: 修复 bug
docs: 文档更新
style: 代码格式调整
refactor: 重构
test: 测试相关
chore: 构建/工具相关
```

## 测试

### 后端测试

```bash
cd backend

# 运行所有测试
pytest

# 运行特定文件
pytest tests/test_project_service.py

# 显示覆盖率
pytest --cov=app --cov-report=html
```

### 前端测试

```bash
cd frontend

# 运行测试
npm test

# 运行特定测试
npm test -- MyComponent.test.tsx

# 显示覆盖率
npm test -- --coverage
```

### 集成测试

```bash
# 启动后端和前端
./start.sh

# 在浏览器中手动测试
# http://localhost:3000
```

## 调试技巧

### 后端调试

**使用 Python debugger**：
```python
import pdb; pdb.set_trace()
```

**查看日志**：
```bash
tail -f backend.log
```

**数据库查询**：
```bash
sqlite3 workspace/platform.db
.tables
SELECT * FROM project;
```

### 前端调试

**React DevTools**：
安装浏览器扩展查看组件树和状态。

**Network 监控**：
使用浏览器开发者工具查看 API 请求。

**Console 日志**：
```typescript
console.log("Debug info:", data);
```

## 性能优化

### 后端优化

- 使用数据库索引
- 避免 N+1 查询
- 使用异步 I/O
- 实现缓存策略

### 前端优化

- 使用 React.memo 避免不必要的渲染
- 使用 useMemo 和 useCallback
- 实现虚拟列表
- 图片懒加载
- 代码分割

## 常见问题

### 数据库迁移失败

**问题**：`alembic upgrade head` 失败

**解决**：
```bash
# 检查当前版本
alembic current

# 回滚到上一个版本
alembic downgrade -1

# 手动修复数据库后重新升级
alembic upgrade head
```

### API 请求 CORS 错误

**问题**：前端无法访问后端 API

**解决**：检查 `backend/app/main.py` 中的 CORS 配置：
```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

### FFmpeg 未找到

**问题**：视频合成失败，提示找不到 FFmpeg

**解决**：
1. 下载 FFmpeg
2. 将 FFmpeg 添加到 PATH
3. 或在 `.env` 中配置 `FFMPEG_PATH`

### 端口被占用

**问题**：启动失败，端口 8000 或 3000 被占用

**解决**：
```bash
# Windows
netstat -ano | findstr :8000
taskkill /PID <PID> /F

# Linux/Mac
lsof -ti:8000 | xargs kill -9
```

## 贡献指南

1. Fork 项目
2. 创建功能分支
3. 提交更改
4. 推送到分支
5. 创建 Pull Request

## 资源链接

- [FastAPI 文档](https://fastapi.tiangolo.com/)
- [React 文档](https://react.dev/)
- [SQLAlchemy 文档](https://docs.sqlalchemy.org/)
- [TanStack Query 文档](https://tanstack.com/query/latest)
