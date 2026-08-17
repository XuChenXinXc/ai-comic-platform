# 动态漫流水线与结构化提示词编译器设计

## 1. 背景与目标

### 1.1 背景

当前项目已经具备项目、章节、剧本、角色/场景/物品素材、分镜、关键帧、视频、配音、字幕、BGM 和章节合成能力，但“自由制作”主要是手工分镜页，提示词由用户直接填写或由多个服务分别拼接。当前存在以下问题：

- 自由制作的图片/视频提示词可绕过确定性模板，直接以整段文本提交；
- 分镜提示词主要使用素材名称，角色脸型、发型、服装、饰品、场景布局等视觉事实没有稳定进入最终提示词；
- 图片关键帧与视频动作的职责边界不够清晰；
- 视频缺少可执行的“单一动作、主运镜、起止状态、时间轴、对白/音效 cue”；
- 设定板可能被直接当作视频身份参考图，文字、三视图和版式容易污染生成；
- 页面展示的提示词、媒体版本快照和实际 Provider 请求在不同分支下可能不一致；
- 当前没有可恢复的阶段运行、人工确认、下游失效和交付包导出模型。

### 1.2 目标

在不破坏现有手工分镜功能的前提下，增加一套“动态漫流水线”生产模式：

```text
源文/梗概
→ 源文拆解
→ 人物小传与一致性锚点
→ 节奏与爽点
→ 详细剧本
→ 标准分镜
→ 图片提示词
→ 视频提示词
→ 图片/视频/配音/字幕/BGM/合成
→ Markdown + ZIP 交付包
```

本次设计的核心不是增加几个提示词输入框，而是建立一个统一的、可追溯的结构化提示词编译器：

```text
结构化创作意图
→ Prompt IR
→ 参考图计划
→ 图片/视频语义计划
→ Provider 专用渲染
→ 质量校验
→ 最终快照
→ Provider 请求
```

### 1.3 非目标

- 不删除或重写现有 `/custom-shots` 手工分镜能力；
- 不创建一套独立的 PipelineProject、PipelineShot、PipelineMedia 等重复业务模型；
- 不把所有现有提示词逻辑一次性重构成分布式服务；
- 不让大模型直接独占最终提示词编译；
- 不在全自动模式下绕过结构校验、素材门禁、Provider 能力限制或安全规则；
- 不回退当前工作区已有的未提交修改，也不修改与本设计无关的本地改动。

## 2. 产品范围与默认行为

### 2.1 自由制作的两种模式

自由制作工作区分为两个明确模式：

```text
自由制作
├── 手工分镜
│   └── 保留现有 CustomShotsPage 行为
└── 动态漫流水线
    └── 新增 PipelineWorkspace
```

现有 `/custom-shots` 继续作为手工分镜兼容入口。新增入口使用独立自由制作上下文，不依赖普通项目上下文，也不把流水线状态放进现有 `manual-*` 或默认场景 `localStorage` 键。

### 2.2 一次运行的边界

第一版一个 `WorkflowRun` 对应：

- 一个自由项目；
- 一个新建的章节；
- 一集/一章源文或梗概；
- 一次动态漫流水线运行。

运行默认创建独立章节，不能和手工分镜章节共用镜头编号或合成范围。运行之间可以共享项目级素材，但默认先创建本次运行使用的素材映射；只有用户明确选择“复用已确认素材”时，才绑定已有素材。

### 2.3 默认设置

| 设置 | 默认值 |
|---|---|
| 输入粒度 | 单集/单章 |
| 目标时长 | 约30秒；允许按项目配置调整 |
| 画幅 | 竖屏9:16 |
| 交互模式 | 半自动 |
| 暂停点 | 阶段0–2完成后待确认 |
| 图片输出 | 中文即梦提示词 + 简短英文备用提示词 |
| 视频输出 | Seedance/即梦时间轴格式 |
| 强动态预算 | 默认不超过3处，由节奏阶段输出并校验 |
| 媒体生成 | 阶段6按门禁逐步生成 |
| 交付 | Markdown 文件和 ZIP |

