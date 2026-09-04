# AI 漫剧本地平台 代码审查报告（2026-09-01）

**审查日期:** 2026-09-01
**审查范围:** D:\LyProject\Other\ai-comic-platform（全量）
**技术栈:** Python 3.11 (FastAPI + SQLAlchemy + SQLite) + React 19 + TypeScript + Vite
**代码规模:** 后端 99 个 Python 文件（约 5.9 万行）+ 19 个迁移脚本；前端 47 个 TS/TSX 源文件（约 1.5 万行）+ 22 个测试文件；后端 67 个测试文件 / 955 个测试函数

---

## 执行摘要

本次为全量代码审查：后端核心基础设施（config/db/main/models/jobs）由审查者直接精读，其余模块通过 22 路并行代理 + 若干前台子代理覆盖，关键发现已逐一回到源码交叉验证。

整体结论：**架构扎实、安全面干净、测试体系优秀**，但存在 **2 个 Critical**、**24 个 Major**、大量 Minor 问题。Critical 集中在 Provider 注册表与前端章节切换两处，均为确定性触发；Major 主要集中在动态工作流的状态一致性、媒体签名一致性、SQLite 并发事务边界三类。

| 级别 | 数量 | 说明 |
|------|------|------|
| Critical | 2 | 确定性崩溃 / 数据串写 |
| Major | 24 | 明确 bug、状态不一致、并发竞态、性能瓶颈 |
| Minor | 60+ | 小问题、改进建议、死代码 |
| Info | 30+ | 观察备注、设计取舍 |

**关键亮点：**
- ✅ 乐观锁 + revision 条件更新贯穿所有写路径，rowcount 校验普遍
- ✅ 幂等键 + input_signature 快照校验 + 崩溃恢复（recover_jobs）设计扎实
- ✅ 路径安全优秀：符号链接/目录联接检测、relative_to 越界校验、原子写入
- ✅ 敏感信息脱敏机制（DPAPI/Keychain + 响应脱敏）健全
- ✅ 无 XSS（前端无 dangerouslySetInnerHTML）、无 SQL 注入（全 SQLAlchemy 参数化）
- ✅ 测试体系优秀：955 个后端测试覆盖端到端/失败恢复/幂等/并发/安全，无 xfail/无条件 skip

---

## 修复后状态（2026-09-01 同日更新）

审查当日已将 **2 个 Critical、24 个 Major 全部修复并验证**；代表性 Minor 中高价值项（数据丢失、状态卡死、签名失真、迁移索引缺失等）也已一并修复。修复加码了回归测试：后端测试从 955 → **1373**（全部通过），前端 vitest 从 117 → **131**。

