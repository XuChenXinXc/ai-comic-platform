# AI 漫剧本地平台 代码审查报告（2026-09-04）

**审查日期:** 2026-09-04
**审查范围:** D:\LyProject\Other\ai-comic-platform（全量）
**技术栈:** Python 3.11 (FastAPI + SQLAlchemy + SQLite) + React 19 + TypeScript + Vite
**代码规模:** 后端 102 个 Python 文件（约 6.3 万行）+ 21 个 alembic 迁移；前端 63 个 TS/TSX 源文件（约 1.9 万行）+ 22 个前端测试文件（约 4850 行）+ 75 个后端测试文件（约 1102 个测试函数）

---

## 执行摘要

本次为全量代码审查：17 个并行审查分片覆盖后端 13 个模块切片与前端 4 个模块切片，审查者本人额外精读核心文件（main/config/db/dependencies/tables/file_service/secret_service/providers/runner/api 层）并交叉验证全部 critical/high 级发现。**所有行号均来自实际读取的源码。**

整体结论：**架构扎实、安全基线优秀（路径穿越/SSRF 文字面/命令注入/密钥加密均无显著缺口）、测试体系健全**，但存在 **1 个 Critical**、**8 个 High**、**71 个 Medium**、**约 180 个 Low** 问题。Critical 集中在旧库升级迁移的数据安全网失效；High 集中在动态工作流状态机绕过、场景素材合并数据丢失、媒体任务重试语义与前端章节切换数据丢失/死锁。

| 级别 | 数量 | 说明 |
|------|------|------|
| Critical | 1 | 破坏性迁移备份失败不阻断，旧库升级可能无备份清库 |
| High | 8 | 状态机绕过、场景合并丢当前图、重试必败、SSRF 主机名绕过、3 个前端章节切换问题、2 个停服脚本误杀进程 |
| Medium | 71 | 明确 bug、竞态、契约漂移、性能瓶颈、错误处理缺陷 |
| Low | ~180 | 小问题、死代码、可访问性、弱断言 |

**关键亮点：**
- ✅ 路径安全多层防护：resolve+relative_to、reparse point 检测、文件名消毒与 UTF-16 截断，17 个分片均未发现可利用穿越
- ✅ 无命令注入：全仓无 shell=True / os.system / eval / exec，ffmpeg 全部列表化
- ✅ 密钥安全：Windows DPAPI（附加熵+UI_FORBIDDEN）+ macOS Keychain，无明文回退，响应脱敏体系完整
- ✅ 乐观锁体系：revision/input_signature 条件 UPDATE + rowcount 校验贯穿所有写路径
- ✅ 无 SQL 注入（全参数化）、无硬编码密钥、无 XSS
- ✅ 测试体系：后端 75 文件约 1102 函数、前端 22 文件 150 用例，CI 门禁已落地（历史报告遗留项已解决）
- ✅ 历史报告（2026-09-01）的 C1/C2 及 24 个 Major 修复均验证属实，无复发

---

## Critical 问题（1 个）

### C1. 迁移 0008 备份导出失败不阻断清库，升级旧库可能无备份删除全部创作数据

**文件:** `backend/alembic/versions/20260806_0008_multi_chapter.py:78-79, 140-150`
**风险:** 数据丢失（不可恢复）

`export_data_before_deletion()` 对每个表的 JSON 导出用 `except Exception as e: print(...)` 兜底（78-79 行），导出失败仅打印后继续；随后 `upgrade()` 在 140-150 行无条件 `DELETE FROM keyframe_version/video_version/shot/novel_analysis/screenplay/job`。磁盘满、权限不足或序列化失败时，迁移会在**没有备份的情况下**清空用户全部分镜/剧本/任务数据。且 103-109 行的"再次确认"仅为 `time.sleep(3)` 软确认，restart.ps1 全自动启动场景下无人值守。

**建议修复:** 导出失败时 `raise RuntimeError` 终止迁移（备份是唯一安全网，必须硬失败）；或失败后要求显式确认（环境变量/交互输入）再继续。已修复完成的用户不受影响，但任何持有旧库待升级的实例都存在该风险。

---

## High 问题（8 个）

### H1. save_stage_draft 绕过状态机直接写 ready/blocked，stale 屏障可被绕过

**文件:** `backend/app/services/dynamic_workflow_service.py:3314-3317`
**风险:** 状态不一致 / 门禁语义失效（已源码复核）