全自动是本次运行级别的选项。全自动只跳过正常人工确认，不绕过阻断错误、素材缺失、Provider 错误、上游版本冲突或缺镜头合成门禁。

## 3. 用户流程与路由

### 3.1 路由

新增以下路由，具体命名可沿用项目现有路由风格：

```text
/free-create
/free-create/:projectId
/free-create/:projectId/pipeline/new
/free-create/:projectId/pipeline/:runId
/free-create/:projectId/pipeline/:runId/stage/:stageId
```

兼容入口：

```text
/custom-shots → 继续打开手工分镜工作区
```

不在访问流水线入口时自动创建项目、章节或运行；创建动作必须由用户点击“新建动态漫流水线”触发。

### 3.2 运行创建流程

用户填写：

- 剧名；
- 集数/章节名；
- 源文或梗概（二选一）；
- 目标时长；
- 画幅；
- 风格关键词；
- 是否全自动；
- 是否执行配音、字幕、BGM 和合成；
- 是否允许复用已确认素材。

提交后系统在一个短事务中创建自由项目/独立章节/运行快照，然后异步排入阶段0。Provider 调用不能发生在数据库事务内。

### 3.3 页面信息架构

```text
PipelineWorkspace
├── PipelineRunHeader
├── PipelineStageRail
├── PipelineStageHeader
├── PipelineArtifactPreview
├── PipelineGatePanel
├── PipelineRevisionNotice
├── PipelineErrorPanel
└── PipelineActionBar
```

桌面端为左阶段导航、中间产物编辑/预览、右侧门禁和操作区；窄屏改为单列，顶部显示“第 N / 8 阶段”，完整阶段列表放在抽屉或下拉中，底部固定主要操作。

每个阶段只展示一个主要任务，不把源文、剧本、分镜、媒体全部堆到一页。阶段导航显示：编号、名称、任务状态、确认状态、错误、上游变化和可进入性。

### 3.4 阶段键

阶段使用稳定字符串，不使用仅靠数字的内部判断：

```text
0-source
1-character-bios
2-rhythm
3-script
4-storyboard
5a-image-prompts
5b-video-prompts
6-delivery
```

阶段状态区分：

```text
pending
queued
running
blocked
ready
confirmed
stale
partial_failed
failed
interrupted
cancelled
```

“任务完成”“产物可用”“用户已确认”是三个不同概念，不能用一个布尔字段替代。

## 4. 数据模型与隔离

### 4.1 复用的现有实体

以下实体继续作为最终业务产物：

```text
Project
Chapter
NovelAnalysis
Screenplay
Asset
Shot
AssetImage
KeyframeVersion
VideoVersion
ShotAudio
ShotSubtitle
ProjectBgm
ChapterComposition
Job
```

不新增重复的 PipelineShot 或 PipelineMedia 表。

### 4.2 `workflow_run`

新增工作流运行表，建议字段：

```text
id
project_id
chapter_id
kind                    # dynamic_comic_0_6
schema_version
status
current_stage
source_kind             # full_text / synopsis
source_snapshot_json
config_snapshot_json
input_signature
revision
error
created_at
started_at
updated_at
finished_at
```

约束：

- `project_id` 必须指向 `is_free_create=true` 的项目；
- `chapter_id` 必须属于该项目；
- 同一章节最多一个可物化的活动运行；
- 可以保留历史完成/失败运行；
- 源文和配置以启动时快照为准，不能直接读取用户后续正在编辑的章节正文。

### 4.3 `workflow_stage`

建议字段：

```text
id
workflow_run_id
stage_key
ordinal
status
input_signature
raw_output_json
normalized_output_json
output_refs_json
gate_result_json
attempt
error
created_at
started_at
updated_at
finished_at
confirmed_at
confirmed_revision
```

约束：

- `workflow_run_id + stage_key + attempt` 唯一；
- 当前版本和历史版本分开保存，不静默覆盖；
- 上游输入变化时，下游阶段标记 `stale`；
- 阶段输出先存于 `workflow_stage`，在用户确认或自动门禁通过后才物化到 Chapter/Asset/Screenplay/Shot 等 canonical 实体。

