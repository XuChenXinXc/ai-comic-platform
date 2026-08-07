# 贡献指南

感谢您考虑为 AI 漫剧平台做出贡献！

## 行为准则

参与本项目的所有人都应遵守以下准则：

- 使用友好和包容的语言
- 尊重不同的观点和经验
- 优雅地接受建设性批评
- 关注对社区最有利的事情
- 对其他社区成员表示同理心

## 如何贡献

### 报告 Bug

在提交 Bug 报告之前：

1. **检查现有 Issues** - 确认问题是否已被报告
2. **使用最新版本** - 确保使用的是最新版本
3. **收集信息** - 准备详细的复现步骤

提交 Bug 报告时请包含：

- **清晰的标题** - 简洁描述问题
- **环境信息** - 操作系统、Python/Node 版本等
- **复现步骤** - 详细的步骤说明
- **预期行为** - 应该发生什么
- **实际行为** - 实际发生了什么
- **截图/日志** - 如果适用

**模板**：

```markdown
**环境信息**
- OS: Windows 11 / macOS / Linux
- Python: 3.11.5
- Node.js: 18.17.0

**描述**
简洁描述 bug

**复现步骤**
1. 打开页面 '...'
2. 点击按钮 '...'
3. 观察错误 '...'

**预期行为**
应该显示成功消息

**实际行为**
显示错误消息：[错误内容]

**截图**
[如有必要]

**日志**
```
[粘贴相关日志]
```
```

### 建议新功能

在提交功能建议之前：

1. **检查路线图** - 查看是否已在计划中
2. **讨论必要性** - 考虑功能的普适性

提交功能建议时请包含：

- **功能描述** - 清晰描述功能是什么
- **使用场景** - 为什么需要这个功能
- **实现建议** - 可选的实现思路
- **替代方案** - 考虑过的其他方案

### 提交代码

#### 1. 准备工作

**Fork 并克隆仓库**：

```bash
# Fork 项目（在 GitHub 上点击 Fork）

# 克隆你的 Fork
git clone https://github.com/YOUR_USERNAME/ai-comic-platform.git
cd ai-comic-platform

# 添加上游仓库
git remote add upstream https://github.com/ORIGINAL_OWNER/ai-comic-platform.git
```

**设置开发环境**：

参考 [DEVELOPMENT.md](DEVELOPMENT.md) 设置开发环境。

#### 2. 创建分支

```bash
# 确保本地 main 分支是最新的
git checkout main
git pull upstream main

# 创建功能分支
git checkout -b feature/your-feature-name

# 或修复分支
git checkout -b fix/your-bug-fix
```

**分支命名规范**：
- `feature/xxx` - 新功能
- `fix/xxx` - Bug 修复
- `docs/xxx` - 文档更新
- `refactor/xxx` - 代码重构
- `test/xxx` - 测试相关

#### 3. 开发

**编码规范**：

- 遵循项目代码风格（Python: PEP 8, TypeScript: ESLint）
- 添加必要的注释和文档字符串
- 保持函数简洁，单一职责
- 使用有意义的变量名

**提交规范**：

使用 [Conventional Commits](https://www.conventionalcommits.org/)：

```
<type>(<scope>): <subject>

<body>

<footer>
```

**类型（type）**：
- `feat`: 新功能
- `fix`: Bug 修复
- `docs`: 文档更新
- `style`: 代码格式（不影响功能）
- `refactor`: 重构
- `perf`: 性能优化
- `test`: 测试相关
- `chore`: 构建/工具/依赖更新

**示例**：

```
feat(api): add project export endpoint

- Add GET /api/projects/{id}/export endpoint
- Support JSON and ZIP formats
- Include all project assets and metadata

Closes #123
```

#### 4. 测试

**运行测试**：

```bash
# 后端
cd backend
pytest

# 前端
cd frontend
npm test
```

**添加测试**：

- 为新功能添加测试
- 为 Bug 修复添加回归测试
- 确保测试覆盖率不降低

#### 5. 提交 Pull Request

**推送分支**：

```bash
git push origin feature/your-feature-name
```

**创建 PR**：

1. 访问 GitHub 上的仓库
2. 点击 "Compare & pull request"
3. 填写 PR 描述

**PR 模板**：

```markdown
## 变更类型

- [ ] Bug 修复
- [ ] 新功能
- [ ] 重构
- [ ] 文档更新
- [ ] 其他

## 变更描述

简要描述你的变更

## 相关 Issue

Closes #123

## 测试

描述如何测试这些变更

## 截图

[如果适用]

## 检查清单

- [ ] 代码遵循项目规范
- [ ] 已添加必要的注释
- [ ] 已添加测试
- [ ] 所有测试通过
- [ ] 已更新文档
```

#### 6. 代码审查

- **响应反馈** - 及时回复审查意见
- **更新代码** - 根据反馈修改代码
- **保持礼貌** - 尊重审查者的意见

**处理审查意见**：

```bash
# 修改代码后
git add .
git commit -m "fix: address review comments"
git push origin feature/your-feature-name
```

## 开发流程

### 小改动

- 文档修正
- 代码格式调整
- 简单 Bug 修复

→ 可以直接提交 PR

### 大改动

- 新功能
- 架构变更
- API 变更

→ 先创建 Issue 讨论再开发

## 代码审查标准

### 功能性

- [ ] 功能按预期工作
- [ ] 边缘情况处理正确
- [ ] 错误处理完善

### 代码质量

- [ ] 代码清晰易读
- [ ] 遵循项目规范
- [ ] 没有重复代码
- [ ] 适当的抽象层次

### 测试

- [ ] 有足够的测试覆盖
- [ ] 测试用例合理
- [ ] 所有测试通过

### 文档

- [ ] 代码有必要的注释
- [ ] API 文档已更新
- [ ] README 已更新（如需要）

### 性能

- [ ] 没有明显的性能问题
- [ ] 资源使用合理
- [ ] 数据库查询优化

### 安全

- [ ] 没有安全漏洞
- [ ] 输入验证完善
- [ ] 敏感信息不暴露

## 发布流程

维护者负责发布新版本：

1. 更新版本号
2. 更新 CHANGELOG
3. 创建 Git 标签
4. 构建发布包
5. 发布到生产环境

## 社区

- **Issues** - 报告 Bug 和建议功能
- **Discussions** - 提问和讨论
- **Email** - 私密问题联系维护者

## 许可证

贡献的代码将在与项目相同的许可证下发布。

## 致谢

感谢所有贡献者！你们的贡献让这个项目变得更好。