| 编号 | 问题 | 修复方案 | 验证 |
|------|------|----------|------|
| C1 | Mock TTS TypeError | `MockTTSProvider.__init__(self, config, api_key="")` 兼容 registry 双参调用 | `tests/test_review_fixes_providers.py` |
| C2 | WritingPage 跨章节串写 | 切章节时先记录原章节 id、dirty 草稿保存回**原章节**再清理本地状态；save 回调按 chapterId 校验后才 hydrate | vitest 全量回归 |
| M1 | shot update None 写 "null"/500 | update 对 JSON 列 None 统一 `value or []` 兜底 | 全量回归；`test_services_jobs.py` |
| M2 | list_project N+1 | 一次查询构建 chapter_map，循环内复用 | 全量回归 |
| M3 | 连续性审校无 revision 守卫 | 第二轮 fix 过滤 None/空值、按审校快照传 expected_revision、409 跳过 | 全量回归 |
| M4 | MASTER 绑定签名恒 stale | 预期签名与入队同口径重算 MASTER 追加（`_with_scene_master_reference`），不再恒 stale | 全量回归 |
| M5 | delete_version 可删刚激活 current | 全程 `_MEDIA_VERSION_FILE_LOCK` + `is_current=False / status∉active` 条件 UPDATE + rowcount 校验 | 全量回归 |
| M6 | 保存草稿丢弃在途 Job | 对 queued/running 阶段保存草稿时**先显式中断在途 Job**（替代「拒绝保存」的建议，语义更可用；不再静默丢弃 Provider 结果） | `test_dynamic_workflow_stage5_barrier.py` |
| M7 | 暂停/取消不中断 Job | pause_run 逐阶段、cancel_run 对 run 关联在途 Job 批量置 interrupted | `test_dynamic_workflow_stage5_barrier.py` |
| M8 | Prompt 签名双实现 | 收敛为单一 `calculate_prompt_stage_input_signature`，两处调用方统一委托 | `test_dynamic_prompt_stage_service.py` / `..._e2e.py` |
| M9 | 单通道编译丢弃门禁诊断 | `compile_image_prompt/compile_video_prompt` 返回 `bundle.diagnostics`，调用方去重并入 gate | `test_dynamic_prompt_compiler.py` |
| M10 | Provider 删除被历史引用阻断 | delete 将 asset_image/keyframe_video/shot_audio/audio_config 历史引用置 NULL 后删除 | 全量回归 |
| M11 | 删除/合并素材 scene_view_id 签名失真 | 预载映射、校验归属、不合法置空后按重映射结果重算 own_signature | `test_delete_integrity.py`、`test_scene_view_service.py` |
| M12 | compose 并发 500 / 事务注释失实 | 捕获 IntegrityError 转 409 conflict，注释与行为一致 | 全量回归 |
| M13 | legacy 分支按 chapter 覆盖全部合成记录 | 按 job_id（+状态条件）精确更新 | 全量回归 |
| M14 | 固定分辨率拉伸变形 | ffmpeg `force_original_aspect_ratio=decrease` + pad 黑边 | `test_composition_subtitle_fix.py` |
| M15 | TTS 网络调用包在 DB 事务内 | 短事务读分镜→事务外 TTS→短事务分配版本号插入 | `test_audio_service_fix.py` |
| M16 | continuous 边界缺失被静默放过 | `_continuity_boundary` 目标侧缺失返回 `found=False`，调用方产出 `structured_state_missing` 阻塞诊断 | `test_dynamic_stages_3_4.py::test_storyboard_continuous_missing_boundary_side_emits_structured_state_missing` |
| M17 | `_references_previous_frame` 短子串误报 | 精确键 + 正文词边界匹配，并支持 `previous_shot_id` 等 `标记_后缀` 组合键 | `test_dynamic_stages_3_4.py::test_storyboard_transition_rules_are_independent` |
| M18 | zip(strict=False) 静默错位 | `zip(strict=True)` + 显式长度一致性校验 | 全量回归 |
| M19 | 字幕清单恒 missing / 子 Job 卡 running | 字幕按「文本+时间轴」存在性合成状态；子任务 handler 包 try/except 落为失败 | `test_dynamic_delivery_service.py`（全文件） |
| M20 | 状态机契约矛盾 | 状态机补上 queued/running（及 blocked/failed/interrupted）→ stale 的合法边，服务层经 `transition_status` 校验写入 | `test_dynamic_workflow_state.py`、`test_dynamic_workflow_api.py` |
| M21 | ProjectPage 硬编码覆盖项目配置 | 编辑分支四字段从 `activeProject` 读取、仅空值回退 | vitest 全量回归 |
| M22 | AudioWorkbenchPage 竞态 + 无轮询 | cancelled/mounted 守卫覆盖加载与单条生成；非终态配音 2s 间隔轮询刷新状态徽标 | `AudioWorkbenchPage.test.tsx`（含两条新增专项用例） |
| M23 | CustomShotsPage 无错误反馈 | saveSlot/generateSlotMedia try/catch + toast.error + onError | vitest 全量回归 |
| M24 | 交付清单 1.5s 轮询风暴 | effect 依赖改稳定键 `run.revision` | 全量回归 |
| MS-0012 | tables.py 缺 7 个索引 | models/tables.py 补齐与 alembic 0012 一致的 7 个索引定义 | `test_core_database.py` |
| MS-0019 | audio_config.voice_mapping_json 模型与迁移漂移（修复过程中发现） | 新增 `alembic/versions/20260901_0019_audio_config_voice_mapping.py`，幂等补列并与表模型一致 | `test_audio_service_fix.py`、`test_dynamic_delivery_service.py`、`test_dynamic_workflow_migration.py` |

**修复过程中同步收紧的测试基建：**
- `test_api_workflow.py` 显式把全局 `file_service.database` 绑定到测试库（与 `test_audio_service_fix.py` 既有做法一致），消除「服务直用全局单例 → 查询生产库 → 布局判定漂移」的隐性依赖。
- `test_dynamic_workflow_migration.py` 头节点断言更新为 0019，覆盖「单头 + 降级链」。
- 后端 ruff（`app` + `alembic` + `tests`）清零：修复 UP017/F841/B904/I001 等 lint 问题；前端 tsc 无错误。