### 4.4 Job 扩展

现有 `job` 增加可空字段：

```text
workflow_run_id
workflow_stage_id
idempotency_key
input_signature
depends_on_json
```

`batch_id` 继续用于批次展示，不承担 DAG 语义。每个实际目标仍然使用独立 Job，编排器根据 `depends_on_json` 和阶段屏障推进下一阶段。

建议幂等语义：

```text
workflow_stage_id
+ target_type
+ target_id
+ input_signature
+ job_type
```

同一组合再次启动时返回现有活动任务或已完成产物，不重复创建 Provider 请求。

### 4.5 产物来源与版本

现有 `asset_image`、`keyframe_version`、`video_version` 已有 `prompt_snapshot`、`negative_prompt_snapshot`、`reference_bindings_json`、`input_signature` 等字段。第一版不抽象通用 MediaVersion 表，但增加或统一保存以下 provenance JSON：

```json
{
  "workflow_run_id": "...",
  "workflow_stage_id": "...",
  "compiler_version": "dynamic-prompt-1",
  "prompt_ir_hash": "...",
  "style_version": "...",
  "asset_versions": [],
  "provider_capabilities": {},
  "human_overrides": [],
  "diagnostics": []
}
```

对于旧数据，`workflow_run_id` 为空并标记为 `legacy`，不根据项目名称或 localStorage 推断历史归属。

### 4.6 素材复用与运行隔离

项目级 Asset 继续作为共享资源，但运行需要保存显式映射：

```json
{
  "workflow_asset_id": "run-asset-1",
  "asset_id": "canonical-asset-1",
  "usage": "character",
  "locked_revision": 3,
  "reuse_mode": "new|approved_existing"
}
```

默认流程：

1. 阶段1输出角色/场景/物品锚点；
2. 用户确认后建立本次运行的 asset mapping；
3. 同名已有 Asset 只有在用户确认复用时才绑定；
4. 运行中使用的 Asset revision 变化后，相关下游阶段标记 stale；
5. 不允许后台提取结果无提示地合并并覆盖用户已确认 Asset。

## 5. 动态漫阶段设计

### 5.1 阶段0：源文拆解

输入：源文快照、剧名、集数、时长、画幅和风格设置。

输出结构至少包含：

- 元信息；
- 一句话本集；
- 场次表；
- 角色清单；
- 冲突与转折；
- 不适合拍/需改写；
- 假设与待确认；
- 超出单集体量时的后续集大纲。

门禁：源文非空、输出结构完整、场次可拍字段存在。

### 5.2 阶段1：人物小传与一致性锚点

核心角色输出完整五模块：基础信息、核心背景、性格与行为逻辑、动态漫专属设定、剧情关联；每个主角色强制包含：

```text
不可变特征
禁用变化
定妆关键词串
```

该阶段不直接生成最终分镜图，只生成可复用的 AssetVisualAnchor。路人/群像不创建详细主角色，避免角色数量失控。

### 5.3 阶段2：节奏与爽点

输出：时间轴、核心爽点、小爽点、强动态预算、伏笔与留白、对白/音效建议。

支持约30秒短片的短段时间轴，也支持3–5分钟单集。编译器以运行的 `target_duration_sec` 为准，不把3–5分钟模板硬套到30秒。

门禁：核心爽点存在、时间段覆盖完整、强动态预算不超过配置上限、每段有叙事任务。

### 5.4 阶段3：详细剧本

输出使用固定场景头、人物标注、可执行动作、口型级别、动态等级、音效和字幕格式。必须兑现阶段2的节奏时间轴。

阶段3物化为 `screenplay` 前必须检查：

- 角色已映射；
- 场景已映射；
- 台词行可解析；
- 动态强度不超预算；
- 时长和场次顺序合法。

### 5.5 阶段4：标准分镜

输出连续分镜块：场景、时间、人物、镜头、动作/画面、台词/音效、动态效果、转场、备注。

