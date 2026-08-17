import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Film, ImageOff, Plus, Trash2, WandSparkles } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { api, mediaUrl, queryKeys } from "../api/client";
import {
  EmptyState,
  LoadingState,
  PageHeader,
  StatusBadge,
} from "../components/status";
import {
  Button,
  Card,
  Input,
  Label,
  Modal,
  Select,
  Tabs,
  Textarea,
} from "../components/ui";
import { ImagePreview } from "../components/image-preview";
import { formatError } from "../lib/utils";
import type {
  ApiProvider,
  Asset,
  AssetType,
  BatchResponse,
  Project,
  Shot,
} from "../types/api";

const RUNNING = new Set([
  "queued",
  "running",
  "generating",
  "waiting_provider",
  "downloading",
]);

const FREE_PROJECT_IDS_KEY = "free-create-project-ids";
const FREE_PROJECT_ID_KEY = "free-create-project-id";
const FREE_SCENE_DEFAULTED_SHOT_IDS_KEY = "free-create-scene-defaulted-shot-ids";
const legacyFreeNames = new Set(["手动分镜", "手动"]);

const assetLabels: Record<AssetType, string> = {
  character: "角色",
  scene: "场景",
  prop: "物品",
};

const assetTypes = Object.keys(assetLabels) as AssetType[];

function readFreeProjectIds(): string[] {
  try {
    const value = JSON.parse(
      localStorage.getItem(FREE_PROJECT_IDS_KEY) ?? "[]",
    );
    return Array.isArray(value)
      ? value.filter((item): item is string => typeof item === "string")
      : [];
  } catch {
    return [];
  }
}

function readFreeSceneDefaultedShotIds(): Set<string> {
  try {
    const value = JSON.parse(
      localStorage.getItem(FREE_SCENE_DEFAULTED_SHOT_IDS_KEY) ?? "[]",
    );
    return new Set(
      Array.isArray(value)
        ? value.filter((item): item is string => typeof item === "string")
        : [],
    );
  } catch {
    return new Set();
  }
}

function writeFreeSceneDefaultedShotIds(ids: Set<string>) {
  localStorage.setItem(
    FREE_SCENE_DEFAULTED_SHOT_IDS_KEY,
    JSON.stringify([...ids]),
  );
}

export function nextFreeProjectName(existingNames: string[]): string {
  const names = new Set(existingNames.map((name) => name.trim()));
  if (!names.has("自由制作")) return "自由制作";
  let index = 2;
  while (names.has(`自由制作 ${index}`)) index += 1;
  return `自由制作 ${index}`;
}

export function nextShotTransition(
  existingShotCount: number,
): Shot["transition_from_prev"] {
  return existingShotCount === 0 ? "scene_change" : "continuous";
}

export function isTianyueVideoProvider(
  provider: ApiProvider | undefined,
): boolean {
  return Boolean(
    provider?.base_url?.toLowerCase().includes("api.tianyue.xyz"),
  );
}

function freeProjectPayload(name: string) {
  return {
    name,
    source_text: "",
    style_prompt: "",
    aspect_ratio: "16:9",
    image_size: "2048x1152",
    video_size: "16:9",
    default_duration_sec: 6,
    is_free_create: true,
  };
}

export function selectedAssetContext(
  assets: Asset[],
  sceneId: string,
  characterIds: string[],
  propIds: string[],
): string {
  const groups = [
    ...assets
      .filter((asset) => asset.id === sceneId)
      .map((asset) => ({ label: "场景", asset })),
    ...assets
      .filter((asset) => characterIds.includes(asset.id))
      .map((asset) => ({ label: "角色", asset })),
    ...assets
      .filter((asset) => propIds.includes(asset.id))
      .map((asset) => ({ label: "物品", asset })),
  ];
  return groups
    .map(
      ({ label, asset }) =>
        `${label}「${asset.name}」设定：${asset.prompt.trim() || "以名称为准"}`,
    )
    .join("\n");
}

export function firstSceneAssetId(assets: Asset[]): string {
  return assets.find((asset) => asset.asset_type === "scene")?.id ?? "";
}

function mergeAssetContext(context: string, prompt: string): string {
  const trimmedPrompt = prompt.trim();
  const trimmedContext = context.trim();
  if (!trimmedPrompt) return "";
  if (!trimmedContext) return trimmedPrompt;
  if (trimmedPrompt.startsWith(trimmedContext)) return trimmedPrompt;
  return [trimmedContext, trimmedPrompt].join("\n\n");
}

export function customShotPayload({
  sceneAssetId,
  characterIds,
  propIds,
  duration,
  imagePrompt,
  videoPrompt,
  assetContext,
  transitionFromPrev = "scene_change",
}: {
  sceneAssetId: string;
  characterIds: string[];
  propIds: string[];
  duration: number;
  imagePrompt: string;
  videoPrompt: string;
  assetContext: string;
  transitionFromPrev?: Shot["transition_from_prev"];
}): Partial<Shot> {
  const payload: Partial<Shot> = {
    source_type: "screenplay",
    source_ref: "manual",
    duration_sec: duration,
    image_prompt_override: mergeAssetContext(assetContext, imagePrompt),
    video_prompt_override: mergeAssetContext(assetContext, videoPrompt),
    character_ids: characterIds,
    prop_ids: propIds,
    transition_from_prev: transitionFromPrev,
  };
  if (sceneAssetId) {
    payload.scene_asset_id = sceneAssetId;
  }
  return payload;
}

export const FREE_SHOT_SLOT_COUNT = 8;

export type FreeShotSlotDraft = {
  sceneAssetId: string;
  characterIds: string[];
  propIds: string[];
  imagePrompt: string;
  videoPrompt: string;
  duration: number;
};

export const emptyFreeShotDraft = (): FreeShotSlotDraft => ({
  sceneAssetId: "",
  characterIds: [],
  propIds: [],
  imagePrompt: "",
  videoPrompt: "",
  duration: 6,
});

export function applyFreeSceneDefault(
  draft: FreeShotSlotDraft,
  defaultSceneAssetId: string,
  alreadyDefaulted: boolean,
): FreeShotSlotDraft {
  if (
    alreadyDefaulted ||
    draft.sceneAssetId ||
    !defaultSceneAssetId ||
    draft.imagePrompt.trim() ||
    draft.videoPrompt.trim()
  ) {
    return draft;
  }
  return { ...draft, sceneAssetId: defaultSceneAssetId };
}

