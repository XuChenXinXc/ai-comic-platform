# 动态漫流水线与结构化提示词编译器实施计划

## 0. 计划范围、前置基线与保护边界

### 0.1 依据与交付目标

- 设计依据：`D:\LyProject\Other\ai-comic-platform\docs\superpowers\specs\2026-08-16-dynamic-comic-pipeline-and-prompt-compiler-design.md`。
- 本计划覆盖 `Phase 0` 至 `Phase 6`，共 7 个 Phase。
- 本计划本身只新增本文档；执行实施计划时，必须先在
  `D:\LyProject\Other\ai-comic-platform` 内读取最新 `git status`、`git diff` 和
  `git diff --staged`，把当时的本地修改记录为基线，不回退、不覆盖、不重新格式化。
- 目标是增量接入动态漫流水线和统一 Prompt IR 编译链，不建立第二套项目、分镜或媒体业务域。

### 0.2 强制保护边界：只增量接入、不动已有逻辑

1. **现有 `CustomShotsPage` 手工模式不回退。**
   - `frontend/src/pages/CustomShotsPage.tsx` 继续是 `/custom-shots` 的手工分镜入口。
   - 不在该页面增加 `mode` 分支，不把动态流水线状态塞进现有 `manual-*`、
     `free-create-project-id`、`free-create-project-ids` 或默认场景的 localStorage 键。
   - 不改变固定手工分镜位、手工提示词 override、手工 Provider 选择、手工媒体生成和手工合成行为。
2. **常规项目不回退。**
   - 常规项目仍走现有项目、章节、剧本、素材、分镜、媒体、任务和合成 API。
   - 动态流水线优先通过新增 API、服务、编排器、适配器旁路接入；只有明确列出的兼容性修补才可改旧服务。
3. **已有 Provider/media/job 行为不回退。**
   - 旧 Job 的 `workflow_run_id`、`workflow_stage_id`、`idempotency_key`、`input_signature`、`depends_on_json` 保持可空；旧任务按原恢复逻辑处理。
   - 旧媒体版本的签名、版本号、当前版本切换、Provider task ID、下载和错误脱敏语义保持不变。
   - 新的动态任务使用新增字段和新编排器，不能通过修改旧任务类型含义来“复用”或覆盖旧任务。
4. **当前工作区未提交修改不属于本计划改动。**
   - 当前已有的修改、删除、未跟踪文件、设计规格文件和迁移文件冲突均须原样保留。
   - 实施前后都要用 `git status --short` 对比基线；任何与本计划无关的差异必须停止并报告，不得顺手清理。
5. **数据库只做增量迁移。**
   - 新增迁移，不修改、删除、重命名任何旧迁移。
   - 不以 `metadata.create_all()` 替代生产迁移；`create_all()` 仅继续作为现有测试工具，不改变 `init_db()` 的 Alembic 升级路径。
   - 不删除旧数据；历史媒体、任务和 canonical 实体的 `workflow_run_id` 为空，并在新增 provenance 中标记 `legacy`。
6. **不得创建重复业务表。**
   - 不新增 `PipelineProject`、`PipelineChapter`、`PipelineShot`、`PipelineMedia`。
   - 动态运行只增加 `workflow_run`、`workflow_stage` 作为编排和版本容器，确认后的产物仍写入现有 `Chapter`、`NovelAnalysis`、`Screenplay`、`Asset`、`Shot`、`AssetImage`、`KeyframeVersion`、`VideoVersion`、`ShotAudio`、`ShotSubtitle`、`ProjectBgm`、`ChapterComposition`、`Job`。

### 0.3 执行前停止点

- 进入 `backend` 后执行 `alembic heads` 或项目等价命令，确认唯一有效 head。
- 当前工作区中若出现旧迁移同一路径的删除/未跟踪冲突，**不得修复该冲突**；只读取实际文件内容并将其视为基线。
- 如果已有多 head 不是本计划新增迁移造成的，停止 Phase 0，交由主编排器决定，不通过修改旧迁移“合并”分支。
- 每个 Phase 完成前只允许提交该 Phase 允许路径中的新增/兼容性修补文件；主编排器复核 `git status` 和 diff 后才能进入下一阶段。

### 0.4 稳定阶段合同

阶段键只能使用以下稳定字符串，不用前端数字分支代替：

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

阶段状态必须区分任务状态、产物可用状态和人工确认状态；至少支持：

```text
pending / queued / running / blocked / ready / confirmed / stale /
partial_failed / failed / interrupted / cancelled
```

默认屏障为：

```text
0-source → 1-character-bios → 2-rhythm → 人工确认点
                                      ↓
3-script → 4-storyboard → 5a-image-prompts / 5b-video-prompts
                                      ↓
6-delivery（图片/关键帧、视频、配音、字幕、BGM、合成子任务）
```

同一个 `WorkflowRun` 的输入以启动快照为准；不直接读取用户之后正在编辑的普通章节正文。

---

## Phase 0：数据迁移与 WorkflowRun/WorkflowStage 骨架

### 0.1 新增增量迁移与 SQLAlchemy 表定义

- **目标**：建立可保存运行快照、阶段版本、门禁结果、任务依赖和 Prompt provenance 的最小数据骨架，同时保留所有旧数据。
- **允许修改/新增的精确路径**：
  - 新增：`backend/alembic/versions/20260816_0015_dynamic_comic_workflow.py`
  - 修改：`backend/app/models/tables.py`
  - 新增：`backend/tests/test_dynamic_workflow_migration.py`
- **实现要点**：
  1. 新迁移的 `down_revision` 以 Phase 0 执行时探测到的唯一有效 head 为准；不能猜测、不能修改旧迁移来适配。
  2. 新增 `workflow_run` 表，字段至少为：
     `id`、`project_id`、`chapter_id`、`kind`、`schema_version`、`status`、`current_stage`、
     `source_kind`、`source_snapshot_json`、`config_snapshot_json`、`asset_mapping_json`、
     `input_signature`、`revision`、`error`、`created_at`、`started_at`、`updated_at`、`finished_at`。
     `kind` 默认 `dynamic_comic_0_6`；`source_kind` 只允许 `full_text`/`synopsis`；状态约束使用本计划的稳定状态集合。
  3. 新增 `workflow_stage` 表，字段至少为：
     `id`、`workflow_run_id`、`stage_key`、`ordinal`、`status`、`input_signature`、
     `raw_output_json`、`normalized_output_json`、`draft_output_json`、`output_refs_json`、
     `gate_result_json`、`attempt`、`error`、`created_at`、`started_at`、`updated_at`、
     `finished_at`、`confirmed_at`、`confirmed_revision`。
     对 `(workflow_run_id, stage_key, attempt)` 建唯一约束；为当前阶段、状态和运行 ID 建索引。
  4. 扩展 `job` 的可空字段：`workflow_run_id`、`workflow_stage_id`、`idempotency_key`、
     `input_signature`、`depends_on_json`。旧行保持 NULL 或空 JSON，不回填为虚构的运行归属。
  5. 为最终产物保留统一 provenance：在 `asset_image`、`keyframe_version`、`video_version` 增加可空的
     `prompt_ir_json`、`provenance_json`；在 `asset_image` 增加 `reference_purpose`、`approved`；在 `asset` 增加
     `visual_anchor_json`。旧媒体的新增 provenance 只标记
     `{"legacy":true,"workflow_run_id":null}`，不删除或重写原 Prompt 快照。
  6. 为流水线镜头补充可追踪字段：在 `shot` 增加可空 `external_key`、`action_events_json`、
     `dialogue_cues_json`、`sound_cues_json`、`continuity_json`、`pipeline_metadata_json`。
     现有 `start_state`、`action`、`end_state`、`next_hook`、`transition_from_prev`、`dialogue_json` 继续保留并作为兼容 canonical 字段。
  7. 为行级音频/字幕和交付关联预留增量字段：
     - `shot_audio`：`dialogue_line_id`、`line_order`、`source_text_snapshot`、`input_signature`、`provenance_json`；
     - `shot_subtitle`：`dialogue_line_id`、`audio_id`、`provenance_json`；
     - `chapter_composition`：可空 `workflow_run_id`、`input_signature`、`preflight_json`、`provenance_json`。
  8. 将旧的 `shot_audio` 当前唯一索引改造成兼容索引：NULL `dialogue_line_id` 仍保持每镜一个 legacy 当前音频；非 NULL 行按 `(shot_id, dialogue_line_id)` 保证每条对白独立当前版本。迁移只调整索引，不删除现有音频记录。
  9. `workflow_run.project_id` 和 `chapter_id` 使用外键；“项目必须 `is_free_create=1`、章节必须属于项目、同章节最多一个可物化活动运行”由服务层短事务加锁/条件更新校验，不能用 SQLite CHECK 跨表替代。
- **依赖**：当前 Alembic head、`backend/app/models/tables.py`、现有 0011–0014 迁移内容、当前工作区基线。
- **测试文件和验证命令**：
  - 新增/执行：`backend/tests/test_dynamic_workflow_migration.py`、`backend/tests/test_core_database.py`（只验证，不为本计划改写既有断言）。
  - 命令：
    ```powershell
    cd D:\LyProject\Other\ai-comic-platform\backend
    alembic heads
    pytest tests/test_dynamic_workflow_migration.py tests/test_core_database.py -q
    ```