物化到 `shot` 时需要额外生成稳定的外部镜头 key，保存：

- `start_state`；
- `action`；
- `end_state`；
- `next_hook`；
- `transition_from_prev`；
- 角色站位/视线/表情；
- 道具持有者/位置/状态；
- 对白 cue；
- 音效 cue；
- 目标时长和 Provider 生成时长。

### 5.6 阶段5a：图片提示词

图片只描述当前静止关键帧，禁止自动加入视频时间轴、连续动作过程和动作完成结果。

最终输出包含：

- 中文即梦提示词；
- 简短英文 MJ 备用提示词；
- 图片负面提示词；
- 关键帧时间点（固定为0秒）；
- 参考图职责和编号；
- 图片语义计划；
- 编译诊断和 provenance。

图片编译必须注入必要的角色、场景、物品视觉事实，而不是只注入素材名称。角色至少包含脸型/发型/服装/饰品/不可变特征；场景至少包含布局/轴线/固定陈设/光线；物品至少包含形状/材质/颜色/当前持有状态。

### 5.7 阶段5b：视频提示词

视频按“一个主要动作 + 一个主要运镜 + 一个明确结束状态”编译。每段包含：

- Subject；
- Action；
- Scene；
- Camera；
- Lighting/Style；
- Audio；
- Timeline；
- Continuity；
- Negative；
- Reference map。

视频时间轴必须满足：

```text
第一段 start = 0
相邻段首尾相接
无间隙、无重叠
最后一段 end = generation_duration_sec
```

对白、旁白、动作音效和手机消息必须进入结构化 cue，并绑定到一个时间段；中文文字默认以后期配音/字幕完成，生成提示词只负责表演和声音意图。

### 5.8 阶段6：交付整理与媒体生成

阶段6展示交付检查清单：

```text
角色/场景参考图    x / total
关键帧              x / total
分镜视频            x / total
配音                x / total
字幕                x / total
BGM                 selected / none
成片                pending / completed
```

合成前必须显示缺失镜头、失败项、将要跳过的内容和预计任务数。默认不允许静默跳过缺失视频；用户必须明确选择“修复缺失项”或“按部分结果合成”。

交付包输出：

```text
00_拆解.md
01_人物小传.md
02_节奏爽点.md
03_详细剧本.md
04_标准分镜.md
05_图片提示词.md
06_视频提示词.md
README.md
```

同时保留结构化阶段 JSON 供页面重新编辑和断点续跑，Markdown/ZIP 是面向用户的交付格式。

## 6. 结构化提示词编译器

### 6.1 通用 Prompt IR

编译器接收 Provider 无关的语义输入：

```text
VisualStyleSpec
AssetVisualAnchor
ShotIntent
ContinuityContext
ReferencePlan
ProviderCapabilities
HumanOverrideSet
```

输出 `CompiledPromptBundle`：

```json
{
  "schema_version": "dynamic-comic-prompt-v1",
  "compiler_version": "dynamic-prompt-1",
  "input_hash": "...",
  "status": "ready|warning|blocked",
  "image": {
    "prompt": "...",
    "negative_prompt": "...",
    "keyframe_time_sec": 0,
    "reference_slots": [],
    "semantic_plan": {}
  },
  "video": {
    "prompt": "...",
    "negative_prompt": "...",
    "edit_duration_sec": 6,
    "generation_duration_sec": 6,
    "timeline": [],
    "end_state": {},
    "reference_slots": [],
    "semantic_plan": {}
  },
  "diagnostics": [],
  "provenance": {}
}
```

### 6.2 视觉规范 `VisualStyleSpec`

现有 `style_prompt` 保留为用户自由文本补充，但新增结构化规范：

```text
medium
line_style
color_palette
lighting_style
material_style
realism_level
mood
text_policy
global_negative_rules
immutable_rules
style_version
```

默认动态漫风格不使用空泛的“超高质量、极致、震撼”等词作为主要质量保证，而以可执行的线条、上色、光影、材质、构图和限制描述为主。

### 6.3 角色/场景/物品视觉锚点