对非 confirmed 阶段无条件 `values["status"] = "ready"/"blocked"` 落库，未调用 `transition_status`。对照 `state_machine.py:48-75` 的合法边：`pending→ready`、`blocked→ready`、`stale→ready`、`failed→ready`、`interrupted→ready` 均非法（仅 `queued/running→ready/blocked` 是设计内允许的）。典型场景：上游编辑使阶段被标 stale 后，用户保存草稿即可直接置 ready 绕过"stale 必须重跑"的屏障。

**建议修复:** 写 ready/blocked 前按当前状态校验：`queued/running` 允许（先中断 Job），`confirmed→stale` 保留，其余状态（pending/stale/failed/interrupted/blocked）要么拒绝要么经合法路径（stale→queued 两步转移）。

### H2. 后代阶段标 stale 但不中断其在途 Job，作废内容继续消耗 Provider 配额

**文件:** `backend/app/services/dynamic_workflow_service.py:3325, 3814`
**风险:** 资源浪费 / 语义不一致（已源码复核）

`save_stage_draft`（3287）与 `regenerate_stage` 只对**当前**阶段调用 `_interrupt_active_stage_jobs`；随后 `mark_descendants_stale` 仅把后代 stage 置 stale，不动后代 Job。上游内容已作废，下游 queued/running/waiting_provider/downloading 的 Job 仍继续调用 Provider 并写文件。对比 `pause_run/cancel_run` 会全量中断 Job（3092-3094、3127-3142），口径不一致。

**建议修复:** 在 mark_descendants_stale 同事务内对后代阶段调用 `_interrupt_active_stage_jobs`，保证 stale 与中断原子完成。

### H3. 场景素材合并必然使源图 scene_view_id 悬空并丢失当前图

**文件:** `backend/app/services/asset_service.py:682-714, 742`
**风险:** 数据丢失（已源码复核）

合并场景时先把源 asset_image 全部改挂到 target（682-692，全部 `is_current=False`），再迁移源 scene_view：view_key 冲突时直接 `continue` 跳过（706-708），随后 742 删除源 asset，被跳过的 scene_view 行随 FK CASCADE 删除。而 `asset_image.scene_view_id` 无外键（tables.py:316），图像行存活但指向已删除视图。由于每个场景默认有 master/front/reverse/left45/right45 五个视图（scene_view_service DEFAULT_SCENE_VIEWS），实际每次场景合并源视图**全部冲突、全部跳过**——合并后源图全部变成无视图归属的孤儿，且源图 is_current 已被清零，目标原本无图时该场景将没有任何当前图。

**建议修复:** 迁移视图冲突时，把源图像的 scene_view_id 改指到目标同 view_key 的视图；若目标该视图无图，应把源 MASTER 当前图恢复为 target 的 current。

### H4. 重入队 failed Job 未重置 provider_task_id/result_json 与 version 状态，重试必败或复用旧产物

**文件:** `backend/app/dynamic_pipeline/media_queue_service.py:1018-1037`
**风险:** 重试永远失败 / 语义上未重新生成

failed/interrupted Job 重入队时只重置 status/error，不清 `job.provider_task_id`、`result_json`，也不把 keyframe/video_version 的 status 从 failed 置回 queued。`media_service._handle_image_job` 见到残留 provider_task_id 走 `_poll_existing`，而 `_poll_existing` 对 failed 结果直接抛 RuntimeError 无重新提交回退——重试必然再次失败；若上次失败前已 `_save_provider_result`，则直接复用旧结果下载，并未真正重新生成。同时 version 停留 failed，`_set_media_status` 的 `status.in(_ACTIVE_MEDIA_STATUSES)` 条件 rowcount=0 静默失效，UI 持续显示失败。对照 `JobRepository.retry`（repository.py:548-559）明确区分"有 task_id 续轮询/无 task_id 重新提交"，队列服务的无条件置 queued 与 handler 状态机矛盾。

**建议修复:** 重入队 failed Job 时清空 job 与 version 的 provider_task_id、重置 result_json、version.status 置回 queued；interrupted/等待超时可保留 task_id 以续轮询原任务（与 repository.retry 语义对齐）。

### H5. 下载 SSRF 防护只拦 IP 字面量，localhost/内网域名可绕过

**文件:** `backend/app/providers/base.py:569` + `backend/app/services/file_service.py:199-214`
**风险:** SSRF（本地单用户部署降低危害，但可打本机/内网端口）

