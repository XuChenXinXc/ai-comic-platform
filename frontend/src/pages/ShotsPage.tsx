import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Archive,
  ChevronDown,
  ChevronUp,
  Clapperboard,
  Copy,
  Film,
  GripVertical,
  ImageOff,
  Images,
  Link2,
  Merge,
  Pencil,
  Plus,
  RefreshCw,
  Scissors,
  Trash2,
  TriangleAlert,
  WandSparkles,
  Wrench,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useLocation } from "react-router";
import { toast } from "sonner";
import { api, mediaUrl, queryKeys } from "../api/client";
import { ImagePreview } from "../components/image-preview";
import { ContinuityCheck } from "../components/continuity-check";
import { JsonEditor } from "../components/json-editor";
import { ChapterRequired } from "../components/chapter-required";
import { ChapterSelector } from "../components/chapter-selector";
import { ProjectRequired } from "../components/project-required";
import { ShotMediaCard } from "../components/shot-media-card";
import {
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
  StaleNotice,
  StatusBadge,
} from "../components/status";
import {
  Badge,
  Button,
  Card,
  Input,
  Label,
  Modal,
  Select,
  Textarea,
} from "../components/ui";
import { useChapter } from "../hooks/use-chapter";
import { useProject } from "../hooks/use-project";
import { formatError, makeDownloadName, parseJson } from "../lib/utils";
import type {
  BatchResponse,
  DialogueLine,
  ReferenceBinding,
  Shot,
  ShotExpression,
  ShotPosition,
  ShotTransitionFromPrev,
} from "../types/api";

const transitionLabels: Record<ShotTransitionFromPrev, string> = {
  continuous: "连续承接",
  elliptical: "省略承接",
  same_scene_cut: "同场切镜",
  scene_change: "场景切换",
};

function blankShot(chapterId: string): Partial<Shot> {
  return {
    project_id: "",
    chapter_id: chapterId,
    shot_no: 1,
    shot_order: 1,
    source_type: "screenplay",
    source_ref: "",
    scene_asset_id: null,
    character_ids_json: [],
    prop_ids_json: [],
    positions_json: [],
    start_state: "",
    action: "",
    expressions_json: [],
    dialogue_json: [],
    narration: "",
    shot_type: "中景",
    camera_angle: "平视",
    camera_move: "固定",
    duration_sec: 0,
    end_state: "",
    next_hook: "",
    transition_from_prev: "scene_change",
    frame_description: "",
    image_prompt: "",
    video_prompt: "",
    negative_prompt: "",
    reference_bindings_json: [],
    status: "draft",
  };
}

function idList(value: string[] | string | undefined) {
  return parseJson<string[]>(value, []);
}

function jsonList<T extends Record<string, unknown>>(
  value: T[] | string | undefined,
) {
  return parseJson<T[]>(value, []);
}

function firstText(item: Record<string, unknown>, keys: string[]) {
  for (const key of keys) {
    const value = String(item[key] ?? "").trim();
    if (value) return value;
  }
  return "";
}

function formatDialogue(item: DialogueLine) {
  const speaker = firstText(item, ["speaker", "character", "name"]);
  const text = firstText(item, ["text", "content", "line"]);
  if (!text) return "";
  return speaker ? `${speaker}：${text}` : text;
}

function formatPosition(item: ShotPosition) {
  const character = firstText(item, [
    "character",
    "name",
    "speaker",
    "character_name",
  ]);
  const details = [
    firstText(item, ["position", "location", "side", "placement"]),
    firstText(item, ["facing", "direction"]),
    firstText(item, ["gaze", "look", "eye_line"]),
    firstText(item, ["action_direction"]),
  ].filter(Boolean);
  if (!details.length) return "";
  return character ? `${character}：${details.join(" · ")}` : details.join(" · ");
}

function formatExpression(item: ShotExpression) {
  const character = firstText(item, [
    "character",
    "name",
    "speaker",
    "character_name",
  ]);
  const expression = firstText(item, ["expression", "emotion", "mood"]);
  if (!expression) return "";
  return character ? `${character}：${expression}` : expression;
}