`AssetVisualAnchor` 逻辑上包含：

```text
canonical_name
visual_facts
immutable_features
variants
current_variant
approved_references
```

角色额外包含 face、hair、body、costume、accessories；场景额外包含 layout、axis、entrances、fixed_furnishings、lighting_direction、weather、time_of_day；物品额外包含 shape、material、color、wear、owner、current_holder、visibility。

设定板和干净参考图用途分离：

```text
design_sheet       角色/场景设定展示
clean_identity     角色身份参考
layout_reference   场景布局参考
prop_identity      道具身份参考
keyframe_reference 分镜关键帧参考
```

视频默认只使用 approved 的 clean_identity/layout_reference/prop_identity/keyframe_reference，不把含文字和版式的设定板作为视频参考。

### 6.4 参考图计划

参考图计划由一个函数生成，并同时供页面、编译器、媒体 Provider 和快照使用：

```text
ReferenceSlot {
  index
  role
  asset_id / keyframe_id
  image_version_id
  required
  purpose
}
```

`@ImageN` 标签、实际上传数组和快照必须从同一计划生成，禁止不同服务自行猜顺序。Provider 不支持多图时，必须在编译前裁剪计划并产生 warning 或 blocked，而不是让文本继续引用不存在的图片。

### 6.5 图片编译规则

图片编译步骤：

1. 归一化素材 ID、镜头状态、时长和风格；
2. 规划角色/场景/物品参考图；
3. 推导当前静止开始状态；
4. 编译单一关键构图、景别、机位、视线和光线；
5. 合并角色/场景/道具锚点；
6. 合并默认负向和用户追加负向；
7. 检查画面禁字、写实真人脸、Logo、设定板和多动作；
8. 输出中文提示词、英文备用、负向提示词、参考映射和诊断。

### 6.6 视频编译规则

视频编译步骤：

1. 根据 Provider 能力先确定有效生成时长；
2. 根据转场类型推导上一镜继承规则；
3. 规划当前关键帧、上一镜末帧、角色/场景/道具参考图；
4. 将 action_events 编译为连续 timeline；
5. 将 dialogue_cues/sound_cues 绑定到时间段；
6. 选择一个主运镜并写明端点；
7. 编译 end_state 和下一镜衔接状态；
8. 合并风格、连续性、负向约束；
9. 根据 Provider 渲染平台可直接复制的中文提示词；
10. 保存 Prompt IR、文本、参考图计划和 Provider 能力快照。

### 6.7 人工覆盖

允许字段级修改：

- 表演细节；
- 镜头微调；
- 负向追加项；
- 时间段动作措辞；
- 音效描述；
- 非核心艺术偏好。

默认不允许静默修改：

- 角色身份锚点；
- 参考图编号和顺序；
- Provider 时长边界；
- 时间轴首尾和连续性；
- 资产 canonical ID；
- 禁字和安全规则。

专家模式可以整段覆盖，但必须标记已关闭的自动能力，并保存 `base_compiler_hash`。上游输入变化后，旧覆盖标记为“基于旧输入”，提供重新编译或差异确认，不继续静默使用。

### 6.8 编译诊断

阻断级诊断：

- asset ID 未解析；
- 参考图不存在或类型错误；
- `@ImageN` 与实际数组不一致；
- 时间轴不从0开始、有间隙、有重叠或末段不等于有效时长；
- continuous 没有上一镜末帧；
- Provider 不支持当前参考图数量或时长；
- 视频没有有效动作或结束状态；
- 图片包含视频时间轴或动作完成结果。

警告级诊断：

- 角色没有 clean identity；
- 场景只有设定板没有 layout reference；
- 运镜过于抽象；
- 台词接近时长上限；
- 风格规范不完整；
- 人工覆盖可能造成角色漂移；
- 连续性只依赖文字。

## 7. API 与服务边界

### 7.1 Workflow API

建议新增：

