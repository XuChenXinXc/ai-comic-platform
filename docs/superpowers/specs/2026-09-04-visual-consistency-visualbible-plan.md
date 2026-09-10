# 视觉语义重解释治理：差距分析与分阶段改造计划

> 日期：2026-09-04
> 范围：AI 漫剧平台后端，改造主战场为 `backend/app/dynamic_pipeline/`（新链路），旧链路 `backend/app/services/prompt_service.py` 后续收敛。
> 依据：用户提供的《视觉语义重解释》25 条咨询分析；本项目既有设计文档 `docs/superpowers/specs/2026-08-16-dynamic-comic-pipeline-and-prompt-compiler-design.md`。

## 1. 结论摘要

1. 咨询分析批评的"最高优先级文字锚定"正是旧链路 `prompt_service.py` 的做法（`STYLE_ANCHOR`，`backend/app/services/prompt_service.py:18`），该链路仍在被 `shot_service` / `media_service` / `asset_service` 调用。
2. 新链路 `dynamic_pipeline` 已经实现咨询建议的一部分：结构化 `VisualStyleSpec`、`AssetVisualAnchor`（canonical 资产 + 不可变特征 + 禁用变化 + 参考图用途白名单）、分镜自然导演语言、部分漂移诊断。
3. 目前真正缺失的是"权限边界的强制执行"，共 4 个硬缺口：
   - **缺口 A：VisualBible 不是结构锁。** `VisualStyleSpec` 字段默认值只是"完全遵循项目统一画风（style_prompt）的要求"这类占位文本（`visual_anchor.py:33-54`），没有 forbiddenStyles、没有线条粗细/上色方式/材质 PBR 开关/景深开关等结构化键值，画风事实仍然只存在用户自由文本 `project.style_prompt` 里。
   - **缺口 B：没有视觉冲突检测器。** 全库没有 PBR / photorealistic / cinematic / volumetric / ray tracing 禁词表，也没有"粗笔 vs 精细轮廓"同级语义冲突校验。
   - **缺口 C：没有风格一致性评分。** 不存在 0–100 加权评分与 <85 重写 / <70 阻断机制。
   - **缺口 D：视频层没有 Motion Style Lock / Temporal Lock。** 视频编译只有 "Lighting/Style" 行（`prompt_compiler.py:1087`），没有"仅对二维绘画做运动延展、动态元素禁止物理化"的协议文本；旧链路 `natural_video_prompt` / `director_plan` 同样没有。
4. 改造思路一句话：**把美术决策权收回到唯一的结构化 VisualBible，下游模块只产出事实与调度，编译出口用确定性校验器兜底，视频层加时间锁。**

## 2. 两条链路现状

### 2.1 旧链路（线上主路径，仍在使用）

- `backend/app/services/prompt_service.py`：`STYLE_ANCHOR`（18 行）、`style_anchor()`（110 行）在每个资产/关键帧/视频提示词头部注入"最高优先级"文字；角色/场景/道具设定板、干净参考图、关键帧、视频提示词都在此拼接。
- 场景设定板含自由艺术指导字段：`材质与色彩设定：主色/点缀色/色彩气质`（602-604 行）、`画质与成图要求：高精度视觉效果，材质纹理清晰可见`（656-658 行）——正是咨询报告指出的场景 Art Direction 漂移源。
- 关键帧提示词含 `【画面美术质感】保持画风与质感…材质区分明确`（956-961 行），视频提示词经 `director_plan` 或 `natural_video_prompt` 渲染。
- 调用方：`shot_service.py:510-567`（编译关键帧/视频提示词写回 shot）、`media_service.py`（多处）、`asset_service.py`。
- 该链路没有结构化 IR、没有参考图用途白名单，漂移只能靠文字约束。

### 2.2 新链路（dynamic_pipeline，咨询建议的部分实现）