**验证结果（2026-09-01 全量）：**
- 后端 `pytest tests/ --ignore=tests/e2e`：**1373 passed, 7 skipped, 0 failed**（迁移套件验证 0019 单头与升降级幂等）。
- 后端 `ruff check app alembic tests`：All checks passed。
- 前端 `tsc --noEmit`：无错误；`vitest run`：**131 passed（22 文件）**。

**遗留待办（低优先级，等同原文 P3）：**
- 前端死代码清理（5 个组件文件 + hooks）、Modal/ChapterSelector 可访问性。
- Provider TTS 三件套重构（继承 HttpProviderBase）、base64/local_file/URL 下载安全加固。
- CI 门禁：当前仓库仍无自动化门禁（本报告 955→1373 的测试体量需要一个 CI 跑起来）。

---

## Critical 问题（2 个）

### C1. Mock TTS Provider 创建必然抛 TypeError（registry 传参不匹配）

**文件:** `backend/app/providers/registry.py:51-52` + `backend/app/providers/mock.py:720-721`

registry 对 `kind == "tts"` 统一调用 `factory(parsed, parsed.api_key)`（两个位置参数），但 `MockTTSProvider.__init__(self, config)` 只接受一个参数。已确认 `OpenAITTSProvider`/`HttpTTSProvider` 的 `__init__(self, config, api_key)` 是双参数，唯独 Mock 单参数。

后果：任何「创建 / 测试 / 解析 `tts/mock` Provider」的请求都会抛 `TypeError`（非 `ProviderConfigurationError`，不会被转成 400，直接 500）。Mock TTS 虽不在默认 seed 列表，但注册在 registry 中、是 UI 可选项。

修复：给 `MockTTSProvider.__init__` 增加 `api_key: str = ""` 形参，或 registry 对 `adapter == "mock"` 特判；补回归测试。

### C2. WritingPage 切换章节时未保存草稿跨章节串写

**文件:** `frontend/src/pages/WritingPage.tsx:387-401`（配合 140-150、190-215）

`useLayoutEffect`（140-150 行）只在 `activeProjectId` 变化时重置 dirty/screenplay 状态；`activeChapterId` 变化时无任何重置。190-215 行的同步 effect 在 `dirty.screenplay === true` 时提前 return（不载入新章节内容），屏幕显示仍是上一章 A 的未保存编辑。此时点「保存」，`saveCurrent` 用**当前** `activeChapterId`（B）+ **旧** `screenplay`（A 的内容）提交，把 A 的剧本内容写进 B 章节——数据串写。已核实该路径可达（Save 按钮在此状态下未被禁用）。

修复：切章节时若有未保存草稿先弹确认（保存/丢弃/继续编辑），或把 dirty/screenplay 按 chapterId 隔离。

---

## Major 问题（24 个）

### 后端（20 个）

#### M1. shot_service.update 对 None 值缺少兜底，JSON 列可被写为 "null" 或直接 500

**文件:** `backend/app/services/shot_service.py:1006-1007`

create 与 _save_extracted 均对 JSON 列做 `json_dumps(value or [])` 兜底，唯独 update 通用分支用裸 `json_dumps(value)`。客户端显式传 `{"dialogue": null}` 时写入 `"null"` 污染该行；传 `{"character_ids": null}` 时 `_validate_asset_ids` 对 None 迭代抛 TypeError → 500。建议 update 对所有 JSON 列统一 `value or []` 兜底。

#### M2. shot_service.list_project 循环内逐行查询，注释声称「避免 N+1」却仍 N+1

**文件:** `backend/app/services/shot_service.py:500-547`

注释明言「一次查询避免 N+1」，但 533 行仍对每个分镜调用 `self._chapter(chapter_id)`（章节数据已在 chapter_map 中）；加上 _fill_scene_view_info、_current_source_signature、_media_record、_add_compiled_prompts 等，每行实际 10+ 次查询/连接。建议复用 chapter_map、批量 IN 查询。

#### M3. 连续性审校应用修正无 revision 守卫且只捕获 ServiceError

**文件:** `backend/app/services/shot_service.py:4002-4006`