```text
POST   /api/projects/{project_id}/dynamic-runs
GET    /api/projects/{project_id}/dynamic-runs
GET    /api/dynamic-runs/{run_id}
POST   /api/dynamic-runs/{run_id}/start
POST   /api/dynamic-runs/{run_id}/pause
POST   /api/dynamic-runs/{run_id}/cancel
POST   /api/dynamic-runs/{run_id}/resume
GET    /api/dynamic-runs/{run_id}/stages
GET    /api/dynamic-runs/{run_id}/stages/{stage_key}
PUT    /api/dynamic-runs/{run_id}/stages/{stage_key}/draft
POST   /api/dynamic-runs/{run_id}/stages/{stage_key}/regenerate
POST   /api/dynamic-runs/{run_id}/stages/{stage_key}/confirm
POST   /api/dynamic-runs/{run_id}/stages/{stage_key}/compile
GET    /api/dynamic-runs/{run_id}/stages/{stage_key}/diagnostics
GET    /api/dynamic-runs/{run_id}/artifacts
POST   /api/dynamic-runs/{run_id}/delivery/export
```

接口必须校验项目类型、章节归属、阶段依赖、运行状态和 revision；不能只依赖前端禁用按钮。

### 7.2 Prompt Compiler API/Service

编译器作为后端服务模块，不直接从 HTTP 层拼接字符串：

```text
DynamicPromptCompiler
├── normalize_inputs
├── build_asset_visual_anchors
├── plan_references
├── derive_continuity
├── compile_image_bundle
├── compile_video_bundle
├── render_provider_prompt
├── validate_bundle
└── build_provenance
```

通用 Prompt IR 能力与动态漫的剧本/连续性规则分离。动态漫流水线负责把阶段输出转换成 IR；通用编译器负责渲染、参考图、Provider 能力、诊断和快照。

### 7.3 物化边界

阶段输出先保存为 `workflow_stage.normalized_output_json`。确认后由 materializer 写入：

```text
stage 0 → novel_analysis / run stage artifact
stage 1 → AssetVisualAnchor + asset mapping
stage 2 → run stage artifact
stage 3 → screenplay
stage 4 → shot
stage 5a/5b → shot prompt fields + prompt compilation snapshot
stage 6 → existing media/audio/subtitle/composition tables
```

物化必须检查输入 revision。冲突时返回“内容已被修改”，保留阶段输出，不覆盖用户内容。

## 8. 阶段编排、恢复与失败策略

### 8.1 阶段屏障

阶段不能一次性全部入队：

```text
阶段0 → 阶段1 → 阶段2 → 人工确认点
                                   ↓
阶段3 → 阶段4 → 阶段5a/5b
                          ↓
                    阶段6a 图片/关键帧
                    阶段6b 视频
                    阶段6c 配音
                    阶段6d 字幕
                    阶段6e 合成
```

关键帧可按镜头并行；连续视频按连续段顺序生成；视频和音频可并行，但字幕依赖音频或明确的对白时间；合成依赖明确的当前视频、音频、字幕和 BGM 选择。

### 8.2 恢复

启动或运行恢复时：

1. 读取未终态 WorkflowRun；
2. 检查每个 Stage 和子 Job；
3. 已完成且输入签名匹配的阶段直接复用；
4. 有 Provider task ID 的任务继续轮询；
5. 有本地结果但未下载完成的任务只继续下载；
6. 没有 task ID 且无法确认提交结果的任务标记 `interrupted/needs_review`，不自动重新提交可能重复计费；
7. 部分成功阶段只补失败/缺失目标；
8. 所有子任务终态后由协调器推进下一阶段。

### 8.3 取消和暂停

- 暂停：停止继续入队，已运行的外部任务按 Provider 能力继续或协作式取消；
- 取消：取消尚未开始的子任务，运行中的 Provider 任务不承诺可撤回；
- UI 必须显示“已停止后续编排，已提交任务可能继续运行”的语义。

## 9. 音频、字幕和合成兼容修复

这些是动态漫交付所必需的基础修复，不另起一套音视频系统：