Provider 返回的绝对下载 URL 仅做 `_url_origin` 格式校验；真正拦截在 `download_url_atomic → _reject_private_download_host`，该函数只对 **IP 字面量**生效，hostname 不做 DNS 解析预检。`http://localhost:8080/...`、`http://127.0.0.1.nip.io/...`、内网域名（intranet.local）都会解析到环回/私有地址并被放行。file_service 注释自认该残余风险，但 base.py 的下载路径（Provider 结果，可被恶意/被入侵的中转控制）与历史遗留项重叠，应视为待修。

**建议修复:** 下载前对 hostname 做 DNS 解析并用 ipaddress 校验解析结果；至少先拦截 `localhost`、`.local`、`.internal` 等主机名形态，并保留逐跳重定向复查。

### H6. 批量生成忽略保存失败结果，未保存草稿的分镜仍入队（前端）

**文件:** `frontend/src/pages/CustomShotsPage.tsx:980`
**风险:** 基于旧内容浪费生成资源 / 用户误判

`mediaBatchMutation` 中 `await saveFilledDrafts()` 的返回值被丢弃，ids 直接从 slotDrafts 构建不区分保存成功与否；saveSlot 失败（返回 false）时该槽 shot.id 仍被加入批量 ids，生成基于后端旧内容而非新草稿，与 972-974 行单槽路径"保存失败则不入队"的约定矛盾。

**建议修复:** 检查 saveFilledDrafts 返回值，失败时排除失败槽并在 toast 汇总说明。

### H7. 配音工作台切换章节后 generating/loading 状态残留，新章节同 shot_no 按钮永久禁用（前端）

**文件:** `frontend/src/pages/AudioWorkbenchPage.tsx:312-318, 399-403`
**风险:** 功能死锁（刷新页面才能恢复）

`finally` 中仅当 `canCommit()` 为真才从 generating/loading 集合清除；章节切换后 canCommit() 恒 false，清理被跳过，而章节加载 effect 从不重置这些集合。由于 shot_no 每章从 1 编号，新章节同 shot_no 按钮一直显示"生成中"且 disabled，只能刷新页面。

**建议修复:** 章节加载 effect 开头无条件重置 generating/loading/batchProgress；finally 清理不依赖 canCommit。

### H8. 停止脚本按端口/命令行模式无差别杀进程，可能误杀本机其他服务

**文件:** `stop.sh:28-29`、`stop-mac.sh:75-96`
**风险:** 误杀无关进程

`pkill -f "uvicorn app.main:app"` / `pkill -f "vite.*3000"` 匹配当前用户机器上**所有**进程命令行，不校验身份/目录；`lsof -ti:3000` 对 3000 端口（React 开发极常用）直接 kill（必要时 -9）。同一机器上其他项目的服务会被一起杀死。Windows 侧 platform-process.ps1 已有进程身份快照防误杀机制，Unix 侧未对齐。

**建议修复:** 优先按 PID 文件停止并校验进程身份；端口兜底清理校验命令行（含本项目路径）后才 kill；`.run` 目录创建顺序 bug（start-mac.sh:177-179 先写 PID 后 mkdir）也需一并修复。

---

## Medium 问题（71 个，按模块归类摘录）

### 后端核心与 API（3 个）
| 位置 | 问题 |
|------|------|
| api/settings.py:61 | POST /settings/imgbb-key 用裸 dict 接收，缺 api_key 字段静默清空已配置密钥 |
| api/audio.py:55 | 两条 async 路由在事件循环内同步执行 DB 连接与最多 50MB 磁盘写，阻塞事件循环 |
| api/dynamic_runs.py:143 | 动态交付导出全量内存组装 ZIP，多视频项目内存峰值数百 MB |

### shot/prompt 服务（4 个）
| 位置 | 问题 |
|------|------|
| shot_service.py:3653 | handle_image_prompt 把上一镜关键帧绑定实际落库（与"不写库"注释矛盾），覆盖人工绑定并静默引用旧帧 |
| shot_service.py:1198 | update 修改视频提示词连带作废当前关键帧（video_only 未区分） |
| shot_service.py:566/1168 | 未校验的 director_plan 落库后整章 list/get 500 |
| shot_service.py:586 | list/get 每镜 10+ 条查询 N+1，list_project 的修复不彻底 |