function stripMergedContext(text: string, context: string): string {
  const trimmedContext = context.trim();
  if (!trimmedContext || !text.trim().startsWith(trimmedContext)) return text;
  return text
    .slice(text.indexOf(trimmedContext) + trimmedContext.length)
    .replace(/^\s+/, "");
}

export function draftFromFreeShot(
  shot: Shot,
  assets: Asset[] = [],
): FreeShotSlotDraft {
  const assetContext = selectedAssetContext(
    assets,
    shot.scene_asset_id ?? "",
    shot.character_ids ?? [],
    shot.prop_ids ?? [],
  );
  return {
    sceneAssetId: shot.scene_asset_id ?? "",
    characterIds: shot.character_ids ?? [],
    propIds: shot.prop_ids ?? [],
    imagePrompt: stripMergedContext(
      shot.image_prompt_override || "",
      assetContext,
    ),
    videoPrompt: stripMergedContext(
      shot.video_prompt_override || "",
      assetContext,
    ),
    duration: shot.duration_sec ?? 6,
  };
}

export function CustomShotsPage() {
  return <CustomShotsInner />;
}

function CustomShotsInner() {
  const queryClient = useQueryClient();
  const [projects, setProjects] = useState<Project[]>([]);
  const [manualProjectId, setManualProjectId] = useState("");
  const [manualChapterId, setManualChapterId] = useState("");
  const [workspaceError, setWorkspaceError] = useState("");
  const [newProjectOpen, setNewProjectOpen] = useState(false);
  const [newProjectName, setNewProjectName] = useState("");
  const [assetType, setAssetType] = useState<AssetType>("character");
  const [assetName, setAssetName] = useState("");
  const [assetPrompt, setAssetPrompt] = useState("");
  const [slotDrafts, setSlotDrafts] = useState<FreeShotSlotDraft[]>(() =>
    Array.from({ length: FREE_SHOT_SLOT_COUNT }, emptyFreeShotDraft),
  );
  const [savingSlotIndex, setSavingSlotIndex] = useState<number | null>(null);
  const [generatingAssetIds, setGeneratingAssetIds] = useState<Set<string>>(
    new Set(),
  );
  const [imageProviderId, setImageProviderId] = useState(
    () => localStorage.getItem("manual-image-provider") ?? "",
  );
  const [videoProviderId, setVideoProviderId] = useState(
    () => localStorage.getItem("manual-video-provider") ?? "",
  );
  const [resolution, setResolution] = useState<"720p" | "1080p" | "4k">(
    () => {
      const stored = localStorage.getItem("manual-resolution");
      return stored === "720p" || stored === "1080p" || stored === "4k"
        ? stored
        : "1080p";
    },
  );
  const [composing, setComposing] = useState(false);
  const [composition, setComposition] = useState<{
    status: string;
    file_path: string | null;
    error: string;
    job_id: string | null;
  } | null>(null);
  const assetJobsRef = useRef<Set<string>>(new Set());
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const composePollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const assetPollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const freeProjects = projects;
  const suggestedProjectName = nextFreeProjectName(
    projects.map((project) => project.name),
  );

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        // 兼容旧数据：早期自由制作项目只靠名称或本地存储识别。
        // 这里一次性把尚未打标的旧项目转成自由制作项目，之后统一按标记区分。
        const savedIds = readFreeProjectIds();
        const allProjects = await api.projects.list();
        const pendingConversion = allProjects.filter(
          (project) =>
            !project.is_free_create &&
            (legacyFreeNames.has(project.name) ||
              project.name.startsWith("自由制作") ||
              savedIds.includes(project.id)),
        );
        for (const project of pendingConversion) {
          await api.projects.update(project.id, { is_free_create: true });
        }

        let list = await api.projects.list({ kind: "free" });
        const usedNames = new Set(list.map((project) => project.name.trim()));
        for (const project of list.filter((item) =>
          legacyFreeNames.has(item.name),
        )) {
          const nextName = nextFreeProjectName([...usedNames]);
          usedNames.add(nextName);
          await api.projects.update(project.id, { name: nextName });
          project.name = nextName;
        }

        if (!list.length) {
          const created = await api.projects.create(
            freeProjectPayload(nextFreeProjectName(list.map((p) => p.name))),
          );
          list = [created];
        }

        const storedId = localStorage.getItem(FREE_PROJECT_ID_KEY);
        const project =
          list.find((item) => item.id === storedId) ?? list[0];
        const chapters = await api.chapters.list(project.id);
        let chapter = chapters[0] ?? null;
        if (!chapter) {
          chapter = await api.chapters.create(project.id, { title: "第1章" });
        }
        if (!cancelled) {
          setProjects(list);
          setManualProjectId(project.id);
          setManualChapterId(chapter.id);
          localStorage.setItem(FREE_PROJECT_ID_KEY, project.id);
        }
      } catch (error) {
        if (!cancelled) {
          setWorkspaceError(
            error instanceof Error ? error.message : "创建自由制作项目失败",
          );
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const shotsQuery = useQuery({
    queryKey: queryKeys.shots(manualChapterId),
    queryFn: () => api.shots.list(manualChapterId),
    enabled: Boolean(manualChapterId),
  });
  const shots = shotsQuery.data ?? [];
  const manualShots = shots.filter((shot) => shot.source_ref === "manual");

  // 进入页面固定预置 8 个分镜位；空位只作为草稿，不填内容就不参与生成与合成。
  useEffect(() => {
    if (!manualChapterId) return;
    let cancelled = false;
    (async () => {
      try {
        let list = await api.shots.list(manualChapterId);
        const manual = list.filter((shot) => shot.source_ref === "manual");
        for (let no = 1; no <= FREE_SHOT_SLOT_COUNT; no += 1) {
          if (manual.some((shot) => shot.shot_no === no)) continue;
          await api.shots.create(
            manualChapterId,
            customShotPayload({
              sceneAssetId: "",
              characterIds: [],
              propIds: [],
              duration: 6,
              imagePrompt: "",
              videoPrompt: "",
              assetContext: "",
              transitionFromPrev: no === 1 ? "scene_change" : "continuous",
            }),
          );
        }
        list = await api.shots.list(manualChapterId);
        if (cancelled) return;
        const assetList = await api.assets.list(manualProjectId);
        if (cancelled) return;
        const defaultSceneAssetId = firstSceneAssetId(assetList);
        const defaultedShotIds = readFreeSceneDefaultedShotIds();
        let defaultsApplied = false;
        setSlotDrafts(
          Array.from({ length: FREE_SHOT_SLOT_COUNT }, (_, index) => {
            const shot = list.find(
              (item) => item.source_ref === "manual" && item.shot_no === index + 1,
            );
            if (!shot) return emptyFreeShotDraft();
            const draft = draftFromFreeShot(shot, assetList);
            const nextDraft = applyFreeSceneDefault(
              draft,
              defaultSceneAssetId,
              defaultedShotIds.has(shot.id),
            );
            if (nextDraft !== draft) {
              defaultedShotIds.add(shot.id);
              defaultsApplied = true;
            }
            return nextDraft;
          }),
        );
        if (defaultsApplied) {
          writeFreeSceneDefaultedShotIds(defaultedShotIds);
        }
        await queryClient.invalidateQueries({
          queryKey: queryKeys.shots(manualChapterId),
        });
      } catch {
        // 保留现有草稿，用户仍可继续编辑
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [manualChapterId, manualProjectId, queryClient]);

  const assetsQuery = useQuery({
    queryKey: queryKeys.assets(manualProjectId),
    queryFn: () => api.assets.list(manualProjectId),
    enabled: Boolean(manualProjectId),
  });
  const assets = assetsQuery.data ?? [];
  const currentAssets = assets.filter(
    (asset) => asset.asset_type === assetType,
  );
  const imageProvidersQuery = useQuery({
    queryKey: ["providers", "image"],
    queryFn: () => api.providers.list("image"),
  });
  const videoProvidersQuery = useQuery({
    queryKey: ["providers", "video"],
    queryFn: () => api.providers.list("video"),
  });
  const imageProviders = (imageProvidersQuery.data ?? []).filter(
    (provider) => provider.enabled,
  );
  const videoProviders = (videoProvidersQuery.data ?? []).filter(
    (provider) => provider.enabled,
  );
  const manualProject = projects.find(
    (project) => project.id === manualProjectId,
  );
  const effectiveVideoProviderId =
    videoProviderId ||
    manualProject?.default_video_provider_id ||
    videoProviders.find((provider) => provider.is_default)?.id ||
    "";
  const effectiveVideoProvider = videoProviders.find(
    (provider) => provider.id === effectiveVideoProviderId,
  );
  const tianyueFixedVideo = isTianyueVideoProvider(effectiveVideoProvider);

  useEffect(() => {
    if (imageProviders.length === 0) return;
    if (
      imageProviderId &&
      !imageProviders.some((provider) => provider.id === imageProviderId)
    ) {
      setImageProviderId("");
      localStorage.removeItem("manual-image-provider");
    }
  }, [imageProviders, imageProviderId]);

  useEffect(() => {
    if (videoProviders.length === 0) return;
    if (
      videoProviderId &&
      !videoProviders.some((provider) => provider.id === videoProviderId)
    ) {
      setVideoProviderId("");
      localStorage.removeItem("manual-video-provider");
    }
  }, [videoProviders, videoProviderId]);

  const changeImageProvider = (value: string) => {
    setImageProviderId(value);
    localStorage.setItem("manual-image-provider", value);
  };

  const changeVideoProvider = (value: string) => {
    setVideoProviderId(value);
    localStorage.setItem("manual-video-provider", value);
  };

  const changeResolution = (value: "720p" | "1080p" | "4k") => {
    setResolution(value);
    localStorage.setItem("manual-resolution", value);
  };

  const invalidateShots = async () => {
    await queryClient.invalidateQueries({
      queryKey: queryKeys.shots(manualChapterId),
    });
  };

  const invalidateAssets = async () => {
    await queryClient.invalidateQueries({
      queryKey: queryKeys.assets(manualProjectId),
    });
  };

  useEffect(() => {
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
      if (composePollRef.current) clearInterval(composePollRef.current);
      if (assetPollRef.current) clearInterval(assetPollRef.current);
    };
  }, []);

  useEffect(() => {
    if (!manualChapterId) return;
    api.composition
      .getChapterComposition(manualChapterId)
      .then((record) => setComposition(record))
      .catch(() => setComposition(null));
  }, [manualChapterId]);

  const stopTimers = () => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    if (composePollRef.current) {
      clearInterval(composePollRef.current);
      composePollRef.current = null;
    }
    if (assetPollRef.current) {
      clearInterval(assetPollRef.current);
      assetPollRef.current = null;
    }
    assetJobsRef.current = new Set();
    setGeneratingAssetIds(new Set());
  };

  const resetWorkspaceState = () => {
    stopTimers();
    setAssetName("");
    setAssetPrompt("");
    setSlotDrafts(
      Array.from({ length: FREE_SHOT_SLOT_COUNT }, emptyFreeShotDraft),
    );
    setComposition(null);
  };

  const startPolling = () => {
    if (pollRef.current) return;
    pollRef.current = setInterval(async () => {
      let running = true;
      try {
        await invalidateShots();
        // 新生成的版本在完成前还不是 current_*，不能用分镜状态判断；
        // 以任务列表为准，任务全部结束后再做一次最终刷新。
        const jobs = await api.jobs.list({ project_id: manualProjectId });
        running = jobs.some(
          (job) =>
            ["generate_keyframe", "generate_video"].includes(job.job_type) &&
            RUNNING.has(job.status),
        );
      } catch {
        running = true;
      }
      if (!running && pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
        await invalidateShots();
      }
    }, 3000);
  };

  const startAssetPolling = () => {
    if (assetPollRef.current) return;
    assetPollRef.current = setInterval(async () => {
      try {
        await invalidateAssets();
        const jobs = await api.jobs.list({
          project_id: manualProjectId,
          job_type: "generate_asset_image",
        });
        const pending = jobs.filter(
          (job) =>
            assetJobsRef.current.has(job.id) && RUNNING.has(job.status),
        );
        if (pending.length === 0) {
          if (assetPollRef.current) clearInterval(assetPollRef.current);
          assetPollRef.current = null;
          assetJobsRef.current = new Set();
          setGeneratingAssetIds(new Set());
          await invalidateAssets();
        }
      } catch {
        if (assetPollRef.current) clearInterval(assetPollRef.current);
        assetPollRef.current = null;
        assetJobsRef.current = new Set();
        setGeneratingAssetIds(new Set());
      }
    }, 3000);
  };

  const switchProject = async (projectId: string) => {
    if (!projectId || projectId === manualProjectId) return;
    try {
      const chapters = await api.chapters.list(projectId);
      let chapter = chapters[0] ?? null;
      if (!chapter) {
        chapter = await api.chapters.create(projectId, { title: "第1章" });
      }
      resetWorkspaceState();
      setManualProjectId(projectId);
      setManualChapterId(chapter.id);
      localStorage.setItem(FREE_PROJECT_ID_KEY, projectId);
    } catch (error) {
      toast.error(
        `切换自由制作项目失败：${error instanceof Error ? error.message : "未知错误"}`,
      );
    }
  };

  const createProjectMutation = useMutation({
    mutationFn: async () => {
      const name =
        newProjectName.trim() ||
        nextFreeProjectName(projects.map((project) => project.name));
      const project = await api.projects.create(freeProjectPayload(name));
      const chapter = await api.chapters.create(project.id, {
        title: "第1章",
      });
      return { project, chapter };
    },
    onSuccess: ({ project, chapter }) => {
      setProjects((current) =>
        current.some((item) => item.id === project.id)
          ? current
          : [...current, project],
      );
      resetWorkspaceState();
      setManualProjectId(project.id);
      setManualChapterId(chapter.id);
      localStorage.setItem(FREE_PROJECT_ID_KEY, project.id);
      setNewProjectOpen(false);
      setNewProjectName("");
      toast.success("新的自由制作项目已创建");
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const deleteProjectMutation = useMutation({
    mutationFn: () => api.projects.remove(manualProjectId),
    onSuccess: () => {
      const savedIds = readFreeProjectIds().filter(
        (id) => id !== manualProjectId,
      );
      localStorage.setItem(FREE_PROJECT_IDS_KEY, JSON.stringify(savedIds));
      localStorage.removeItem(FREE_PROJECT_ID_KEY);
      toast.success("自由制作项目已删除");
      window.location.reload();
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const createAssetMutation = useMutation({
    mutationFn: () =>
      api.assets.create(manualProjectId, {
        asset_type: assetType,
        name: assetName.trim(),
        prompt: assetPrompt.trim(),
        profile_json: {},
        status: "ready",
      }),
    onSuccess: async (asset) => {
      await invalidateAssets();
      setAssetName("");
      setAssetPrompt("");
      toast.success(`${assetLabels[assetType]}「${asset.name}」已添加`);
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const generateAssetImageMutation = useMutation({
    mutationFn: (assetId: string) =>
      api.assets.images.generate(assetId, {
        provider_id: imageProviderId || undefined,
      }),
    onSuccess: (job, assetId) => {
      setGeneratingAssetIds((current) => new Set(current).add(assetId));
      assetJobsRef.current.add(job.id);
      toast.success("素材图生成已入队");
      startAssetPolling();
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const assetBatchMutation = useMutation({
    mutationFn: () =>
      api.assets.images.batch(manualProjectId, {
        only_missing: true,
        provider_id: imageProviderId || undefined,
      }),
    onSuccess: async (result) => {
      for (const job of result.jobs) {
        assetJobsRef.current.add(job.id);
      }
      setGeneratingAssetIds((current) => {
        const next = new Set(current);
        for (const asset of assets) {
          if (asset.current_image?.status !== "completed") next.add(asset.id);
        }
        return next;
      });
      await invalidateAssets();
      startAssetPolling();
      const failures = result.preflight_failures ?? [];
      if (!result.jobs.length && !failures.length) {
        toast.info("所有素材都已生成参考图");
        return;
      }
      if (failures.length) {
        toast.warning(
          `已入队 ${result.jobs.length} 个素材图任务，跳过 ${failures.length} 项`,
        );
        return;
      }
      toast.success(`已入队 ${result.jobs.length} 个素材图任务`);
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const removeAssetMutation = useMutation({
    mutationFn: (id: string) => api.assets.remove(id),
    onSuccess: async (_data, id) => {
      await invalidateAssets();
      setSlotDrafts((current) =>
        current.map((draft) => ({
          ...draft,
          sceneAssetId: draft.sceneAssetId === id ? "" : draft.sceneAssetId,
          characterIds: draft.characterIds.filter((item) => item !== id),
          propIds: draft.propIds.filter((item) => item !== id),
        })),
      );
      toast.success("素材已删除");
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const updateSlotDraft = (
    index: number,
    patch: Partial<FreeShotSlotDraft>,
  ) => {
    setSlotDrafts((current) =>
      current.map((draft, itemIndex) =>
        itemIndex === index ? { ...draft, ...patch } : draft,
      ),
    );
  };

  const slotShot = (index: number) =>
    manualShots.find((shot) => shot.shot_no === index + 1);

  const buildSlotPayload = (index: number): Partial<Shot> | null => {
    const draft = slotDrafts[index];
    if (!draft) return null;
    const payload = customShotPayload({
      sceneAssetId: draft.sceneAssetId,
      characterIds: draft.characterIds,
      propIds: draft.propIds,
      duration: tianyueFixedVideo ? 15 : draft.duration,
      imagePrompt: draft.imagePrompt,
      videoPrompt: draft.videoPrompt,
      // 素材设定通过参考图传给模型，不拼进用户填写的提示词，避免干扰视频提示词。
      assetContext: "",
      transitionFromPrev: index === 0 ? "scene_change" : "continuous",
    });
    return { ...payload, scene_asset_id: draft.sceneAssetId || null };
  };

  const saveSlot = async (index: number) => {
    const shot = slotShot(index);
    const payload = buildSlotPayload(index);
    if (!shot || !payload) return;
    setSavingSlotIndex(index);
    try {
      await api.shots.update(shot.id, payload);
      await invalidateShots();
    } finally {
      setSavingSlotIndex(null);
    }
  };

  const saveFilledDrafts = async () => {
    const tasks: Promise<void>[] = [];
    slotDrafts.forEach((draft, index) => {
      if (!draft.imagePrompt.trim() && !draft.videoPrompt.trim()) return;
      if (!slotShot(index)) return;
      tasks.push(saveSlot(index));
    });
    await Promise.all(tasks);
  };

  const mediaMutation = useMutation({
    mutationFn: ({ id, kind }: { id: string; kind: "keyframe" | "video" }) =>
      kind === "keyframe"
        ? api.shots.keyframes.generate(id, {
            provider_id: imageProviderId || undefined,
          })
        : api.shots.videos.generate(id, {
            provider_id: videoProviderId || undefined,
          }),
    onSuccess: async (_, variables) => {
      await invalidateShots();
      startPolling();
      toast.success(
        variables.kind === "keyframe" ? "分镜图生成已入队" : "视频生成已入队",
      );
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const generateSlotMedia = async (
    index: number,
    kind: "keyframe" | "video",
  ) => {
    const shot = slotShot(index);
    if (!shot) return;
    await saveSlot(index);
    mediaMutation.mutate({ id: shot.id, kind });
  };

  const mediaBatchMutation = useMutation({
    mutationFn: async (kind: "keyframe" | "video") => {
      await saveFilledDrafts();
      const ids = slotDrafts
        .map((draft, index) => ({ draft, shot: slotShot(index) }))
        .filter(({ draft, shot }) => {
          if (!shot) return false;
          if (kind === "keyframe") return Boolean(draft.imagePrompt.trim());
          return (
            Boolean(draft.videoPrompt.trim()) &&
            shot.current_keyframe?.status === "completed"
          );
        })
        .map(({ shot }) => shot!.id);
      if (ids.length === 0) {
        return {
          batch_id: "",
          jobs: [],
          preflight_failures: [],
        } as BatchResponse;
      }
      return kind === "keyframe"
        ? api.shots.keyframes.batch(manualChapterId, {
            ids,
            only_missing: true,
            provider_id: imageProviderId || undefined,
          })
        : api.shots.videos.batch(manualChapterId, {
            ids,
            only_missing: true,
            provider_id: videoProviderId || undefined,
          });
    },
    onSuccess: async (result, kind) => {
      await invalidateShots();
      startPolling();
      const noun = kind === "keyframe" ? "分镜图" : "分镜视频";
      const failures = result.preflight_failures ?? [];
      if (!result.jobs.length && !failures.length) {
        toast.info(`所有${noun}都已生成`);
        return;
      }
      if (failures.length) {
        const description = failures
          .map(({ target_id, error }) => {
            const shot = manualShots.find((item) => item.id === target_id);
            return `分镜 ${shot?.shot_no ?? target_id}：${error}`;
          })
          .join("；");
        toast.warning(
          `已入队 ${result.jobs.length} 个${noun}任务，跳过 ${failures.length} 项`,
          { description },
        );
        return;
      }
      toast.success(`已入队 ${result.jobs.length} 个${noun}任务`);
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const handleCompose = async () => {
    if (!manualChapterId) return;
    setComposing(true);
    try {
      await saveFilledDrafts();
      const result = await api.composition.composeChapter(manualChapterId, {
        resolution,
        include_audio: false,
        burn_subtitles: false,
        crossfade: true,
        crossfade_seconds: 0.5,
      });
      setComposition(result);
      toast.success("合成任务已创建，正在合成成片");
      if (result.job_id) {
        if (composePollRef.current) clearInterval(composePollRef.current);
        composePollRef.current = setInterval(async () => {
          try {
            const job = await api.jobs.get(result.job_id!);
            setComposition((prev) =>
              prev
                ? { ...prev, status: job.status, job_id: result.job_id }
                : prev,
            );
            if (
              ["completed", "failed", "cancelled", "interrupted"].includes(
                job.status,
              )
            ) {
              if (composePollRef.current) clearInterval(composePollRef.current);
              composePollRef.current = null;
              const record = await api.composition.getChapterComposition(
                manualChapterId,
              );
              if (record) setComposition(record);
            }
          } catch {
            if (composePollRef.current) clearInterval(composePollRef.current);
            composePollRef.current = null;
          }
        }, 3000);
      }
    } catch (error) {
      console.error(error);
      toast.error(`合成失败：${error instanceof Error ? error.message : "未知错误"}`);
    } finally {
      setComposing(false);
    }
  };

  const canAddAsset = Boolean(assetName.trim() && assetPrompt.trim());
  const filledImageSlots = slotDrafts.filter((draft) =>
    draft.imagePrompt.trim(),
  ).length;
  const filledVideoSlots = slotDrafts.filter(
    (draft, index) =>
      Boolean(draft.videoPrompt.trim()) &&
      slotShot(index)?.current_keyframe?.status === "completed",
  ).length;
  const hasInvalidFilledDuration = slotDrafts.some(
    (draft) =>
      (draft.imagePrompt.trim() || draft.videoPrompt.trim()) &&
      !tianyueFixedVideo &&
      draft.duration < 5,
  );

  if (workspaceError) {
    return (
      <EmptyState
        title="自由制作项目创建失败"
        description={workspaceError}
      />
    );
  }
  if (!manualChapterId) {
    return <LoadingState label="正在准备自由制作工作区…" />;
  }

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Free creation"
        title="自由制作"
        description="自由制作项目与左侧常规项目相互独立，只在本页展示；固定 8 个分镜位，填写需要制作的分镜即可，未填写的空位自动忽略。"
        actions={
          <>
            <div className="w-64">
              <Select
                aria-label="选择自由制作项目"
                value={manualProjectId}
                onChange={(event) => void switchProject(event.target.value)}
              >
                {freeProjects.map((project) => (
                  <option key={project.id} value={project.id}>
                    {project.name}
                  </option>
                ))}
              </Select>
            </div>
            <Button
              variant="primary"
              onClick={() => setNewProjectOpen(true)}
            >
              <Plus className="h-4 w-4" />
              新建
            </Button>
            <Button
              variant="danger"
              busy={deleteProjectMutation.isPending}
              onClick={() => {
                if (
                  window.confirm(
                    `确认删除自由制作项目“${manualProject?.name ?? ""}”及其全部本地媒体吗？此操作不可恢复。`,
                  )
                ) {
                  deleteProjectMutation.mutate();
                }
              }}
            >
              <Trash2 className="h-4 w-4" />
              删除当前项目
            </Button>
          </>
        }
      />

      <Card className="p-5">
        <div className="mb-4">
          <h2 className="text-lg font-bold">模型配置</h2>
          <p className="mt-1 text-sm text-muted">
            统一控制本页所有生成：素材参考图、分镜图和分镜视频都会使用这里选择的模型。
          </p>
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <Label htmlFor="free-image-provider">图片模型</Label>
            <Select
              id="free-image-provider"
              value={imageProviderId}
              onChange={(event) => changeImageProvider(event.target.value)}
            >
              <option value="">项目默认</option>
              {imageProviders.map((provider) => (
                <option key={provider.id} value={provider.id}>
                  {provider.name}（{provider.model}）
                </option>
              ))}
            </Select>
          </div>
          <div>
            <Label htmlFor="free-video-provider">视频模型</Label>
            <Select
              id="free-video-provider"
              value={videoProviderId}
              onChange={(event) => changeVideoProvider(event.target.value)}
            >
              <option value="">项目默认</option>
              {videoProviders.map((provider) => (
                <option key={provider.id} value={provider.id}>
                  {provider.name}（{provider.model}）
                </option>
              ))}
            </Select>
          </div>
        </div>
      </Card>

      <Card className="p-5">
        <div className="mb-4 flex flex-col justify-between gap-3 sm:flex-row sm:items-center">
          <div>
            <h2 className="text-lg font-bold">自定义素材</h2>
            <p className="mt-1 text-sm text-muted">
              为素材填写名称和生图提示词，可直接生成参考图，并在分镜中选择使用。
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <Button
              variant="primary"
              busy={assetBatchMutation.isPending}
              onClick={() => assetBatchMutation.mutate()}
            >
              <WandSparkles className="h-4 w-4" />
              一键生成素材图
            </Button>
            <Tabs
              value={assetType}
              onChange={(value) => {
                setAssetType(value as AssetType);
                setAssetName("");
                setAssetPrompt("");
              }}
              items={assetTypes.map((type) => ({
                value: type,
                label: assetLabels[type],
                count: assets.filter(
                  (asset) => asset.asset_type === type,
                ).length,
              }))}
            />
          </div>
        </div>

        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,2fr)_auto]">
          <div>
            <Label htmlFor="free-asset-name">{assetLabels[assetType]}名称</Label>
            <Input
              id="free-asset-name"
              value={assetName}
              onChange={(event) => setAssetName(event.target.value)}
              placeholder={`例如：林小满 / 山间道观 / 青铜匕首`}
            />
          </div>
          <div>
            <Label htmlFor="free-asset-prompt">生图提示词</Label>
            <Textarea
              id="free-asset-prompt"
              rows={2}
              value={assetPrompt}
              onChange={(event) => setAssetPrompt(event.target.value)}
              placeholder="描述外貌、服装、空间布局、材质等用于生图的细节…"
            />
          </div>
          <div className="flex items-end">
            <Button
              variant="primary"
              busy={createAssetMutation.isPending}
              disabled={!canAddAsset}
              onClick={() => createAssetMutation.mutate()}
            >
              <Plus className="h-4 w-4" />
              添加{assetLabels[assetType]}
            </Button>
          </div>
        </div>

        {assetsQuery.isLoading ? (
          <LoadingState label="正在读取素材…" />
        ) : currentAssets.length === 0 ? (
          <p className="mt-4 text-sm text-muted">
            还没有{assetLabels[assetType]}，填写名称和生图提示词后即可添加。
          </p>
        ) : (
          <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {currentAssets.map((asset) => {
              const generating = generatingAssetIds.has(asset.id);
              return (
                <div
                  key={asset.id}
                  className="rounded-md border border-line bg-white/60 p-3"
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <p className="truncate text-sm font-bold">{asset.name}</p>
                      <p className="mt-1 line-clamp-3 whitespace-pre-wrap text-xs leading-5 text-muted">
                        {asset.prompt || "未填写生图提示词"}
                      </p>
                    </div>
                    <Button
                      variant="ghost"
                      size="icon"
                      aria-label={`删除${assetLabels[assetType]} ${asset.name}`}
                      busy={
                        removeAssetMutation.isPending &&
                        removeAssetMutation.variables === asset.id
                      }
                      onClick={() => {
                        if (
                          window.confirm(
                            `确认删除${assetLabels[assetType]}「${asset.name}」吗？`,
                          )
                        ) {
                          removeAssetMutation.mutate(asset.id);
                        }
                      }}
                    >
                      <Trash2 className="h-4 w-4 text-[#a3332a]" />
                    </Button>
                  </div>

                  {asset.current_image && mediaUrl(asset.current_image) ? (
                    <ImagePreview
                      src={mediaUrl(asset.current_image)}
                      alt={`${asset.name}参考图`}
                      buttonClassName="mt-3 h-24 w-full"
                      imageClassName="h-24 w-full object-cover"
                    />
                  ) : (
                    <div className="mt-3 flex h-24 w-full items-center justify-center rounded border border-line bg-canvas">
                      <ImageOff className="h-5 w-5 text-muted" />
                    </div>
                  )}

                  <div className="mt-3 flex items-center justify-between gap-2">
                    {asset.prompt.trim() ? (
                      <Button
                        size="sm"
                        busy={generating}
                        disabled={generating}
                        onClick={() =>
                          generateAssetImageMutation.mutate(asset.id)
                        }
                      >
                        <WandSparkles className="h-4 w-4" />
                        {generating ? "生成中" : "用提示词生图"}
                      </Button>
                    ) : (
                      <span className="text-xs text-muted">缺少生图提示词</span>
                    )}
                    {asset.current_image ? (
                      <StatusBadge
                        status={asset.current_image.status}
                        className="text-xs"
                      />
                    ) : null}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </Card>

      <Card className="p-5">
        <div className="mb-4 flex flex-col justify-between gap-3 lg:flex-row lg:items-center">
          <div>
            <h2 className="text-lg font-bold">分镜编排</h2>
            <p className="mt-1 text-sm text-muted">
              固定 8 个分镜位：填写提示词并保存后即可生成；未填写的空位不会参与生成和成片合成。
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button
              variant="primary"
              busy={
                mediaBatchMutation.isPending &&
                mediaBatchMutation.variables === "keyframe"
              }
              disabled={filledImageSlots === 0 || hasInvalidFilledDuration}
              onClick={() => mediaBatchMutation.mutate("keyframe")}
            >
              一键生成分镜图
            </Button>
            <Button
              busy={
                mediaBatchMutation.isPending &&
                mediaBatchMutation.variables === "video"
              }
              disabled={filledVideoSlots === 0 || hasInvalidFilledDuration}
              onClick={() => mediaBatchMutation.mutate("video")}
            >
              一键生成分镜视频
            </Button>
          </div>
        </div>

        <div className="grid grid-cols-1 gap-4">
          {slotDrafts.map((draft, index) => {
            const shot = slotShot(index);
            const keyframe = shot?.current_keyframe;
            const video = shot?.current_video;
            const keyframeRunning = RUNNING.has(keyframe?.status ?? "");
            const videoRunning = RUNNING.has(video?.status ?? "");
            const keyframeReady = keyframe?.status === "completed";
            const durationValid = tianyueFixedVideo || draft.duration >= 5;
            const slotBoundAssets = assets.filter((asset) =>
              [draft.sceneAssetId, ...draft.characterIds, ...draft.propIds].includes(
                asset.id,
              ),
            );
            const slotMissingImages = slotBoundAssets
              .filter((asset) => asset.current_image?.status !== "completed")
              .map((asset) => asset.name);
            const missingScene =
              (draft.characterIds.length > 0 || draft.propIds.length > 0) &&
              !draft.sceneAssetId;
            return (
              <div
                key={index}
                className="rounded-md border border-line bg-white/70 p-4"
              >
                <div className="mb-3 flex flex-wrap items-center gap-2">
                  <span className="text-sm font-bold">分镜 {index + 1}</span>
                  <StatusBadge
                    status={keyframe?.status || "empty"}
                    className="text-xs"
                  />
                  <span className="text-xs text-muted">分镜图</span>
                  <StatusBadge
                    status={video?.status || "empty"}
                    className="text-xs"
                  />
                  <span className="text-xs text-muted">视频</span>
                  <div className="ml-auto flex flex-wrap gap-2">
                    <Button
                      size="sm"
                      busy={savingSlotIndex === index}
                      disabled={!durationValid}
                      onClick={() => void saveSlot(index)}
                    >
                      保存
                    </Button>
                    <Button
                      variant="primary"
                      size="sm"
                      disabled={
                        !durationValid || !draft.imagePrompt.trim() || keyframeRunning
                      }
                      onClick={() => void generateSlotMedia(index, "keyframe")}
                    >
                      生成分镜图
                    </Button>
                    <Button
                      size="sm"
                      disabled={
                        !durationValid ||
                        !draft.videoPrompt.trim() ||
                        !keyframeReady ||
                        videoRunning
                      }
                      onClick={() => void generateSlotMedia(index, "video")}
                    >
                      生成视频
                    </Button>
                  </div>
                </div>

                <div className="flex flex-col gap-4 lg:flex-row lg:items-start">
                  <div className="flex shrink-0 gap-4">
                    <div className="flex aspect-video w-48 items-center justify-center overflow-hidden rounded-md border border-line bg-canvas">
                      {keyframeReady && keyframe ? (
                        <ImagePreview
                          src={mediaUrl(keyframe)}
                          alt={`分镜 ${index + 1} 分镜图`}
                          buttonClassName="h-full w-full"
                          imageClassName="h-full w-full object-contain"
                        />
                      ) : (
                        <ImageOff className="h-6 w-6 text-muted" />
                      )}
                    </div>
                    <div className="flex aspect-video w-48 items-center justify-center overflow-hidden rounded-md border border-line bg-canvas">
                      {video?.status === "completed" && video ? (
                        <video
                          src={mediaUrl(video)}
                          controls
                          className="h-full w-full object-contain"
                          preload="metadata"
                        />
                      ) : (
                        <Film className="h-6 w-6 text-muted" />
                      )}
                    </div>
                  </div>

                  <div className="min-w-0 flex-1 space-y-3">
                    <div className="grid gap-3 sm:grid-cols-2">
                      <div>
                        <Label htmlFor={`slot-${index}-scene`}>场景</Label>
                        <Select
                          id={`slot-${index}-scene`}
                          value={draft.sceneAssetId}
                          onChange={(event) =>
                            updateSlotDraft(index, {
                              sceneAssetId: event.target.value,
                            })
                          }
                        >
                          <option value="">不使用场景素材</option>
                          {assets
                            .filter((asset) => asset.asset_type === "scene")
                            .map((asset) => (
                              <option key={asset.id} value={asset.id}>
                                {asset.name}
                              </option>
                            ))}
                        </Select>
                      </div>
                      {tianyueFixedVideo ? (
                        <div>
                          <Label>视频时长</Label>
                          <div className="text-sm leading-6 text-muted">
                            15 秒（模型固定）
                          </div>
                        </div>
                      ) : (
                        <div>
                          <Label htmlFor={`slot-${index}-duration`}>时长（秒）</Label>
                          <Input
                            id={`slot-${index}-duration`}
                            type="number"
                            min={5}
                            step={1}
                            value={draft.duration}
                            onChange={(event) =>
                              updateSlotDraft(index, {
                                duration: Number(event.target.value),
                              })
                            }
                          />
                        </div>
                      )}
                    </div>
                    <div className="grid gap-3 sm:grid-cols-2">
                      <div>
                        <Label>出场角色</Label>
                        <div className="max-h-20 space-y-1 overflow-y-auto rounded-md border border-line bg-white p-2">
                          {assets
                            .filter((asset) => asset.asset_type === "character")
                            .map((asset) => (
                              <label
                                key={asset.id}
                                className="flex items-center gap-2 text-xs"
                              >
                                <input
                                  type="checkbox"
                                  className="accent-vermilion"
                                  checked={draft.characterIds.includes(asset.id)}
                                  onChange={(event) =>
                                    updateSlotDraft(index, {
                                      characterIds: event.target.checked
                                        ? [...draft.characterIds, asset.id]
                                        : draft.characterIds.filter(
                                            (id) => id !== asset.id,
                                          ),
                                    })
                                  }
                                />
                                {asset.name}
                              </label>
                            ))}
                          {!assets.some(
                            (asset) => asset.asset_type === "character",
                          ) ? (
                            <p className="text-xs text-muted">还没有角色素材。</p>
                          ) : null}
                        </div>
                      </div>
                      <div>
                        <Label>使用物品</Label>
                        <div className="max-h-20 space-y-1 overflow-y-auto rounded-md border border-line bg-white p-2">
                          {assets
                            .filter((asset) => asset.asset_type === "prop")
                            .map((asset) => (
                              <label
                                key={asset.id}
                                className="flex items-center gap-2 text-xs"
                              >
                                <input
                                  type="checkbox"
                                  className="accent-vermilion"
                                  checked={draft.propIds.includes(asset.id)}
                                  onChange={(event) =>
                                    updateSlotDraft(index, {
                                      propIds: event.target.checked
                                        ? [...draft.propIds, asset.id]
                                        : draft.propIds.filter(
                                            (id) => id !== asset.id,
                                          ),
                                    })
                                  }
                                />
                                {asset.name}
                              </label>
                            ))}
                          {!assets.some(
                            (asset) => asset.asset_type === "prop",
                          ) ? (
                            <p className="text-xs text-muted">还没有物品素材。</p>
                          ) : null}
                        </div>
                      </div>
                    </div>
                    <div>
                      <Label htmlFor={`slot-${index}-image-prompt`}>
                        分镜图提示词
                      </Label>
                      <Textarea
                        id={`slot-${index}-image-prompt`}
                        rows={4}
                        value={draft.imagePrompt}
                        onChange={(event) =>
                          updateSlotDraft(index, {
                            imagePrompt: event.target.value,
                          })
                        }
                        placeholder="描述这一镜的分镜图内容、构图、人物与场景…"
                      />
                    </div>
                    <div>
                      <Label htmlFor={`slot-${index}-video-prompt`}>
                        分镜视频提示词
                      </Label>
                      <Textarea
                        id={`slot-${index}-video-prompt`}
                        rows={4}
                        value={draft.videoPrompt}
                        onChange={(event) =>
                          updateSlotDraft(index, {
                            videoPrompt: event.target.value,
                          })
                        }
                        placeholder="描述这一镜视频要发生的动作、时间轴与镜头…"
                      />
                    </div>
                    {missingScene ? (
                      <p className="text-xs leading-5 text-amber-700">
                        已选角色或物品时，请同时绑定场景素材，否则无法生成分镜图。
                      </p>
                    ) : null}
                    {slotMissingImages.length > 0 ? (
                      <p className="text-xs leading-5 text-amber-700">
                        以下素材还没有完成参考图：{slotMissingImages.join("、")}
                        ；请先在「自定义素材」中点击「用提示词生图」。
                      </p>
                    ) : null}
                    {keyframe?.error || video?.error ? (
                      <p className="text-xs leading-5 text-red-600">
                        {keyframe?.error || video?.error}
                      </p>
                    ) : null}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </Card>

      <Card className="p-5">
        <h2 className="mb-4 text-lg font-bold">合成成片</h2>
        <div className="flex flex-wrap items-end gap-4">
          <div>
            <Label htmlFor="free-compose-resolution">分辨率</Label>
            <Select
              id="free-compose-resolution"
              value={resolution}
              onChange={(event) =>
                changeResolution(event.target.value as "720p" | "1080p" | "4k")
              }
            >
              <option value="720p">720p</option>
              <option value="1080p">1080p</option>
              <option value="4k">4K</option>
            </Select>
          </div>
          <Button
            variant="primary"
            busy={composing}
            onClick={handleCompose}
          >
            开始合成
          </Button>
        </div>

        {composition ? (
          <div className="mt-4 space-y-2">
            <div className="flex items-center gap-2">
              <span className="text-sm font-medium">状态:</span>
              <StatusBadge status={composition.status} className="text-xs" />
            </div>
            {composition.error ? (
              <p className="text-sm text-red-600">错误: {composition.error}</p>
            ) : null}
            {composition.file_path && composition.status === "completed" && manualProjectId ? (
              <div className="mt-3">
                <video
                  controls
                  src={`/media/${manualProjectId}/${composition.file_path}`}
                  className="w-full max-w-2xl rounded-md border border-line"
                />
                <a
                  href={`/media/${manualProjectId}/${composition.file_path}`}
                  download
                  className="mt-2 inline-block rounded-md bg-green-500 px-4 py-2 text-sm text-white hover:bg-green-600"
                >
                  下载成片
                </a>
              </div>
            ) : null}
          </div>
        ) : null}
      </Card>

      <Modal
        open={newProjectOpen}
        onClose={() => setNewProjectOpen(false)}
        title="新建自由制作项目"
        description="每个项目独立保存角色、场景、物品、分镜与成片；自由制作项目不会出现在左侧常规项目列表中。"
        footer={
          <>
            <Button onClick={() => setNewProjectOpen(false)}>取消</Button>
            <Button
              variant="primary"
              busy={createProjectMutation.isPending}
              onClick={() => createProjectMutation.mutate()}
            >
              创建项目
            </Button>
          </>
        }
      >
        <div>
          <Label htmlFor="new-free-project-name">项目名称</Label>
          <Input
            id="new-free-project-name"
            value={newProjectName}
            maxLength={120}
            placeholder={suggestedProjectName}
            onChange={(event) => setNewProjectName(event.target.value)}
          />
          <p className="mt-2 text-xs leading-5 text-muted">
            留空将使用“{suggestedProjectName}”。
          </p>
        </div>
      </Modal>
    </div>
  );
}