- 阶段目录：`0-source → 1-character-bios → 2-rhythm → 3-script → 4-storyboard → 5a-image / 5b-video → 6-delivery`（`stage_catalog.py:28-37`）。
- 结构化编译：`VisualStyleSpec` / `AssetVisualAnchor` / `ShotIntent` / `ContinuityContext` / `ReferencePlan` / `HumanOverrideSet` → `CompiledPromptBundle`（`prompt_ir.py`）。
- 编译入口：`prompt_compiler.py` 的 `compile_prompt_bundle` / `compile_image_prompt` / `compile_video_prompt`；`_style_text()`（730 行）把风格字段渲染进每个提示词一次；`_negative_text()`（747 行）合并 `global_negative_rules` 与锚点 `forbidden_changes`。
- 参考图：`reference_plan.py` + `reference_resolver.py`，用途白名单 `clean_identity / layout_reference / prop_identity / keyframe_reference`，设定板不作为生成参考（`visual_anchor.py:593-687`）。
- 诊断体系：`diagnostics.py` 注册码集合（8-28 行），编译出口强制所有诊断码已注册（`prompt_compiler.py:1269-1271`），门禁由 `diagnostics_status` 判定 ready/warning/blocked。
- 画风来源：阶段 5a/5b 执行时读取项目当前 `style_prompt` 构建 `VisualStyleSpec`（`prompt_stage_service.py:1530-1558`）；run 快照中的 style_prompt 仅作兜底。
- ⚠️ 默认画风本身即混合风：`api/schemas.py:23-28` 的 `DEFAULT_STYLE_PROMPT` 含"电影感…层次分明的电影光影…真实清晰的微表情"，新建项目即自带电影写实语义。校验器需区分「项目画风原文」（唯一事实来源，不校验）与「下游生成提示词」（校验对象）。
- 前端已展示 gate diagnostics（`PipelineWorkspacePage.tsx` 的 `DiagnosticList`），新增诊断可自动可见。

## 3. 差距清单（对照 25 条建议）

> 每条：现状（file:line）→ 差距 → 建议改动点 → 风险。