### media/composition 服务（3 个）
| 位置 | 问题 |
|------|------|
| media_service.py:1670 | version_no 用 max+1 分配，并发插入撞唯一约束直接 500 |
| media_service.py:3538 | 图片任务轮询超时被当最终 failed（视频路径为可恢复），重试重复计费 |
| composition_service.py:956 | 无叠化拼接用 -c copy，分镜分辨率/帧率不一致时成片损坏 |

### 动态工作流（5 个）
| 位置 | 问题 |
|------|------|
| dynamic_workflow_service.py:2174 | delivery 依赖集合不过滤终态 Job，失败依赖永久阻塞交付入队 |
| dynamic_workflow_service.py:3404 | confirm_stage 门禁拒绝先写库再抛 conflict，且可能不 bump revision |
| dynamic_workflow_service.py:3849 | regenerate_stage 新 attempt 沿用旧 input_signature，可能触发假 blocking |
| job_service.py:211 | 取消任务同步媒体目标不检查同 target 其他活跃 Job，误标 failed |
| dynamic_workflow_repository.py:330 | repository 半数写方法生产零调用，与 service 内联实现语义分叉 |

### services 杂项（4 个）
| 位置 | 问题 |
|------|------|
| provider_service.py:361 | 删除 Provider 被历史 job 引用永久阻断（遗漏 job.provider_id 置 NULL） |
| asset_service.py:615 | 素材删除提交后文件清理失败抛 500，DB 已删文件残留 |
| asset_service.py:336 | 素材列表 N+1（100 场景 ×5 视图约 600+ 条 SQL） |
| prompt_template_renderer.py:64 | 模板渲染失败无兜底，注释声称的回退路径不存在 |

### 导演计划与写作（5 个）
| 位置 | 问题 |
|------|------|
| shot_service.py:566/574 | director_plan 重解析无防护可致列表 500；validate_shot_consistency 三处调用点均未传 director_plan，运镜动机校验形同虚设 |
| writing_service.py:496 | 每次生成剧本都无条件重跑小说分析，重复 LLM 付费调用 |
| prompts/batch_refine_prompts.md:8 | 批量精修白名单停留在旧【xxx】段落格式，与自然语言提示词漂移 |
| camera_library.py:727 | find_preset 剥离括号导致「跟镜（飞行）/（战斗）」两个预设永不可达 |

### jobs/providers（4 个）
| 位置 | 问题 |
|------|------|
| runner.py:210 | interrupted 终态不传播依赖失败，下游 Job 运行中永久卡 queued（仅重启恢复兜底） |
| runner.py:238 | worker 线程二次 DB 异常逃逸后 Future 无人消费，任务静默悬挂 |
| providers/tts/http_tts.py:76 | 任务 params 控制字段（headers/verify_ssl）原样序列化进请求体 |
| providers/image/http_image.py:249 | 上游 raw 完整响应（含大 base64）随 result_data 落库 |

### 交付与分镜（9 个，摘录 4 个）
| 位置 | 问题 |
|------|------|
| delivery.py:2325/2476 | 子 Job claim 失败返回 queued 被当作成功，allow_partial 模式可误确认 6-delivery |
| delivery.py:2602 | shot_subtitle 无 is_current，resume 换 generation 后旧字幕行残留重复 |
| delivery.py:3712 | 用 sanitize 后错误文案子串分类状态，与注释"不要匹配错误文案"矛盾 |
| storyboard_stage.py:1149 | "上一镜"子串匹配误命中"上一镜头/上一镜中"→ scene_change 误阻断 |

### 提示词编译链（3 个）
| 位置 | 问题 |
|------|------|
| prompt_stage_service.py:1396 | 编译期能力快照取自 job_input，与签名来源（run config）口径不一致无法检出 |
| prompt_stage_service.py:1543 | 参考图 blocking 诊断在编译前被异常整体吞掉，per-slot 详情丢失 |

### 阶段编排（2 个）
| 位置 | 问题 |
|------|------|
| stage_service.py:1018 | plan_storyboard_batches 漏传 rhythm，节奏尾段认领失效 → 特定时长偏差下阶段 4 永久 blocked |
| stage_service.py:1286 | _mark_result_conflict 裸 except 吞冲突标记落库失败且无日志 |

