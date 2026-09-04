import type {
  Chapter,
  ApiProvider,
  Asset,
  AssetType,
  BatchResponse,
  ContentRecord,
  DynamicChecklistResponse,
  DynamicDeliveryOptions,
  DynamicPreflightResult,
  DynamicRunCreateInput,
  DynamicStageDraftUpdate,
  DynamicStageKey,
  DynamicWorkflowAction,
  DynamicWorkflowRun,
  DynamicWorkflowStage,
  Job,
  MediaVersion,
  CameraPolicySetting,
  Project,
  ProjectInput,
  ProjectStorageSelection,
  ProjectStorageSetting,
  ProviderInput,
  SceneView,
  ScreenplayContent,
  Shot,
  ShotQualityReport,
} from "../types/api";

export function getDynamicRunTitle(
  run: Pick<DynamicWorkflowRun, "title" | "source_snapshot">,
) {
  // 兼容旧接口把项目名放在 title 的情况；source_snapshot 保存真正的运行标题。
  const snapshotTitle = run.source_snapshot?.title;
  if (typeof snapshotTitle === "string" && snapshotTitle.trim()) {
    return snapshotTitle.trim();
  }
  if (typeof run.title === "string" && run.title.trim()) {
    return run.title.trim();
  }
  return "未命名运行";
}

/**
 * 解析 VITE_API_BASE_URL。空、相对路径、原型 localhost 的相对形式都接受；
 * 非法的绝对 URL（如 `http://`）降级为相对路径 `/api`，避免请求 host 错乱。
 */
function resolveApiBase(raw: string | undefined): string {
  const candidate = (raw || "/api").replace(/\/$/, "");
  if (!candidate) return "/api";
  if (candidate.startsWith("/")) return candidate;
  try {
    // 仅做合法性校验（构造失败即抛错），URL 解析失败时降级为 /api
    new URL(candidate);
    return candidate;
  } catch {
    return "/api";
  }
}

const API_BASE = resolveApiBase(import.meta.env.VITE_API_BASE_URL);

export class ApiError extends Error {
  status: number;
  details: unknown;

  constructor(message: string, status: number, details?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.details = details;
  }
}

function errorMessage(payload: unknown, fallback: string) {
  if (!payload || typeof payload !== "object") return fallback;
  const detail = (payload as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (typeof item === "string") return item;
        if (item && typeof item === "object" && "msg" in item) {
          return String((item as { msg: unknown }).msg);
        }
        return JSON.stringify(item);
      })
      .join("；");
  }
  const message = (payload as { message?: unknown }).message;
  return typeof message === "string" ? message : fallback;
}

// 用于标记 204 No Content 响应的类型
export type NoContent = void;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  headers.set("Accept", "application/json");

  const response = await fetch(`${API_BASE}${path}`, { ...init, headers });
  if (response.status === 204) return undefined as T;

  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : await response.text();

  if (!response.ok) {
    throw new ApiError(
      errorMessage(payload, `请求失败（${response.status}）`),
      response.status,
      payload,
    );
  }
  return payload as T;
}

function json(method: string, body?: unknown): RequestInit {
  return {
    method,
    body: body === undefined ? undefined : JSON.stringify(body),
  };
}

function asList<T>(value: T[] | { items: T[] }) {
  return Array.isArray(value) ? value : value.items;
}

function params(values: Record<string, string | number | boolean | undefined | null>) {
  const query = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") {
      query.set(key, String(value));
    }
  });
  const result = query.toString();
  return result ? `?${result}` : "";
}

function assetPayload(body: Partial<Asset>, includeType: boolean) {
  const payload: Record<string, unknown> = {
    name: body.name,
    profile: body.profile_json ?? body.profile,
    prompt: body.prompt,
    prompt_override: body.prompt_override,
    negative_prompt: body.negative_prompt,
    consistency_keywords:
      body.consistency_json ?? body.consistency_keywords,
    composition: body.composition,
    importance: body.importance,
    status: body.status,
  };
  if (includeType) payload.asset_type = body.asset_type;
  return payload;
}

