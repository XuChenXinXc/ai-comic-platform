# 苹果风格前端 → 本项目真实前端 · 迁移改造提示词

> 使用方式：把【新设计代码】占位处替换成你手上的苹果风格前端代码（整段粘贴或给出文件路径），
> 然后连同本项目的 `frontend/` 目录一起交给 AI（DeepSeek/Claude/Cursor 等，需带代码上下文）。

```markdown
# Role（角色）
你是一位资深前端架构师，擅长在"不破坏任何现有功能"的前提下完成 UI 重设计（reskin）。
你精通 React 19、react-router 8、TanStack Query 5、Tailwind CSS 3、TypeScript。
你的任务：以【新设计代码】为唯一视觉基准，把本项目 `frontend/src` 重构成苹果官网风格，
同时保证**展示对齐 + 功能对齐**，现有每一个功能都必须继续可用。

# 输入材料
- 现有项目前端：`frontend/`（React 19 + Vite + Tailwind 3.4 + TanStack Query + sonner + lucide-react）
  - 路由表：`src/App.tsx`（16 条路由，见下）
  - 布局壳：`src/components/app-shell.tsx`（侧边导航 + 项目选择器 + 任务角标 + 任务加载条）
  - 基础组件库：`src/components/ui.tsx`（Button/Input/Textarea/Card/Select/Badge/Modal 等，基于 CVA）
  - 状态 hooks：`src/hooks/use-project.tsx`、`use-chapter.tsx`、`use-job-polling.ts`
  - API 客户端：`src/api/client.ts`（禁止改动导出签名与请求行为）
  - 现有设计令牌（tailwind.config.ts）：canvas #F2F0EA / ink #191A17 / sidebar #1C211F / vermilion #E8613A
- 新设计代码：【把苹果风格前端代码粘贴在这里，或注明文件路径】

# 核心目标（按优先级排序）
1. **功能对齐（最高优先级）**：现有 16 条路由、全部页面功能、全部按钮动作、全部 API 调用、
   加载态/错误态/空态、任务轮询与角标，逐项保留且真实可用。视觉改造不得以丢失功能为代价。
2. **展示对齐**：全局视觉（导航布局、字体、配色、圆角、阴影、留白、动效）对齐新设计代码；
   页面内部结构可按新设计的视觉语言重排，但每个控件的功能和反馈不变。
3. **可验证**：`npm run build`、`npm run lint`、`npm run test` 全部通过，e2e 不产生功能回归。

# 硬性约束（EARS 规范，逐条满足）
1. 系统不得删除或重命名 `App.tsx` 中的任何路由与路径（/project /writing /assets /asset-images
   /shots /custom-shots /keyframes /videos /audio/:projectId/:chapterId /composition/:projectId
   /providers /jobs /settings /free-create 及其 pipeline 子路由）。
2. 系统不得修改 `api/client.ts` 的导出签名、请求 URL、参数与响应处理逻辑。
3. 系统不得修改三个 hooks 的状态语义（use-project 的项目选择与上下文、use-chapter 的章节选择、
   use-job-polling 的任务轮询与 activeJobs 计数）。
4. 当用户执行任何现有操作时，系统应保留原操作的全部行为：请求触发条件、请求参数、成功后
   数据刷新方式、失败时的 sonner 错误提示与重试入口。
5. 若新设计缺少某个现有功能的入口（如项目选择器、章节选择器、任务角标、任务加载条、
   "手工新增"、"上传替换"、多版本切换），系统应在对应位置补齐该入口，禁止丢弃。
6. 系统不得用写死的示例数据替换真实数据流（列表必须来自 API 查询结果）。
7. 若新设计中出现本项目没有的功能，系统不得伪造空实现；应标注"超出范围，未接入"并保留布局占位。
8. 当测试因文案或定位器变化失败时，只允许按语义更新 getByRole/getByLabel/getByText 定位器与
   文案，禁止删除断言、跳过测试或吞掉异常。
9. 系统不得修改后端代码；若发现某样式需要新数据字段，应标记为待后端配合，而不是改接口。

# 工作流（严格按序执行）
## 第 1 步：盘点现有功能（产出"功能清单"，改造前后逐项对照）
- 逐个阅读 15 个页面文件，为每个页面列出：输入控件、按钮及动作、数据列表、模态框、
  加载态、错误态、空态、版本/上传操作。
- 阅读 app-shell.tsx、ui.tsx、三个 hooks、api/client.ts，列出全局功能点。

## 第 2 步：拆解新设计代码（产出"设计令牌 + 组件清单"）
- 提取设计令牌：字体栈、颜色（浅色/深色）、圆角、阴影、间距、过渡时长。
- 提取布局骨架：导航形态（顶栏/侧栏）、内容区最大宽度、卡片栅格。
- 提取组件样式：按钮（胶囊/圆角）、输入框、下拉框、徽章、模态框、进度条、空状态。

## 第 3 步：建立映射表（产出"现有控件 → 新样式"对照）
- 把现有设计令牌映射为新令牌，示例：
  - `vermilion #E8613A`（主操作）→ 苹果蓝 `#0071e3`（如新设计为蓝色主按钮）
  - `canvas #F2F0EA` → `#f5f5f7`；`sidebar #1C211F` → 顶栏毛玻璃 `bg-white/70 backdrop-blur`
  - Button 圆角 `rounded-md` → `rounded-full` 胶囊；Card 圆角 12px → 20px、阴影更柔和大范围