1. 统一剧本对白结构，使 `scene.dialogue` 能生成稳定 dialogue line ID；
2. 让音频生成按对白行建立可恢复 Job，并记录输入签名、说话人、音色和源文本快照；
3. 允许一个分镜多条对白，不再用一个 shot 的 current audio 覆盖所有句子；
4. 字幕与对白行/音频行绑定，生成后允许人工校正时间轴；
5. SRT/烧录使用实际视频时间轴，处理 xfade 导致的时长变化；
6. 合成前列出缺失视频、音频和字幕，不静默跳过；
7. 合成输入保存版本和签名，重启后同步 `chapter_composition` 与 Job 状态；
8. 合成所有输入路径统一通过 `FileService.resolve_project_path` 校验；
9. BGM 校验必须属于当前项目；
10. 章节删除时清理音频、字幕关联文件和合成文件。

## 10. 前端组件与状态

### 10.1 组件

```text
FreeCreateShell
├── FreeProjectContext
├── FreeProjectSwitcher
├── FreeModeTabs
└── Outlet

PipelineWorkspace
├── PipelineRunHeader
├── PipelineStageRail
├── PipelineStageHeader
├── PipelineArtifactPreview
├── PipelineGatePanel
├── PipelineRevisionNotice
├── PipelineErrorPanel
└── PipelineActionBar

阶段组件
├── SourceBreakdownStage
├── CharacterBiosStage
├── RhythmStage
├── DetailedScriptStage
├── StandardStoryboardStage
├── ImagePromptStage
├── VideoPromptStage
└── DeliveryStage
```

### 10.2 Hook 查询维度

```text
usePipelineRun(projectId, runId)
usePipelineStages(runId)
usePipelineStage(runId, stageKey)
usePipelineArtifacts(runId, stageKey)
usePipelineJobs(runId, stageKey)
usePipelineGate(runId, stageKey)
```

真实阶段状态来自服务器。localStorage 只保存上次打开的项目/run 偏好，不保存阶段进度、确认状态或任务结果。

### 10.3 提示词页面

阶段5a/5b每个镜头显示三个层次：

```text
生成稿
用户修改
最终发送稿
```

同时展示：

- 中文可复制提示词；
- 英文备用提示词；
- 负面提示词；
- 时间轴；
- `@图片N` 参考映射；
- 实际上传参考图；
- 诊断；
- 编译器版本和输入 hash；
- Provider 实际请求快照。

不把 JSON 编辑器作为普通用户主界面；高级用户可以查看原始 JSON/Prompt IR。

## 11. 测试与验收标准

### 11.1 工作流与数据

- 源文/梗概二选一校验；空文本阻断；源文快照和签名持久化；
- 同一章节重复启动返回活动运行，不创建重复运行；
- 阶段依赖不能跳过；阶段确认前不能物化下游；
- 上游修改后下游标记 stale；旧版本保留；
- 手工章节与流水线章节镜头编号和合成结果隔离；
- 运行刷新页面、重启服务后恢复正确阶段和任务状态；
- 同一 stage/target/input signature 不重复入队；
- 取消、暂停、部分失败和恢复状态可见。

### 11.2 Prompt IR 与编译器

- 相同输入、素材版本、Provider 能力和 compiler version 的编译结果确定性一致；
- 页面最终提示词、媒体快照和 Provider 捕获请求一致；
- `get` 与 `list` 对同一分镜得到一致的连续性编译结果；
- 自由制作和常规分镜走同一编译器；
- 图片提示词不含视频时间轴和动作完成结果；
- 视频提示词含单一主要动作、主运镜、明确结束状态；
- 时间轴从0开始、无间隙、无重叠、末段等于 Provider 有效时长；
- 对白、旁白、音效 cue 实际进入最终视频计划；
- `continuous`、`elliptical`、`same_scene_cut`、`scene_change` 有独立继承规则测试；
- 多图/单图 Provider 的 `@ImageN` 与实际数组一致；
- 设定板和 clean reference 用途分离；
- 角色、场景、物品锚点和负向约束稳定合并；
- 清空人工覆盖后恢复自动编译结果；
- 上游输入变化时旧覆盖显示基于旧 hash，不静默使用。

