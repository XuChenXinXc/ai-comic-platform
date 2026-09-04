export type ProviderKind = "text" | "image" | "video" | "tts";
export type AssetType = "character" | "scene" | "prop";
/** 分镜与上一镜的衔接类型：只有 continuous 会引用上一镜视频末帧作为参考图。 */
export type ShotTransitionFromPrev =
  | "continuous"
  | "elliptical"
  | "same_scene_cut"
  | "scene_change";
export type ContentStatus =
  | "draft"
  | "generating"
  | "ready"
  | "confirmed"
  | "failed";
export type MediaStatus =
  | "queued"
  | "running"
  | "waiting_provider"
  | "downloading"
  | "completed"
  | "failed";
export type JobStatus =
  | MediaStatus
  | "cancelled"
  | "interrupted";

export interface Project {
  id: string;
  name: string;
  storage_path: string;
  source_text: string;
  source_signature: string;
  source_revision: number;
  style_prompt: string;
  aspect_ratio: string;
  image_size: string;
  video_size: string;
  default_duration_sec: number;
  default_text_provider_id?: string | null;
  default_image_provider_id?: string | null;
  default_video_provider_id?: string | null;
  is_free_create: boolean;
  created_at: string;
  updated_at: string;
}

export type ProjectInput = Pick<
  Project,
  | "name"
  | "source_text"
  | "style_prompt"
  | "aspect_ratio"
  | "image_size"
  | "video_size"
  | "default_duration_sec"
> &
  Partial<
    Pick<
      Project,
      | "default_text_provider_id"
      | "default_image_provider_id"
      | "default_video_provider_id"
      | "is_free_create"
    >
  >;

export type ProjectStorageSource = "database" | "environment" | "default";

export interface ProjectStorageSetting {
  path: string;
  source: ProjectStorageSource;
}

export interface ProjectStorageSelection extends ProjectStorageSetting {
  selected: boolean;
}

export interface ContentRecord<T = Record<string, unknown>> {
  id: string;
  project_id: string;
  chapter_id: string;
  content_json: T | string;
  content?: T;
  status: ContentStatus;
  source_signature: string;
  own_signature?: string;
  revision: number;
  generated_by?: string;
  error?: string;
  updated_at: string;
  is_stale?: boolean;
}

export interface ScreenplayScene {
  scene_no: number;
  scene_name: string;
  time_weather?: string;
  characters?: string[];
  objective?: string;
  action?: string;
  dialogue?: DialogueLine[];
  narration?: string[];
  emotion?: string;
  transition_to_next?: string;
}

export type DialogueLine = Record<string, unknown> & {
  speaker?: string;
  character?: string;
  name?: string;
  text?: string;
  content?: string;
  line?: string;
};

export type ShotPosition = Record<string, unknown> & {
  character?: string;
  name?: string;
  speaker?: string;
  character_name?: string;
  position?: string;
  location?: string;
  side?: string;
  placement?: string;
  facing?: string;
  direction?: string;
  gaze?: string;
  look?: string;
  eye_line?: string;
  action_direction?: string;
};

export type ShotExpression = Record<string, unknown> & {
  character?: string;
  name?: string;
  speaker?: string;
  character_name?: string;
  expression?: string;
  emotion?: string;
  mood?: string;
};

export interface ScreenplayContent {
  title?: string;
  logline?: string;
  scenes: ScreenplayScene[];
  [key: string]: unknown;
}

export interface Asset {
  id: string;
  project_id: string;
  asset_type: AssetType;
  name: string;
  profile_json: Record<string, unknown> | string;
  profile?: Record<string, unknown>;
  prompt: string;
  compiled_prompt: string;
  negative_prompt: string;
  prompt_override?: string | null;
  consistency_json: string[] | string;
  consistency_keywords?: string[];
  composition: string;
  importance: "core" | "normal";
  status: Exclude<ContentStatus, "generating">;
  source_signature: string;
  own_signature: string;
  revision: number;
  current_image?: MediaVersion | null;
  /** 场景素材的 Scene View 列表（仅 scene 类型填充）。 */
  scene_views?: SceneView[];
  created_at: string;
  updated_at: string;
  is_stale?: boolean;
}

