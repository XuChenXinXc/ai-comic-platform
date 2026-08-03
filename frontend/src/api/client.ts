import type {
  ApiProvider,
  Asset,
  AssetType,
  BatchResponse,
  ContentRecord,
  Job,
  MediaVersion,
  Project,
  ProjectInput,
  ProjectStorageSelection,
  ProjectStorageSetting,
  ProviderInput,
  ScreenplayContent,
  Shot,
} from "../types/api";

const API_BASE = (import.meta.env.VITE_API_BASE_URL || "/api").replace(/\/$/, "");

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
  if (value.startsWith("/media/")) return value;
  return `/media/${value.replace(/^\/+/, "")}`;
}

export async function download(path: string, filename: string) {
  const response = await fetch(`${API_BASE}${path}`);
  if (!response.ok) {
    let payload: unknown;
    try {
      payload = await response.json();
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
  URL.revokeObjectURL(url);
}

export const queryKeys = {
  projects: ["projects"] as const,
  project: (id: string) => ["projects", id] as const,
  analysis: (id: string) => ["analysis", id] as const,
  screenplay: (id: string) => ["screenplay", id] as const,
  assets: (id: string, type?: AssetType) => ["assets", id, type || "all"] as const,
  assetImages: (id: string) => ["asset-images", id] as const,
  shots: (id: string) => ["shots", id] as const,
  keyframes: (id: string) => ["keyframes", id] as const,
  videos: (id: string) => ["videos", id] as const,
  providers: ["providers"] as const,
  projectStorage: ["settings", "project-storage"] as const,
  jobs: (projectId?: string) => ["jobs", projectId || "all"] as const,
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
  },
  projects: {
    list: async () => asList(await request<Project[] | { items: Project[] }>("/projects")),
    get: (id: string) => request<Project>(`/projects/${id}`),
    create: (body: ProjectInput) => request<Project>("/projects", json("POST", body)),
    update: (id: string, body: Partial<ProjectInput>) =>
      request<Project>(`/projects/${id}`, json("PUT", body)),
    remove: (id: string) => request<void>(`/projects/${id}`, json("DELETE")),
    exportVideos: (id: string, filename: string) =>
      download(`/projects/${id}/videos/export`, filename),
  },
  analysis: {
    get: (projectId: string) =>
      request<ContentRecord>(`/projects/${projectId}/analysis`),
    generate: (projectId: string, body: Record<string, unknown> = {}) =>
      request<Job>(`/projects/${projectId}/analysis/generate`, json("POST", body)),
    update: (
      projectId: string,
      body: { content_json?: unknown; content?: unknown; status?: string },
    ) => request<ContentRecord>(`/projects/${projectId}/analysis`, json("PUT", body)),
  },
  screenplay: {
    get: (projectId: string) =>
      request<ContentRecord<ScreenplayContent>>(
        `/projects/${projectId}/screenplay`,
      ),
    generate: (projectId: string, body: Record<string, unknown> = {}) =>
      request<Job>(`/projects/${projectId}/screenplay/generate`, json("POST", body)),
    regenerateScene: (
      projectId: string,
      sceneNo: number,
      body: Record<string, unknown> = {},
    ) =>
      request<Job>(
        `/projects/${projectId}/screenplay/scenes/${sceneNo}/generate`,
        json("POST", body),
      ),
    update: (
      projectId: string,
      body: { content_json?: unknown; content?: unknown; status?: string },
    ) =>
      request<ContentRecord<ScreenplayContent>>(
        `/projects/${projectId}/screenplay`,
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
      projectId: string,
      body: { asset_types?: AssetType[] } = {},
    ) => request<Job | BatchResponse>(
      `/projects/${projectId}/assets/extract`,
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
      list: async (assetId: string) =>
        asList(
          await request<MediaVersion[] | { items: MediaVersion[] }>(
            `/assets/${assetId}/images`,
          ),
        ),
      generate: (assetId: string, body: Record<string, unknown> = {}) =>
        request<Job>(`/assets/${assetId}/images/generate`, json("POST", body)),
      batch: (
        projectId: string,
        body: { ids?: string[]; asset_type?: AssetType } = {},
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
  },
  shots: {
    list: async (projectId: string) =>
      asList(
        await request<Shot[] | { items: Shot[] }>(
          `/projects/${projectId}/shots`,
        ),
      ),
    extract: (
      projectId: string,
      body: {
        scope?: "all" | "scene";
        scene_no?: number;
      },
    ) =>
      request<Job | BatchResponse>(
        `/projects/${projectId}/shots/extract`,
        json("POST", body),
      ),
    create: (projectId: string, body: Partial<Shot>) =>
      request<Shot>(
        `/projects/${projectId}/shots`,
        json("POST", shotPayload(body, true)),
      ),
    update: (id: string, body: Partial<Shot>) =>
      request<Shot>(`/shots/${id}`, json("PUT", shotPayload(body, false))),
    remove: (id: string) => request<void>(`/shots/${id}`, json("DELETE")),
    reorder: (projectId: string, ids: string[]) =>
      request<Shot[]>(
        `/projects/${projectId}/shots/reorder`,
        json("POST", { ids }),
      ),
    split: (id: string) =>
      request<Shot[]>(`/shots/${id}/split`, json("POST", {})),
    copy: (id: string) =>
      request<Shot>(`/shots/${id}/copy`, json("POST", {})),
    merge: (projectId: string, ids: string[]) =>
      request<Shot>(
        `/projects/${projectId}/shots/merge`,
        json("POST", { ids }),
      ),
    generateImagePrompt: (id: string, body: Record<string, unknown> = {}) =>
      request<Job>(`/shots/${id}/image-prompt/generate`, json("POST", body)),
    generateVideoPrompt: (id: string, body: Record<string, unknown> = {}) =>
      request<Job>(`/shots/${id}/video-prompt/generate`, json("POST", body)),
    optimizeVideoPrompt: (
      id: string,
      body: { prompt: string; provider_id?: string | null; params?: Record<string, unknown> },
    ) =>
      request<{ shot_id: string; original: string; prompt: string }>(
        `/shots/${id}/video-prompt/optimize`,
        json("POST", body),
      ),
    optimizeVideoPromptsAll: (
      projectId: string,
      body: { ids?: string[]; provider_id?: string | null; params?: Record<string, unknown> } = {},
    ) =>
      request<BatchResponse>(
        `/projects/${projectId}/video-prompts/optimize-all`,
        json("POST", body),
      ),
    batchPrompts: (
      projectId: string,
      body: { ids?: string[]; prompt_types?: Array<"image" | "video"> } = {},
    ) =>
      request<BatchResponse>(
        `/projects/${projectId}/shots/prompts/batch`,
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
        projectId: string,
        body: { ids?: string[]; scene_no?: number } = {},
      ) =>
        request<BatchResponse>(
          `/projects/${projectId}/keyframes/batch`,
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
        projectId: string,
        body: { ids?: string[]; scene_no?: number } = {},
      ) =>
        request<BatchResponse>(
          `/projects/${projectId}/videos/batch`,
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
};