| # | 咨询建议 | 现状 | 差距与建议 |
|---|---|---|---|
| 10 | VisualBible 结构化视觉 DNA | `VisualStyleSpec` 仅占位文本（`visual_anchor.py:33-54`、`prompt_ir.py:18-32`） | 高。升级为结构化键值（line/coloring/shading/material/lighting/DoF/realism/forbiddenStyles），见 Phase 1 |
| 11 | 三层信息模型 | 无显式分层 | 中。Immutable 进 VisualBible；Controlled（天气/时间/固有色/光源方向）进 AssetVisualAnchor 受控字段；Content 保持现状 |
| 12 | 角色只输出身份、不负责怎么画 | `CharacterAssetOutput` 身份字段齐全（`schemas.py:203-236`），但混入 `lighting`/`atmosphere` 自由字段（`schemas.py:228-229`）；`character_sheet.md:26/36/50` 有"精致、高精度、发丝眼眸清楚可见"写实化文案；`asset_extract.md:67-68` 要求补"布光和造型气质" | 高。删 `lighting/atmosphere`、模板去写实词、提取提示词加禁写画法词，见 Phase 2 |
| 13 | 场景只输出空间事实 | `SceneAssetOutput` 有 `main_color/accent_color/color_quality/main_light/fill_light/atmosphere/lighting/color_mood`（`schemas.py:307-318`），全部 LLM 自由生成；提取调用连 `style_prompt` 都不注入（`asset_service.py:914-938`）；模板直消费（`scene_sheet.md:22-23/68-70`） | 高。改为 VisualBible 派生默认 + 仅事实性差异，见 Phase 1/2 |
| 14 | 道具材质降维 | `prop_sheet.md:12-14` 无降维约束，仅 `:41` 一句"受控边缘反光"；默认负向无 PBR/反射/折射条款（`prompt_service.py:236-248`） | 高。模板加材质降维段 + 负向补条款，见 Phase 2 |
| 15 | 分镜禁美术重写 | `dynamic_storyboard.md` 模板本身干净（`:24` 禁生成 image/video_prompt），但 `frame_description`/`prompt_intent`（`storyboard_stage.py:72/75`）无美术词禁令；`shot_director_plan.md:39-41` 的 `visualTone` 字段正式允许逐镜写灯光/明暗/色调并经 `director_plan.py:1041-1043` 进入视频提示词 | 高。收紧 visualTone + 分镜语义诊断加美术词扫描，见 Phase 3 |
| 16 | 视频 Motion Style Lock | 全库零匹配；最接近的是静态画风继承（`natural_video_prompt.py:516-521`、`director_plan.py:1031-1037`）；`natural_video_prompt.py:410-415/633-635` 反而正向写"自然摆动/光线细微变化"；Temporal 漂移零防范 | 高。协议放渲染器尾部（注意 `shot_video_optimize.md:23-26` 会删"锁定/保持"措辞），见 Phase 0 |
| 17 | Canonical Asset 唯一事实来源 | 新链路基本达标（锚点只来自 DB profile、按 asset_id 绑定：`prompt_stage_service.py:829-927`、`prompt_compiler.py:707-727`）；残留：旧链路按名 fuzzy 解析（`shot_service.py:3199-3232`）、materializer 名称回退（`materializer.py:612-629`）、`natural_video_prompt.py:250/493-494` 按名匹配、bio 与资产双事实来源（`prompt_stage_service.py:781-826`） | 中。见 Phase 5 |
| 18 | 越往下游权限越少 | 新链路分层基本成立；但 `HumanOverrideSet` 可整段替换 image/video 提示词且 style 段随之消失（`prompt_ir.py:395-410`、`prompt_compiler.py:1275-1286`） | 中。覆盖只允许 shot 级字段、style 段不可覆盖，见 Phase 0/1 |
| 19 | 视觉冲突检测器 | 不存在集中校验器；仅负向文本含"3D建模渲染"（`prompt_service.py:240-248`）。PBR/photorealistic/cinematic/volumetric/ray tracing 全库 0 命中 | 高。纯函数插在 `compile_prompt_bundle` 出口（`prompt_compiler.py:1252-1272`），新诊断码登记进 `diagnostics.py:8-28`，见 Phase 0 |
| 20 | 同级语义冲突检测 | 不存在；`style_incomplete` 只查字段全空（`prompt_compiler.py:531-543`） | 中。结构冲突用 `VisualStyleSpec` model_validator 互斥断言，文本冲突用词对启发式，见 Phase 0/1 |
| 21 | 风格偏离评分 0–100 | 不存在；门禁仅 ready/warning/blocked 三态（`diagnostics.py:66-91`），无任何 scoring LLM 调用 | 中。前置确定性评分（零成本，插 `prompt_stage_service.py:1604-1616`）+ 后置 LLM 视觉评分（media 完成回调），见 Phase 4 |
| 22 | Style Block 唯一注入、不膨胀 | 新链路单点注入但语义膨胀：7 个默认占位同义 + Style note 重复（`prompt_compiler.py:731-743`）、lighting 双注入（`1029/1060/1087`）、Negative 双份（`1094/1103`）；旧链路 `style_anchor` 多轨注入（`prompt_service.py:807-861`、`shot_service.py:3689`+`media_service.py:1941` 同一分镜两遍） | 中。默认占位去重渲染 + 删重复行，见 Phase 1；旧轨收敛见 Phase 5 |
| 23 | 自然语言禁止越权 | 导演式语言已成型（`shot_director_plan.md:20/40`、`shot_video_prompt.md:62`、optimize `:41-44`）；残留越权：`visualTone`（`shot_director_plan.md:39-41`）、`camera_library.py:134-135`"景深 f1.8 背景模糊"、`shot_image_prompt.md:28`"写光线" | 中。见 Phase 3 |
| 8 | 五类漂移监控 | Identity 有 `character_drift_risk`、`style_incomplete` 等；Spatial/Temporal 无专用诊断 | 中。补齐诊断矩阵，见 Phase 4 |

✅ 三路审计全部完成：资产侧（角色/场景/道具）、分镜/视频侧、基础设施侧（compiler/引用/校验/评分/五类漂移矩阵）。

### 3.1 五类漂移诊断覆盖矩阵（现状）