/** 场景素材的单个视角视图：一张独立场景参考图。 */
export interface SceneView {
  id: string;
  asset_id: string;
  /** "master" | "front" | "reverse" | "left45" | "right45" | 自定义 */
  view_key: string;
  /** 展示名：母版 / 正向 / 反向 / 左前45° / 右前45° */
  name: string;
  /** 视角附加提示词（用户可编辑） */
  prompt: string;
  /** 用户自定义最终提示词（可为空） */
  prompt_override: string;
  sort_order: number;
  /** 该 View 的当前图（与 Asset.current_image 同形） */
  current_image?: MediaVersion | null;
  created_at: string;
  updated_at: string;
}

export interface ReferenceBinding {
  role:
    | "character_identity"
    | "scene_layout"
    | "prop_identity"
    | "previous_keyframe"
    | "scene_master";
  asset_id?: string;
  asset_image_id?: string;
  keyframe_id?: string;
}

export interface DirectorAudienceFocus {
  first: string;
  second?: string;
  third?: string;
}

export interface DirectorMicroPerformance {
  trigger?: string;
  action: string;
  source?: string;
}

export interface DirectorDialogueBinding {
  speaker: string;
  text: string;
  startTime: number;
  endTime: number;
  requiresLipSync: boolean;
  voiceSource?: string;
}

export interface DirectorTimelineSegment {
  start: number;
  end: number;
  action: string;
}

/** 导演设计层：只决定「如何呈现」，不修改剧情 / 连续性 / 资产事实。 */
export interface DirectorPlan {
  shotPurpose: string;
  audienceFocus: DirectorAudienceFocus;
  composition: string;
  foreground?: string;
  midground?: string;
  background?: string;
  cameraMotivation?: string;
  performance?: string;
  microPerformance?: DirectorMicroPerformance[];
  environmentMotion?: string;
  visualTone?: string[];
  timeline: DirectorTimelineSegment[];
  dialogueBindings?: DirectorDialogueBinding[];
}

export interface ConsistencyIssue {
  code: string;
  severity: "warning" | "blocking";
  message: string;
  details?: Record<string, unknown>;
}

export interface Shot {
  id: string;
  project_id: string;
  chapter_id: string;
  shot_no: number;
  shot_order: number;
  scene_no?: number | null;
  source_type: "novel" | "screenplay";
  source_ref: string;
  source_signature: string;
  scene_asset_id?: string | null;
  /** 绑定场景素材后选中的 Scene View id（只读展示用） */
  scene_view_id?: string | null;
  /** 绑定场景素材后选中的 Scene View key（提交给后端，空字符串=默认母版） */
  scene_view_key?: string | null;
  /** 后端回填的 Scene View 展示名（如「正向」） */
  scene_view_name?: string | null;
  scene_name?: string;
  character_ids_json: string[] | string;
  character_ids?: string[];
  prop_ids_json: string[] | string;
  prop_ids?: string[];
  positions_json: ShotPosition[] | string;
  positions?: ShotPosition[];
  start_state: string;
  action: string;
  expressions_json: ShotExpression[] | string;
  expressions?: ShotExpression[];
  dialogue_json: DialogueLine[] | string;
  dialogue?: DialogueLine[];
  narration: string;
  shot_type: string;
  camera_angle: string;
  camera_move: string;
  duration_sec: number;
  end_state: string;
  next_hook: string;
  transition_from_prev: ShotTransitionFromPrev;
  frame_description: string;
  image_prompt: string;
  image_prompt_override?: string | null;
  final_image_prompt?: string;
  compiled_image_prompt: string;
  video_prompt: string;
  video_prompt_override?: string | null;
  final_video_prompt?: string;
  compiled_video_prompt: string;
  negative_prompt: string;
  reference_bindings_json: ReferenceBinding[] | string;
  reference_bindings?: ReferenceBinding[];
  /** 导演设计层产出：导演计划 + 唯一时间轴 + 对白绑定（空对象表示尚未生成） */
  director_plan?: DirectorPlan | null;
  /** 最终一致性校验问题（只读，blocking 问题会阻断视频入队） */
  consistency_issues?: ConsistencyIssue[];
  status: Exclude<ContentStatus, "generating">;
  own_signature: string;
  revision: number;
  current_keyframe?: MediaVersion | null;
  current_video?: MediaVersion | null;
  created_at: string;
  updated_at: string;
  is_stale?: boolean;
}

