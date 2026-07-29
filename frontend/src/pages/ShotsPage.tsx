import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  ChevronDown,
  ChevronUp,
  Copy,
  GripVertical,
  Merge,
  Pencil,
  Plus,
  Scissors,
  Sparkles,
  Trash2,
  WandSparkles,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { api, queryKeys } from "../api/client";
import { JsonEditor } from "../components/json-editor";
import { ProjectRequired } from "../components/project-required";
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
import { useProject } from "../hooks/use-project";
import { formatError, parseJson } from "../lib/utils";
import type { ReferenceBinding, Shot } from "../types/api";

function blankShot(projectId: string, sourceType: "novel" | "screenplay"): Partial<Shot> {
  return {
    project_id: projectId,
    shot_no: 1,
    shot_order: 1,
    source_type: sourceType,
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
    duration_sec: 6,
    end_state: "",
    next_hook: "",
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

function jsonList(
  value: Array<Record<string, unknown>> | string | undefined,
) {
  return parseJson<Array<Record<string, unknown>>>(value, []);
}

export function ShotsPage() {
  const queryClient = useQueryClient();
  const { activeProject, activeProjectId } = useProject();
  const [sourceType, setSourceType] = useState<"novel" | "screenplay">("novel");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [editing, setEditing] = useState<Shot | "new" | null>(null);
  const [form, setForm] = useState<Partial<Shot>>(
    blankShot(activeProjectId, "novel"),
  );
  const [jsonValidity, setJsonValidity] = useState<Record<string, boolean>>({});
  const [draggedId, setDraggedId] = useState("");

  const query = useQuery({
    queryKey: queryKeys.shots(activeProjectId),
    queryFn: () => api.shots.list(activeProjectId),
    enabled: Boolean(activeProjectId),
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

  useEffect(() => {
    setSelected(new Set());
  }, [activeProjectId]);

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: queryKeys.shots(activeProjectId) });

  const taskMutation = useMutation({
    mutationFn: async ({
      action,
      shot,
    }: {
      action:
        | "extract"
        | "extractScene"
        | "imagePrompt"
        | "videoPrompt"
        | "batchPrompts";
      shot?: Shot;
    }) => {
      if (action === "extract") {
        return api.shots.extract(activeProjectId, {
          source_type: sourceType,
          scope: "all",
        });
      }
      if (action === "extractScene" && shot) {
        return api.shots.extract(activeProjectId, {
          source_type: shot.source_type,
          scope: "scene",
          scene_no: shot.scene_no || undefined,
        });
      }
      if (action === "imagePrompt" && shot) {
        return api.shots.generateImagePrompt(shot.id);
      }
      if (action === "videoPrompt" && shot) {
        return api.shots.generateVideoPrompt(shot.id);
      }
      return api.shots.batchPrompts(activeProjectId, {
        ids: selected.size ? [...selected] : undefined,
        prompt_types: ["image", "video"],
      });
    },
    onSuccess: async (result, variables) => {
      await queryClient.invalidateQueries({
        queryKey: queryKeys.jobs(activeProjectId),
      });
      if (
        variables.action === "batchPrompts" &&
        result &&
        "jobs" in result
      ) {
        const failures = result.preflight_failures ?? [];
        if (failures.length) {
          const description = failures
            .map(({ target_id, error }) => {
              const shot = shots.find((item) => item.id === target_id);
              return `分镜 ${shot?.shot_no ?? target_id}：${error}`;
            })
            .join("；");
          toast.warning(
            `已入队 ${result.jobs.length} 个分镜提示词任务，跳过 ${failures.length} 项`,
            { description },
          );
          return;
        }
        toast.success(`已入队 ${result.jobs.length} 个分镜提示词任务`);
        return;
      }
      toast.success("分镜任务已进入队列");
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const saveMutation = useMutation({
    mutationFn: () =>
      editing === "new"
        ? api.shots.create(activeProjectId, form)
        : api.shots.update((editing as Shot).id, form),
    onSuccess: async () => {
      await invalidate();
      toast.success(editing === "new" ? "分镜已添加" : "分镜已保存");
      setEditing(null);
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const updateMutation = useMutation({
    mutationFn: ({
      id,
      patch,
    }: {
      id: string;
      patch: Partial<Shot>;
    }) => api.shots.update(id, patch),
    onSuccess: async () => {
      await invalidate();
      toast.success("分镜已确认");
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
    mutationFn: () => api.shots.merge(activeProjectId, [...selected]),
    onSuccess: async () => {
      setSelected(new Set());
      await invalidate();
      toast.success("分镜已合并");
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const reorderMutation = useMutation({
    mutationFn: (ids: string[]) => api.shots.reorder(activeProjectId, ids),
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
      ...blankShot(activeProjectId, sourceType),
      shot_no: shots.length + 1,
      shot_order: shots.length + 1,
      duration_sec: activeProject?.default_duration_sec || 6,
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
      <div>
        <PageHeader
          eyebrow="Step 05 / Storyboard"
          title="分镜与提示词"
          description="每个镜头只承载一个主要动作，并明确站位、起止状态与连续性钩子。卡片可拖动，也可用按钮精确排序。"
          actions={
            <>
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
                variant="primary"
                busy={
                  taskMutation.isPending &&
                  taskMutation.variables?.action === "batchPrompts"
                }
                disabled={!shots.length}
                onClick={() => taskMutation.mutate({ action: "batchPrompts" })}
              >
                <Sparkles className="h-4 w-4" />
                {selected.size
                  ? `生成所选提示词（${selected.size}）`
                  : "批量补齐提示词"}
              </Button>
            </>
          }
        />

        <Card className="mb-5 flex flex-col justify-between gap-4 p-4 sm:flex-row sm:items-center">
          <div>
            <p className="text-xs font-bold text-muted">分镜来源</p>
            <div className="mt-2 flex gap-2">
              {(["novel", "screenplay"] as const).map((value) => (
                <button
                  type="button"
                  key={value}
                  onClick={() => setSourceType(value)}
                  className={`rounded-md border px-3 py-2 text-sm font-semibold ${
                    sourceType === value
                      ? "border-vermilion bg-[#fff1eb] text-[#9a3d21]"
                      : "border-line bg-white text-muted"
                  }`}
                >
                  {value === "novel" ? "从小说提取" : "从当前剧本提取"}
                </button>
              ))}
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
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
            description="选择小说或当前剧本作为来源，然后启动提取；也可以手工建立第一镜。"
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

        <div className="space-y-4">
          {shots.map((shot, index) => {
            const characterIds = idList(shot.character_ids_json);
            const propIds = idList(shot.prop_ids_json);
            const dialogue = jsonList(shot.dialogue_json);
            const scene = shot.scene_asset_id
              ? assetMap.get(shot.scene_asset_id)
              : undefined;
            const stale =
              shot.is_stale ||
              (shot.source_type === "novel" &&
                shot.source_signature &&
                shot.source_signature !== activeProject?.source_signature);

            return (
              <Card
                key={shot.id}
                className="overflow-hidden"
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
                          {stale ? (
                            <Badge className="border-[#e2aa88] bg-[#fff3e9] text-[#833f24]">
                              来源已变化
                            </Badge>
                          ) : null}
                        </div>
                        <p className="mt-2 text-sm font-semibold leading-6 text-[#3f423d]">
                          {shot.action || shot.frame_description || "尚未填写镜头动作"}
                        </p>
                        <div className="mt-2 flex flex-wrap gap-1.5">
                          {characterIds.map((id) => (
                            <Badge key={id}>
                              {assetMap.get(id)?.name || `未知角色 ${id.slice(0, 6)}`}
                            </Badge>
                          ))}
                          {propIds.map((id) => (
                            <Badge key={id} className="bg-[#f3f0e8]">
                              {assetMap.get(id)?.name || `未知物品 ${id.slice(0, 6)}`}
                            </Badge>
                          ))}
                        </div>
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

                    <div className="grid gap-4 px-5 py-5 xl:grid-cols-[0.75fr_1fr_1fr]">
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
                        <div>
                          <p className="text-[10px] font-bold uppercase tracking-[0.14em] text-muted">
                            对白 / 旁白
                          </p>
                          <p className="mt-2 line-clamp-4 whitespace-pre-wrap text-xs leading-5 text-[#454842]">
                            {dialogue
                              .map((item) =>
                                `${String(item.speaker || "")}：${String(item.text || item.content || "")}`.replace(
                                  /^：/,
                                  "",
                                ),
                              )
                              .filter(Boolean)
                              .join("\n") ||
                              shot.narration ||
                              "本镜无对白"}
                          </p>
                        </div>
                      </div>
                      <div className="rounded-md border border-line bg-white/55 p-3">
                        <div className="flex items-center justify-between gap-2">
                          <p className="text-[10px] font-bold uppercase tracking-[0.14em] text-muted">
                            关键帧提示词
                          </p>
                          <Button
                            variant="ghost"
                            size="sm"
                            busy={
                              taskMutation.isPending &&
                              taskMutation.variables?.action === "imagePrompt" &&
                              taskMutation.variables.shot?.id === shot.id
                            }
                            onClick={() =>
                              taskMutation.mutate({
                                action: "imagePrompt",
                                shot,
                              })
                            }
                          >
                            <Sparkles className="h-3.5 w-3.5" />
                            生成
                          </Button>
                        </div>
                        <p className="mt-2 line-clamp-5 text-xs leading-5 text-[#454842]">
                          {shot.image_prompt || "尚未生成"}
                        </p>
                      </div>
                      <div className="rounded-md border border-line bg-white/55 p-3">
                        <div className="flex items-center justify-between gap-2">
                          <p className="text-[10px] font-bold uppercase tracking-[0.14em] text-muted">
                            视频执行提示词
                          </p>
                          <Button
                            variant="ghost"
                            size="sm"
                            busy={
                              taskMutation.isPending &&
                              taskMutation.variables?.action === "videoPrompt" &&
                              taskMutation.variables.shot?.id === shot.id
                            }
                            onClick={() =>
                              taskMutation.mutate({
                                action: "videoPrompt",
                                shot,
                              })
                            }
                          >
                            <Sparkles className="h-3.5 w-3.5" />
                            生成
                          </Button>
                        </div>
                        <p className="mt-2 line-clamp-5 text-xs leading-5 text-[#454842]">
                          {shot.video_prompt || "尚未生成"}
                        </p>
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
                        {shot.status !== "confirmed" ? (
                          <Button
                            variant="ghost"
                            size="sm"
                            busy={
                              updateMutation.isPending &&
                              updateMutation.variables?.id === shot.id
                            }
                            onClick={() =>
                              updateMutation.mutate({
                                id: shot.id,
                                patch: { status: "confirmed" },
                              })
                            }
                          >
                            <Check className="h-4 w-4" />
                            确认
                          </Button>
                        ) : null}
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
                disabled={!form.action?.trim() || !jsonValid}
                onClick={() => saveMutation.mutate()}
              >
                保存分镜
              </Button>
            </>
          }
        >
          <div className="space-y-6">
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
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
                <Label>来源</Label>
                <Select
                  disabled={editing !== "new"}
                  value={form.source_type || sourceType}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      source_type: event.target.value as Shot["source_type"],
                    }))
                  }
                >
                  <option value="novel">小说</option>
                  <option value="screenplay">剧本</option>
                </Select>
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
                  min={0.5}
                  step={0.5}
                  value={form.duration_sec || 0}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      duration_sec: Number(event.target.value),
                    }))
                  }
                />
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

            <div className="grid gap-4 lg:grid-cols-2">
              <div>
                <Label>关键帧图片提示词</Label>
                <Textarea
                  rows={8}
                  value={form.image_prompt || ""}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      image_prompt: event.target.value,
                    }))
                  }
                />
              </div>
              <div>
                <Label>视频执行提示词</Label>
                <Textarea
                  rows={8}
                  value={form.video_prompt || ""}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      video_prompt: event.target.value,
                    }))
                  }
                />
              </div>
              <div className="lg:col-span-2">
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
      </div>
    </ProjectRequired>
  );
}