| 漂移类 | 已有检测（code → 位置） | 完全无检测 |
|---|---|---|
| Style 画风 | `style_incomplete`（`prompt_compiler.py:536`）、`validate_natural_style` 模板措辞检查（`natural_video_prompt.py:145-161`） | 禁词命中、同级语义冲突、数值评分 |
| Identity 身份 | `character_drift_risk`（`prompt_compiler.py:501`）、`character_core_visual_*_empty`（`stage_gates.py:539-579`）、`storyboard_prop_alias_drift`（`storyboard_stage.py:1846`）、`design_sheet_as_reference` | 生成后图像级身份比对 |
| Asset 资产 | `asset_unresolved`、`reference_missing/wrong_type/index_mismatch`、签名失效重生成（`signature_service.py:87-103`） | 生成后资产结构/材质漂移检测 |
| Spatial 空间 | 轴/站位仅透传不校验（`prompt_ir.py:97-105`）；场景字段必填（`stage_gates.py:374-388`） | 跨镜空间一致性、轴线合规检测 |
| Temporal 时间 | 时间轴文本层完备：`timeline_discontinuous/dropped/clamped`、`dialogue_over_time`、`continuous_previous_end_frame_missing`、`storyboard_time_phase_drift` | 成片级帧间画法漂移测量（Temporal Consistency Controller 的闭环） |

## 4. 六个核心模块现状矩阵

| 模块 | 现状 | 等级 |
|---|---|---|
| 1 Visual Bible | 只有用户自由文本 `style_prompt` + 占位式 `VisualStyleSpec` | 缺 |
| 2 Asset Canon | `AssetVisualAnchor` + 参考图用途白名单 + 修订版本签名 | 基本有 |
| 3 Prompt Composer | `prompt_compiler` 纯函数编译，风格单点注入 | 有 |
| 4 Storyboard Director | 自然导演语言 + 部分校验 | 部分有 |
| 5 Visual Consistency Validator | 无 | 缺 |
| 6 Temporal Consistency Controller | 无 | 缺 |

## 5. 分阶段实施计划（草案，待确认后细化到任务级）

### Phase 0 — 止血：确定性视觉校验器 + 视频时间锁（无架构改动）

1. 新增 `backend/app/dynamic_pipeline/visual_validator.py`：
   - 禁词表（按咨询建议 19）：`3D`、`PBR`、`photorealistic`、`realistic skin`、`cinematic`、`cinematic lighting`、`ray tracing`、`volumetric lighting`、`octane render`、`unreal engine`、`realistic reflection`、`physically accurate`、`soft depth of field`、`realistic rain/fire/water` 等（中文同义词：真实反射、电影感布光、体积光、物理精确、真实材质、每根发丝清晰可见、背景柔和虚化）。
   - 同级语义冲突对（建议 20）：`粗笔↔精细纤细轮廓`、`写意↔每根发丝`、`二维↔真实空间体积感`、`无景深↔背景虚化`、`低饱和↔高饱和重彩` 等，按 VisualBible 键值对做结构化判定，文本冲突做启发式词对判定。
   - 输出注册诊断码（需在 `diagnostics.py:8-28` 注册）：`visual_conflict_forbidden_term`（blocking）、`visual_conflict_semantic`（warning）、`style_score_low`（后续 Phase 4）。
2. 接线点：
   - 首选 `prompt_compiler.compile_prompt_bundle` 编译出口（`prompt_compiler.py:1252-1272`，gate 判定之前）新增纯函数 `_style_forbidden_diagnostics(prompt, negative_prompt)`，对 `image.prompt` / `video.prompt` / **final（含人工覆盖）** 做校验；`compile_image_prompt` / `compile_video_prompt` 单通道出口同样校验。
   - 次选 `PromptStageService._compile` 输出前（`prompt_stage_service.py:1604-1605`）复用同一函数做集中二次校验（单一实现，不双写）。
   - 新诊断码必须登记进 `PROMPT_DIAGNOSTIC_CODES`（`diagnostics.py:8-28`），否则编译出口未注册码断言会抛错（`prompt_compiler.py:1269-1271`）。
   - 校验口径：项目画风原文（`style_prompt`/VisualBible）是唯一事实来源、**不校验**；校验对象是下游生成提示词与其是否越权（如"最高优先级"文字锚定不会阻止的 PBR/电影光词）。