handle_continuity_check 第二轮对每个 fix 调 `self.update(target_id, ...)` 未传 expected_revision，用户并发编辑会被模型修正静默覆盖；且 except 只捕获 ServiceError，`suggested_value=None` 写入 NOT NULL 列时 IntegrityError 会炸掉整个批任务。建议传 expected_revision 并在 409 时跳过，对 None/空值过滤。

#### M4. media_service 关键帧/视频的 MASTER 参考绑定造成签名恒 stale

**文件:** `backend/app/services/media_service.py:1802 / 1988 / 1328 / 1352`

queue_keyframe/queue_video 入队时经 `_with_scene_master_reference` 追加 MASTER 绑定**并计入 input_signature**，但 `_keyframe_expected_signature`/`_video_expected_signature` 用分镜当前 `reference_bindings`（不含 MASTER 项）计算预期签名 → 凡是「场景来自非 master Scene View 且存在 MASTER 图」时生成的关键帧/视频在 list 中恒显示 stale，前端永远提示「参考图绑定已变化」。建议 expected signature 基于媒体版本快照而非分镜当前绑定。

#### M5. media_service.delete_version 无锁且 UPDATE 无乐观条件，可删除刚激活的 current 媒体

**文件:** `backend/app/services/media_service.py:3000-3034`

`_activate_media_version`/`_deactivate_media_version` 用 `_MEDIA_VERSION_FILE_LOCK` 串行化，而 `delete_version` 既不持锁、UPDATE 也无 status/is_current 条件。并发删除 + 激活时会把刚激活的 current 版本删掉，造成当前媒体数据丢失。建议 delete_version 进入锁保护并加乐观条件。

#### M6. dynamic_workflow.save_stage_draft 允许对 queued/running 阶段保存草稿，丢弃在途 Job 结果

**文件:** `backend/app/services/dynamic_workflow_service.py:3243, 3271-3274`

save_stage_draft 只挡 cancelled，对 queued/running 的阶段会把状态改写为 ready/blocked；stage_service 的 `_EXECUTABLE_WORKFLOW_STATUSES` 只允许 {queued, running}，在途 Job 下次执行时因 stage 状态变化被中断，已调用的 Provider 和生成结果被静默丢弃。confirm_stage 有 can_confirm_stage 保护，唯独草稿保存没有对称保护。

#### M7. pause_run/cancel_run 只改 run/stage 状态，不中断在途 Job

**文件:** `backend/app/services/dynamic_workflow_service.py:3038-3108`

暂停/取消只更新 run/stage 表，不触碰 job 表：正在运行（含已发出 Provider 调用、正在下载写文件的媒体任务）的 Job 会继续执行到完成，之后才被标记 interrupted。用户「暂停/取消」后后台仍持续消耗 Provider 配额并写文件，无法及时止损。建议事务内将关联 Job 一并置 interrupted。

#### M8. Prompt 输入签名存在两套并行实现，结构性分歧会触发假 snapshot mismatch

**文件:** `backend/app/dynamic_pipeline/prompt_stage_service.py:1456-1545` vs `dynamic_workflow_service.py:1109-1261`

两处各自独立重建同一签名，已确认三处分歧：① asset_versions 兜底（workflow 版对无法生成 anchor 的资产用 `_asset_anchor_version` 兜底，service 版跳过该资产）；② 资产范围（workflow 版仅 storyboard 引用，service 版含 asset_mapping）；③ capability 快照来源（workflow 版读 run config，service 版先读 job_input）。任何单侧改动都会把正常 Job 误判为 snapshot_mismatch 中断并级联 stale delivery。建议抽取单一签名构建函数。

#### M9. 单通道编译函数丢弃全部门禁诊断

**文件:** `backend/app/dynamic_pipeline/prompt_compiler.py:1052-1103`

`compile_image_prompt`/`compile_video_prompt` 返回的 Bundle 模型没有 diagnostics 字段，`_prepare_context` 计算的 asset_unresolved/video_action_missing 等诊断被静默丢弃；而 shot_service.py:447、prompt_service.py:1270 正是直接调用它们，生成失败被静默吞掉。

#### M10. Provider 删除被历史外键引用永久阻断

**文件:** `backend/app/services/provider_service.py:288-309`