function shotPayload(body: Partial<Shot>, includeCreateFields: boolean) {
  const rawSourceRef = body.source_ref || "";
  const scenePrefix = body.scene_no ? `scene:${body.scene_no}|` : "";
  const sourceRef = scenePrefix
    ? `${scenePrefix}${rawSourceRef.replace(/^scene:\d+\|?/, "")}`
    : rawSourceRef;
  const payload: Record<string, unknown> = {
    scene_asset_id: body.scene_asset_id,
    // 后端 shot update 接收 scene_view_key（空字符串=默认母版）；scene_view_id 只读展示用，不提交
    scene_view_key: body.scene_view_key,
    character_ids: body.character_ids_json ?? body.character_ids,
    prop_ids: body.prop_ids_json ?? body.prop_ids,
    positions: body.positions_json ?? body.positions,
    start_state: body.start_state,
    action: body.action,
    expressions: body.expressions_json ?? body.expressions,
    dialogue: body.dialogue_json ?? body.dialogue,
    narration: body.narration,
    shot_type: body.shot_type,
    camera_angle: body.camera_angle,
    camera_move: body.camera_move,
    duration_sec: body.duration_sec,
    end_state: body.end_state,
    next_hook: body.next_hook,
    transition_from_prev: body.transition_from_prev,
    frame_description: body.frame_description,
    image_prompt: body.image_prompt,
    image_prompt_override: body.image_prompt_override,
    video_prompt: body.video_prompt,
    video_prompt_override: body.video_prompt_override,
    negative_prompt: body.negative_prompt,
    reference_bindings:
      body.reference_bindings_json ?? body.reference_bindings,
    status: body.status,
  };
  if (body.source_ref !== undefined || body.scene_no !== undefined) {
    payload.source_ref = sourceRef;
  }
  if (includeCreateFields) {
    payload.source_type = body.source_type;
    payload.shot_no = body.shot_no;
    payload.shot_order = body.shot_order;
  }
  return payload;
}

function providerPayload(body: Partial<ProviderInput>, includeKind: boolean) {
  const payload: Record<string, unknown> = {
    name: body.name,
    adapter: body.adapter,
    base_url: body.base_url,
    api_key_env: body.api_key_env,
    model: body.model,
    params: body.params,
    timeout_sec: body.timeout_sec,
    max_retries: body.max_retries,
    enabled: body.enabled,
    is_default: body.is_default,
  };
  if (body.api_key) payload.api_key = body.api_key;
  if (includeKind) payload.kind = body.kind;
  return payload;
}