3. 视频时间锁（Motion/Temporal Style Lock）：
   - 协议文本四个沉淀点（渲染器尾部，不放导演计划自由文本——`shot_video_optimize.md:23-26` 会删除"锁定/保持/稳定"措辞）：
     ① `prompt_compiler.py:_compile_video_bundle` 的 `Lighting/Style` 行后追加 `Motion style lock:` 行；
     ② `director_plan.py:render_director_video_prompt`（1031-1044 行附近）；
     ③ `natural_video_prompt.py:render_natural_video_prompt`（512-527 行附近）；
     ④ `h3_adapter.py:_REF2VA_SYSTEM_PROMPT`（338-364 行附近）。
   - 协议内容（咨询建议 16）：「视频仅对现有二维绘画画面进行运动延展；全程保持原始关键帧的二维绘画属性；不得将人物、建筑、服装、器物、水体、雨水或光影重解释为真实摄影/3D/PBR 材质；运动过程中线条粗细、墨色浓度、上色方式和笔触质感保持稳定；人物面部不得因转头、眨眼或嘴部运动提高写实程度；雨、水、烟、火、光等动态元素继续采用二维写意动画表达。」
   - 负向补齐（反动态写实化）：`visual_anchor.py:42-48` global_negative_rules、`natural_video_prompt.py:439-455`、`director_plan.py:929-942` 增加"写实化动态、真实物理模拟、流体、PBR"条款；`natural_video_prompt.py:410-415/633-635` 的物理化措辞改为二维写意表达。
4. 旧链路止血：`prompt_service.py` 的 `compile_keyframe_prompt` / `compile_video_prompt` / `compile_asset_prompt` 出口复用同一个 validator（只读校验 + 诊断返回，不改变旧提示词文本，先在分镜编译流程暴露）。
5. 测试：`backend/tests/test_visual_validator.py` + 扩展 `test_dynamic_prompt_compiler.py`（断言禁词触发 blocking、覆盖稿同样校验、时间锁文本进入 video.prompt）。

### Phase 1 — 结构化 VisualBible（真正的风格锁）

1. `VisualStyleSpec` 升级（`prompt_ir.py:18-32`）：新增结构化子模型 `LineSpec`（type/thickness/continuity）、`ColoringSpec`（method/saturation/color_count）、`MaterialSpec`（method/pbr:bool/photographic_reflection:bool）、`LightingSpec`（realism/volumetric/cinematic_glow）、`depth_of_field.enabled`、`realism`（character/environment 数值）、`texture`、`forbidden_styles: list[str]`；自由文本字段保留作补充。`_style_text`（`prompt_compiler.py:730`）改为渲染结构化键值；默认占位去重（默认时只渲染 style_prompt + immutable_rules），删 lighting 双注入（`1029/1060`）与 Negative 双份（`1094/1103`）。
2. 同级互斥断言：`VisualStyleSpec` 的 model_validator 校验字段互斥（如 `forbidden_styles` 含 3D 时 material.pbr 必须为 false）。
3. 项目级持久化：`project` 表新增 `visual_bible_json`（`tables.py:28-41`），API（`projects.py` / `schemas.py`）读写；`prompt_stage_service.py:1556` 构建 style 时优先读 VisualBible JSON，缺省时从 `style_prompt` 推导默认结构（映射表：水墨→粗笔焦墨/低饱和/墨色明暗块/PBR off…）。
4. 覆盖权限收紧：`apply_human_override`（`prompt_ir.py:380-442`）只允许覆盖 shot 级字段，style 段不可被覆盖；`build_visual_style_spec` 的 overrides 键白名单化（`visual_anchor.py:367-376`）。
5. 前端：项目设置页（`ProjectPage` / `WritingPage` 画风输入旁）展示"视觉圣经"结构化面板 + 从画风文本一键生成/重新生成（LLM 提取），保存后所有下游生效。
6. 测试：schema 校验、编译渲染含结构字段、项目读写 API、旧项目无 JSON 时兜底。

### Phase 2 — 资产模块权限边界

1. 模板收紧：`character_sheet.md` / `scene_sheet.md` / `prop_sheet.md` / `scene_master.md` 删除或降级自由艺术指导字段；场景的"主色/辅色/灯光/氛围"改为 `controlled_variables`（天气/时间/固有色/光源方向），表现方式一律回指 VisualBible；道具模板加材质降维约束（金属/玻璃/塑料用二维笔触表达，禁用 PBR/折射/高光真实）。
2. `AssetVisualAnchor` 增加受控变量字段（`prompt_ir.py:35-72`），`asset_profile_to_anchor` 投影对应字段（`visual_anchor.py:383-532`）。
3. 输出 schema（`schemas.py` 的 Asset 系列）同步调整。
4. 测试：模板渲染不含自由艺术字段、锚点受控变量投影、道具负向含材质降维。