### 11.3 媒体与交付

- 关键帧签名匹配时复用，不匹配时重新生成；
- 连续视频按连续段顺序生成，不能提前引用不存在的上一镜末帧；
- TTS、字幕和合成进入统一任务恢复体系；
- 合成前显示缺失镜头并要求明确选择；
- 成片输入、输出和字幕时间轴一致；
- Markdown 七阶段文件内容与结构化阶段结果一致；
- ZIP 包包含全部文件、版本信息和 README；
- 现有手工分镜、默认场景、音频/合成加载错误及 Router 测试继续通过。

### 11.4 验证命令

实现阶段按子项目执行，禁止在 `D:\LyProject` 根目录运行全量命令：

```powershell
cd D:\LyProject\Other\ai-comic-platform\backend
pytest -q

cd D:\LyProject\Other\ai-comic-platform\frontend
npm test -- --run
npm run lint
npm run build
```

测试过程中不调用真实 Provider，使用现有 Mock Provider/fixture，不生成或保留测试媒体数据。

## 12. 实施分期

### Phase 1：数据与运行骨架

- 新增 workflow_run/workflow_stage 迁移和模型；
- 扩展 Job 关联、签名和幂等字段；
- 增加 Workflow API、状态机、恢复和阶段门禁；
- 增加运行隔离测试；
- 不改现有手工分镜页面行为。

### Phase 2：阶段0–2与确认点

- 源文快照；
- 拆解、人设、节奏输出；
- 阶段编辑、确认、重做和 stale 标记；
- PipelineWorkspace 基础页面；
- Markdown 阶段产物预览。

### Phase 3：结构化提示词编译器

- VisualStyleSpec；
- AssetVisualAnchor；
- ShotIntent/ContinuityContext；
- ReferencePlan；
- Prompt IR；
- 图片/视频 Provider-neutral 编译；
- Provider-specific 渲染；
- 诊断和 provenance；
- 页面最终发送稿预览。

### Phase 4：阶段3–5与媒体衔接

- 详细剧本、标准分镜物化；
- 图片/视频提示词阶段；
- 关键帧与视频按签名和依赖生成；
- 连续镜头顺序调度；
- 版本与下游失效。

### Phase 5：阶段6交付与音视频修复

- 对白行级 TTS Job；
- 字幕行级绑定和时间轴校正；
- BGM、合成预检、缺镜头策略；
- Markdown/ZIP 导出；
- 端到端 Mock Provider 测试。

## 13. 风险与取舍

| 风险 | 处理 |
|---|---|
| 现有 `CustomShotsPage` 已很大 | 新建流水线页面和上下文，不增加模式分支 |
| 外部阶段输出与现有 Schema 不一致 | 保存 raw + normalized 双份，确认后物化 |
| 角色/场景同名误合并 | 默认运行映射隔离，复用必须确认 |
| Provider 提交窗口重复计费 | 保存幂等键和 task ID；未知提交结果不自动重提 |
| 设定板污染视频参考 | reference purpose 分离，视频默认 clean reference |
| 长提示词导致平台不稳定 | IR 到 Provider 的压缩渲染，保留动作/状态/参考图/约束，删除空泛词 |
| 全自动覆盖用户修改 | run 级快照、revision、stale、确认门禁 |
| 合成静默缺镜头 | 合成前预检并要求用户选择 |
| 当前未提交修改冲突 | 仅增量新增文件/迁移/模块，实施前逐文件读取，禁止回退本地修改 |

## 14. 设计自检结论

- 没有留下未完成的实现占位；所有首版范围均已明确；
- 已明确动态漫阶段与现有项目实体的边界；
- 已明确图片与视频提示词职责分离；
- 已明确参考图、连续性、对白、音效、时长和 Provider 能力进入统一编译链；
- 已明确手工模式兼容、运行隔离、阶段门禁、重做、恢复和交付导出；
- 已明确当前未提交修改需保留；
- 未把真实 Provider、复杂分布式队列或独立重复媒体域列入首版范围。