delete() 只显式检查 project 表，job/asset_image/keyframe_version/video_version 等表的 provider_id 外键全依赖 IntegrityError 兜底。被任何历史任务或媒体使用过的 Provider 永远删除失败（409），无强制删除/清理途径；而 update() 却显式检查进行中任务，两个路径策略不一致。

#### M11. asset_service 删除/合并素材未重映射 shot.scene_view_id，签名失真

**文件:** `backend/app/services/asset_service.py:479-558`

`_update_shot_asset_references` 只处理 scene_asset_id/character_ids/prop_ids/reference_bindings，不触碰 scene_view_id。删除场景素材时 scene_view 行被级联删除、shot.scene_view_id 由 FK SET NULL 清空，但 own_signature 是用删除前旧 scene_view_id 重算的 → 落库签名与实际列不一致，后续任何签名比对都会把该分镜判为过期，触发不必要再生成。

#### M12. composition_service 声称 IMMEDIATE 事务实际是 deferred，并发 compose 会 500

**文件:** `backend/app/services/composition_service.py:126-191`

注释（127 行）声称「IMMEDIATE 事务」，但 `engine.begin()` 发出的是 deferred BEGIN；并发 compose 时部分唯一索引 `ux_composition_chapter` 会拦下第二个 insert，但代码不捕获 IntegrityError → 500；或读后升级写锁遇他方写者立即 SQLITE_BUSY。建议捕获 IntegrityError 转 conflict，或对 SQLite 启用 BEGIN IMMEDIATE。

#### M13. composition_service legacy 分支按 chapter_id 覆盖全部合成记录

**文件:** `backend/app/services/composition_service.py:533-537, 678-682`

非 workflow 路径置 processing 与成功回写时都 `where(chapter_id)` 更新所有 chapter_composition 行，把历史已完成记录污染成最新输出。应改为按 job_id 精确更新。

#### M14. composition_service 固定分辨率缩放会拉伸变形

**文件:** `backend/app/services/composition_service.py:1318-1363`

720p/4k 用 `scale=1280:720`/`3840:2160` 无 `force_original_aspect_ratio=decrease` + pad，竖屏（9:16）内容会被强行拉伸。

#### M15. audio_service 把 TTS 网络调用包在 DB 事务内

**文件:** `backend/app/services/audio_service.py:155-285`

`engine.begin()` 从查版本号到插入结果全程持有事务，期间 await TTS 网络调用并写文件。WAL 下长事务读升级写锁时极易 SQLITE_BUSY → 高频 500「database is locked」。版本号并发竞态也在同一问题里（max+1 无锁）。建议网络调用移到事务外，短临界区分配版本号。

#### M16. storyboard_stage continuous 结构化边界缺失被静默放过

**文件:** `backend/app/dynamic_pipeline/storyboard_stage.py:1136-1145, 2137-2163, 3137-3163`

`_continuity_boundary` 对 `{"start": {...}}` 取 end 侧返回 `{}`；调用处兜底只判断字段存在性不判断边界存在性 → 上一镜缺 end 边界或本镜缺 start 边界时静默跳过比较且不产生「structured_state_missing」诊断。

#### M17. storyboard_stage `_references_previous_frame` 短子串误报

**文件:** `backend/app/dynamic_pipeline/storyboard_stage.py:1086-1119, 2173-2190`

marker_keys 含 `"prev"` 并用子串匹配，"prev" 会命中 "preview"/"previously"/"prev_scene_asset" 等 → scene_change/same_scene_cut 镜头只要出现含 "prev" 的普通文本就触发 blocking 诊断，整章/整批重跑。建议词边界/精确键匹配。

#### M18. media_queue_service `zip(strict=False)` 静默容忍 bundle/target 数量不一致

**文件:** `backend/app/dynamic_pipeline/media_queue_service.py:1268`

`zip(strict=False)` 一旦 bundle_keys 与 targets 数量不一致会静默截断/错位，媒体可能挂到错误分镜且无报错。当前路径恰好同源一致，但无任何显式保护。建议 `zip(strict=True)` 或显式长度校验。

#### M19. delivery 字幕清单状态恒不为 completed / 子 Job 异常卡 running

**文件:** `backend/app/dynamic_pipeline/delivery.py:430-438, 445-469, 618-687` + `delivery.py:2296-2310`