### Phase 3 — 分镜禁美术重写

1. `dynamic_storyboard.md` / `shot_director_plan.md` / `shot_image_prompt.md` 增加"只负责导演，不重新担任美术指导"的硬约束 + 导演字段白名单。
2. 收紧/删除 `shot_director_plan.md:39-41` 的 `visualTone`（逐镜灯光/色调自由字段），改为只允许事实性差异（时间/天气/光源方向）。
3. `storyboard_stage.py` 的 `_semantic_consistency_diagnostics`（1717-1889 行附近）对 `frame_description` / `prompt_intent` 加美术词扫描（复用 Phase 0 词表）。
4. `camera_library.py:134-135` 删除"景深 f1.8 背景模糊"；该文件其他预设也自带画风词（如 `:145-146`"国漫清新画风"），统一清理为纯机位/构图描述；`shot_image_prompt.md:28`"写光线"改为从 VisualBible 取光照表达。
5. 测试：分镜输出含美术词时产生诊断；visualTone 受控字段校验。

### Phase 4 — 风格一致性评分 + 漂移矩阵

1. 前置评分（编译期、零 LLM 成本）：`prompt_stage_service._compile` 的 gate 之后（`1604-1616`）按权重（核心画风25/线条15/上色15/光影15/材质10/写实度10/色彩10）以"结构化字段完整度 + 禁词命中 + 语义冲突对"确定性扣分，<85 产生 `style_score_low`（warning，可配置自动重编），<70 走 blocked 禁生成。
2. 后置评分（生成后、需 LLM 视觉评判）：挂 media 生成完成回调（`media_queue_service` / `media_service`），分数落库并与 signature 关联；默认关闭，配置开关控制。
3. 漂移矩阵补齐：Spatial 增加 `spatial_drift_risk`（跨镜轴线/站位结构化校验）；Temporal 保持提示词层 + Phase 0 时间锁文本，成片级帧间比对（模块 6 闭环）单列后续评估（依赖视觉模型，不做首版承诺）。
4. 测试：确定性评分单测（阈值行为、开关关闭不触发）；mock LLM 后置评分。

### Phase 5 — 旧链路收敛

1. `prompt_service.py` 各 builder 的 style 注入改为共享 VisualBible 渲染（同一个 `_style_text` 等价物），去除每个模块重复写画风（建议 22）；同一分镜的两遍注入（`shot_service.py:3689` + `media_service.py:1941`）合并为一遍。
2. 关键帧/视频出口统一走 validator（Phase 0 已埋）。
3. 按名解析收紧：`_resolve_asset_name` 的 fuzzy 场景兜底（`shot_service.py:3199-3232`）改为"唯一命中 + 人工确认"或显式 asset_id；`materializer.py:612-629` 名称回退同样处理；`natural_video_prompt.py:250/493-494` 按名匹配改按 asset_id。
4. `_merge_character_anchor`（`prompt_stage_service.py:781-826`）的 bio/资产双来源合并明确冲突优先级并出诊断。
5. 视工作量评估：`shot_service` / `media_service` 调用是否整体切换为新链路编译器（不强制，评估后单独提案）。

## 6. 需要你拍板的决策点

1. **评分器成本**：风格一致性评分需每次 LLM 调用，是否默认开启还是仅批量模式？（建议：默认关闭、可配置开启）
2. **VisualBible 来源**：从 `style_prompt` 自动推导默认结构（推荐，零录入成本）还是强制用户逐项填写？
3. **阈值**：<85 重写 / <70 阻断是否采纳咨询建议原值，还是先宽松（如 <70 警告）？
4. **Phase 范围确认**：是否先只做 Phase 0（止血）+ Phase 1（VisualBible），2–5 视效果再排期？
5. **默认画风文案**：`DEFAULT_STYLE_PROMPT`（`api/schemas.py:23-28`）自带"电影感/电影光影/真实微表情"，是否替换为中性默认（如纯"二维国漫"）并把画风选择交给用户？（建议：替换，避免新项目默认即漂移）

### 6.1 已确认决策（2026-09-04）