- 逐控件列出映射：Button(primary/secondary/ghost/danger/dark + busy 加载态)、Input、Textarea、
  Select、Badge、Modal、进度条、面包屑/步骤条。
- 明确"新设计没有、现有必须保留"的控件：项目选择器、章节选择器、任务角标、TaskLoadingBar、
  MediaVersions（多版本切换）、ImagePreview、ContinuityCheck、JSON 编辑器、SceneViewPanel。

## 第 4 步：先改全局层（本步结束前页面必须能编译运行）
1. `tailwind.config.ts`：新增/替换设计令牌（保留被引用的旧令牌名可先加新色板避免大面积改动）。
2. `src/index.css`：字体栈（-apple-system, "SF Pro Display", "PingFang SC"）、全局背景与文字色。
3. `src/components/ui.tsx`：按映射表重写全部基础组件样式，保留所有 props 与 busy/disabled 行为。
4. `src/components/app-shell.tsx`：布局改为新设计的导航形态；保留：8 个生产步骤导航、
   Logo「漫制局」、项目选择器（含"新建"入口）、任务角标（activeJobs 计数）、接口配置/平台设置/
   任务中心入口、移动端折叠；任务加载条位置随布局调整但行为不变。

## 第 5 步：逐页改造（每页完成后立即 npm run build 验证）
按依赖顺序逐页重排视觉，功能清单逐项核对，不跳页、不批量：
1. ProjectPage（项目列表/新建/编辑/删除/选择/跳转，含模态框）
2. WritingPage（小说原文、AI 分析、剧本标题/梗概/场次/对白/旁白、STYLE_ANCHOR 画风、保存）
3. AssetsPage（角色/场景/物品、AI 提取、手工新增、多版本、生成参考图）
4. AssetImagesPage（素材图生成与版本管理）
5. ShotsPage / KeyframesPage（分镜列表、手工新增、图像/视频/负向提示词、生成关键帧、
   生成视频、多版本、上传替换、连续性检查）
6. VideosPage（单镜视频管理）
7. AudioWorkbenchPage（章节选择、配音、字幕、BGM）
8. CompositionPage（视频合成、导出）
9. CustomShotsPage（自由制作）
10. FreeCreatePage / PipelineNewPage / PipelineWorkspacePage（动态漫流水线，含阶段导航）
11. JobsPage（任务列表、状态、重试、取消、详情）
12. ProvidersPage（Provider 增删改查、API Key 加密保存表单）
13. PlatformSettingsPage（媒体目录选择等设置）

## 第 6 步：回归验证（全部通过才可交付）
- `npm run build`（tsc --noEmit && vite build）零错误
- `npm run lint`（--max-warnings=0）零告警
- `npm run test`（Vitest）全绿；只允许语义级更新定位器/文案
- `npm run test:e2e`（Playwright）全绿
- 手工验收：启动 `npm run dev`，逐路由打开，完成"功能清单"逐项点击：
  新建项目 → AI 分析小说 → 生成剧本 → 提取素材 → 生成关键帧 → 生成视频 → 配音 → 合成 →
  导出 → 任务重试/取消 → 项目切换 → 章节切换，每个操作都要有真实的请求、加载态、结果反馈。

# 输出要求
- 每完成一步，输出：改动文件列表、功能清单核对结果（√/⚠）、当前验证命令结果。
- 最终交付：改造后的完整代码 + "功能对齐清单"（每页每个功能的状态）+ 未接入/需后端配合的标注。
- 禁止输出只有样式 demo、按钮无动作的"假前端"。
```
