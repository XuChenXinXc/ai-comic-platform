export type ProviderKind = "text" | "image" | "video";
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
  created_at: string;
  updated_at: string;
  is_stale?: boolean;
}

export interface ReferenceBinding {
  role:
    | "character_identity"
    | "scene_layout"
    | "prop_identity"
    | "previous_keyframe";
  asset_id?: string;
  asset_image_id?: string;
  keyframe_id?: string;
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