- **阶段范围**：Phase 0–3 一起做（Phase 4 评分/漂移矩阵、Phase 5 旧链路整体收敛后续排期）。
- **评分阈值**：采纳原值 <85 自动重写 / <70 阻断（实施于 Phase 4）。
- **VisualBible 来源**：从 `style_prompt` 自动推导（关键词映射，无 LLM 成本）+ 前端可编辑覆盖，落库 `project.visual_bible_json`。
- **默认画风**：替换为中性默认「二维国漫，线条与上色风格统一，画面干净整洁」。
  - 2026-09-04 二次调整：默认画风改为**数据驱动**，不再写死在代码/前端——存 `platform_setting.default_style_prompt`（API：`GET/POST /api/settings/default-style-prompt`），新建项目留空时由 `ProjectService.create` 回填；存量项目中仍为旧系统默认或空画风的记录已一次性更新为「东方粗笔重彩·写意水墨二维国漫风」（自定义画风未动）。
- 覆盖权限（原 Phase 1 第 4 条）调整为：保留专家模式整段覆盖，改为由校验器对 final（含人工覆盖）稿兜底校验——不静默改写用户覆盖稿，只产生诊断。

## 7. 风险与边界

- 不修改、不回退当前未提交的 frontend 改动（git status 显示大量 M 文件）。
- 新旧链路并存期：validator 对旧链路只读暴露诊断、不直接改文本，避免线上行为突变。
- `human_override` 是用户显式意图：校验 final 产生诊断但不静默改写用户覆盖稿（展示 warning/blocking 由用户决策）。
- 所有新增诊断码必须注册进 `PROMPT_DIAGNOSTIC_CODES`，否则编译出口会抛错（`prompt_compiler.py:1269`）。

## 8. 验证命令

```powershell
cd D:\LyProject\Other\ai-comic-platform\backend
pytest -q
```

测试不调用真实 Provider，沿用 Mock Provider。

## 9. 实施进度（2026-09-04，Phase 0–3 已完成）

| 阶段 | 状态 | 主要产物 |
|---|---|---|
| Phase 0 校验器 + 时间锁 | ✅ | `app/dynamic_pipeline/visual_validator.py`（禁词表/语义冲突对/协议常量）、`diagnostics.py` 注册 `visual_conflict_forbidden_term`/`visual_conflict_semantic`、`prompt_compiler.py` 编译出口接线（含 final 覆盖稿）+ `Motion style lock` 行、`shot_service.py` 旧链路只读暴露；`director_plan.py`/`natural_video_prompt.py`/`h3_adapter.py` 渲染器尾部协议 + 反动态写实化负向 |
| Phase 1 VisualBible | ✅ | `VisualStyleSpec` 结构化子模型（Line/Coloring/Shading/Material/Lighting/DoF/Realism）+ 互斥校验；`build_visual_style_spec` 关键词自动推导（水墨→粗笔焦墨…）；`_style_text` 占位去重渲染；`project.visual_bible_json` 列 + 迁移 0021 + API + `prompt_stage_service` 优先读；前端 `WritingPage` 可编辑面板（自动推导/重置） |
| Phase 2 资产权限边界 | ✅ | 三份 sheet 模板去写实化文案、场景改固有色+时间/天气/光源方向受控事实、道具材质降维段；`asset_extract.md`/`asset_prompt.md` 禁写画法词硬约束；`asset_service` 提取/补全注入画风与事实约束；负向补 PBR/镜面反射/透明折射 |
| Phase 3 分镜禁美术重写 | ✅ | `dynamic_storyboard.md` 硬约束节、`shot_director_plan.md` visualTone 收紧、`shot_image_prompt.md` 光线事实化；`storyboard_stage.py` 分镜文本禁词扫描（warning）；`camera_library.py` 清画风/景深/品牌词 |
| 默认画风替换 | ✅ | `api/schemas.py` 与 `tables.py` 改为中性「二维国漫…」 |
| 测试 | ✅ | 新增 `tests/test_visual_validator.py`；各阶段更新受影响断言（保持原测试意图）；ruff 全绿；全量 pytest 见第 8 节命令 |

Phase 4（评分 85/70、Spatial 诊断、后置 LLM 评分）与 Phase 5（旧链路整体收敛）后续排期。