export function mediaUrl(version?: MediaVersion | null) {
  const value = version?.media_url || version?.file_url || version?.file_path;
  if (!value) return "";
  if (/^https?:\/\//i.test(value) || value.startsWith("blob:")) return value;
  if (value.startsWith("/media/")) {
    // 服务端返回的路径理论上可信，仍清洗路径段防止拼出越界 URL；
    // 必须补回前导斜杠，否则嵌套路由下会解析成相对路径导致 404。
    const parts = value.split("/").filter((part) => part && part !== "..");
    return `/${parts.join("/")}`;
  }
  const cleaned = value
    .replace(/^\/+/, "")
    .split("/")
    .filter((part) => part && part !== "..")
    .join("/");
  return `/media/${cleaned}`;
}

export async function download(path: string, filename: string) {
  return downloadBlob(path, filename, { method: "GET" });
}

async function downloadBlob(path: string, filename: string, init?: RequestInit) {
  const headers = new Headers(init?.headers);
  if (init?.body && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  headers.set("Accept", "application/zip");
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers });
  if (!response.ok) {
    let payload: unknown;
    try {
      const contentType = response.headers.get("content-type") || "";
      payload = contentType.includes("application/json")
        ? await response.json()
        : await response.text();
    } catch {
      payload = null;
    }
    throw new ApiError(
      errorMessage(payload, `下载失败（${response.status}）`),
      response.status,
      payload,
    );
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  // 延后 revoke：部分浏览器（Firefox/Safari）在 click 后立即回收会中断下载。
  window.setTimeout(() => URL.revokeObjectURL(url), 1_000);
}

function safeDownloadFilename(value: string) {
  return value.replace(/[\\/:*?"<>|]+/g, "-").trim() || "dynamic-delivery";
}

export const queryKeys = {
  projects: ["projects"] as const,
  project: (id: string) => ["projects", id] as const,
  analysis: (id: string) => ["analysis", id] as const,
  screenplay: (id: string) => ["screenplay", id] as const,
  chapters: (projectId: string) => ["chapters", projectId] as const,
  assets: (id: string, type?: AssetType) => ["assets", id, type || "all"] as const,
  assetImages: (id: string) => ["asset-images", id] as const,
  shots: (id: string) => ["shots", id] as const,
  keyframes: (id: string) => ["keyframes", id] as const,
  videos: (id: string) => ["videos", id] as const,
  providers: ["providers"] as const,
  projectStorage: ["settings", "project-storage"] as const,
  cameraPolicy: ["settings", "camera-policy"] as const,
  jobs: (projectId?: string) => ["jobs", projectId || "all"] as const,
  dynamicRuns: (projectId?: string) => ["dynamic-runs", projectId || "all"] as const,
  dynamicRun: (runId: string) => ["dynamic-runs", runId] as const,
};

export const api = {
  settings: {
    projectStorage: {
      get: () =>
        request<ProjectStorageSetting>("/settings/project-storage"),
      select: () =>
        request<ProjectStorageSelection>(
          "/settings/project-storage/select",
          json("POST"),
        ),
    },
    cameraPolicy: {
      get: () =>
        request<CameraPolicySetting>("/settings/camera-policy"),
      set: (policy: string) =>
        request<CameraPolicySetting>(
          "/settings/camera-policy",
          json("POST", { policy }),
        ),
    },
  },
  projects: {
    list: async (filters: { kind?: "regular" | "free" } = {}) =>
      asList(
        await request<Project[] | { items: Project[] }>(
          `/projects${params(filters)}`,
        ),
      ),
    get: (id: string) => request<Project>(`/projects/${id}`),
    create: (body: ProjectInput) => request<Project>("/projects", json("POST", body)),
    update: (id: string, body: Partial<ProjectInput>) =>
      request<Project>(`/projects/${id}`, json("PUT", body)),
    remove: (id: string) => request<void>(`/projects/${id}`, json("DELETE")),
    exportVideos: (id: string, filename: string) =>
      download(`/projects/${id}/videos/export`, filename),
  },
  chapters: {
    list: (projectId: string) => request<Chapter[]>(`/projects/${projectId}/chapters`),
    get: (chapterId: string) => request<Chapter>(`/chapters/${chapterId}`),
    create: (projectId: string, body: { title?: string; source_text?: string } = {}) =>
      request<Chapter>(`/projects/${projectId}/chapters`, json("POST", body)),
    update: (
      chapterId: string,
      body: { title?: string; source_text?: string; status?: string },
    ) => request<Chapter>(`/chapters/${chapterId}`, json("PUT", body)),
    deletePreview: (chapterId: string) =>
      request<{
        chapter_no: number;
        title: string;
        shot_count: number;
        keyframe_count: number;
        video_count: number;
        note: string;
      }>(`/chapters/${chapterId}/delete-preview`),
    remove: (chapterId: string) =>
      request<void>(`/chapters/${chapterId}`, json("DELETE")),
    reorder: (projectId: string, ids: string[]) =>
      request<Chapter[]>(`/projects/${projectId}/chapters/reorder`, json("POST", { ids })),
  },
  analysis: {
    get: (chapterId: string) =>
      request<ContentRecord>(`/chapters/${chapterId}/analysis`),
    generate: (chapterId: string, body: Record<string, unknown> = {}) =>
      request<Job>(`/chapters/${chapterId}/analysis/generate`, json("POST", body)),
    update: (
      chapterId: string,
      body: { content_json?: unknown; content?: unknown; status?: string },
    ) => request<ContentRecord>(`/chapters/${chapterId}/analysis`, json("PUT", body)),
  },
  screenplay: {
    get: (chapterId: string) =>
      request<ContentRecord<ScreenplayContent>>(
        `/chapters/${chapterId}/screenplay`,
      ),
    generate: (chapterId: string, body: Record<string, unknown> = {}) =>
      request<Job>(`/chapters/${chapterId}/screenplay/generate`, json("POST", body)),
    regenerateScene: (
      chapterId: string,
      sceneNo: number,
      body: Record<string, unknown> = {},
    ) =>
      request<Job>(
        `/chapters/${chapterId}/screenplay/scenes/${sceneNo}/generate`,
        json("POST", body),
      ),
    update: (
      chapterId: string,
      body: { content_json?: unknown; content?: unknown; status?: string },
    ) =>
      request<ContentRecord<ScreenplayContent>>(
        `/chapters/${chapterId}/screenplay`,
        json("PUT", body),
      ),
  },
  assets: {
    list: async (projectId: string, assetType?: AssetType) =>
      asList(
        await request<Asset[] | { items: Asset[] }>(
          `/projects/${projectId}/assets${params({ asset_type: assetType })}`,
        ),
      ),
    extract: (
      chapterId: string,
      body: { asset_types?: AssetType[] } = {},
    ) => request<Job | BatchResponse>(
      `/chapters/${chapterId}/assets/extract`,
      json("POST", body),
    ),
    create: (projectId: string, body: Partial<Asset>) =>
      request<Asset>(
        `/projects/${projectId}/assets`,
        json("POST", assetPayload(body, true)),
      ),
    update: (id: string, body: Partial<Asset>) =>
      request<Asset>(`/assets/${id}`, json("PUT", assetPayload(body, false))),
    remove: (id: string) => request<void>(`/assets/${id}`, json("DELETE")),
    merge: (projectId: string, ids: string[]) =>
      request<Asset>(
        `/projects/${projectId}/assets/merge`,
        json("POST", { ids }),
      ),
    generatePrompt: (id: string, body: Record<string, unknown> = {}) =>
      request<Job>(`/assets/${id}/prompt/generate`, json("POST", body)),
    batchPrompts: (
      projectId: string,
      body: { ids?: string[]; asset_type?: AssetType } = {},
    ) =>
      request<BatchResponse>(
        `/projects/${projectId}/assets/prompts/batch`,
        json("POST", body),
      ),
    images: {
      list: async (
        assetId: string,
        filters: { scene_view_id?: string } = {},
      ) =>
        asList(
          await request<MediaVersion[] | { items: MediaVersion[] }>(
            `/assets/${assetId}/images${params(filters)}`,
          ),
        ),
      generate: (assetId: string, body: Record<string, unknown> = {}) =>
        request<Job>(`/assets/${assetId}/images/generate`, json("POST", body)),
      batch: (
        projectId: string,
        body: {
          ids?: string[];
          asset_type?: AssetType;
          only_missing?: boolean;
          provider_id?: string | null;
        } = {},
      ) =>
        request<BatchResponse>(
          `/projects/${projectId}/assets/images/batch`,
          json("POST", body),
        ),
      upload: (assetId: string, file: File) => {
        const body = new FormData();
        body.append("file", file);
        return request<MediaVersion>(`/assets/${assetId}/images/upload`, {
          method: "POST",
          body,
        });
      },
      current: (id: string) =>
        request<MediaVersion>(`/asset-images/${id}/current`, json("PUT")),
      remove: (id: string) =>
        request<void>(`/asset-images/${id}`, json("DELETE")),
    },
    sceneViews: {
      list: (assetId: string) =>
        request<SceneView[]>(`/assets/${assetId}/scene-views`),
      ensure: (assetId: string) =>
        request<SceneView[]>(`/assets/${assetId}/scene-views/ensure`, json("POST")),
      update: (viewId: string, body: { prompt: string }) =>
        request<SceneView>(`/scene-views/${viewId}`, json("PUT", body)),
      remove: (viewId: string) =>
        request<void>(`/scene-views/${viewId}`, json("DELETE")),
      generate: (
        assetId: string,
        viewKey: string,
        body: { provider_id?: string } = {},
      ) =>
        request<Job>(
          `/assets/${assetId}/scene-views/${viewKey}/generate`,
          json("POST", body),
        ),
    },
  },
  shots: {
    list: async (chapterId: string) =>
      asList(
        await request<Shot[] | { items: Shot[] }>(
          `/chapters/${chapterId}/shots`,
        ),
      ),
    extract: (
      chapterId: string,
      body: {
        scope?: "all" | "scene";
        scene_no?: number;
      },
    ) =>
      request<Job | BatchResponse>(
        `/chapters/${chapterId}/shots/extract`,
        json("POST", body),
      ),
    create: (chapterId: string, body: Partial<Shot>) =>
      request<Shot>(
        `/chapters/${chapterId}/shots`,
        json("POST", shotPayload(body, true)),
      ),
    update: (id: string, body: Partial<Shot>) =>
      request<Shot>(`/shots/${id}`, json("PUT", shotPayload(body, false))),
    remove: (id: string) => request<void>(`/shots/${id}`, json("DELETE")),
    reorder: (chapterId: string, ids: string[]) =>
      request<Shot[]>(
        `/chapters/${chapterId}/shots/reorder`,
        json("POST", { ids }),
      ),
    split: (id: string) =>
      request<Shot[]>(`/shots/${id}/split`, json("POST", {})),
    copy: (id: string) =>
      request<Shot>(`/shots/${id}/copy`, json("POST", {})),
    merge: (chapterId: string, ids: string[]) =>
      request<Shot>(
        `/chapters/${chapterId}/shots/merge`,
        json("POST", { ids }),
      ),
    generateImagePrompt: (id: string, body: Record<string, unknown> = {}) =>
      request<Job>(`/shots/${id}/image-prompt/generate`, json("POST", body)),
    generateVideoPrompt: (id: string, body: Record<string, unknown> = {}) =>
      request<Job>(`/shots/${id}/video-prompt/generate`, json("POST", body)),
    generateDirectorPlan: (id: string, body: Record<string, unknown> = {}) =>
      request<Job>(`/shots/${id}/director-plan/generate`, json("POST", body)),
    batchDirectorPlans: (
      chapterId: string,
      body: { ids?: string[] } = {},
    ) =>
      request<BatchResponse>(
        `/chapters/${chapterId}/shots/director-plans/batch`,
        json("POST", body),
      ),
    batchPrompts: (
      chapterId: string,
      body: { ids?: string[]; prompt_types?: Array<"image" | "video"> } = {},
    ) =>
      request<BatchResponse>(
        `/chapters/${chapterId}/shots/prompts/batch`,
        json("POST", body),
      ),
    checkContinuity: (
      projectId: string,
      body: {
        ids?: string[];
        apply_fixes?: boolean;
        insert_transitions?: boolean;
        provider_id?: string | null;
        params?: Record<string, unknown>;
      } = {},
    ) =>
      request<Job>(
        `/projects/${projectId}/shots/continuity/check`,
        json("POST", body),
      ),
    refinePrompts: (
      projectId: string,
      body: {
        prompt_types?: Array<"image" | "video">;
        chunk_size?: number;
        provider_id?: string | null;
        params?: Record<string, unknown>;
      } = {},
    ) =>
      request<Job>(
        `/projects/${projectId}/shots/prompts/refine`,
        json("POST", body),
      ),
    qualityReport: (projectId: string) =>
      request<ShotQualityReport>(`/projects/${projectId}/shots/quality-report`),
    fixQuality: (
      projectId: string,
      body: {
        ids?: string[];
        provider_id?: string | null;
        params?: Record<string, unknown>;
      } = {},
    ) =>
      request<Job>(
        `/projects/${projectId}/shots/quality-fix`,
        json("POST", body),
      ),
    keyframes: {
      list: async (shotId: string) =>
        asList(
          await request<MediaVersion[] | { items: MediaVersion[] }>(
            `/shots/${shotId}/keyframes`,
          ),
        ),
      generate: (shotId: string, body: Record<string, unknown> = {}) =>
        request<Job>(`/shots/${shotId}/keyframes/generate`, json("POST", body)),
      batch: (
        chapterId: string,
        body: {
          ids?: string[];
          scene_no?: number;
          only_missing?: boolean;
          provider_id?: string | null;
        } = {},
      ) =>
        request<BatchResponse>(
          `/chapters/${chapterId}/keyframes/batch`,
          json("POST", body),
        ),
      upload: (shotId: string, file: File) => {
        const body = new FormData();
        body.append("file", file);
        return request<MediaVersion>(`/shots/${shotId}/keyframes/upload`, {
          method: "POST",
          body,
        });
      },
      current: (id: string) =>
        request<MediaVersion>(`/keyframes/${id}/current`, json("PUT")),
      remove: (id: string) =>
        request<void>(`/keyframes/${id}`, json("DELETE")),
    },
    videos: {
      list: async (shotId: string) =>
        asList(
          await request<MediaVersion[] | { items: MediaVersion[] }>(
            `/shots/${shotId}/videos`,
          ),
        ),
      generate: (shotId: string, body: Record<string, unknown> = {}) =>
        request<Job>(`/shots/${shotId}/videos/generate`, json("POST", body)),
      batch: (
        chapterId: string,
        body: {
          ids?: string[];
          scene_no?: number;
          only_missing?: boolean;
          provider_id?: string | null;
        } = {},
      ) =>
        request<BatchResponse>(
          `/chapters/${chapterId}/videos/batch`,
          json("POST", body),
        ),
      upload: (shotId: string, file: File) => {
        const body = new FormData();
        body.append("file", file);
        return request<MediaVersion>(`/shots/${shotId}/videos/upload`, {
          method: "POST",
          body,
        });
      },
      current: (id: string) =>
        request<MediaVersion>(`/videos/${id}/current`, json("PUT")),
      remove: (id: string) =>
        request<void>(`/videos/${id}`, json("DELETE")),
      download: (id: string, filename: string) =>
        download(`/videos/${id}/download`, filename),
    },
  },
  providers: {
    list: async (kind?: string) =>
      asList(
        await request<ApiProvider[] | { items: ApiProvider[] }>(
          `/providers${params({ kind })}`,
        ),
      ),
    create: (body: ProviderInput) =>
      request<ApiProvider>("/providers", json("POST", providerPayload(body, true))),
    update: (id: string, body: Partial<ProviderInput>) =>
      request<ApiProvider>(
        `/providers/${id}`,
        json("PUT", providerPayload(body, false)),
      ),
    remove: (id: string) => request<void>(`/providers/${id}`, json("DELETE")),
    test: (id: string) =>
      request<{ ok: boolean; message: string }>(
        `/providers/${id}/test`,
        json("POST"),
      ),
  },
  jobs: {
    list: async (filters: {
      project_id?: string;
      status?: string;
      job_type?: string;
      batch_id?: string;
    } = {}) =>
      asList(
        await request<Job[] | { items: Job[] }>(
          `/jobs${params(filters)}`,
        ),
      ),
    get: (id: string) => request<Job>(`/jobs/${id}`),
    retry: (id: string) => request<Job>(`/jobs/${id}/retry`, json("POST")),
    cancel: (id: string) => request<Job>(`/jobs/${id}/cancel`, json("POST")),
    batch: (id: string) =>
      request<{ batch_id: string; jobs: Job[] }>(`/jobs/batches/${id}`),
  },

  dynamicRuns: {
    createFreeCreate: (body: DynamicRunCreateInput) =>
      request<DynamicWorkflowAction>("/free-create/dynamic-runs", json("POST", body)),
    createForProject: (projectId: string, body: DynamicRunCreateInput) =>
      request<DynamicWorkflowAction>(
        `/projects/${projectId}/dynamic-runs`,
        json("POST", { ...body, project_id: projectId }),
      ),
    list: async (projectId: string) =>
      asList(
        await request<DynamicWorkflowRun[] | { items: DynamicWorkflowRun[] }>(
          `/projects/${projectId}/dynamic-runs`,
        ),
      ),
    get: (runId: string) =>
      request<DynamicWorkflowRun>(`/dynamic-runs/${runId}`),
    getWithStages: (runId: string) =>
      request<DynamicWorkflowAction>(`/dynamic-runs/${runId}/stages`),
    getStage: (runId: string, stageKey: DynamicStageKey) =>
      request<DynamicWorkflowStage>(`/dynamic-runs/${runId}/stages/${stageKey}`),
    start: (runId: string, expectedRevision: number) =>
      request<DynamicWorkflowAction>(
        `/dynamic-runs/${runId}/start`,
        json("POST", { expected_revision: expectedRevision }),
      ),
    pause: (runId: string, expectedRevision: number) =>
      request<DynamicWorkflowAction>(
        `/dynamic-runs/${runId}/pause`,
        json("POST", { expected_revision: expectedRevision }),
      ),
    resume: (runId: string, expectedRevision: number) =>
      request<DynamicWorkflowAction>(
        `/dynamic-runs/${runId}/resume`,
        json("POST", { expected_revision: expectedRevision }),
      ),
    cancel: (runId: string, expectedRevision: number) =>
      request<DynamicWorkflowAction>(
        `/dynamic-runs/${runId}/cancel`,
        json("POST", { expected_revision: expectedRevision }),
      ),
    saveDraft: (
      runId: string,
      stageKey: DynamicStageKey,
      body: DynamicStageDraftUpdate,
    ) =>
      request<DynamicWorkflowAction>(
        `/dynamic-runs/${runId}/stages/${stageKey}/draft`,
        json("PUT", body),
      ),
    confirmStage: (
      runId: string,
      stageKey: DynamicStageKey,
      expectedRevision: number,
    ) =>
      request<DynamicWorkflowAction>(
        `/dynamic-runs/${runId}/stages/${stageKey}/confirm`,
        json("POST", { expected_revision: expectedRevision }),
      ),
    regenerateStage: (
      runId: string,
      stageKey: DynamicStageKey,
      expectedRevision: number,
    ) =>
      request<DynamicWorkflowAction>(
        `/dynamic-runs/${runId}/stages/${stageKey}/regenerate`,
        json("POST", { expected_revision: expectedRevision }),
      ),
    checklist: (runId: string) =>
      request<DynamicChecklistResponse>(`/dynamic-runs/${runId}/delivery/checklist`),
    preflight: (
      runId: string,
      options: DynamicDeliveryOptions = {},
    ) =>
      request<DynamicPreflightResult>(
        `/dynamic-runs/${runId}/delivery/preflight`,
        json("POST", {
          strict: options.strict ?? true,
          allow_partial: options.allow_partial ?? false,
        }),
      ),
    exportZip: async (
      runId: string,
      options: DynamicDeliveryOptions = {},
    ) => {
      const fallbackName = `dynamic-delivery-${safeDownloadFilename(runId)}.zip`;
      await downloadBlob(
        `/dynamic-runs/${runId}/delivery/export`,
        fallbackName,
        json("POST", {
          strict: options.strict ?? true,
          allow_partial: options.allow_partial ?? false,
          exported_at: options.exported_at,
        }),
      );
    },
  },

  audio: {
    getEdgeTTSVoices: () =>
      request<Array<{
        name: string;
        display_name: string;
        gender: string;
        description: string;
      }>>(`/audio/edge-tts/voices`),

    getConfig: (projectId: string) =>
      request<{
        project_id: string;
        tts_provider_id: string | null;
        bgm_volume: number;
        dialogue_volume: number;
        sound_effect_volume: number;
        // 角色→音色映射（服务端持久化；键为角色名，值为音色 ID）
        voice_mapping: Record<string, string>;
      }>(`/audio/projects/${projectId}/config`),

    updateConfig: (projectId: string, config: {
      tts_provider_id?: string;
      bgm_volume?: number;
      dialogue_volume?: number;
      sound_effect_volume?: number;
      voice_mapping?: Record<string, string>;
    }) =>
      request<{
        project_id: string;
        tts_provider_id: string | null;
        bgm_volume: number;
        dialogue_volume: number;
        sound_effect_volume: number;
        voice_mapping: Record<string, string>;
      }>(`/audio/projects/${projectId}/config`, json("PUT", config)),

    extractDialogues: (chapterId: string) =>
      request<Array<{
        shot_no: number;
        speaker: string;
        text: string;
      }>>(`/audio/chapters/${chapterId}/dialogues`),

    generateShotAudio: (shotId: string, body: {
      dialogue_text: string;
      voice_id: string;
      tts_provider_id?: string;
    }) =>
      request<{
        id: string;
        shot_id: string;
        dialogue_text: string;
        speaker_name: string;
        voice_id: string;
        file_path: string | null;
        duration_sec: number | null;
        version_no: number;
        status: string;
        is_current: number;
        created_at: string;
      }>(`/audio/shots/${shotId}/audio`, json("POST", body)),

    getShotAudio: (shotId: string) =>
      request<{
        id: string;
        shot_id: string;
        dialogue_text: string;
        voice_id: string;
        file_path: string | null;
        duration_sec: number | null;
        version_no: number;
        status: string;
        is_current: number;
      } | null>(`/audio/shots/${shotId}/audio`),

    listShotAudioVersions: (shotId: string) =>
      request<Array<{
        id: string;
        shot_id: string;
        dialogue_text: string;
        version_no: number;
        status: string;
        is_current: number;
      }>>(`/audio/shots/${shotId}/audio/versions`),

    getChapterAudios: (chapterId: string) =>
      request<Array<{
        shot_id: string;
        shot_no: number;
        audio: {
          id: string;
          dialogue_text: string;
          voice_id: string;
          file_path: string | null;
          duration_sec: number | null;
          status: string;
        } | null;
      }>>(`/audio/chapters/${chapterId}/audios`),

    deleteShotAudio: (audioId: string) =>
      request<void>(`/audio/shot-audios/${audioId}`, json("DELETE")),

    listBGMs: (projectId: string) =>
      request<Array<{
        id: string;
        project_id: string;
        name: string;
        file_path: string;
        duration_sec: number | null;
        mood_tags: string;
        created_at: string;
      }>>(`/audio/projects/${projectId}/bgm`),

    uploadBGM: async (projectId: string, file: File, name: string, moodTags: string[]) => {
      const formData = new FormData();
      formData.append("file", file);
      formData.append("name", name);
      formData.append("mood_tags", JSON.stringify(moodTags));

      return request<{
        id: string;
        project_id: string;
        name: string;
        file_path: string;
        duration_sec: number | null;
        mood_tags: string;
        created_at: string;
      }>(`/audio/projects/${projectId}/bgm`, {
        method: "POST",
        body: formData,
      });
    },

    deleteBGM: (bgmId: string) =>
      request<{ message: string }>(`/audio/bgm/${bgmId}`, json("DELETE")),
  },

  composition: {
    generateSubtitle: (shotId: string) =>
      request<Array<{
        text: string;
        start_time: number;
        end_time: number;
        style_preset: string;
      }>>(`/composition/shots/${shotId}/subtitles/generate`, json("POST")),

    saveSubtitles: (shotId: string, subtitles: Array<{
      text: string;
      start_time: number;
      end_time: number;
      style_preset: string;
    }>) =>
      request<Array<{
        id: string;
        shot_id: string;
        text: string;
        start_time: number;
        end_time: number;
        style_preset: string;
        created_at: string;
      }>>(`/composition/shots/${shotId}/subtitles`, json("POST", { subtitles })),

    getSubtitles: (shotId: string) =>
      request<Array<{
        id: string;
        shot_id: string;
        text: string;
        start_time: number;
        end_time: number;
        style_preset: string;
        created_at: string;
      }>>(`/composition/shots/${shotId}/subtitles`),

    getSubtitleStyles: () =>
      request<Record<string, {
        name: string;
        position: string;
        font: string;
        font_size: string;
        color: string;
        outline: string;
        outline_color: string;
      }>>(`/composition/subtitle-styles`),

    exportChapterSRT: async (chapterId: string) => {
      const response = await fetch(
        `${API_BASE}/composition/chapters/${chapterId}/subtitles/srt`,
      );
      if (!response.ok) {
        let payload: unknown = null;
        try {
          const contentType = response.headers.get("content-type") || "";
          payload = contentType.includes("application/json")
            ? await response.json()
            : await response.text();
        } catch {
          payload = null;
        }
        throw new ApiError(
          errorMessage(payload, `下载失败（${response.status}）`),
          response.status,
          payload,
        );
      }
      return response.blob();
    },

    composeChapter: (chapterId: string, config: {
      resolution?: "720p" | "1080p" | "4k";
      include_audio?: boolean;
      burn_subtitles?: boolean;
      bgm_id?: string;
      bgm_volume?: number;
      crossfade?: boolean;
      crossfade_seconds?: number;
    }) =>
      request<{
        id: string;
        chapter_id: string;
        job_id: string | null;
        file_path: string | null;
        duration_sec: number | null;
        resolution: string;
        include_audio: number;
        burn_subtitles: number;
        bgm_id: string | null;
        status: string;
        error: string;
        created_at: string;
        updated_at: string;
      }>(`/composition/chapters/${chapterId}/compose`, json("POST", config)),

    getChapterComposition: (chapterId: string) =>
      request<{
        id: string;
        chapter_id: string;
        job_id: string | null;
        file_path: string | null;
        duration_sec: number | null;
        resolution: string;
        status: string;
        error: string;
        created_at: string;
      } | null>(`/composition/chapters/${chapterId}/composition`),
  },
};