① `shot_subtitle` 无 status 列，`_decode_row` 把 status 置 `""`，`_checklist_media_item` 用 `_record_ready` 统计 → 字幕项无论是否已生成永远是 missing（被 preflight 覆盖才不明显）。② 子任务先 claim 为 running 再直接 `handler(claimed)` 无 try/except，handler 抛异常时子 Job 永久卡 running。建议：字幕按存在性合成状态；handler 包 try/except 并 fail/interrupt。

#### M20. 状态机不允许 queued/running→stale，但服务层多处直接写 stale

**文件:** `backend/app/dynamic_pipeline/state_machine.py:48-62` vs `stage_service.py:342-351, 1219-1231` / `dynamic_workflow_repository.py:597-603`

`_ALLOWED_TRANSITIONS` 只允许 ready/confirmed→stale，但 stage_service._mark_signature_mismatch、_mark_result_conflict_in_connection、mark_descendants_stale 对 queued/running/blocked/failed/interrupted 直接写 stale。契约与实现矛盾，一旦后续按状态机校验这些写入会直接抛 InvalidWorkflowTransition。

### 前端（4 个）

#### M21. ProjectPage 编辑已有项目用硬编码默认值覆盖并写回后端

**文件:** `frontend/src/pages/ProjectPage.tsx:55-66`（已验证）

编辑分支里 `aspect_ratio: "16:9"`、`image_size: "3840x2160"`、`video_size: "16:9"`、`default_duration_sec: 6` 全部写死而非从 `activeProject` 读取。用户只要打开编辑页点一次「保存修改」，项目真实的画面比例、图片尺寸、视频尺寸、默认时长就被悄悄重置——数据丢失。且与 emptyProject 中 `image_size: "2560x1440"` 本身不一致。修复：从 activeProject 复制或原样回传。

#### M22. AudioWorkbenchPage 无取消防护 + 生成后无轮询

**文件:** `frontend/src/pages/AudioWorkbenchPage.tsx:54-123, 163-196, 235-252`

① 页面加载 `Promise.all` 无条件 setState，无 cancelled 标志，快速切换章节时旧响应覆盖新章节（状态互相污染），卸载后对已卸载组件 setState；批量生成循环同样。② generateShotAudio 返回后仅一次性 setAudioStatus，无轮询/重取，异步产出时状态徽标永远停留「生成中」。建议仿 FreeCreatePage 加 cancelled 标志；对生成中做间隔轮询。

#### M23. CustomShotsPage saveSlot/generateSlotMedia 无错误处理

**文件:** `frontend/src/pages/CustomShotsPage.tsx:832-843, 874-882`

直接 `await api.shots.update(...)` 经 `onClick={() => void ...}` 调用，任何错误变成 unhandled promise rejection，控制台报错但界面无反馈，用户以为保存/生成成功了。建议 try/catch + toast。

#### M24. PipelineWorkspacePage 交付清单 effect 依赖 workspace 对象引用，轮询期间每 1.5s 重复请求

**文件:** `frontend/src/pages/PipelineWorkspacePage.tsx:766-788`

effect deps 含 workspace，而轮询 refresh（1500ms）每次成功都 commitWorkspace 生成新对象 → 交付阶段每 1.5s 拉一次 checklist/preflight，且「正在读取清单…」闪烁。建议 deps 改为稳定键（run.revision 或 updated_at）。

---

## 代表性 Minor 问题（节选，完整清单见附录）