### 媒体队列（6 个）
| 位置 | 问题 |
|------|------|
| media_queue_service.py:1182 | 依赖 pending 时 enqueue 返回 False 被误报为入队失败写入 stage.error（5b 等 5a 高频误报） |
| audio_job_service.py:203 | _JobNoLongerActive 与普通失败同路径，取消语义被污染为 failed 并级联 |
| audio_delivery.py:217 | _coerce_dialogue_line 兜底对空输入取 [0] 抛 IndexError → 500 |
| media_bridge.py:977 | to_job_payload 的 target_type 语义反转，与队列服务契约相反（当前仅测试消费） |
| media_bridge.py:927 | to_media_version_payload 产出表中不存在的列，过时死契约 |
| audio_job_service.py:889 | 复用已有音频时 Job 状态更新失败仍提交 is_current 切换 |

### 导出与合成预检（4 个）
| 位置 | 问题 |
|------|------|
| subtitle_delivery.py:726 | crossfade<=0 时字幕偏移公式与 CompositionService 实际 0.1s 叠化不一致，时间轴逐镜漂移 |
| composition_preflight.py:946 | subtitle 路径的穿越/绝对路径等硬阻断错误被静默丢弃（历史遗留项未修） |
| composition_preflight.py:1152 | 标量 bgm_id 被误当 file_path 校验，误阻断合成 |
| exporter.py:1187 | strict 模式配置显式禁用音频/字幕导致 manifest 被标 blocked |

### 迁移与工程脚本（4 个）
| 位置 | 问题 |
|------|------|
| start-mac.sh:104 | 硬编码 127.0.0.1:8012 忽略 .env 配置，双平台漂移 |
| start-mac.sh:128 | macOS/Linux 双击启动走 dev 模式，Windows 走生产托管 dist，行为漂移 |
| ci.yml:10 | CI 全部 ubuntu，Windows/macOS 关键路径（DPAPI/Keychain/脚本）无自动化验证 |
| pyproject.toml:61 | 注释声称 CI 跑 pip-compile 验证，实际无此步骤且无锁文件 |

### 前端三大页面（7 个）
| 位置 | 问题 |
|------|------|
| CustomShotsPage.tsx:737 | switchProject 无并发防护，快速切换项目旧异步回调覆盖新状态 |
| ShotsPage.tsx:500 | 编辑保存为无 revision 的全量 PUT，可覆盖任务期间的更新 |
| ShotsPage.tsx:596 | 手工新增分镜编号取 length+1，删镜后与现有编号冲突 |
| ShotsPage.tsx:985 | 拖拽排序无 pending 防护与乐观更新，与移动按钮并发顺序错乱 |
| ShotsPage.tsx:528 | 删除分镜后 selected 集合残留已删 id |
| use-project.tsx:43 | 项目列表加载失败即清空选中项并擦除持久化选择 |
| CustomShotsPage.tsx:1282 | 素材查询失败无错误 UI，显示误导性空状态 |

### 前端其余页面（3 个）
| 位置 | 问题 |
|------|------|
| WritingPage.tsx:296 | stylePromptDirty 保存成功后永不重置，回填分支成为死逻辑 |
| CompositionPage.tsx:166 | 合成创建与轮询响应无章节守卫，切章后旧响应覆盖新章节状态 |
| WritingPage.tsx:648 | analysis 标签页不可达，整段分析编辑逻辑为死代码（约 300 行） |

### 前端基础设施（6 个）
| 位置 | 问题 |
|------|------|
| api/client.ts:105 | 请求层无超时、无 AbortSignal，长请求/下载不可取消 |
| api/client.ts:109 | JSON 解析失败与网络失败泄漏英文原始错误（Failed to fetch） |
| components/chapter-selector.tsx:104 | 使用 Tailwind 未定义类（bg-card/border-border 等），样式静默失效 |
| components/ui.tsx:216 | Modal 无焦点陷阱，关闭不恢复焦点 |
| components/chapter-selector.tsx:102 | 下拉 ARIA 语义错误（listbox 内 role=button），无键盘管理 |

### 测试体系（7 个）
| 位置 | 问题 |
|------|------|
| playwright.config.ts:7 | e2e 未隔离数据库，本地运行写默认业务库且清理失败静默 |
| tests/e2e/smoke.spec.ts:144 | 空 catch 吞掉真实点击失败，失败被误导性超时掩盖 |
| AudioWorkbenchPage.test.tsx:168 | 真实时钟硬等待轮询（3s/1.6s×4），拖慢套件且对间隔脆弱 |
| AudioWorkbenchPage.test.tsx:45 | 大量 as never 强转使 mock 与 API 契约脱节 |
| CompositionPage.test.tsx:37 | 仅 2 个初始化错误用例，合成主链路无测试 |
| tests/e2e/smoke.spec.ts:112 | e2e 全正向链路，无失败/恢复/409 冲突覆盖 |
| playwright.config.ts:39 | 本地 e2e 复用任意已存在服务实例，可能跑在陈旧代码上 |