- **完成标准**：
  - 新鲜数据库 `alembic upgrade head` 建出新增表和字段；重复升级幂等。
  - 从当前 head 迁移含旧项目、章节、Job、媒体、音频和字幕的数据库后，旧行数量、外键关系和文件路径不变。
  - 旧媒体 provenance 明确为 legacy，`workflow_run_id` 为空；没有任何旧迁移被修改，没有调用 `create_all()` 代替升级。

### 0.2 增量字段的表模型与 schema 合同

- **目标**：让 Python 层能以强类型读写新表和新增 JSON 字段，并为后续状态机、Prompt IR、任务幂等提供统一模型。
- **允许修改/新增的精确路径**：
  - 修改：`backend/app/models/tables.py`
  - 修改：`backend/app/schemas.py`
  - 新增：`backend/app/dynamic_pipeline/__init__.py`
  - 新增：`backend/app/dynamic_pipeline/schemas.py`
  - 新增：`backend/tests/test_dynamic_pipeline_schemas.py`
- **实现要点**：
  1. `dynamic_pipeline/schemas.py` 定义运行、阶段、门禁、产物引用、资产映射、对白 cue、音效 cue 和交付清单模型；额外字段使用 `extra="forbid"`，未知 Provider 输出先进入 raw JSON，不直接绕过 normalized schema。
  2. 明确定义 `StageKey`、`WorkflowStatus`、`StageStatus`、`DeliverySubstage` 和 `RevisionConflict`，后续 API、Job 和前端共享同一字符串合同。
  3. `tables.py` 的 `ALL_TABLES` 只增加 `workflow_run`、`workflow_stage`；旧表对象只做与新迁移一致的字段增量，不调整原有列默认值或约束语义。
- **依赖**：0.1。
- **测试文件和验证命令**：
  - `backend/tests/test_dynamic_pipeline_schemas.py`：校验阶段键、源文二选一、时间轴 cue、资产映射和未知字段拒绝。
  - 命令：
    ```powershell
    cd D:\LyProject\Other\ai-comic-platform\backend
    pytest tests/test_dynamic_pipeline_schemas.py -q
    ```
- **完成标准**：所有后续阶段可从同一 schema 读写运行快照和阶段 JSON；旧 `JobCreate`、Provider、Shot、Screenplay schema 的调用方式保持兼容。

### Phase 0 验收门禁

- 迁移只增量，不删数据、不改旧迁移、不使用 `create_all` 替代迁移。
- `workflow_run`/`workflow_stage` 能保存历史 attempt 和旧版本，旧数据明确 legacy。
- 迁移测试、核心数据库测试通过；任一既有工作区差异变化均阻止进入 Phase 1。

---

## Phase 1：Workflow API、状态机、阶段屏障与恢复

### 1.1 新增状态机、仓储和运行协调服务

- **目标**：实现运行创建、阶段依赖、确认、重做、stale、暂停、取消、恢复和幂等的服务端真源。
- **允许修改/新增的精确路径**：
  - 新增：`backend/app/services/dynamic_workflow_service.py`
  - 新增：`backend/app/services/dynamic_workflow_repository.py`
  - 新增：`backend/app/dynamic_pipeline/stage_catalog.py`
  - 新增：`backend/app/dynamic_pipeline/state_machine.py`
  - 新增：`backend/tests/test_dynamic_workflow_state.py`
  - 新增：`backend/tests/test_dynamic_workflow_repository.py`
- **实现要点**：
  1. `stage_catalog.py` 固定 8 个阶段键及 ordinal、父子依赖、默认门禁和可重试策略；`6-delivery` 内部再用 `image/keyframe`、`video`、`audio`、`subtitle`、`composition` 子任务，不创建重复阶段表。
  2. `state_machine.py` 以纯函数实现合法转移：不能跳过前置阶段，`confirmed` 前不能物化下游；输入 revision 或上游输出变化时，所有依赖下游保留旧 attempt 并标记 `stale`。
  3. 运行创建使用短事务保存项目/章节/运行快照；Provider 调用、文件写入、Job 入队均在事务提交后执行。已有自由项目只能创建属于自身的新章节；同一章节已有可物化活动运行时返回冲突或活动运行，不重复创建。
  4. 幂等键固定由 `workflow_stage_id + target_type + target_id + input_signature + job_type` 计算；相同组合返回现有活动 Job 或已完成产物，不重复提交 Provider。
  5. 运行暂停只停止后续入队；取消只取消未开始任务，并在返回值中说明已提交的 Provider 任务可能继续运行；恢复必须携带 expected revision。
  6. 服务层校验项目 `is_free_create=1`、章节归属、运行归属、阶段键、阶段状态、请求 revision、Provider 能力和门禁结果，不能只依赖前端按钮禁用。
- **依赖**：Phase 0；现有 `project`、`chapter`、`Job` 事务和 `FileService`。
- **测试文件和验证命令**：
  - `backend/tests/test_dynamic_workflow_state.py`：合法/非法转移、确认门禁、上游 stale、历史 attempt 保留、revision 冲突。
  - `backend/tests/test_dynamic_workflow_repository.py`：同章节活动运行幂等、Job idempotency、旧任务 NULL 字段兼容。
  - 命令：
    ```powershell
    cd D:\LyProject\Other\ai-comic-platform\backend
    pytest tests/test_dynamic_workflow_state.py tests/test_dynamic_workflow_repository.py -q
    ```
- **完成标准**：所有状态变更可由纯函数测试覆盖；重复启动不产生第二个活动运行；任何跳阶段或旧 revision 写入都得到明确 409/blocked 结果。

### 1.2 Workflow API 和任务依赖扩展

- **目标**：提供设计规格中的运行、阶段、草稿、编译、诊断、产物和导出 API 外壳，所有接口使用服务端状态机。
- **允许修改/新增的精确路径**：
  - 新增：`backend/app/api/dynamic_runs.py`
  - 修改：`backend/app/api/schemas.py`
  - 修改：`backend/app/dependencies.py`
  - 修改：`backend/app/main.py`
  - 修改：`backend/app/jobs/repository.py`
  - 修改：`backend/app/services/job_service.py`
  - 新增：`backend/tests/test_dynamic_workflow_api.py`