export interface MediaVersion {
  id: string;
  asset_id?: string;
  shot_id?: string;
  keyframe_version_id?: string | null;
  version_no: number;
  file_path?: string | null;
  file_url?: string | null;
  media_url?: string | null;
  prompt_snapshot: string;
  negative_prompt_snapshot: string;
  provider_id?: string | null;
  model: string;
  provider_task_id?: string;
  status: MediaStatus;
  is_current: boolean;
  input_signature: string;
  source: "generated" | "uploaded";
  error?: string;
  requested_duration_sec?: number;
  actual_duration_sec?: number | null;
  created_at: string;
  is_stale?: boolean;
  stale_reason?: string;
  warnings?: string[];
}

export interface ApiProvider {
  id: string;
  kind: ProviderKind;
  name: string;
  adapter: string;
  base_url: string;
  api_key_env: string;
  api_key_configured: boolean;
  api_key_source: "direct" | "env" | "none";
  model: string;
  params: Record<string, unknown> | string;
  params_json?: Record<string, unknown> | string;
  timeout_sec: number;
  max_retries: number;
  enabled: boolean;
  is_default: boolean;
  last_test_ok?: boolean | null;
  last_test_message?: string;
  created_at: string;
  updated_at: string;
}

export type ProviderInput = Omit<
  ApiProvider,
  | "id"
  | "created_at"
  | "updated_at"
  | "last_test_ok"
  | "last_test_message"
  | "api_key_configured"
  | "api_key_source"
> & {
  api_key?: string;
};