---

## 模块评分

| 分片 | 模块 | 评分 | 发现数 |
|------|------|------|--------|
| B1 | backend 核心与 API | 4/5 | 12 |
| B2 | shot/prompt 服务 | 4/5 | 17 |
| B3 | media/composition 服务 | 4/5 | 15 |
| B4 | 动态工作流服务 | 3/5 | 17 |
| B5 | services 杂项 | 4/5 | 14 |
| B6 | 导演计划与写作 | 3/5 | 17 |
| B7 | jobs/providers 层 | 4/5 | 20 |
| B8 | 交付与分镜阶段 | 4/5 | 17 |
| B9 | 提示词编译链 | 4/5 | 18 |
| B10 | 阶段编排与状态机 | 3/5 | 12 |
| B11 | 媒体队列与音频交付 | 4/5 | 16 |
| B12 | 导出与合成预检 | 4/5 | 15 |
| B13 | 迁移与工程脚本 | 3/5 | 19 |
| F1 | 前端三大页面 | 4/5 | 22 |
| F2 | 前端其余页面 | 4/5 | 13 |
| F3 | 前端基础设施与组件 | 4/5 | 18 |
| F4 | 测试体系 | 4/5 | 18 |

---

## Low 问题概览（约 180 个，摘录高频模式）

- **吞异常无日志**：runner._mark_dynamic_failure_state、stage_service、prompt_stage_service 多处 `except Exception: return`
- **死代码**：ffmpeg_utils 两个函数、dynamic_recovery._has_non_executable_upstream、_PHASE2_CONTENT_KEYS、ui.tsx 4 处、repository 半数写方法、_stage_input 4-storyboard 分支
- **重复实现**：门禁聚合逻辑三处、_text 助手四份、时长探测三套、TTS 三件套、saveStageDraft 双实现、safeDownloadFilename 双份
- **签名口径漂移**：SHOT_SIGNATURE_FIELDS 缺 transition_from_prev、human_override_hash 被 stage 级覆盖、截断参考计划 hash 丢 video 标志
- **健壮性**：@ImageN 字典序比较（≥10 图错序）、bool('false')、NaN/Infinity 透传击穿导出、Windows 保留设备名未过滤、_direct_child 注释与行为失真
- **a11y**：Tabs/Modal/ChapterSelector/视频元素缺键盘与 ARIA 语义
- **测试**：弱断言（toBeTruthy/className/document.body）、时序负断言、DOM 结构脆弱定位器

---

## 修复优先级建议

**第一批（数据安全/正确性，建议立即处理）：**
1. C1 迁移 0008 备份失败硬阻断（一行 raise）
2. H3 场景合并 scene_view_id 悬空（重挂视图 + current 恢复）
3. H4 媒体 Job 重入队状态重置
4. H1/H2 动态工作流状态机绕过与后代 Job 中断
5. H6/H7 前端批量生成保存失败入队、配音工作台按钮死锁
6. H8 stop.sh/stop-mac.sh 误杀进程

**第二批（语义与体验，本周处理）：**
- H5 SSRF 主机名拦截；F2 WritingPage:410 清空正文切章丢失
- M 级中高价值项：imgbb-key 裸 dict、delivery 依赖含失败 Job、stage_service 漏传 rhythm、重试误报入队失败、图片轮询超时不可恢复、素材合并 provider 删除阻断、前端无 revision PUT 与切章竞态

**第三批（工程债）：**
- 死代码清理、重复实现收敛、TTS 三件套重构、e2e 数据隔离、a11y 欠账、弱断言整改

---

## 验证方法说明

- 17 个审查分片全部基于 read 工具逐行读取实际源码，行号真实
- 审查者本人精读 11 个核心文件（main/config/db/dependencies/tables/file_service/secret_service/providers/runner/api）交叉验证
- C1/H1/H3 三处 critical/high 经审查者二次源码复核确认属实
- 全仓 grep 反模式扫描：无 shell=True / eval / exec / os.system / 硬编码密钥 / 吞异常 except pass
- 历史报告（2026-09-01）修复项逐一核对，无复发
- 本次审查未运行测试套件（纯静态审查），未修改任何代码