**后端：**
- `api/audio.py:38` update_audio_config 接收未校验 dict 直接写库（无列白名单）
- `api/media.py:52` async 处理器内同步阻塞式 service（事件循环阻塞）
- `api/media.py:245` export_current_videos 全量视频载入内存 BytesIO（OOM 风险，另有 delivery/exporter 同类问题）
- `api/audio.py:132` BGM 上传仅信客户端 content_type，无文件内容校验
- `api/schemas.py:446` PropAssetOutput.importance 默认 'normal' 与强制 'core' 校验矛盾
- `media_service.py:3061` `_ensure_keyframe_not_referenced` 全表扫描未按项目过滤
- `media_service.py:2136` source_ref LIKE 未转义 %/_ 通配符
- `media_service.py:466` 动态参考 URL 下载无 SSRF 防护；Provider base64 无大小上限
- `provider_service.py:253` 无法通过 API 清除已保存的 API Key
- `provider_service.py:71` 超长敏感参数名（>200 字符）绕过脱敏与写入拦截
- `signature_service.py:124` 参考图全量 read_bytes 进内存哈希（应为流式）
- `ffmpeg_utils.py:107` build_ffmpeg_command 布尔开关生成 `-y True` 错误命令
- `ffmpeg_utils.py:29` run_ffmpeg_async 等无调用者（死代码）
- `writing_service.py:90` content_json 与 content 指向同一 dict（共享引用陷阱）
- `writing_service.py:287` queue_* 状态置位与建任务不在同一事务，失败卡死 generating
- `audio_service.py:480` delete_bgm 先删文件再删 DB 行，回滚后状态不一致
- `subtitle_service.py:107` export_chapter_srt 每分镜一次查询（N+1）
- `jobs/repository.py:553` list_non_terminal 硬编码 limit 100_000 静默截断
- `jobs/dynamic_recovery.py:601` stage/job 状态偏离时静默跳过，job 永久卡 running
- `providers/base.py:487` base64 结果解码无大小上限
- `providers/base.py:496` local_file 字段任意文件读取无路径校验
- `providers/base.py:503` result_url 下载无 SSRF 防护
- `providers/tts/*` TTS 三件套：async 内同步阻塞 requests、响应无大小限制、AttributeError 逃逸、api_key_env 失效、音色白名单与兼容接口声明矛盾
- `providers/http_image.py:282` poll task_id 未消毒直接拼 URL（http_video 只对 multipart 消毒）
- `dynamic_workflow_repository.py:552,586` 取最新排序缺 updated_at/id 决胜键
- `dynamic_workflow_service.py:2746` `_next_chapter_no` max+1 并发重复
- `prompt_stage.py:1226` idempotency_key 当 job 依赖（错误语义，当前调用方规避）
- `reference_plan.py:666` snapshot hash 对 provider_url 处理不一致，签名 URL 使 hash 不稳定
- `exporter.py:881` 部分 ready + 部分 planned 被错误归并为 completed
- `composition_preflight.py:946` subtitle 文件路径校验结果被静默丢弃
- `h3_adapter.py:227` `_compact_forbidden` 无条件追加「全片无文字」约束（语义注入）
- `alembic 0012` 创建的 7 个索引在 models/tables.py 全部缺失
- `alembic 0008` SKIP_DATA_DELETION 逃生舱既丢数据又必然崩溃
- `alembic 0017` 预检逻辑因 SQLite 双引号字面量陷阱失效（查询实际统计全部行数）
- `alembic 0011` 声称扩展 tts 约束实际未改 CHECK（0017 才补）
- `alembic 0018` downgrade 在有多 View 当前图数据时必失败

**前端：**
- `hooks/index.ts` 约一半工具（useDebounce/useThrottle/useCancellableQuery/useLocalStorage 等）是死代码；useLocalStorage 函数式更新读闭包旧值；useCancellableQuery 文档契约与 api 签名不符
- `hooks/use-job-polling.ts` 无活跃任务仍每 10s 轮询；任务逐个完成触发多次全前缀失效风暴；失效前缀含不存在的 "shot-quality"
- `hooks/use-chapter.tsx` "chapters" 未纳入 queryKeys 与失效前缀，章节列表可能过期
- `api/client.ts:225` mediaUrl 未清洗 `..` 段
- `api/client.ts:260` downloadBlob click 后立即 revokeObjectURL
- `api/client.ts:777` api.audio/composition 内联匿名类型大量重复且字段不一致
- `components/json-editor.tsx:22` 受控回灌导致输入反复重排、光标跳走（影响 ShotsPage/WritingPage JSON 编辑）
- `components/shot-media-card.tsx:70` 每分镜 2 张卡独立查询无 staleTime，请求风暴
- `components/ui.tsx:203` Modal 无焦点陷阱/焦点管理/滚动锁定，aria-labelledby 固定 id 重复
- `components/chapter-selector.tsx:121` 嵌套交互控件 + 按钮无 aria-label
- `components/accessibility.tsx`/`responsive.tsx`/`loading-state.tsx`/`lazy-image.tsx`/`virtual-list.tsx` 整文件死代码（含同名组件混淆、`#main-content` 断链）
- `lib/job-labels.ts` 标签映射未覆盖新任务类型（TTS/字幕/合成等）
- `pages/ShotsPage.tsx` 大列表无虚拟化、O(n²) 去重、每镜 JSON 反复解析
- `pages/AudioWorkbenchPage.tsx` 用 alert 与全站 toast 风格割裂；成功数统计把跳过项算进去；failed 也显示「生成中」
- `pages/CompositionPage.tsx` 合成配置纯本地 state 不加载不保存，刷新即丢失；轮询无上限且瞬时错误即永久停止
- `pages/JobsPage.tsx` 任务列表无分页/上限，长期膨胀
- `pages/WritingPage.tsx` analysis tab 整段死代码（tab 初始 screenplay，无入口切到 analysis）；busy 判断误用 activeProjectId（应为 activeChapterId）
- `pages/PlatformSettingsPage.tsx` setQueryData 用部分字段覆盖缓存