export interface Job {
  id: string;
  project_id: string;
  project_name?: string;
  job_type: string;
  target_type: string;
  target_id?: string | null;
  target_name?: string;
  batch_id?: string | null;
  provider_kind: ProviderKind | "";
  provider_id?: string | null;
  provider_name?: string;
  provider_task_id?: string;
  status: JobStatus;
  progress_current: number;
  progress_total: number;
  message: string;
  result_json?: Record<string, unknown> | string;
  error?: string;
  retry_count: number;
  created_at: string;
  updated_at: string;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface BatchResponse {
  batch_id: string;
  jobs: Job[];
  preflight_failures?: Array<{
    target_id: string;
    error: string;
  }>;
}

export interface ListResponse<T> {
  items: T[];
  total?: number;
}

export interface ShotQualityIssue {
  shot_id: string;
  shot_no: number;
  chapter_no?: number;
  chapter_title?: string;
  duration_sec: number;
  warnings: string[];
}

export interface ShotQualityReport {
  total: number;
  items: ShotQualityIssue[];
}

export interface Chapter {
  id: string;
  project_id: string;
  chapter_no: number;
  title: string;
  source_text: string;
  source_signature: string;
  source_revision: number;
  status: string;
  shot_count?: number;
  created_at: string;
  updated_at: string;
}

// 动态漫 PipelineWorkspace 的独立工作流类型。
export type DynamicStageKey =
  | "0-source"
  | "1-character-bios"
  | "2-rhythm"
  | "3-script"
  | "4-storyboard"
  | "5a-image-prompts"
  | "5b-video-prompts"
  | "6-delivery";

export type DynamicWorkflowStatus =
  | "pending"
  | "queued"
  | "running"
  | "blocked"
  | "ready"
  | "confirmed"
  | "stale"
  | "partial_failed"
  | "failed"
  | "interrupted"
  | "cancelled";

export type DynamicStageStatus = DynamicWorkflowStatus;
export type DynamicSourceKind = "full_text" | "synopsis";
export type DynamicDiagnosticLevel = "warning" | "blocking";
export type DynamicGateStatus =
  | "ready"
  | "warning"
  | "blocked"
  | "partial_failed";

export type DynamicJsonObject = Record<string, unknown>;

export interface DynamicDiagnostic {
  level: DynamicDiagnosticLevel;
  code: string;
  message: string;
  path?: string | null;
  details?: DynamicJsonObject;
}

export interface DynamicGateResult {
  status: DynamicGateStatus;
  passed: boolean;
  diagnostics: DynamicDiagnostic[];
  checked_at?: string | null;
  can_confirm: boolean;
}

export interface DynamicAssetMapping {
  workflow_asset_id: string;
  asset_id?: string | null;
  usage: string;
  locked_revision?: number | null;
  reuse_mode?: "new" | "approved_existing" | string;
  [key: string]: unknown;
}

export interface DynamicWorkflowRun {
  id: string;
  project_id?: string | null;
  chapter_id?: string | null;
  title?: string | null;
  chapter_title?: string | null;
  kind: string;
  schema_version: string;
  status: DynamicWorkflowStatus;
  current_stage: DynamicStageKey;
  source_kind: DynamicSourceKind;
  source_snapshot: DynamicJsonObject;
  config_snapshot: DynamicJsonObject;
  asset_mapping: DynamicAssetMapping[];
  input_signature: string;
  revision: number;
  error: string;
  created_at: string;
  started_at?: string | null;
  updated_at: string;
  finished_at?: string | null;
  [key: string]: unknown;
}

export interface DynamicWorkflowStage {
  id: string;
  workflow_run_id: string;
  stage_key: DynamicStageKey;
  ordinal: number;
  status: DynamicStageStatus;
  input_signature: string;
  raw_output: DynamicJsonObject;
  normalized_output: DynamicJsonObject;
  draft_output: DynamicJsonObject;
  output_refs: DynamicJsonObject[];
  gate_result: DynamicGateResult;
  attempt: number;
  error: string;
  created_at: string;
  started_at?: string | null;
  updated_at: string;
  finished_at?: string | null;
  confirmed_at?: string | null;
  confirmed_revision?: number | null;
  [key: string]: unknown;
}

export interface DynamicWorkflowAction {
  run: DynamicWorkflowRun;
  stages: DynamicWorkflowStage[];
}

export interface DynamicRunCreateInput {
  project_id?: string | null;
  title: string;
  chapter_title: string;
  source_text?: string;
  synopsis?: string;
  target_duration_sec: number;
  aspect_ratio: string;
  style_prompt: string;
  auto_run: boolean;
  generate_audio: boolean;
  generate_subtitle: boolean;
  generate_bgm: boolean;
  generate_composition: boolean;
  reuse_approved_assets?: boolean;
}

export interface DynamicStageDraftUpdate {
  expected_revision: number;
  draft_output: DynamicJsonObject;
  // 通过的门禁作为请求上下文传递；阻塞草稿由服务端根据新内容重新计算。
  gate_result?: DynamicGateResult;
}

export interface DynamicDeliveryChecklistItem {
  key: string;
  label: string;
  status: string;
  completed: number;
  total: number;
  reason: string;
  details?: DynamicJsonObject;
  [key: string]: unknown;
}

export interface DynamicPreflightItem {
  kind: string;
  id: string;
  status: string;
  path?: string | null;
  reason: string;
  required: boolean;
  [key: string]: unknown;
}

export interface DynamicPreflightResult {
  workflow_run_id: string;
  project_id: string;
  chapter_id: string;
  strict: boolean;
  allow_partial: boolean;
  status: "ready" | "warning" | "blocked";
  items: DynamicPreflightItem[];
  missing: DynamicPreflightItem[];
  failed: DynamicPreflightItem[];
  skipped: DynamicPreflightItem[];
  estimated_jobs: number;
  input_signature: string;
  provenance: DynamicJsonObject;
}

export interface DynamicChecklistResponse {
  checklist: DynamicDeliveryChecklistItem[];
  preflight: DynamicPreflightResult;
}

export interface DynamicDeliveryOptions {
  strict?: boolean;
  allow_partial?: boolean;
  exported_at?: string;
}