export function ShotsPage() {
  const queryClient = useQueryClient();
  const location = useLocation();
  const { activeProject, activeProjectId } = useProject();
  const { activeChapterId, activeChapter } = useChapter();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [continuityOpen, setContinuityOpen] = useState(false);
  const [scope, setScope] = useState<"all" | "scene">("all");
  const [sceneNo, setSceneNo] = useState("");
  const [exporting, setExporting] = useState(false);
  const [editing, setEditing] = useState<Shot | "new" | null>(null);
  const [form, setForm] = useState<Partial<Shot>>(
    blankShot(activeChapterId),
  );
  const [jsonValidity, setJsonValidity] = useState<Record<string, boolean>>({});
  const [draggedId, setDraggedId] = useState("");

  const query = useQuery({
    queryKey: queryKeys.shots(activeChapterId),
    queryFn: () => api.shots.list(activeChapterId),
    enabled: Boolean(activeChapterId),
  });
  const assetsQuery = useQuery({
    queryKey: queryKeys.assets(activeProjectId),
    queryFn: () => api.assets.list(activeProjectId),
    enabled: Boolean(activeProjectId),
  });
  const shots = useMemo(() => query.data || [], [query.data]);
  const assets = useMemo(() => assetsQuery.data || [], [assetsQuery.data]);
  const assetMap = useMemo(
    () => new Map(assets.map((asset) => [asset.id, asset])),
    [assets],
  );
  const hasExecutablePrompt = (shot: Shot, kind: "image" | "video") =>
    kind === "image"
      ? Boolean(shot.image_prompt_override?.trim() || shot.image_prompt.trim())
      : Boolean(shot.video_prompt_override?.trim() || shot.video_prompt.trim());
  const batchIds = useMemo(() => {
    if (selected.size) return [...selected];
    if (scope !== "scene" || !sceneNo) return undefined;
    return shots
      .filter((shot) => shot.scene_no === Number(sceneNo))
      .map((shot) => shot.id);
  }, [sceneNo, scope, selected, shots]);
  const batchDisabled =
    !shots.length ||
    (!selected.size &&
      scope === "scene" &&
      (!sceneNo || batchIds?.length === 0));

  useEffect(() => {
    setSelected(new Set());
    setSceneNo("");
  }, [activeChapterId]);

  useEffect(() => {
    const stage = new URLSearchParams(location.search).get("stage");
    if (!shots.length || !["keyframes", "videos"].includes(stage || "")) return;
    document
      .getElementById(`stage-${stage}`)
      ?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [location.search, shots.length]);

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: queryKeys.shots(activeChapterId) });

  const showBatchFeedback = (
    result: BatchResponse,
    noun: "关键帧" | "视频",
  ) => {
    const failures = result.preflight_failures ?? [];
    if (failures.length) {
      const description = failures
        .map(({ target_id, error }) => {
          const shot = shots.find((item) => item.id === target_id);
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
  };

  const taskMutation = useMutation({
    mutationFn: async ({
      action,
      shot,
    }: {
      action: "extract" | "extractScene";
      shot?: Shot;
    }) => {
      if (action === "extract") {
        return api.shots.extract(activeChapterId, {
          scope: "all",
        });
      }
      if (action === "extractScene" && shot) {
        return api.shots.extract(activeChapterId, {
          scope: "scene",
          scene_no: shot.scene_no || undefined,
        });
      }
      throw new Error("缺少待提取的分镜");
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: queryKeys.jobs(activeProjectId),
      });
      toast.success("分镜任务已进入队列");
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const handleContinuityCheck = () => {
    if (
      localStorage.getItem(`continuity-check-run-${activeProjectId}`) &&
      !window.confirm(
        "再次执行剧情连贯审校会重新生成并覆盖修正内容，确定继续吗？",
      )
    ) {
      return;
    }
    continuityFixMutation.mutate();
  };

  const mediaBatchMutation = useMutation({
    mutationFn: (kind: "keyframe" | "video") =>
      kind === "keyframe"
        ? api.shots.keyframes.batch(activeChapterId, { ids: batchIds })
        : api.shots.videos.batch(activeChapterId, { ids: batchIds }),
    onSuccess: async (result, kind) => {
      await queryClient.invalidateQueries({
        queryKey: queryKeys.jobs(activeProjectId),
      });
      showBatchFeedback(result, kind === "keyframe" ? "关键帧" : "视频");
    },
    onError: (error) => toast.error(formatError(error)),
  });

  // 剧情连贯审校：把相邻分镜的起止状态与旁白交给文字模型逐对检查，
  // 修正直接写回源字段。改完需要重新生成提示词才会生效。
  const continuityFixMutation = useMutation({
    mutationFn: () =>
      api.shots.checkContinuity(activeProjectId, {
        ids: batchIds,
        apply_fixes: true,
        insert_transitions: false,
      }),
    onSuccess: async () => {
      localStorage.setItem(`continuity-check-run-${activeProjectId}`, "1");
      await queryClient.invalidateQueries({
        queryKey: queryKeys.jobs(activeProjectId),
      });
      toast.success("剧情连贯审校已入队", {
        description: "完成后请重新生成提示词，修正才会写入提示词",
      });
    },
    onError: (error) => toast.error(formatError(error)),
  });

  // 重新生成提示词：把当前源字段重新编译为图片/视频提示词。
  // 审校修正写回源字段后必须执行这一步，否则旧提示词仍会被使用。
  const promptBatchMutation = useMutation({
    mutationFn: () =>
      api.shots.batchPrompts(activeChapterId, {
        ids: batchIds,
        prompt_types: ["image", "video"],
      }),
    onSuccess: async (result) => {
      await queryClient.invalidateQueries({
        queryKey: queryKeys.jobs(activeProjectId),
      });
      toast.success(`已入队 ${result.jobs.length} 个提示词任务`, {
        description: "完成后请重新生成关键帧与视频，修正才会进入画面",
      });
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const handlePromptBatch = () => {
    if (
      !window.confirm(
        "将重新生成提示词并覆盖现有提示词（含手工覆盖的版本），确定继续吗？",
      )
    ) {
      return;
    }
    promptBatchMutation.mutate();
  };

  // 分镜质量报告：提取阶段的警告不阻断保存，这里主动扫描存量分镜。
  const qualityQuery = useQuery({
    queryKey: ["shot-quality", activeProjectId],
    queryFn: () => api.shots.qualityReport(activeProjectId),
    enabled: Boolean(activeChapterId) && shots.length > 0,
  });
  const qualityIssues = qualityQuery.data?.items ?? [];

  const qualityFixMutation = useMutation({
    mutationFn: async () => {
      // 入队前先刷新质量报告，避免用旧数量误导“本次修复几个”
      await qualityQuery.refetch();
      return api.shots.fixQuality(activeProjectId, {});
    },
    onSuccess: async (job) => {
      await queryClient.invalidateQueries({
        queryKey: ["shot-quality", activeProjectId],
      });
      await queryClient.invalidateQueries({
        queryKey: queryKeys.jobs(activeProjectId),
      });
      toast.success(`已入队 ${job.progress_total ?? qualityIssues.length} 个分镜的修复任务`, {
        description: "完成后会自动调整时长与时间轴，可在分镜列表查看结果",
      });
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const exportZip = async () => {
    if (!activeProject) return;
    setExporting(true);
    try {
      await api.projects.exportVideos(
        activeProject.id,
        makeDownloadName(`${activeProject.name}-分镜视频`, "zip"),
      );
      toast.success("ZIP 导出已开始下载");
    } catch (error) {
      toast.error(formatError(error));
    } finally {
      setExporting(false);
    }
  };

  const saveMutation = useMutation({
    mutationFn: () =>
      editing === "new"
        ? api.shots.create(activeChapterId, form)
        : api.shots.update((editing as Shot).id, form),
    onSuccess: async () => {
      await invalidate();
      toast.success(editing === "new" ? "分镜已添加" : "分镜已保存");
      setEditing(null);
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const structureMutation = useMutation<
    unknown,
    Error,
    { action: "split" | "copy" | "delete"; id: string }
  >({
    mutationFn: async ({
      action,
      id,
    }: {
      action: "split" | "copy" | "delete";
      id: string;
    }) => {
      if (action === "split") return await api.shots.split(id);
      if (action === "copy") return await api.shots.copy(id);
      return await api.shots.remove(id);
    },
    onSuccess: async (_, variables) => {
      await invalidate();
      toast.success(
        variables.action === "split"
          ? "分镜已拆分"
          : variables.action === "copy"
            ? "分镜已复制"
            : "分镜已删除",
      );
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const mergeMutation = useMutation({
    mutationFn: () => api.shots.merge(activeChapterId, [...selected]),
    onSuccess: async () => {
      setSelected(new Set());
      await invalidate();
      toast.success("分镜已合并");
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const reorderMutation = useMutation({
    mutationFn: (ids: string[]) => api.shots.reorder(activeChapterId, ids),
    onSuccess: async () => {
      await invalidate();
      toast.success("分镜顺序已更新");
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const reorder = (fromId: string, toId: string) => {
    if (!fromId || fromId === toId) return;
    const ids = shots.map((shot) => shot.id);
    const from = ids.indexOf(fromId);
    const to = ids.indexOf(toId);
    if (from < 0 || to < 0) return;
    const [moved] = ids.splice(from, 1);
    ids.splice(to, 0, moved);
    reorderMutation.mutate(ids);
  };

  const move = (index: number, direction: -1 | 1) => {
    const target = index + direction;
    if (target < 0 || target >= shots.length) return;
    reorder(shots[index].id, shots[target].id);
  };

  const openNew = () => {
    setForm({
      ...blankShot(activeProjectId),
      shot_no: shots.length + 1,
      shot_order: shots.length + 1,
    });
    setJsonValidity({});
    setEditing("new");
  };
  const openEdit = (shot: Shot) => {
    setForm({
      ...shot,
      character_ids_json: idList(shot.character_ids_json),
      prop_ids_json: idList(shot.prop_ids_json),
      positions_json: jsonList(shot.positions_json),
      expressions_json: jsonList(shot.expressions_json),
      dialogue_json: jsonList(shot.dialogue_json),
      reference_bindings_json: parseJson<ReferenceBinding[]>(
        shot.reference_bindings_json,
        [],
      ),
    });
    setJsonValidity({});
    setEditing(shot);
  };
  const setJsonSectionValidity = (section: string, valid: boolean) => {
    setJsonValidity((current) => ({ ...current, [section]: valid }));
  };
  const jsonValid = Object.values(jsonValidity).every(Boolean);

  return (
    <ProjectRequired>
      <ChapterRequired>
      <div>
        <PageHeader
          eyebrow="Step 05 / Storyboard"
          title="分镜视频"
          description="提取时自动补齐 V2 关键帧与视频提示词，并在同一条流水线中完成关键帧和单镜视频生成。"
          actions={
            <>
              <ChapterSelector />
              <Button onClick={openNew}>
                <Plus className="h-4 w-4" />
                手工新增
              </Button>
              <Button
                busy={
                  taskMutation.isPending &&
                  taskMutation.variables?.action === "extract"
                }
                onClick={() => taskMutation.mutate({ action: "extract" })}
              >
                <WandSparkles className="h-4 w-4" />
                {shots.length ? "重新提取全部" : "提取分镜"}
              </Button>
              <Button
                variant="secondary"
                busy={continuityFixMutation.isPending}
                disabled={!shots.length}
                title="把相邻分镜的起止状态与旁白交给文字模型逐对审校并自动修正源字段；完成后需重新生成提示词才会生效"
                onClick={handleContinuityCheck}
              >
                <Link2 className="h-4 w-4" />
                剧情连贯审校
              </Button>
              <Button
                variant="secondary"
                busy={promptBatchMutation.isPending}
                disabled={!shots.length}
                title="把所选（或全部）分镜的最新字段重新编译为图片与视频提示词；会覆盖现有提示词与手工覆盖"
                onClick={handlePromptBatch}
              >
                <RefreshCw className="h-4 w-4" />
                {selected.size
                  ? `重新生成所选提示词（${selected.size}）`
                  : scope === "scene"
                    ? "重新生成当前场次提示词"
                    : "重新生成全部提示词"}
              </Button>
              <Button
                variant="secondary"
                onClick={() => setContinuityOpen(true)}
              >
                <Clapperboard className="h-4 w-4" />
                连贯性检查
              </Button>
              <Button busy={exporting} onClick={() => void exportZip()}>
                <Archive className="h-4 w-4" />
                导出当前版本 ZIP
              </Button>
            </>
          }
        />

        {qualityIssues.length > 0 && (
          <Card className="mb-5 border-amber-300/60 bg-amber-50/60 p-4 dark:border-amber-500/30 dark:bg-amber-500/5">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 text-sm font-medium text-amber-900 dark:text-amber-200">
                  <TriangleAlert className="h-4 w-4 shrink-0" />
                  {qualityIssues.length} 个分镜存在质量问题
                </div>
                <ul className="mt-2 space-y-1 text-xs text-amber-800/90 dark:text-amber-200/80">
                  {qualityIssues.slice(0, 4).map((issue) => (
                    <li key={issue.shot_id}>
                      <span className="font-medium">
                        {issue.chapter_no ? `第${issue.chapter_no}章 ` : ""}
                        镜 {issue.shot_no}
                      </span>
                      （{issue.duration_sec}秒）：{issue.warnings[0]}
                    </li>
                  ))}
                  {qualityIssues.length > 4 && (
                    <li className="opacity-70">
                      还有 {qualityIssues.length - 4} 个分镜存在问题…
                    </li>
                  )}
                </ul>
              </div>
              <Button
                variant="secondary"
                busy={qualityFixMutation.isPending}
                title="调用文字模型逐个修复：优先延长时长并补齐时间轴，不改动剧情内容"
                onClick={() => qualityFixMutation.mutate()}
              >
                <Wrench className="h-4 w-4" />
                一键修复
              </Button>
            </div>
          </Card>
        )}

        <Card className="mb-5 overflow-hidden">
          <div className="grid gap-4 p-4 lg:grid-cols-[minmax(280px,1fr)_auto] lg:items-end">
            <div className="flex flex-wrap items-end gap-3">
              <div>
                <Label htmlFor="shot-batch-scope">批量范围</Label>
                <Select
                  id="shot-batch-scope"
                  value={scope}
                  className="w-40"
                  onChange={(event) =>
                    setScope(event.target.value as "all" | "scene")
                  }
                >
                  <option value="all">全部分镜</option>
                  <option value="scene">指定场次</option>
                </Select>
              </div>
              {scope === "scene" && !selected.size ? (
                <div>
                  <Label htmlFor="shot-batch-scene">场次编号</Label>
                  <Input
                    id="shot-batch-scene"
                    type="number"
                    min={1}
                    className="w-32"
                    value={sceneNo}
                    onChange={(event) => setSceneNo(event.target.value)}
                  />
                </div>
              ) : null}
            </div>
            <div className="flex flex-wrap items-center justify-start gap-2 lg:justify-end">
              {selected.size >= 2 ? (
                <Button
                  size="sm"
                  busy={mergeMutation.isPending}
                  onClick={() => {
                    if (
                      window.confirm(
                        `确认合并所选 ${selected.size} 个分镜吗？请确保它们在叙事上连续。`,
                      )
                    ) {
                      mergeMutation.mutate();
                    }
                  }}
                >
                  <Merge className="h-4 w-4" />
                  合并所选
                </Button>
              ) : null}
              {shots.length ? (
                <label className="flex items-center gap-2 px-2 text-xs font-semibold text-muted">
                  <input
                    type="checkbox"
                    className="accent-vermilion"
                    checked={shots.every((shot) => selected.has(shot.id))}
                    onChange={(event) =>
                      setSelected(
                        event.target.checked
                          ? new Set(shots.map((shot) => shot.id))
                          : new Set(),
                      )
                    }
                  />
                  全选
                </label>
              ) : null}
            </div>
          </div>

          <div className="flex flex-col gap-4 border-t border-line bg-white/40 p-4 xl:flex-row xl:items-center xl:justify-between">
            <div className="flex flex-wrap gap-2 text-xs text-muted">
              <Badge>{shots.length} 个分镜</Badge>
              <Badge>
                {
                  shots.filter(
                    (shot) =>
                      hasExecutablePrompt(shot, "image") &&
                      hasExecutablePrompt(shot, "video"),
                  ).length
                }{" "}
                个已自动补齐提示词
              </Badge>
              <Badge>
                {
                  shots.filter(
                    (shot) => shot.current_keyframe?.status === "completed",
                  ).length
                }{" "}
                个当前关键帧
              </Badge>
              <Badge>
                {
                  shots.filter(
                    (shot) => shot.current_video?.status === "completed",
                  ).length
                }{" "}
                个当前视频
              </Badge>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button
                busy={
                  mediaBatchMutation.isPending &&
                  mediaBatchMutation.variables === "keyframe"
                }
                disabled={batchDisabled}
                onClick={() => mediaBatchMutation.mutate("keyframe")}
              >
                <Images className="h-4 w-4" />
                {selected.size
                  ? `生成所选关键帧（${selected.size}）`
                  : scope === "scene"
                    ? "生成当前场次关键帧"
                    : "生成全部关键帧"}
              </Button>
              <Button
                variant="primary"
                busy={
                  mediaBatchMutation.isPending &&
                  mediaBatchMutation.variables === "video"
                }
                disabled={batchDisabled}
                onClick={() => mediaBatchMutation.mutate("video")}
              >
                <Film className="h-4 w-4" />
                {selected.size
                  ? `生成所选视频（${selected.size}）`
                  : scope === "scene"
                    ? "生成当前场次视频"
                    : "生成全部视频"}
              </Button>
            </div>
          </div>
        </Card>

        {query.isLoading ? <LoadingState label="正在读取分镜…" /> : null}
        {query.error ? (
          <ErrorState
            message={formatError(query.error)}
            onRetry={() => void query.refetch()}
          />
        ) : null}
        {!query.isLoading && !query.error && !shots.length ? (
          <EmptyState
            title="还没有分镜"
            description="先生成剧本并提取场景素材，然后启动分镜提取；也可以手工建立第一镜。"
            action={
              <div className="flex flex-wrap justify-center gap-2">
                <Button
                  variant="primary"
                  busy={taskMutation.isPending}
                  onClick={() => taskMutation.mutate({ action: "extract" })}
                >
                  提取全部分镜
                </Button>
                <Button onClick={openNew}>手工新增</Button>
              </div>
            }
          />
        ) : null}

        <div className="space-y-0">
          {shots.map((shot, index) => {
            const connectsToPrevious =
              shots[index].transition_from_prev === "continuous";
            const cardSpacing =
              index === 0 ? "" : connectsToPrevious ? "" : "mt-4";
            const cardShape = connectsToPrevious ? "rounded-none border-t-0" : "";
            const characterIds = idList(shot.character_ids_json);
            const propIds = idList(shot.prop_ids_json);
            const referenceBindings = parseJson<ReferenceBinding[]>(
              shot.reference_bindings_json,
              [],
            );
            const boundAssets = [
              ...(shot.scene_asset_id
                ? [
                    {
                      role: "scene_layout" as const,
                      assetId: shot.scene_asset_id,
                    },
                  ]
                : []),
              ...characterIds.map((assetId) => ({
                role: "character_identity" as const,
                assetId,
              })),
              ...propIds.map((assetId) => ({
                role: "prop_identity" as const,
                assetId,
              })),
            ]
              .filter(
                (item, itemIndex, items) =>
                  items.findIndex(
                    (candidate) =>
                      candidate.role === item.role &&
                      candidate.assetId === item.assetId,
                  ) === itemIndex,
              )
              .map(({ role, assetId }) => {
                const asset = assetMap.get(assetId);
                const typeLabel =
                  role === "scene_layout"
                    ? "场景"
                    : role === "character_identity"
                      ? "角色"
                      : "物品";
                return {
                  asset,
                  assetId,
                  assetName:
                    asset?.name || `未知${typeLabel} ${assetId.slice(0, 6)}`,
                  binding: referenceBindings.find(
                    (binding) =>
                      binding.role === role && binding.asset_id === assetId,
                  ),
                  role,
                  typeLabel,
                };
              });
            const dialogue = jsonList<DialogueLine>(shot.dialogue_json);
            const positions = jsonList<ShotPosition>(shot.positions_json)
              .map(formatPosition)
              .filter(Boolean);
            const expressions = jsonList<ShotExpression>(shot.expressions_json)
              .map(formatExpression)
              .filter(Boolean);
            const scene = shot.scene_asset_id
              ? assetMap.get(shot.scene_asset_id)
              : undefined;
            const stale =
              shot.is_stale ||
              (shot.source_type === "novel" &&
                shot.source_signature &&
                shot.source_signature !== activeChapter?.source_signature);

            return (
              <Card
                key={shot.id}
                className={`overflow-hidden ${cardSpacing} ${cardShape}`}
                draggable
                onDragStart={() => setDraggedId(shot.id)}
                onDragEnd={() => setDraggedId("")}
                onDragOver={(event) => event.preventDefault()}
                onDrop={() => reorder(draggedId, shot.id)}
              >
                <div className="grid md:grid-cols-[64px_minmax(0,1fr)]">
                  <div className="flex items-center gap-3 border-b border-line bg-[#252a27] px-4 py-3 text-white md:flex-col md:border-b-0 md:border-r md:px-2 md:py-5">
                    <GripVertical className="h-5 w-5 cursor-grab text-white/35" />
                    <span className="text-xl font-extrabold">
                      {String(shot.shot_no).padStart(2, "0")}
                    </span>
                    <input
                      type="checkbox"
                      className="accent-vermilion"
                      checked={selected.has(shot.id)}
                      aria-label={`选择分镜 ${shot.shot_no}`}
                      onChange={(event) =>
                        setSelected((current) => {
                          const next = new Set(current);
                          if (event.target.checked) next.add(shot.id);
                          else next.delete(shot.id);
                          return next;
                        })
                      }
                    />
                  </div>
                  <div>
                    <div className="flex flex-col justify-between gap-4 border-b border-line px-5 py-4 xl:flex-row xl:items-start">
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                          <h2 className="font-bold">
                            {scene?.name || shot.scene_name || "未绑定场景"}
                          </h2>
                          <StatusBadge status={shot.status} />
                          <Badge>
                            {shot.duration_sec}s · {shot.shot_type || "未定景别"}
                          </Badge>
                          <Badge>
                            {transitionLabels[shot.transition_from_prev] ??
                              "场景切换"}
                          </Badge>
                          {stale ? (
                            <Badge className="border-[#e2aa88] bg-[#fff3e9] text-[#833f24]">
                              来源已变化
                            </Badge>
                          ) : null}
                        </div>
                        <p className="mt-2 text-sm font-semibold leading-6 text-[#3f423d]">
                          {shot.action || shot.frame_description || "尚未填写镜头动作"}
                        </p>
                        {boundAssets.length ? (
                          <div
                            className="mt-2 flex flex-wrap gap-1.5"
                            role="group"
                            aria-label={`分镜 ${shot.shot_no} 素材标签`}
                          >
                            {boundAssets.map(
                              ({ assetId, assetName, role, typeLabel }) => (
                                <Badge key={`${role}-${assetId}`}>
                                  {typeLabel} · {assetName}
                                </Badge>
                              ),
                            )}
                          </div>
                        ) : null}
                      </div>
                      <div className="flex shrink-0 flex-wrap gap-1">
                        <Button
                          size="icon"
                          variant="ghost"
                          aria-label="上移分镜"
                          disabled={index === 0 || reorderMutation.isPending}
                          onClick={() => move(index, -1)}
                        >
                          <ChevronUp className="h-4 w-4" />
                        </Button>
                        <Button
                          size="icon"
                          variant="ghost"
                          aria-label="下移分镜"
                          disabled={
                            index === shots.length - 1 || reorderMutation.isPending
                          }
                          onClick={() => move(index, 1)}
                        >
                          <ChevronDown className="h-4 w-4" />
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => openEdit(shot)}
                        >
                          <Pencil className="h-4 w-4" />
                          编辑
                        </Button>
                      </div>
                    </div>

                    <section
                      className="border-b border-line bg-[#f7f4ed] px-5 py-4"
                      aria-label={`分镜 ${shot.shot_no} 已绑定素材`}
                    >
                      <div className="mb-3 flex items-center justify-between gap-3">
                        <p className="text-[10px] font-bold uppercase tracking-[0.14em] text-muted">
                          已绑定素材
                        </p>
                        <span className="text-[11px] text-muted">
                          {boundAssets.length} 项
                        </span>
                      </div>
                      {boundAssets.length ? (
                        <div className="flex gap-3 overflow-x-auto pb-1">
                          {boundAssets.map(
                            ({
                              asset,
                              assetId,
                              assetName,
                              binding,
                              role,
                              typeLabel,
                            }) => {
                            const currentImage = asset?.current_image;
                            const imageUrl =
                              binding &&
                              currentImage &&
                              currentImage.id === binding.asset_image_id &&
                              currentImage.status === "completed"
                                ? mediaUrl(currentImage)
                                : "";
                            const previewAlt = `${assetName}${typeLabel}参考图`;
                            const unavailableLabel = binding?.asset_image_id
                              ? "绑定图不可用"
                              : "尚无绑定图";

                            return (
                              <div
                                key={`${role}-${assetId}`}
                                className="w-28 shrink-0"
                              >
                                <div className="aspect-[4/3] overflow-hidden rounded-md border border-line bg-[#252925]">
                                  {imageUrl ? (
                                    <ImagePreview
                                      src={imageUrl}
                                      alt={previewAlt}
                                      buttonClassName="h-full"
                                      imageClassName="h-full w-full object-cover"
                                      loading="lazy"
                                    />
                                  ) : (
                                    <div
                                      className="flex h-full flex-col items-center justify-center gap-1 px-2 text-center text-white/55"
                                      title={unavailableLabel}
                                    >
                                      <ImageOff className="h-5 w-5" />
                                      <span className="text-[10px]">
                                        {unavailableLabel}
                                      </span>
                                    </div>
                                  )}
                                </div>
                                <p className="mt-2 truncate text-xs font-semibold text-[#3f423d]" title={assetName}>
                                  {assetName}
                                </p>
                                <p className="mt-0.5 text-[10px] text-muted">{typeLabel}</p>
                              </div>
                            );
                            },
                          )}
                        </div>
                      ) : (
                        <p className="text-xs text-muted">
                          当前分镜尚未绑定场景、角色或物品素材。
                        </p>
                      )}
                    </section>

                    <div className="grid gap-4 px-5 py-5">
                      <div className="space-y-3">
                        <div>
                          <p className="text-[10px] font-bold uppercase tracking-[0.14em] text-muted">
                            镜头执行
                          </p>
                          <p className="mt-2 text-xs leading-5 text-[#454842]">
                            {shot.camera_angle || "未定角度"} ·{" "}
                            {shot.camera_move || "未定运镜"}
                          </p>
                        </div>
                        {positions.length ? (
                          <div>
                            <p className="text-[10px] font-bold uppercase tracking-[0.14em] text-muted">
                              站位 / 视线
                            </p>
                            <p className="mt-2 whitespace-pre-wrap text-xs leading-5 text-[#454842]">
                              {positions.join("\n")}
                            </p>
                          </div>
                        ) : null}
                        {expressions.length ? (
                          <div>
                            <p className="text-[10px] font-bold uppercase tracking-[0.14em] text-muted">
                              人物表情
                            </p>
                            <p className="mt-2 whitespace-pre-wrap text-xs leading-5 text-[#454842]">
                              {expressions.join("\n")}
                            </p>
                          </div>
                        ) : null}
                        <div>
                          <p className="text-[10px] font-bold uppercase tracking-[0.14em] text-muted">
                            对白 / 旁白
                          </p>
                          <p className="mt-2 line-clamp-4 whitespace-pre-wrap text-xs leading-5 text-[#454842]">
                            {dialogue
                              .map(formatDialogue)
                              .filter(Boolean)
                              .join("\n") ||
                              shot.narration ||
                              "本镜无对白"}
                          </p>
                        </div>
                      </div>
                    </div>
                    {stale ? (
                      <div className="px-5 pb-4">
                        <StaleNotice
                          compact
                          reason="该分镜来源已变化。旧文本和媒体不会删除，请核对或按场重生成。"
                        />
                      </div>
                    ) : null}

                    <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line bg-white/35 px-5 py-3">
                      <div className="flex flex-wrap gap-1">
                        <Button
                          variant="ghost"
                          size="sm"
                          busy={
                            structureMutation.isPending &&
                            structureMutation.variables?.action === "split" &&
                            structureMutation.variables.id === shot.id
                          }
                          onClick={() =>
                            structureMutation.mutate({
                              action: "split",
                              id: shot.id,
                            })
                          }
                        >
                          <Scissors className="h-4 w-4" />
                          拆分
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          busy={
                            structureMutation.isPending &&
                            structureMutation.variables?.action === "copy" &&
                            structureMutation.variables.id === shot.id
                          }
                          onClick={() =>
                            structureMutation.mutate({
                              action: "copy",
                              id: shot.id,
                            })
                          }
                        >
                          <Copy className="h-4 w-4" />
                          复制
                        </Button>
                        {shot.scene_no ? (
                          <Button
                            variant="ghost"
                            size="sm"
                            busy={
                              taskMutation.isPending &&
                              taskMutation.variables?.action === "extractScene" &&
                              taskMutation.variables.shot?.id === shot.id
                            }
                            onClick={() =>
                              taskMutation.mutate({
                                action: "extractScene",
                                shot,
                              })
                            }
                          >
                            重生成本场
                          </Button>
                        ) : null}
                      </div>
                      <div className="flex gap-1">
                        <Button
                          variant="ghost"
                          size="sm"
                          className="text-[#9b342c]"
                          busy={
                            structureMutation.isPending &&
                            structureMutation.variables?.action === "delete" &&
                            structureMutation.variables.id === shot.id
                          }
                          onClick={() => {
                            if (
                              window.confirm(
                                `确认删除分镜 ${shot.shot_no} 及其媒体版本吗？`,
                              )
                            ) {
                              structureMutation.mutate({
                                action: "delete",
                                id: shot.id,
                              });
                            }
                          }}
                        >
                          <Trash2 className="h-4 w-4" />
                          删除
                        </Button>
                      </div>
                    </div>

                    <div className="grid gap-4 border-t border-line bg-[#f7f4ed] p-4 2xl:grid-cols-2">
                      <ShotMediaCard
                        shot={shot}
                        kind="keyframe"
                        assetMap={assetMap}
                        selected={selected.has(shot.id)}
                        onToggle={(checked) =>
                          setSelected((current) => {
                            const next = new Set(current);
                            if (checked) next.add(shot.id);
                            else next.delete(shot.id);
                            return next;
                          })
                        }
                        embedded
                        anchorId={index === 0 ? "stage-keyframes" : undefined}
                      />
                      <ShotMediaCard
                        shot={shot}
                        kind="video"
                        assetMap={assetMap}
                        selected={selected.has(shot.id)}
                        onToggle={(checked) =>
                          setSelected((current) => {
                            const next = new Set(current);
                            if (checked) next.add(shot.id);
                            else next.delete(shot.id);
                            return next;
                          })
                        }
                        embedded
                        anchorId={index === 0 ? "stage-videos" : undefined}
                      />
                    </div>
                  </div>
                </div>
              </Card>
            );
          })}
        </div>

        <Modal
          open={Boolean(editing)}
          onClose={() => setEditing(null)}
          title={editing === "new" ? "新增分镜" : `编辑分镜 ${form.shot_no}`}
          description="一个镜头只写一个主要动作；明确人物站位、视线、方向、起止状态与下一镜衔接。"
          wide
          footer={
            <>
              <Button onClick={() => setEditing(null)}>取消</Button>
              <Button
                variant="primary"
                busy={saveMutation.isPending}
                disabled={
                  !form.action?.trim() ||
                  !form.duration_sec ||
                  form.duration_sec < 5 ||
                  !jsonValid
                }
                onClick={() => saveMutation.mutate()}
              >
                保存分镜
              </Button>
            </>
          }
        >
          <div className="space-y-6">
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              <div>
                <Label>分镜编号</Label>
                <Input
                  type="number"
                  min={1}
                  disabled={editing !== "new"}
                  value={form.shot_no || 1}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      shot_no: Number(event.target.value),
                    }))
                  }
                />
              </div>
              <div>
                <Label>场次编号</Label>
                <Input
                  type="number"
                  min={1}
                  value={form.scene_no || ""}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      scene_no: event.target.value
                        ? Number(event.target.value)
                        : null,
                    }))
                  }
                />
              </div>
              <div>
                <Label>场景素材</Label>
                <Select
                  value={form.scene_asset_id || ""}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      scene_asset_id: event.target.value || null,
                    }))
                  }
                >
                  <option value="">未绑定</option>
                  {assets
                    .filter((asset) => asset.asset_type === "scene")
                    .map((asset) => (
                      <option key={asset.id} value={asset.id}>
                        {asset.name}
                      </option>
                    ))}
                </Select>
              </div>
            </div>

            <div className="grid gap-5 lg:grid-cols-2">
              <div>
                <Label>出场角色</Label>
                <div className="grid max-h-40 gap-2 overflow-y-auto rounded-md border border-line bg-white p-3 sm:grid-cols-2">
                  {assets
                    .filter((asset) => asset.asset_type === "character")
                    .map((asset) => {
                      const ids = idList(form.character_ids_json);
                      return (
                        <label
                          key={asset.id}
                          className="flex items-center gap-2 text-sm"
                        >
                          <input
                            type="checkbox"
                            className="accent-vermilion"
                            checked={ids.includes(asset.id)}
                            onChange={(event) =>
                              setForm((current) => {
                                const currentIds = idList(
                                  current.character_ids_json,
                                );
                                return {
                                  ...current,
                                  character_ids_json: event.target.checked
                                    ? [...currentIds, asset.id]
                                    : currentIds.filter((id) => id !== asset.id),
                                };
                              })
                            }
                          />
                          {asset.name}
                        </label>
                      );
                    })}
                  {!assets.some((asset) => asset.asset_type === "character") ? (
                    <p className="text-xs text-muted">素材库中还没有角色。</p>
                  ) : null}
                </div>
              </div>
              <div>
                <Label>使用物品</Label>
                <div className="grid max-h-40 gap-2 overflow-y-auto rounded-md border border-line bg-white p-3 sm:grid-cols-2">
                  {assets
                    .filter((asset) => asset.asset_type === "prop")
                    .map((asset) => {
                      const ids = idList(form.prop_ids_json);
                      return (
                        <label
                          key={asset.id}
                          className="flex items-center gap-2 text-sm"
                        >
                          <input
                            type="checkbox"
                            className="accent-vermilion"
                            checked={ids.includes(asset.id)}
                            onChange={(event) =>
                              setForm((current) => {
                                const currentIds = idList(current.prop_ids_json);
                                return {
                                  ...current,
                                  prop_ids_json: event.target.checked
                                    ? [...currentIds, asset.id]
                                    : currentIds.filter((id) => id !== asset.id),
                                };
                              })
                            }
                          />
                          {asset.name}
                        </label>
                      );
                    })}
                  {!assets.some((asset) => asset.asset_type === "prop") ? (
                    <p className="text-xs text-muted">素材库中还没有剧情物品。</p>
                  ) : null}
                </div>
              </div>
            </div>

            <div className="grid gap-4 lg:grid-cols-2">
              <div>
                <Label>开场状态</Label>
                <Textarea
                  rows={3}
                  value={form.start_state || ""}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      start_state: event.target.value,
                    }))
                  }
                />
              </div>
              <div>
                <Label>结束状态</Label>
                <Textarea
                  rows={3}
                  value={form.end_state || ""}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      end_state: event.target.value,
                    }))
                  }
                />
              </div>
              <div className="lg:col-span-2">
                <Label>主要动作</Label>
                <Textarea
                  rows={4}
                  value={form.action || ""}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      action: event.target.value,
                    }))
                  }
                />
              </div>
              <div className="lg:col-span-2">
                <Label>关键帧画面描述</Label>
                <Textarea
                  rows={4}
                  value={form.frame_description || ""}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      frame_description: event.target.value,
                    }))
                  }
                />
              </div>
            </div>

            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <div>
                <Label>景别</Label>
                <Input
                  value={form.shot_type || ""}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      shot_type: event.target.value,
                    }))
                  }
                />
              </div>
              <div>
                <Label>镜头角度</Label>
                <Input
                  value={form.camera_angle || ""}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      camera_angle: event.target.value,
                    }))
                  }
                />
              </div>
              <div>
                <Label>运镜</Label>
                <Input
                  value={form.camera_move || ""}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      camera_move: event.target.value,
                    }))
                  }
                />
              </div>
              <div>
                <Label>时长（秒）</Label>
                <Input
                  type="number"
                  min={5}
                  step={1}
                  value={form.duration_sec || ""}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      duration_sec: Number(event.target.value),
                    }))
                  }
                />
              </div>
            </div>

            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <Label>与上一镜衔接</Label>
                <Select
                  value={form.transition_from_prev || "scene_change"}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      transition_from_prev: event.target
                        .value as ShotTransitionFromPrev,
                    }))
                  }
                >
                  <option value="continuous">连续承接（使用上一镜末帧）</option>
                  <option value="elliptical">省略承接</option>
                  <option value="same_scene_cut">同场切镜</option>
                  <option value="scene_change">场景切换</option>
                </Select>
                <p className="mt-2 text-xs leading-5 text-muted">
                  选“连续承接”时，前端列表会与上一镜紧贴，视频生成会引用上一镜最后一帧。
                </p>
              </div>
            </div>

            <div className="grid gap-4 lg:grid-cols-2">
              <JsonEditor
                label="人物站位"
                value={parseJson(form.positions_json, [])}
                onChange={(value) =>
                  setForm((current) => ({
                    ...current,
                    positions_json: value as Array<Record<string, unknown>>,
                  }))
                }
                onValidityChange={(valid) =>
                  setJsonSectionValidity("positions", valid)
                }
                rows={8}
              />
              <JsonEditor
                label="人物表情"
                value={parseJson(form.expressions_json, [])}
                onChange={(value) =>
                  setForm((current) => ({
                    ...current,
                    expressions_json: value as Array<Record<string, unknown>>,
                  }))
                }
                onValidityChange={(valid) =>
                  setJsonSectionValidity("expressions", valid)
                }
                rows={8}
              />
              <JsonEditor
                label="对白"
                value={parseJson(form.dialogue_json, [])}
                onChange={(value) =>
                  setForm((current) => ({
                    ...current,
                    dialogue_json: value as Array<Record<string, unknown>>,
                  }))
                }
                onValidityChange={(valid) =>
                  setJsonSectionValidity("dialogue", valid)
                }
                rows={8}
              />
              <div>
                <Label>旁白</Label>
                <Textarea
                  rows={10}
                  value={form.narration || ""}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      narration: event.target.value,
                    }))
                  }
                />
              </div>
            </div>

            <div className="grid gap-4 lg:grid-cols-2">
              <div>
                <Label>下一镜连续性钩子</Label>
                <Textarea
                  rows={3}
                  value={form.next_hook || ""}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      next_hook: event.target.value,
                    }))
                  }
                />
              </div>
              <div>
                <Label>来源位置</Label>
                <Textarea
                  rows={3}
                  value={form.source_ref || ""}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      source_ref: event.target.value,
                    }))
                  }
                />
              </div>
            </div>

            <div className="grid gap-4">
              <div>
                <Label>负向提示词 / 禁止事项</Label>
                <Textarea
                  rows={4}
                  value={form.negative_prompt || ""}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      negative_prompt: event.target.value,
                    }))
                  }
                />
              </div>
            </div>

            <JsonEditor
              label="明确参考图绑定"
              value={parseJson(form.reference_bindings_json, [])}
              onChange={(value) =>
                setForm((current) => ({
                  ...current,
                  reference_bindings_json: value as ReferenceBinding[],
                }))
              }
              onValidityChange={(valid) =>
                setJsonSectionValidity("references", valid)
              }
              rows={9}
            />
          </div>
        </Modal>

        <Modal
          open={continuityOpen}
          onClose={() => setContinuityOpen(false)}
          title="连贯性检查"
          description="连播预览整场视频，或用相邻镜头首末帧对比检查视觉衔接是否突兀。"
          wide
        >
          <ContinuityCheck shots={shots} />
        </Modal>
      </div>
      </ChapterRequired>
    </ProjectRequired>
  );
}