- **实现要点**：
  1. 新增接口：
     ```text
     POST   /api/free-create/dynamic-runs
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
  2. `POST /api/free-create/dynamic-runs` 是用户点击创建后的原子命令：创建 `is_free_create=1` 的项目、独立章节、运行快照；不在 GET 或路由访问时自动创建。已有项目的命令只创建新章节和运行。
  3. 请求 schema 强制源文/梗概二选一且非空，限制时长、画幅、全自动、媒体开关、复用确认和 expected revision；响应统一返回 run/stage/job/gate/error/upstream stale 信息。
  4. `JobRepository` 和 `JobService` 只增量支持新的 workflow 字段、按运行/阶段过滤、幂等查找和依赖 JSON；旧列表、重试、取消 API 的响应与行为不改变。
  5. `main.py` 只增加新 router、依赖和启动恢复调用，不改变现有 router 顺序之外的路由语义；`CustomShotsPage` 不参与 API 分支。
- **依赖**：1.1。
- **测试文件和验证命令**：
  - `backend/tests/test_dynamic_workflow_api.py`：源文校验、free project 门禁、章节归属、所有 endpoint 的状态码、revision 冲突、草稿保存、阶段依赖和不自动创建。
  - 命令：
    ```powershell
    cd D:\LyProject\Other\ai-comic-platform\backend
    pytest tests/test_dynamic_workflow_api.py -q
    ```
- **完成标准**：API 在没有前端参与时也能拒绝非法项目、非法阶段、非法 revision 和越权章节；创建事务失败时不留下半个项目/章节/运行。

### 1.3 Job/Workflow 恢复与安全重启

- **目标**：服务重启后安全恢复未终态运行和子 Job，避免没有 task ID 的任务重复计费。
- **允许修改/新增的精确路径**：
  - 新增：`backend/app/jobs/dynamic_recovery.py`
  - 修改：`backend/app/jobs/recovery.py`（仅追加 workflow 分支）
  - 修改：`backend/app/services/task_handlers.py`（仅注册动态 stage/delivery handler）
  - 修改：`backend/app/main.py`（仅调用动态恢复协调器）
  - 新增：`backend/tests/test_dynamic_workflow_recovery.py`
- **实现要点**：
  1. 启动时读取未终态 `WorkflowRun`，逐阶段核对 Job 和 input signature。
  2. 已完成且签名一致的阶段直接复用；有 Provider task ID 的任务继续轮询；有本地结果但未下载完成的任务只继续下载；无 task ID 且无法确认提交结果的任务标记 `interrupted/needs_review`，不得自动重新提交。
  3. `partial_failed` 只补失败/缺失目标；全部子任务终态后由协调器推进下一阶段；暂停/取消运行不偷偷重启后续任务。
  4. 保留 `recover_jobs()` 现有 legacy 分支及其没有 task ID 的中断语义；动态恢复作为旁路协调，不修改旧 Job 的恢复判断。
- **依赖**：1.1、1.2；现有 `backend/app/jobs/recovery.py`、`runner.py`、`repository.py`。
- **测试文件和验证命令**：
  - `backend/tests/test_dynamic_workflow_recovery.py`：重启恢复、轮询已有 task、下载恢复、未知提交中断、部分成功补偿、暂停/取消语义、相同幂等键不重入。
  - 命令：
    ```powershell
    cd D:\LyProject\Other\ai-comic-platform\backend
    pytest tests/test_dynamic_workflow_recovery.py tests/test_core_jobs.py -q
    ```
- **完成标准**：重建 JobRunner/WorkflowCoordinator 后运行状态、阶段 attempt、Provider task ID、下载结果和需要人工复核的任务均可准确恢复；旧 Job 恢复测试继续通过。

### Phase 1 验收门禁

- API 不依赖前端门禁；阶段不能跳过，确认前不能物化。
- 同章重复启动、相同 stage/target/signature 入队、重启恢复均具有确定结果。
- 旧项目/普通章节/旧 Job API 回归通过。

---

## Phase 2：阶段 0–2、raw/normalized 双层输出与人工确认点

### 2.1 阶段 schema、Provider Job 和归一化

- **目标**：完成源文拆解、人物小传/一致性锚点、节奏与爽点三个文本阶段；保存 raw 输出和可编辑 normalized/draft 输出。
- **允许修改/新增的精确路径**：
  - 修改：`backend/app/dynamic_pipeline/schemas.py`
  - 新增：`backend/app/dynamic_pipeline/stage_service.py`
  - 新增：`backend/app/dynamic_pipeline/normalizers.py`
  - 新增：`backend/app/dynamic_pipeline/stage_gates.py`
  - 修改：`backend/app/services/task_handlers.py`（注册 `dynamic_stage_generate` 等新 Job）
  - 新增：`backend/tests/test_dynamic_stages_0_2.py`
- **实现要点**：
  1. 阶段 0 输入只读运行快照；输出至少包含元信息、一句话本集、场次表、角色清单、冲突/转折、不适合拍/需改写、假设/待确认、超出单集体量的后续集大纲。门禁检查源文非空、结构完整、场次可拍字段存在。
  2. 阶段 1 对核心角色输出基础信息、背景、性格行为逻辑、动态漫专属设定、剧情关联，并强制生成 `immutable_features`、`forbidden_changes`、`makeup_keyword_string`；路人/群像只保存简化锚点，不创建失控数量的主角色。
  3. 阶段 2 依据 `target_duration_sec` 输出覆盖全程的时间轴、核心/小爽点、强动态预算、伏笔留白、对白和音效建议。强动态预算不能超过运行配置；不能把 3–5 分钟模板套到 30 秒运行。
  4. Provider 原始 JSON 保存到 `raw_output_json`；schema 归一化结果保存到 `normalized_output_json`；用户修改单独保存 `draft_output_json`，不覆盖 raw；normalized/draft 都记录输入签名和 compiler/schema version。
  5. Provider 调用始终在 Job handler 中发生，不在创建运行或确认事务内调用；失败写入阶段错误和 Job 错误，保留上一次可用 attempt。
- **依赖**：Phase 1 的阶段屏障、text Provider、Mock Provider。
- **测试文件和验证命令**：
  - `backend/tests/test_dynamic_stages_0_2.py`：二选一、空文本阻断、raw/normalized/draft 保存、结构门禁、短时长时间轴、动态预算、Provider mock 输出归一化。
  - 命令：
    ```powershell
    cd D:\LyProject\Other\ai-comic-platform\backend
    pytest tests/test_dynamic_stages_0_2.py -q
    ```
- **完成标准**：三个阶段可独立重做和恢复；同输入签名复用；输出不直接覆盖 canonical Chapter/Asset/Screenplay；阶段 0–2 结束后运行进入待确认状态。

### 2.2 阶段确认、编辑、重做和 Markdown 预览

- **目标**：实现阶段 0–2 的人工确认点、编辑草稿、重新生成、门禁诊断和可读预览。
- **允许修改/新增的精确路径**：
  - 修改：`backend/app/services/dynamic_workflow_service.py`
  - 新增：`backend/app/dynamic_pipeline/artifact_renderer.py`
  - 修改：`backend/app/api/dynamic_runs.py`
  - 新增：`backend/tests/test_dynamic_stage_confirmation.py`
- **实现要点**：
  1. `confirm` 必须带 stage revision；只允许 `ready`/无阻断诊断的阶段确认；自动模式只跳过正常人工确认，不能跳过阻断错误、素材缺失、Provider 能力错误和安全规则。
  2. 上游 draft 变更后只增加新 attempt，并把下游标记 `stale`；旧输出、旧诊断、旧确认时间和旧 revision 保留。
  3. 确认阶段 0 后才允许写入现有 `novel_analysis`；阶段 1 确认后才建立运行级 asset mapping；阶段 2 只保存阶段产物，不提前创建 Shot 或媒体。
  4. `artifact_renderer.py` 将阶段 JSON 渲染为页面预览 Markdown/纯文本，不把 JSON 编辑器作为普通用户主界面；原始 JSON 只作为高级查看入口。
- **依赖**：2.1、Phase 1 API/状态机。
- **测试文件和验证命令**：
  - `backend/tests/test_dynamic_stage_confirmation.py`：确认前物化阻断、revision 冲突、stale 传播、旧版本保留、Markdown 与 normalized JSON 一致。
  - 命令：
    ```powershell
    cd D:\LyProject\Other\ai-comic-platform\backend
    pytest tests/test_dynamic_stage_confirmation.py -q
    ```
- **完成标准**：阶段 0–2 确认点可在服务端独立执行；任意刷新或重启后仍能得到相同阶段状态和版本；未确认的文本不能生成下游剧本/分镜。

### Phase 2 验收门禁

- 三阶段的源快照、raw、normalized、draft、gate、attempt 和 revision 可追溯。
- 目标时长动态生效，空文本、结构缺失、爽点缺失、时间轴缺口和动态预算超限都阻断。
- 运行进入人工确认点前，不创建 canonical screenplay/shot/media。

---

## Phase 3：结构化 Prompt IR、VisualStyleSpec、AssetVisualAnchor 与 ReferencePlan

### 3.1 Provider-neutral Prompt IR 与确定性签名

- **目标**：建立统一的 Prompt IR 和确定性编译入口，图片和视频不再由多个服务各自拼字符串。
- **允许修改/新增的精确路径**：
  - 新增：`backend/app/dynamic_pipeline/prompt_ir.py`
  - 新增：`backend/app/dynamic_pipeline/prompt_compiler.py`
  - 新增：`backend/app/dynamic_pipeline/provider_capabilities.py`
  - 新增：`backend/app/dynamic_pipeline/diagnostics.py`
  - 新增：`backend/tests/test_dynamic_prompt_ir.py`
  - 新增：`backend/tests/test_dynamic_prompt_compiler.py`
- **实现要点**：
  1. Prompt IR 输入模型固定为 `VisualStyleSpec`、`AssetVisualAnchor`、`ShotIntent`、`ContinuityContext`、`ReferencePlan`、`ProviderCapabilities`、`HumanOverrideSet`。
  2. `CompiledPromptBundle` 至少保存：`schema_version`、`compiler_version`、`input_hash`、`status`、image bundle、video bundle、diagnostics、provenance。
  3. 所有 hash 使用排序后的结构化 JSON、素材版本、Provider capability snapshot、compiler version 和人工覆盖计算；相同输入必须确定性输出。
  4. 页面生成稿、用户修改稿、最终发送稿分层保存：
     ```json
     {
       "generated": {},
       "human_override": {},
       "final": {},
       "base_compiler_hash": "...",
       "override_status": "active|stale|cleared"
     }
     ```
     清空 override 必须恢复自动编译结果；上游 hash 变化时旧 override 标记基于旧输入，不能静默继续使用。
  5. 阻断诊断至少覆盖：asset ID 未解析、参考图不存在/类型错误、`@ImageN` 与实际数组不一致、时间轴不连续、continuous 缺上一镜末帧、Provider 图数/时长不支持、视频缺动作/结束状态、图片包含视频时间轴或动作完成结果。警告诊断至少覆盖缺 clean identity、只存在 design sheet、运镜抽象、对白超时、风格不完整、角色漂移风险和仅靠文字连续性。
  6. Provider capability 先于视频编译确定有效 `generation_duration_sec`，再生成 timeline；不能编译后再静默改时长。
- **依赖**：Phase 0 的 provenance 字段、Phase 2 的阶段/镜头 normalized schema；不改现有 `prompt_service.py` 的 legacy 模板。
- **测试文件和验证命令**：
  - `backend/tests/test_dynamic_prompt_ir.py`：hash 确定性、三层稿、清空 override、版本变更 stale、诊断级别。
  - `backend/tests/test_dynamic_prompt_compiler.py`：图片/视频 bundle schema、Provider 时长适配、阻断/警告、provenance 保存内容。
  - 命令：
    ```powershell
    cd D:\LyProject\Other\ai-comic-platform\backend
    pytest tests/test_dynamic_prompt_ir.py tests/test_dynamic_prompt_compiler.py -q
    ```
- **完成标准**：编译器可以在不依赖 HTTP 的纯函数/服务测试中稳定生成 bundle；最终发送稿、媒体 snapshot 和 Provider 捕获请求可以使用同一 bundle。

### 3.2 VisualStyleSpec 与 AssetVisualAnchor

- **目标**：把视觉事实从“素材名称”提升为可编译的结构化约束，同时保留现有 `style_prompt` 作为用户自由文本补充。
- **允许修改/新增的精确路径**：
  - 修改：`backend/app/dynamic_pipeline/prompt_ir.py`
  - 新增：`backend/app/dynamic_pipeline/visual_anchor.py`
  - 新增：`backend/tests/test_visual_anchor.py`
  - 兼容性修补候选：`backend/app/services/prompt_service.py`（仅在需要将 legacy profile 字段转成 anchor 时增加小型适配函数，禁止重写现有模板）
- **实现要点**：
  1. `VisualStyleSpec` 字段固定为 `medium`、`line_style`、`color_palette`、`lighting_style`、`material_style`、`realism_level`、`mood`、`text_policy`、`global_negative_rules`、`immutable_rules`、`style_version`；`style_prompt` 只作为补充，不以“超高质量/极致/震撼”等空泛词作为主要保证。
  2. 角色 anchor 必须包含 `canonical_name`、`visual_facts`、`immutable_features`、`forbidden_changes`、`makeup_keyword_string`、`face`、`hair`、`body`、`costume`、`accessories`、`variants`、`current_variant`、`approved_references`。
  3. 场景 anchor 必须包含 `layout`、`axis`、`entrances`、`fixed_furnishings`、`lighting_direction`、`weather`、`time_of_day`；物品 anchor 必须包含 `shape`、`material`、`color`、`wear`、`owner`、`current_holder`、`visibility`。
  4. anchor 从本次运行确认的结构化阶段输出和 `Asset.profile_json` 生成；只按 canonical asset ID 和 locked revision 读取，不能按名称猜测或自动合并同名 Asset。
  5. `design_sheet` 只用于角色/场景设定展示；`clean_identity`、`layout_reference`、`prop_identity`、`keyframe_reference` 才能作为生成参考。视频默认拒绝含文字、版式、三视图的 design sheet。
- **依赖**：3.1；Phase 0 的 `visual_anchor_json`、`reference_purpose`、`approved` 字段。
- **测试文件和验证命令**：
  - `backend/tests/test_visual_anchor.py`：角色/场景/物品视觉事实注入、不可变特征、同名素材隔离、design sheet 与 clean reference 分离、旧 profile 兼容。
  - 命令：
    ```powershell
    cd D:\LyProject\Other\ai-comic-platform\backend
    pytest tests/test_visual_anchor.py tests/test_prompt_templates.py -q
    ```
- **完成标准**：图片和视频最终提示词包含必要视觉事实而非只有素材名称；设计板不会被隐式当成视频身份参考；现有素材设定板模板测试不回退。

### 3.3 ReferencePlan、`@ImageN` 与实际上传数组统一

- **目标**：由一个参考图计划同时驱动页面编号、编译文本、实际 Provider 输入数组和媒体 provenance。
- **允许修改/新增的精确路径**：
  - 新增：`backend/app/dynamic_pipeline/reference_plan.py`
  - 新增：`backend/app/dynamic_pipeline/reference_resolver.py`
  - 新增：`backend/tests/test_reference_plan.py`
  - 兼容性修补候选：`backend/app/services/media_service.py`（只增加动态 bundle 的参考图桥接入口，保留既有 manual/legacy 顺序）
  - 兼容性修补候选：`backend/app/services/shot_service.py`（只让动态预览复用 ReferencePlan，禁止改旧手工 override 语义）
- **实现要点**：
  1. `ReferenceSlot` 固定含 `index`、`role`、`asset_id/keyframe_id`、`image_version_id`、`required`、`purpose`、实际相对路径和 Provider URL 状态。
  2. 计划生成后同时返回：`@Image1...@ImageN` 文本映射、排序后的本地文件数组、可用公网 URL 数组、页面展示数组和快照 JSON；任何服务不得自行重新排序。
  3. 多图 Provider 不支持当前数量时，在编译前裁剪并返回 warning/blocked；不允许文本继续引用不存在的 `@ImageN`。
  4. 视频参考默认顺序明确：当前关键帧、continuous 所需上一镜末帧、clean identity/layout/prop/keyframe reference；若 Provider 只支持单图，计划和文本都只保留实际首图。
  5. 统一验证本地文件由 `FileService.resolve_project_path` 解析，Provider URL 只接受可用的 `http/https` 签名地址；失效 URL 回退本地文件但保留诊断，不改变编号。
- **依赖**：3.1、3.2；现有 `MediaService._binding_reference_sources`、Provider 多图能力探测和文件路径校验。
- **测试文件和验证命令**：
  - `backend/tests/test_reference_plan.py`：单图/多图、缺图、过期签名 URL、design sheet 排除、`@ImageN`/实际数组/快照完全一致。
  - 命令：
    ```powershell
    cd D:\LyProject\Other\ai-comic-platform\backend
    pytest tests/test_reference_plan.py tests/test_core_providers.py -q
    ```
- **完成标准**：页面、媒体版本和 Mock Provider 捕获到的参考图数组逐项一致；不支持的图数在 Provider 调用前阻断或显式警告。

### Phase 3 图片/视频提示词核心规则

- **图片编译**：只编译固定 `keyframe_time_sec=0` 的单一静止关键帧；不加入视频 timeline、连续动作过程或动作完成结果。必须注入角色脸型/发型/服装/饰品/不可变特征、场景布局/轴线/陈设/光线、物品形状/材质/颜色/当前持有状态。
- **视频编译**：只编译一个主要动作、一个主运镜、一个明确结束状态；输出 `Subject`、`Action`、`Scene`、`Camera`、`Lighting/Style`、`Audio`、`Timeline`、`Continuity`、`Negative`、`Reference map`。
- **连续时间轴**：第一段从 0 开始，相邻段首尾相接，无间隙无重叠，末段 end 等于 Provider 有效 `generation_duration_sec`。
- **cue**：对白、旁白、动作音效、手机消息均为结构化 cue，绑定到时间段；中文文字由后期配音/字幕处理，生成提示词只描述表演和声音意图。
- **兼容边界**：旧 `/custom-shots` 的用户 `image_prompt_override`/`video_prompt_override` 仍原样发送，不自动插入新 IR 文本；动态流水线和新增 regular-shot bridge 使用同一个 `DynamicPromptCompiler`。

### Phase 3 验收门禁

- 编译结果确定性一致，prompt IR/provenance/input hash 可查询。
- 图片无视频动作结果；视频有单一主动作、主运镜、结束状态和完整连续时间轴。
- `@ImageN`、实际数组、页面展示和 Provider 请求快照完全一致；clean reference 与 design sheet 分离。

---

## Phase 4：阶段 3–5 物化、下游失效与媒体桥接

### 4.1 详细剧本和标准分镜阶段

- **目标**：将阶段 2 的节奏兑现为可解析剧本和标准分镜，经过确认后物化到现有 canonical 实体。
- **允许修改/新增的精确路径**：
  - 修改：`backend/app/dynamic_pipeline/schemas.py`
  - 新增：`backend/app/dynamic_pipeline/script_stage.py`
  - 新增：`backend/app/dynamic_pipeline/storyboard_stage.py`
  - 修改：`backend/app/dynamic_pipeline/stage_service.py`
  - 新增：`backend/tests/test_dynamic_stages_3_4.py`
- **实现要点**：
  1. 阶段 3 固定场景头、人物标注、可执行动作、口型级别、动态等级、音效和字幕格式；每条对白归一成稳定 `dialogue_line_id`，支持一个分镜多条对白。
  2. 物化剧本前检查角色/场景映射、台词可解析、动态预算、时长、场次顺序和阶段 2 timeline 兑现情况；失败只保存阶段输出，不覆盖现有 screenplay。
  3. 阶段 4 输出连续分镜块：场景、时间、人物、镜头、动作/画面、台词/音效、动态效果、转场、备注，并生成稳定 `external_key`。
  4. 每个 shot normalized output 额外保存 `start_state`、`action`、`end_state`、`next_hook`、`transition_from_prev`、站位/视线/表情、道具持有者/位置/状态、dialogue cue、sound cue、目标时长和 Provider 生成时长。
  5. `continuous`、`elliptical`、`same_scene_cut`、`scene_change` 使用独立继承规则；不得通过“场景素材是否相同”推断连续性。
- **依赖**：Phase 2 确认的阶段 0–2、Phase 1 状态机、Phase 0 shot 增量字段。
- **测试文件和验证命令**：
  - `backend/tests/test_dynamic_stages_3_4.py`：节奏兑现、对白行 ID、台词结构、动态预算、场次/时长校验、四种转场规则、external key 稳定性。
  - 命令：
    ```powershell
    cd D:\LyProject\Other\ai-comic-platform\backend
    pytest tests/test_dynamic_stages_3_4.py tests/test_screenplay_dialogue.py -q
    ```
- **完成标准**：阶段 3/4 的 raw、normalized、draft 和 gate 可恢复；未确认时现有 screenplay/shot 不变；确认后输出可以被 materializer 无歧义解析。

### 4.2 Canonical 物化、资产映射与 stale 传播

- **目标**：确认后写入现有章节/素材/剧本/分镜，确保运行隔离、revision 冲突和下游失效。
- **允许修改/新增的精确路径**：
  - 新增：`backend/app/dynamic_pipeline/materializer.py`
  - 修改：`backend/app/services/dynamic_workflow_service.py`
  - 新增：`backend/tests/test_dynamic_materializer.py`
- **实现要点**：
  1. 阶段 0 → 现有 `novel_analysis`；阶段 1 → 现有 `asset` 的 `visual_anchor_json` 和运行级 mapping；阶段 3 → 现有 `screenplay`；阶段 4 → 现有 `shot`。
  2. 默认创建本次运行的 asset mapping；同名已有 Asset 只有用户明确选择 `approved_existing` 才绑定，并保存 `locked_revision`、`reuse_mode`、canonical asset ID。
  3. materializer 只在 expected run/stage revision 匹配时写入；冲突返回“内容已被修改”，保留阶段输出，不覆盖用户内容。
  4. 不新建 PipelineShot/PipelineMedia；shot 编号只在本次独立章节内生成，不能和手工章节共享编号或合成范围。
  5. 资产 revision 或人工确认稿变化后，相关 Prompt/媒体阶段标记 stale；已确认的 Asset 不被后台提取结果静默合并覆盖。
- **依赖**：4.1、Phase 0 表结构、现有 `WritingService`/`ShotService`/`AssetService` 的创建更新契约。
- **测试文件和验证命令**：
  - `backend/tests/test_dynamic_materializer.py`：运行隔离、资产复用确认、章节编号隔离、revision 冲突、旧 canonical 内容不被覆盖、stale 传播。
  - 命令：
    ```powershell
    cd D:\LyProject\Other\ai-comic-platform\backend
    pytest tests/test_dynamic_materializer.py tests/test_delete_integrity.py -q
    ```
- **完成标准**：阶段输出确认后才写 canonical；运行之间可以共享项目素材但有显式 mapping；任何冲突都有可恢复的旧版本和明确诊断。

### 4.3 5a/5b 编译物化与媒体桥接

- **目标**：将统一 Prompt Bundle 写入阶段、Shot、媒体版本和 Job，并按签名、依赖和 Provider 能力生成关键帧/视频。
- **允许修改/新增的精确路径**：
  - 新增：`backend/app/dynamic_pipeline/media_bridge.py`
  - 新增：`backend/app/dynamic_pipeline/prompt_stage.py`
  - 兼容性修补：`backend/app/services/media_service.py`（只增加 `queue_compiled_keyframe`/`queue_compiled_video` 等动态入口，保留旧 `queue_keyframe`/`queue_video` 分支）
  - 兼容性修补：`backend/app/services/shot_service.py`（只增加动态编译/预览适配，不改手工 override）
  - 修改：`backend/app/services/task_handlers.py`（注册动态媒体 handler）
  - 新增：`backend/tests/test_dynamic_media_bridge.py`
  - 新增：`backend/tests/test_dynamic_media_provider_mock.py`
- **实现要点**：
  1. 5a 只保存图片静态 bundle；5b 只保存视频 bundle。每个 bundle 同时写入 stage `draft_output_json`、shot `pipeline_metadata_json`、媒体版本 `prompt_ir_json`/`provenance_json`，最终文本写入现有 `prompt_snapshot`。
  2. provenance 至少包含：`workflow_run_id`、`workflow_stage_id`、`compiler_version`、`prompt_ir_hash`、`style_version`、素材版本、Provider capabilities、人工覆盖、diagnostics、reference plan 和 input signature。
  3. 图片关键帧按镜头可并行；视频连续段按顺序生成，只有上一镜已完成且存在可读 `end_frame_path` 时才可下发 continuous 参考；不能提前引用不存在的末帧。
  4. 每个目标 Job 独立创建，`depends_on_json` 表达 DAG 依赖，`batch_id` 只用于展示；同一幂等键返回原任务/产物，不重复 Provider 请求。
  5. Provider 时长适配先进入 bundle，再写 `requested_duration_sec`；整数时长、最小/最大时长和单图/多图限制必须在入队前校验。
  6. 新 bridge 统一使用 ReferencePlan 生成文本和实际输入数组；Provider 请求捕获值、媒体 snapshot 和页面最终发送稿必须一致。
  7. 旧 `MediaService` 的 manual/legacy 入口保持现有自由制作和常规项目行为；若添加兼容函数，每个函数都必须有旧路径回归测试。
- **依赖**：3.1–3.3、4.1–4.2、现有媒体签名、Provider mock 和 FileService。
- **测试文件和验证命令**：
  - `backend/tests/test_dynamic_media_bridge.py`：关键帧签名复用/失效重生成、视频顺序依赖、下游 stale、三层提示词和 provenance、幂等。
  - `backend/tests/test_dynamic_media_provider_mock.py`：Mock image/video Provider 捕获 prompt、时长、输入数组，断言和页面/媒体 snapshot 一致。
  - 命令：
    ```powershell
    cd D:\LyProject\Other\ai-comic-platform\backend
    pytest tests/test_dynamic_media_bridge.py tests/test_dynamic_media_provider_mock.py tests/test_services_jobs.py -q
    ```
- **完成标准**：动态路径不直接调用旧字符串拼接；图片/视频生成前完成素材、参考图、时长和签名门禁；Provider mock 只调用一次且恢复后不重复计费。

### 4.4 只做必要的兼容性修补与回归保护

- **目标**：为动态漫所需的旧结构适配、签名、路径和幂等补齐缺口，不借机重构旧服务。
- **允许修改/新增的精确路径**：
  - 兼容性修补候选：`backend/app/services/prompt_service.py`
  - 兼容性修补候选：`backend/app/services/media_service.py`
  - 兼容性修补候选：`backend/app/services/file_service.py`
  - 新增：`backend/app/dynamic_pipeline/compatibility.py`
  - 新增：`backend/tests/test_dynamic_compatibility.py`
- **实现要点**：
  1. 剧本对白适配同时接受 `speaker/text`、旧 `character/name` 和 `content/line`，归一为稳定 `dialogue_line_id`；原始文本快照保留，不能丢台词或重复前缀。
  2. 动态媒体签名必须包含 IR hash、参考图版本/用途、Provider capabilities、有效时长、人工 override hash；旧 `signature_service` 结果不变，新增签名仅用于动态目标。
  3. 动态路径和合成路径所有数据库相对文件都通过 `FileService.resolve_project_path` 校验；绝对路径、`..`、符号链接穿越、缺失文件和空文件均在 Provider/FFmpeg 前阻断。
  4. 现有 `prompt_service.py` 只允许新增兼容适配函数或字段归一，不得把 legacy 模板整体迁移、重命名或改变输出。
  5. 任何改动旧 service 的 PR 必须同时加入对应 legacy 回归测试，不能以动态测试替代。
- **依赖**：4.3、现有 `backend/tests/test_screenplay_dialogue.py`、`test_core_providers.py`、`test_project_storage_paths.py`、`test_services_jobs.py`。
- **测试文件和验证命令**：
  - `backend/tests/test_dynamic_compatibility.py`：对白别名、行 ID 稳定性、空/绝对/越界路径、媒体签名变化、幂等和旧 override 原样发送。
  - 命令：
    ```powershell
    cd D:\LyProject\Other\ai-comic-platform\backend
    pytest tests/test_dynamic_compatibility.py tests/test_screenplay_dialogue.py tests/test_core_providers.py tests/test_project_storage_paths.py -q
    ```
- **完成标准**：兼容补丁都有单独回归测试；没有无关重构、格式化或清理；手工和常规旧用例的 Prompt、媒体、Provider 请求继续一致。

### Phase 4 验收门禁

- 阶段 3/4 只在确认后物化；canonical 与流水线章节隔离。
- 5a 图片是静态关键帧；5b 视频是单主动作/主运镜/结束状态/完整 timeline/cue。
- 连续视频严格按末帧依赖顺序；输入签名、幂等键、Prompt IR、provenance 和 Provider 捕获请求可互相校验。

---

## Phase 5：阶段 6 音频、字幕、BGM、合成与交付包

### 5.1 行级对白 TTS Job 化与多对白兼容

- **目标**：动态运行按对白行创建可恢复 TTS Job，不再用一个 shot 的 current audio 覆盖多条对白；保留现有单镜直接配音 API。
- **允许修改/新增的精确路径**：
  - 兼容性修补：`backend/app/services/audio_service.py`
  - 兼容性修补：`backend/app/api/audio.py`
  - 新增：`backend/app/dynamic_pipeline/audio_delivery.py`
  - 修改：`backend/app/services/task_handlers.py`
  - 新增：`backend/tests/test_dynamic_audio_jobs.py`
- **实现要点**：
  1. 对每条对白生成稳定 `dialogue_line_id`，Job input 保存说话人、音色、源文本快照、Provider/model snapshot、run/stage ID 和 input signature。
  2. Provider 调用在 Job handler 中进行，不能在数据库事务内调用；成功后按 `shot_id + dialogue_line_id + version_no` 写 `shot_audio`，当前版本按对白行独立维护。
  3. Edge TTS 和配置 Provider 均通过同一动态 Job 结果契约；未知提交、失败、重试和重启遵循 Job 恢复规则。
  4. 现有 `/api/audio/shots/{shot_id}/audio` 和 `generate_shot_audio` 继续支持 legacy 单对白语义；新增动态接口或 run delivery 内部方法不改变旧响应结构。
- **依赖**：Phase 0 的 `shot_audio` 字段/索引、Phase 4 的对白行 ID、Phase 1 Job 恢复。
- **测试文件和验证命令**：
  - `backend/tests/test_dynamic_audio_jobs.py`：多对白不覆盖、speaker/text snapshot、TTS mock、Job 中断/重试/恢复、legacy endpoint 回归。
  - 命令：
    ```powershell
    cd D:\LyProject\Other\ai-comic-platform\backend
    pytest tests/test_dynamic_audio_jobs.py tests/test_audio_subtitle_composition.py -q
    ```
- **完成标准**：每条对白都有可追踪音频 Job 和独立版本；服务重启可继续已有 task 或安全中断；旧单镜配音测试和 API 行为不变。

### 5.2 对白行字幕、人工校正和真实视频时间轴

- **目标**：字幕绑定对白/音频行，支持生成后人工校正，并按实际视频时长和 xfade 时间轴导出 SRT。
- **允许修改/新增的精确路径**：
  - 兼容性修补：`backend/app/services/subtitle_service.py`
  - 新增：`backend/app/dynamic_pipeline/subtitle_delivery.py`
  - 修改：`backend/app/api/composition.py`（只增加动态 run 字幕接口或复用入口）
  - 新增：`backend/tests/test_dynamic_subtitle_timeline.py`
- **实现要点**：
  1. 每条字幕保存 `dialogue_line_id`、`audio_id`、相对 shot 时间、校正 revision、实际视频时间基准和 provenance；一镜多条对白不互相覆盖。
  2. 自动字幕按音频时长生成，但接受人工修改；校正后重新计算 signature，标记依赖交付 stale。
  3. SRT 导出使用当前视频实际 duration；有 xfade 时使用 CompositionService 的真实 offset，不使用计划时长把后续字幕整体前移。
  4. 旧 `generate_subtitle_from_audio`、`save_subtitles` 和普通章节 API 的输入输出继续兼容；新增校验只阻止不安全的越界时间，不改变合法旧数据。
- **依赖**：5.1、现有 `SubtitleService` 和 `CompositionService._audio_offsets`。
- **测试文件和验证命令**：
  - `backend/tests/test_dynamic_subtitle_timeline.py`：多行绑定、人工时间校正、实际视频 duration、xfade offset、SRT 边界和 legacy 单字幕回归。
  - 命令：
    ```powershell
    cd D:\LyProject\Other\ai-comic-platform\backend
    pytest tests/test_dynamic_subtitle_timeline.py tests/test_audio_subtitle_composition.py -q
    ```
- **完成标准**：字幕行与音频行可逆追踪；导出的 SRT 与成片真实时间轴一致；缺音频或非法时间能在合成前被诊断。

### 5.3 BGM、合成前预检和不静默跳过

- **目标**：动态交付在合成前明确列出缺失视频/音频/字幕、失败项、跳过项和预计任务数，默认阻断静默缺镜头。
- **允许修改/新增的精确路径**：
  - 新增：`backend/app/dynamic_pipeline/composition_preflight.py`
  - 兼容性修补：`backend/app/services/composition_service.py`
  - 兼容性修补：`backend/app/services/file_service.py`
  - 兼容性修补：`backend/app/services/chapter_service.py`
  - 新增：`backend/tests/test_dynamic_composition_preflight.py`
  - 新增：`backend/tests/test_dynamic_delivery_cleanup.py`
- **实现要点**：
  1. 预检按 run 的当前 shot 顺序核对视频、关键帧、音频行、字幕行、BGM 项目归属、输入 signature、文件可读性和失败状态。
  2. 动态 run 默认 `strict_preflight=true`：缺镜头返回结构化阻断结果，用户只能选择“修复缺失项”或明确“按部分结果合成”；不直接复用现有“跳过未生成视频”的普通行为。
  3. `CompositionService` 只增加带 `workflow_run_id/strict_preflight` 的兼容入口；现有普通章节和手工模式的旧跳过行为不改，旧路径仍有回归测试。
  4. 所有输入、输出、字幕文件和中间路径统一经 `resolve_project_path`；BGM 必须属于当前 project，禁止用 ID 直接跨项目读取。
  5. 合成 Job 保存输入版本、signature、preflight JSON 和 provenance；重启后同步 `chapter_composition` 与 Job 状态，不重复创建同一输入的合成。
  6. 章节删除的兼容性修补只补齐本次新增的音频、字幕关联文件和 composition 文件清理，并保留现有素材库不随章节删除的行为。
- **依赖**：5.1、5.2、现有 FFmpeg/CompositionService、FileService 路径约束。
- **测试文件和验证命令**：
  - `backend/tests/test_dynamic_composition_preflight.py`：缺视频/音频/字幕阻断、部分结果显式选择、BGM 跨项目拒绝、路径越界拒绝、输入签名和重启恢复。
  - `backend/tests/test_dynamic_delivery_cleanup.py`：章节删除清理音频/字幕/合成文件但保留项目素材；普通合成 legacy 行为回归。
  - 命令：
    ```powershell
    cd D:\LyProject\Other\ai-comic-platform\backend
    pytest tests/test_dynamic_composition_preflight.py tests/test_dynamic_delivery_cleanup.py tests/test_audio_subtitle_composition.py -q
    ```
- **完成标准**：动态合成在缺输入时不调用 FFmpeg；预检结果可供 UI 展示和用户选择；路径、BGM 所属、签名和恢复均有明确测试。

### 5.4 Markdown/ZIP 交付包和阶段 6 协调

- **目标**：完成 `6-delivery` 的子任务屏障并导出七份阶段 Markdown、README、结构化 JSON 和 ZIP。
- **允许修改/新增的精确路径**：
  - 新增：`backend/app/dynamic_pipeline/delivery.py`
  - 新增：`backend/app/dynamic_pipeline/exporter.py`
  - 修改：`backend/app/api/dynamic_runs.py`
  - 修改：`backend/app/services/task_handlers.py`
  - 新增：`backend/tests/test_dynamic_delivery_export.py`
- **实现要点**：
  1. `6-delivery` 协调顺序为 `6a-image/keyframe`、`6b-video`、`6c-audio`、`6d-subtitle`、`6e-composition`；可并行的图片/音频按依赖并行，连续视频按序，字幕依赖音频或明确对白时间，合成依赖当前视频/音频/字幕/BGM 选择。
  2. 展示清单固定含：角色/场景参考图、关键帧、分镜视频、配音、字幕、BGM、成片的 `x/total` 或 pending/completed 状态；失败和跳过原因可追溯。
  3. 导出文件固定为：
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
     同时将 raw/normalized/draft、Prompt IR、diagnostics、provenance、compiler/provider 版本写入结构化 JSON 文件或 ZIP 内元数据。
  4. Markdown 内容必须从当前阶段 normalized/draft/final 结果渲染，不能重新读取会变化的普通章节正文；ZIP 写入前对所有路径再次做项目内解析。
- **依赖**：5.1–5.3、Phase 2 `artifact_renderer`、Phase 4 Prompt provenance。
- **测试文件和验证命令**：
  - `backend/tests/test_dynamic_delivery_export.py`：七阶段文件齐全、README/版本信息、Markdown 与阶段 JSON 一致、ZIP 路径安全、部分结果明确记录。
  - 命令：
    ```powershell
    cd D:\LyProject\Other\ai-comic-platform\backend
    pytest tests/test_dynamic_delivery_export.py -q
    ```
- **完成标准**：阶段 6 可恢复、可部分失败、可重试；交付包可下载且包含全部要求文件、版本信息和安全路径；不影响现有视频导出 ZIP。

### Phase 5 验收门禁

- TTS、字幕、合成均进入统一 Job/恢复体系；多对白不覆盖。
- 合成前必须有缺失/失败/跳过/预计任务数清单；动态 run 默认不静默跳过。
- BGM 所属、所有文件路径、输入 signature、真实视频时间轴和交付 ZIP 均有测试。

---

## Phase 6：独立 PipelineWorkspace、前端恢复与最终集成测试

### 6.1 独立自由制作上下文与路由

- **目标**：新增动态漫工作区，不在 `CustomShotsPage` 增加模式分支，不依赖常规项目上下文，不进入页面即创建数据。
- **允许修改/新增的精确路径**：
  - 修改：`frontend/src/App.tsx`
  - 修改：`frontend/src/components/app-shell.tsx`（仅增加到新入口的导航，不改已有入口门禁和 active project 语义）
  - 新增：`frontend/src/components/free-create-shell.tsx`
  - 新增：`frontend/src/pages/FreeCreatePage.tsx`
  - 新增：`frontend/src/pages/PipelineNewPage.tsx`
  - 新增：`frontend/src/pages/PipelineWorkspacePage.tsx`
  - 新增：`frontend/src/hooks/use-pipeline.ts`
  - 新增：`frontend/src/types/pipeline.ts`
  - 新增：`frontend/src/pages/PipelineRoutes.test.tsx`
- **实现要点**：
  1. 新增路由：
     ```text
     /free-create
     /free-create/:projectId
     /free-create/:projectId/pipeline/new
     /free-create/:projectId/pipeline/:runId
     /free-create/:projectId/pipeline/:runId/stage/:stageId
     ```
     `/custom-shots` 保持原 route 和页面组件不变。
  2. `FreeCreateShell` 自己读取 free project/run，不使用 `useProject` 的常规 active project 或 `useChapter` 的默认章节；访问列表/详情只读服务器，不自动创建项目、章节或 run。
  3. 只有用户点击“新建动态漫流水线”后才提交创建请求；创建成功后进入 run 详情。项目、章节和 run 的实际状态全部以 API 为准。
  4. localStorage 只能保存 UI 偏好，例如上次打开的 free project、run 页面偏好、stage tab/抽屉展开状态；不得保存阶段进度、确认状态、Job 结果、Prompt、媒体或真实工作流状态。
  5. 服务器返回 404、409、blocked、stale、过期 revision、网络断开和刷新恢复都必须显示明确状态，并提供重新读取/重试/返回入口。
- **依赖**：Phase 1 API、Phase 2 阶段状态、Phase 5 delivery API；不修改 `frontend/src/pages/CustomShotsPage.tsx` 和它的 localStorage 契约。
- **测试文件和验证命令**：
  - `frontend/src/pages/PipelineRoutes.test.tsx`：所有路由、无项目时不自动创建、非 free project 门禁、404/409、刷新后重新从服务端读取。
  - 命令：
    ```powershell
    cd D:\LyProject\Other\ai-comic-platform\frontend
    npm test -- --run src/pages/PipelineRoutes.test.tsx
    ```
- **完成标准**：新入口独立运行；访问路由不产生项目/章节/run；`/custom-shots` 原有测试和行为不改。

### 6.2 PipelineWorkspace 和阶段组件

- **目标**：按阶段提供单一主要任务、服务器状态导航、门禁、产物编辑/预览和操作栏，支持三层提示词稿件。
- **允许修改/新增的精确路径**：
  - 新增：`frontend/src/components/pipeline/**/*.tsx`
  - 新增：`frontend/src/components/pipeline/**/*.test.tsx`
  - 修改：`frontend/src/pages/PipelineWorkspacePage.tsx`
  - 修改：`frontend/src/hooks/use-pipeline.ts`
  - 修改：`frontend/src/types/pipeline.ts`
- **实现要点**：
  1. 组件结构按设计规格拆分为 `PipelineRunHeader`、`PipelineStageRail`、`PipelineStageHeader`、`PipelineArtifactPreview`、`PipelineGatePanel`、`PipelineRevisionNotice`、`PipelineErrorPanel`、`PipelineActionBar`。
  2. 阶段组件独立为 `SourceBreakdownStage`、`CharacterBiosStage`、`RhythmStage`、`DetailedScriptStage`、`StandardStoryboardStage`、`ImagePromptStage`、`VideoPromptStage`、`DeliveryStage`；每阶段只展示其主要任务，不把所有产物堆到同页。
  3. hook 查询维度固定包含 `usePipelineRun`、`usePipelineStages`、`usePipelineStage`、`usePipelineArtifacts`、`usePipelineJobs`、`usePipelineGate`；刷新后重新请求服务器，不从 localStorage 恢复假状态。
  4. 5a/5b 每个镜头展示 `生成稿`、`用户修改`、`最终发送稿`，同时显示中文提示词、英文备用、负向、timeline、`@图片N`、实际上传参考图、诊断、compiler/input hash 和 Provider 请求 snapshot；普通用户不直接编辑 JSON，专家入口只读/明确覆盖。
  5. 操作按钮必须以服务端 gate/status/revision 决定是否可用；前端禁用只作为体验优化，不能代替后端校验。
- **依赖**：6.1、Phase 3 Prompt IR、Phase 5 delivery/preflight。
- **测试文件和验证命令**：
  - `frontend/src/components/pipeline/**/*.test.tsx`：阶段 rail 状态、blocked/stale/error、三层 prompt、参考图编号与数组、门禁和 revision notice。
  - `frontend/src/pages/PipelineWorkspacePage.test.tsx`：刷新恢复、阶段切换、确认/重做/编译/导出、失败重试、部分交付提示。
  - 命令：
    ```powershell
    cd D:\LyProject\Other\ai-comic-platform\frontend
    npm test -- --run src/components/pipeline src/pages/PipelineWorkspacePage.test.tsx
    ```
- **完成标准**：服务器状态与页面完全一致；Prompt 三层可区分且最终发送稿可追溯；阻断错误和上游变化不能被前端绕过。

### 6.3 API client、类型、路由门禁和错误契约

- **目标**：把动态 API 接入现有前端请求层，保持错误解析、下载和 Query cache 约定一致。
- **允许修改/新增的精确路径**：
  - 修改：`frontend/src/api/client.ts`
  - 修改：`frontend/src/types/api.ts`
  - 修改：`frontend/src/types/pipeline.ts`
  - 修改：`frontend/src/api/client.test.ts`
  - 新增：`frontend/src/hooks/use-pipeline.test.tsx`
- **实现要点**：
  1. 在 `api` 下新增 `dynamicRuns` 命名空间和对应 query keys，方法覆盖 run/stages/draft/regenerate/confirm/compile/diagnostics/artifacts/delivery export/start/pause/cancel/resume。
  2. 类型与后端 schema 对齐，尤其是阶段状态、gate diagnostics、three-layer prompt、reference plan、provenance、Job 依赖和 delivery checklist；不要把 `unknown` 扩散为普通 UI 的无类型 JSON。
  3. 复用现有 `ApiError`，分别处理 404、409 revision、422 输入校验、409 blocked/stale 和 5xx；导出使用现有 `download`，不把二进制写 localStorage。
  4. Query invalidation 只失效当前 run/stage/artifact/job 维度，不清理常规项目和手工页的 cache。
- **依赖**：6.1、6.2、Phase 1/5 API 合同。
- **测试文件和验证命令**：
  - 修改/执行：`frontend/src/api/client.test.ts`、`frontend/src/hooks/use-pipeline.test.tsx`。
  - 命令：
    ```powershell
    cd D:\LyProject\Other\ai-comic-platform\frontend
    npm test -- --run src/api/client.test.ts src/hooks/use-pipeline.test.tsx
    ```
- **完成标准**：所有动态请求路径、HTTP method、body、Query key、错误状态和导出行为均有测试；常规 client 测试继续通过。

### 6.4 Mock Provider 端到端和最终回归

- **目标**：验证从创建运行到 Markdown/ZIP 交付的完整链路，以及现有手工/常规功能没有回退。
- **允许修改/新增的精确路径**：
  - 新增：`backend/tests/test_dynamic_pipeline_e2e.py`
  - 新增：`backend/tests/test_dynamic_pipeline_provider_capture.py`
  - 新增：`frontend/tests/e2e/dynamic-pipeline.spec.ts`
  - 必要时只修改：`backend/app/providers/mock.py`（仅为动态阶段 schema/capture 增加最小 Mock 数据，不能引入真实 Provider）
  - 必要时只修改：`frontend/tests/e2e/smoke.spec.ts`（只在路由兼容需要时，默认不改）
- **实现要点**：
  1. 后端 E2E 使用现有 Mock text/image/video/TTS Provider、临时数据库和临时项目目录，不调用真实 Provider，不保留测试媒体。
  2. 链路至少覆盖：创建 free project/chapter/run → 阶段 0–2 生成/确认 → 阶段 3/4 物化 → 5a/5b 编译 → 关键帧/连续视频 → 行级 TTS → 字幕 → BGM mock/预检 → 合成 mock/导出 Markdown/ZIP → 查询 provenance 和 Provider 捕获请求。
  3. 端到端断言页面最终稿、媒体 snapshot、Prompt IR hash、参考图数组、Provider 捕获 prompt/时长、实际输出文件和 ZIP 内容一致；模拟刷新、服务重启、部分失败、retry、blocked missing shot 和 stale。
  4. 前端 E2E 使用 Mock API/Provider，不访问真实服务；验证新路由门禁、创建动作、阶段导航、错误/409、刷新恢复、三层 prompt、明确部分合成选择。
  5. 回归范围包括现有手工分镜、常规项目、Provider 安全、媒体版本、Job 恢复、音频/字幕/合成、Router 和现有前端页面测试；发现与本计划无关的既有失败只报告，不借机修复。
- **依赖**：Phase 0–6 全部实现；所有阶段门禁通过。
- **测试文件和验证命令**：
  - 后端：
    ```powershell
    cd D:\LyProject\Other\ai-comic-platform\backend
    pytest tests/test_dynamic_pipeline_e2e.py tests/test_dynamic_pipeline_provider_capture.py -q
    pytest -q
    ```
  - 前端：
    ```powershell
    cd D:\LyProject\Other\ai-comic-platform\frontend
    npm test -- --run
    npm run lint
    npm run build
    npm run test:e2e -- tests/e2e/dynamic-pipeline.spec.ts
    ```
  - 所有命令都在各自子项目目录执行，禁止在 `D:\LyProject` 根目录跑全量命令。
- **完成标准**：Mock E2E 全链路通过；没有真实 Provider 请求、残留测试数据或测试媒体；后端全量 pytest、前端单测/lint/build 和新增 E2E 通过；最终 `git status` 只包含本计划允许的新增/兼容性修补和原始基线差异。

### Phase 6 验收门禁

- 新流水线入口和 `CustomShotsPage` 完全独立；访问入口不自动创建真实工作流状态。
- localStorage 只保存 UI 偏好；刷新/重启恢复来自服务器。
- 端到端 Mock Provider 证明 Prompt 质量、参考图编号、时长、cue、provenance、媒体 snapshot 和交付包一致。

---

## 7. 现有代码兼容性修补清单

以下修补只能在对应 Phase 实施，且每一项必须有回归测试；不得借机重构无关代码：

| 修补 | 允许位置 | 必须保护的旧行为 | 回归测试 |
|---|---|---|---|
| 剧本 dialogue 别名与稳定行 ID | `backend/app/dynamic_pipeline/compatibility.py`，必要时 `backend/app/services/prompt_service.py`/`audio_service.py` | `speaker/text`、`character/name`、`content/line` 旧输入仍可解析；旧 Prompt 文本不重复前缀 | `test_screenplay_dialogue.py`、`test_dynamic_compatibility.py` |
| 行级 TTS Job | `backend/app/dynamic_pipeline/audio_delivery.py`，必要时 `audio_service.py`/`api/audio.py` | 旧单镜直接配音 endpoint 不改变响应和版本语义 | `test_dynamic_audio_jobs.py`、`test_audio_subtitle_composition.py` |
| 合成缺镜头预检 | `backend/app/dynamic_pipeline/composition_preflight.py`、`composition_service.py` | 普通/手工模式旧合成行为只在未启用 strict workflow 时保持；动态模式不得静默跳过 | `test_dynamic_composition_preflight.py`、现有 composition 测试 |
| 路径校验 | `file_service.py`、动态 bridge/delivery | 旧合法项目相对路径继续可读；禁止绝对路径、`..`、符号链接穿越和跨项目 BGM | `test_dynamic_compatibility.py`、`test_dynamic_composition_preflight.py`、`test_project_storage_paths.py` |
| 媒体签名/provenance | `dynamic_pipeline/prompt_compiler.py`、`media_bridge.py`，必要时 `media_service.py` | 旧 signature、版本、current 切换和 legacy media 不变 | `test_dynamic_media_bridge.py`、`test_services_jobs.py` |
| Job 幂等/恢复 | `dynamic_workflow_repository.py`、`jobs/repository.py`、`dynamic_recovery.py` | 旧任务 NULL workflow 字段仍可创建、查询、重试、取消和恢复 | `test_dynamic_workflow_repository.py`、`test_dynamic_workflow_recovery.py`、`test_core_jobs.py` |
| 合成时间轴 | `dynamic_pipeline/subtitle_delivery.py`、必要时 `composition_service.py` | 旧无 xfade SRT/合成不改变；动态使用真实视频 duration/xfade offset | `test_dynamic_subtitle_timeline.py`、`test_audio_subtitle_composition.py` |

若某项兼容修补无法在不改变旧行为的情况下完成，停止该项并交由主编排器重新确定边界，不把动态逻辑强行塞入旧路径。

---

## 8. 风险、回滚与停止点

### 8.1 主要风险与处理

| 风险 | 处理 |
|---|---|
| 当前旧迁移存在本地删除/未跟踪冲突 | 不修复、不覆盖；Phase 0 先探测 head，多 head 时停止并报告 |
| SQLite 旧索引/约束阻止多对白 | 新迁移以 batch/index 方式兼容重建索引，保留 NULL legacy 索引，不删除数据；先跑迁移副本 |
| 外部阶段输出不符合现有 schema | 始终保存 raw + normalized + draft；门禁失败不物化、不覆盖 canonical |
| 同名素材误绑定 | 默认运行级 mapping 隔离；复用必须显式确认并锁定 revision |
| Provider 提交窗口重复计费 | 持久化 idempotency key、Provider task ID 和 input signature；未知提交结果中断人工复核，不自动重提 |
| 设定板污染视频参考 | `reference_purpose`/approved 分离；视频只选 clean/layout/prop/keyframe reference |
| 长 Prompt 或多时间轴导致 Provider 不稳定 | IR 先校验，Provider-specific renderer 压缩空泛词但保留动作、状态、参考图、cue、约束；失败返回诊断 |
| 全自动覆盖人工修改 | run/stage revision、三层稿、base compiler hash 和 stale 机制；旧覆盖不静默使用 |
| 合成静默缺镜头 | 动态 strict preflight；UI 必须显示缺失项并要求明确选择 |
| 现有未提交修改被误回退 | 每个 Phase 前后 git status/diff allowlist 校验；禁止 reset、checkout、restore 和无关格式化 |

### 8.2 回滚策略

1. **代码回滚**：只回滚本 Phase 新增文件和本 Phase 明确的兼容性补丁；不触碰基线修改和旧迁移。
2. **数据库回滚**：优先停止新动态 API/Job 入队，保留 `workflow_run`/`workflow_stage` 历史数据；只有在备份和迁移测试确认后，才允许对新迁移执行 downgrade。不得通过删除旧表/清空旧数据回滚。
3. **Provider/媒体回滚**：停止后续编排，已提交外部任务按 task ID 继续轮询或人工处理；不删除已生成的 legacy/current 媒体，不把动态 snapshot 改写回旧版本。
4. **前端回滚**：移除新路由入口或关闭新 API 调用即可；`/custom-shots`、常规导航和原 localStorage 键保持可用。
5. **停止点**：迁移多 head、旧数据数量变化、Provider 捕获 Prompt 与页面不一致、`@ImageN` 数组错位、阶段跳过门禁、Job 重启重复提交、strict 合成绕过预检、现有手工/常规回归失败时，立即停止，不通过放宽断言或删除数据“修复”。

---

## 9. 验证矩阵

| 验证层 | 覆盖内容 | 测试/命令 | 通过标准 | Owner |
|---|---|---|---|---|
| 纯函数/schema | 阶段状态、时间轴、Prompt IR、Provider duration、ReferencePlan、dialogue normalize | `pytest tests/test_dynamic_workflow_state.py tests/test_dynamic_pipeline_schemas.py tests/test_dynamic_prompt_ir.py tests/test_dynamic_prompt_compiler.py tests/test_reference_plan.py -q` | 同输入确定性；阻断/警告等级正确；时间轴 0 开始、无间隙重叠、末段等于有效时长 | 主编排器复核结果 |
| 服务层 | run/stage revision、gate、materializer、asset mapping、签名/provenance、音频/字幕/预检 | 对应 `test_dynamic_*` 服务测试 | 未确认不物化；冲突保留旧版本；legacy 不变；每项兼容修补有回归 | 主编排器复核结果 |
| API | 创建、列表、状态机、草稿、确认、重做、编译、诊断、导出、错误/门禁 | `pytest tests/test_dynamic_workflow_api.py -q` | 后端不依赖前端禁用；HTTP 状态和错误结构稳定 | 主编排器复核结果 |
| 迁移 | 新鲜库、旧库、重复升级、旧数据/provenance/索引 | `pytest tests/test_dynamic_workflow_migration.py tests/test_core_database.py -q` | 只新增；旧行/文件/外键保留；唯一 head | 主编排器复核结果 |
| Job 恢复 | 有 task ID 轮询、下载恢复、未知提交中断、幂等、部分失败、取消暂停 | `pytest tests/test_dynamic_workflow_recovery.py tests/test_core_jobs.py -q` | 重启不重复收费；旧 Job 行为继续通过 | 主编排器复核结果 |
| Provider mock | image/video/TTS/text 捕获请求、参考数组、duration、结果下载 | `pytest tests/test_dynamic_media_provider_mock.py tests/test_dynamic_pipeline_provider_capture.py -q` | Provider 请求与页面最终稿、媒体 snapshot、IR/provenance 一致；不调用真实 Provider | 主编排器复核结果 |
| 前端组件/路由 | 独立 shell、阶段组件、门禁、错误、刷新恢复、localStorage 限制、三层 Prompt | `npm test -- --run`（frontend 子项目） | 新路由不自动创建；状态来自服务器；手工页测试不回退 | 主编排器复核结果 |
| 前端质量 | 类型、lint、构建 | `npm run lint`; `npm run build` | 无新增 TypeScript/lint/build 错误 | 主编排器复核结果 |
| 端到端 | 创建运行到 ZIP、刷新/重启/stale/部分失败/明确合成选择 | backend E2E + `npm run test:e2e -- tests/e2e/dynamic-pipeline.spec.ts` | 全链路可恢复，临时数据清理，Mock Provider only | 主编排器复核结果 |
| 既有回归 | 手工分镜、常规项目、Provider/media/job、音频/字幕/合成、Router | `cd ...\backend; pytest -q`；`cd ...\frontend; npm test -- --run` | 现有行为和本地基线未被回退 | 主编排器复核结果 |

---

## 10. 不得修改的既有文件、数据与行为清单

### 10.1 实施期间不得直接修改的既有文件

- `frontend/src/pages/CustomShotsPage.tsx`
- `frontend/src/pages/CustomShotsPage.test.ts`
- `backend/alembic/versions/*.py` 中已有迁移文件，包括当前工作区存在删除/未跟踪冲突的迁移路径
- 当前工作区基线中已经修改、删除或未跟踪的任意文件，除非它同时出现在本计划某个 Phase 的“允许修改/新增路径”中且改动能直接追溯到本计划；即使路径重合，也必须保留其已有 diff，只做最小增量补丁

### 10.2 不得回退的既有行为

- `/custom-shots` 手工分镜、固定手工 slot、手工 prompt override、手工 Provider 选择、手工 media batch 和普通合成。
- 常规项目的项目/章节上下文、现有剧本/素材/分镜/媒体 API、旧 Prompt 模板和旧 `prompt_service` 输出。
- 现有 Provider registry、Mock Provider、HTTP Provider 的安全校验、签名 URL 处理、任务轮询和错误脱敏。
- 现有 Job 的创建、列表、retry、cancel、legacy recovery 语义；旧行 workflow 字段保持 NULL。
- 现有媒体版本 current/history 文件命名、路径布局、版本号和删除保护。
- 普通章节现有配音、字幕、BGM 和合成 API 的合法请求结果；严格缺镜头门禁只对动态 run 的 strict delivery 生效。
- 既有 localStorage 键和默认项目/章节选择逻辑；新流水线只使用独立 UI 偏好键。

### 10.3 最终交付约束

- 不创建 commit、amend、push 或 PR。
- 不把报告、缓存、虚拟环境、测试媒体、密钥或临时数据库加入源码修改。
- 实施完成后由主编排器复核本计划内容、所有 Phase 门禁、`git status`、`git diff`、`git diff --staged` 和验证矩阵；验证 owner 为主编排器。