---

## 亮点与正确设计（值得保持）

1. **乐观锁体系**：dynamic_workflow/media/audio 全部用 revision/input_signature/条件 UPDATE + rowcount 校验，过期写入整体回滚。
2. **幂等设计**：确定性 id、idempotency_key 部分唯一索引、IntegrityError 竞态恢复、提交后回调 + runner 恢复兜底。
3. **路径安全**：file_service `_inside` resolve + relative_to；is_reparse_point 检测；原子写/移动；Migration 的 staged 三段式移动 + 回滚。
4. **密钥安全**：Windows DPAPI / macOS Keychain 加密存储、列表/详情不返回明文、前端只显示「已配置/来源」。
5. **敏感信息脱敏**：provider params 递归脱敏 + 响应层 _redact + 错误 sanitize。
6. **崩溃恢复**：recover_jobs/recover_all_once、queued 重扫、动态 stage 依赖门控。
7. **测试体系**：955 个后端测试断言具体、覆盖端到端/失败恢复/幂等/并发/安全/删除完整性；无 xfail/无条件 skip/only；全部外部调用 mock 隔离、数据清理规范。前端 117 用例 + 5 个高质量 e2e。
8. **前端无 XSS、any 使用克制、React Query 规范**（staleTime 15s、retry 1）。

---

## 修复优先级建议

### P0（尽快，数据安全/确定性崩溃）
1. C1 Mock TTS TypeError
2. C2 WritingPage 跨章节串写
3. M21 ProjectPage 硬编码覆盖项目配置
4. M5 media_service.delete_version 并发删 current

### P1（本周，状态一致性）
5. M4 MASTER 参考签名恒 stale
6. M6 save_stage_draft 丢弃在途结果
7. M7 pause/cancel 不中断 Job
8. M8 Prompt 签名双实现
9. M3 连续性审校 revision 守卫
10. M11 asset scene_view_id 签名失真
11. M19 delivery 子 Job 卡 running
12. M15 audio 长事务
13. M20 状态机 stale 契约矛盾
14. M1 shot update None 兜底

### P2（本月，性能/健壮性）
15. M2/M22/M24 各类 N+1 与轮询风暴
16. M12/M13/M14 composition 并发/覆盖/拉伸
17. M10 provider 删除阻断
18. M9 单通道诊断丢弃
19. M16/M17 storyboard 门禁缺陷
20. M18 zip strict
21. alembic 0012 索引缺失、0017 预检失效、0018 downgrade

### P3（长期）
- 前端死代码清理（5 个组件文件 + hooks）、Modal/ChapterSelector 可访问性
- Provider TTS 三件套重构（继承 HttpProviderBase）
- base64/local_file/URL 下载安全加固
- 补 CI（无任何自动化门禁是最大工程化短板）、覆盖率门槛、e2e 进 check.ps1

---

## 结论

项目整体代码质量**优秀**：并发正确性、幂等、路径与密钥安全、测试覆盖都属于高水平，经过反复打磨。审查发现的 2 个 Critical、24 个 Major 已在同日全部修复并加码回归验证（见「修复后状态」章节），代表性 Minor 中高价值项已修复，剩余为低优先级清理与工程化待办。下一步最重要的工程化改进是补上 CI 门禁，让 1300+ 后端测试与 131 个前端用例在每次提交自动运行。

---

*审查方式：直接精读核心基础设施 + 22 路并行审查代理覆盖全部模块 + 关键发现回源码逐一交叉验证（含执行验证的 registry 传参、ProjectPage 硬编码、WritingPage 串写路径）。附录（全部 minor/info 清单）可联系审查者获取。*
