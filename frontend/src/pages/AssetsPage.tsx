import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ImagePlus,
  ImageOff,
  Merge,
  Pencil,
  Plus,
  Trash2,
  WandSparkles,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { api, mediaUrl, queryKeys } from "../api/client";
import { AssetImagePanel } from "./AssetImagesPage";
import { ImagePreview } from "../components/image-preview";
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
  Tabs,
  Textarea,
} from "../components/ui";
import { useProject } from "../hooks/use-project";
import { formatError, parseJson } from "../lib/utils";
import type { Asset, AssetType } from "../types/api";

function profileSummaryValues(value: unknown): string[] {
  if (typeof value === "string") return value.trim() ? [value.trim()] : [];
  if (Array.isArray(value)) return value.flatMap(profileSummaryValues);
  if (value && typeof value === "object") {
    return Object.values(value).flatMap(profileSummaryValues);
  }
  return [];
}

const labels: Record<AssetType, { name: string; single: string; description: string }> =
  {
    character: {
      name: "角色",
      single: "角色",
      description: "稳定外貌、服装、标志性特征与表演习惯。",
    },
    scene: {
      name: "场景",
      single: "场景",
      description: "空间布局、时间天气、光线与色彩氛围。",
    },
    prop: {
      name: "物品",
      single: "物品",
      description: "承担剧情功能或影响跨镜头一致性的关键物品。",
    },
  };

function blankAsset(projectId: string, type: AssetType): Partial<Asset> {
  return {
    project_id: projectId,
    asset_type: type,
    name: "",
    profile_json: {},
    prompt: "",
    negative_prompt: "",
    consistency_json: [],
    composition: "",
    importance: "normal",
    status: "draft",
  };
}

export function AssetsPage() {
  const queryClient = useQueryClient();
  const { activeProject, activeProjectId } = useProject();
  const [type, setType] = useState<AssetType>("character");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [editing, setEditing] = useState<Asset | "new" | null>(null);
  const [form, setForm] = useState<Partial<Asset>>(
    blankAsset(activeProjectId, "character"),
  );
  const [profileValid, setProfileValid] = useState(true);

  const query = useQuery({
    queryKey: queryKeys.assets(activeProjectId),
    queryFn: () => api.assets.list(activeProjectId),
    enabled: Boolean(activeProjectId),
  });
  const allAssets = useMemo(() => query.data || [], [query.data]);
  const filtered = useMemo(
    () => allAssets.filter((asset) => asset.asset_type === type),
    [allAssets, type],
  );

  useEffect(() => {
    setSelected(new Set());
  }, [activeProjectId, type]);

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["assets", activeProjectId] });

  const taskMutation = useMutation({
    mutationFn: async ({
      action,
    }: {
      action: "extractCurrent" | "extractAll";
    }) => {
      if (action === "extractAll") return api.assets.extract(activeProjectId);
      return api.assets.extract(activeProjectId, { asset_types: [type] });
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: queryKeys.jobs(activeProjectId),
      });
      toast.success("任务已进入队列，可在任务中心查看进度");
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const imageBatchMutation = useMutation({
    mutationFn: () =>
      api.assets.images.batch(activeProjectId, {
        ids: selected.size ? [...selected] : undefined,
        asset_type: selected.size ? undefined : type,
      }),
    onSuccess: async (result) => {
      await queryClient.invalidateQueries({
        queryKey: queryKeys.jobs(activeProjectId),
      });
      const failures = result.preflight_failures ?? [];
      if (failures.length) {
        const description = failures
          .map(({ target_id, error }) => {
            const asset = allAssets.find((item) => item.id === target_id);
            return `素材 ${asset?.name ?? target_id}：${error}`;
          })
          .join("；");
        toast.warning(
          `已入队 ${result.jobs.length} 个素材图片任务，跳过 ${failures.length} 项`,
          { description },
        );
        return;
      }
      toast.success(`已入队 ${result.jobs.length} 个素材图片任务`);
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const saveMutation = useMutation({
    mutationFn: () =>
      editing === "new"
        ? api.assets.create(activeProjectId, form)
        : api.assets.update((editing as Asset).id, form),
    onSuccess: async () => {
      await invalidate();
      toast.success(editing === "new" ? "素材已添加" : "素材已保存");
      setEditing(null);
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.assets.remove(id),
    onSuccess: async () => {
      await invalidate();
      toast.success("素材已删除");
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const mergeMutation = useMutation({
    mutationFn: () => api.assets.merge(activeProjectId, [...selected]),
    onSuccess: async () => {
      setSelected(new Set());
      await invalidate();
      toast.success("重复素材已合并");
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const openNew = () => {
    setForm(blankAsset(activeProjectId, type));
    setProfileValid(true);
    setEditing("new");
  };
  const openEdit = (asset: Asset) => {
    setForm({
      ...asset,
      profile_json: parseJson(asset.profile ?? asset.profile_json, {}),
      consistency_json: parseJson(
        asset.consistency_keywords ?? asset.consistency_json,
        [],
      ),
    });
    setProfileValid(true);
    setEditing(asset);
  };

  return (
    <ProjectRequired>
      <div>
        <PageHeader
          eyebrow="Step 03 / Asset library"
          title="素材库"
          description="提取小说中的角色、场景与物品后自动补齐最终提示词，并在同一页面管理素材图片和历史版本。"
          actions={
            <>
              <Button onClick={openNew}>
                <Plus className="h-4 w-4" />
                新增{labels[type].single}
              </Button>
              <Button
                busy={
                  taskMutation.isPending &&
                  taskMutation.variables?.action === "extractCurrent"
                }
                onClick={() =>
                  taskMutation.mutate({ action: "extractCurrent" })
                }
              >
                <WandSparkles className="h-4 w-4" />
                提取当前分类
              </Button>
              <Button
                variant="primary"
                busy={imageBatchMutation.isPending}
                disabled={!filtered.length}
                onClick={() => imageBatchMutation.mutate()}
              >
                <ImagePlus className="h-4 w-4" />
                {selected.size
                  ? `生成所选素材图（${selected.size}）`
                  : `批量生成${labels[type].name}图`}
              </Button>
            </>
          }
        />

        <div className="mb-5 flex flex-col justify-between gap-3 lg:flex-row lg:items-center">
          <Tabs
            value={type}
            onChange={(value) => setType(value as AssetType)}
            items={(Object.keys(labels) as AssetType[]).map((item) => ({
              value: item,
              label: labels[item].name,
              count: allAssets.filter((asset) => asset.asset_type === item).length,
            }))}
          />
          <div className="flex flex-wrap items-center gap-2">
            {selected.size >= 2 ? (
              <Button
                size="sm"
                busy={mergeMutation.isPending}
                onClick={() => {
                  if (
                    window.confirm(
                      `确认合并所选 ${selected.size} 个${labels[type].name}吗？将保留第一项为主记录。`,
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
            <Button
              variant="ghost"
              size="sm"
              busy={
                taskMutation.isPending &&
                taskMutation.variables?.action === "extractAll"
              }
              onClick={() => taskMutation.mutate({ action: "extractAll" })}
            >
              一键提取全部素材
            </Button>
          </div>
        </div>

        <p className="mb-5 text-sm text-muted">{labels[type].description}</p>

        {query.isLoading ? <LoadingState label="正在读取素材库…" /> : null}
        {query.error ? (
          <ErrorState
            message={formatError(query.error)}
            onRetry={() => void query.refetch()}
          />
        ) : null}
        {!query.isLoading && !query.error && !filtered.length ? (
          <EmptyState
            title={`还没有${labels[type].name}`}
            description={`可以从当前小说提取${labels[type].name}，也可以手工新增。提取时同类型同名项会合并更新。`}
            action={
              <div className="flex flex-wrap justify-center gap-2">
                <Button
                  variant="primary"
                  busy={taskMutation.isPending}
                  onClick={() =>
                    taskMutation.mutate({ action: "extractCurrent" })
                  }
                >
                  提取{labels[type].name}
                </Button>
                <Button onClick={openNew}>手工新增</Button>
              </div>
            }
          />
        ) : null}

        <div className="grid gap-4 xl:grid-cols-2">
          {filtered.map((asset) => {
            const profile = parseJson<Record<string, unknown>>(
              asset.profile ?? asset.profile_json,
              {},
            );
            const description = Object.values(profile)
              .flatMap(profileSummaryValues)
              .slice(0, 3)
              .join(" · ");
            const stale =
              asset.is_stale ||
              (asset.source_signature &&
                asset.source_signature !== activeProject?.source_signature);
            return (
              <Card key={asset.id} className="overflow-hidden">
                <div className="grid sm:grid-cols-[150px_minmax(0,1fr)]">
                  <div className="relative min-h-40 border-b border-line bg-[#252925] sm:border-b-0 sm:border-r">
                    {asset.current_image && mediaUrl(asset.current_image) ? (
                      <ImagePreview
                        src={mediaUrl(asset.current_image)}
                        alt={`${asset.name}当前参考图`}
                        buttonClassName="h-full"
                        imageClassName="h-full min-h-40 w-full object-cover"
                      />
                    ) : (
                      <div className="flex h-full min-h-40 items-center justify-center text-white/35">
                        <ImageOff className="h-7 w-7" />
                      </div>
                    )}
                    <label className="absolute left-3 top-3 z-10 flex h-7 w-7 cursor-pointer items-center justify-center rounded border border-white/20 bg-black/55">
                      <input
                        type="checkbox"
                        className="accent-vermilion"
                        checked={selected.has(asset.id)}
                        aria-label={`选择 ${asset.name}`}
                        onChange={(event) =>
                          setSelected((current) => {
                            const next = new Set(current);
                            if (event.target.checked) next.add(asset.id);
                            else next.delete(asset.id);
                            return next;
                          })
                        }
                      />
                    </label>
                  </div>
                  <div className="min-w-0 p-5">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                          <h2 className="truncate text-lg font-bold">{asset.name}</h2>
                          {asset.importance === "core" ? (
                            <Badge className="border-[#d9ae7f] bg-[#fff3e5] text-[#8b4b1e]">
                              核心
                            </Badge>
                          ) : null}
                          <StatusBadge status={asset.status} />
                        </div>
                        <p className="mt-2 line-clamp-2 text-sm leading-6 text-muted">
                          {description || "尚未填写完整素材描述"}
                        </p>
                      </div>
                      <Button
                        size="icon"
                        variant="ghost"
                        aria-label={`编辑 ${asset.name}`}
                        onClick={() => openEdit(asset)}
                      >
                        <Pencil className="h-4 w-4" />
                      </Button>
                    </div>

                    {stale ? (
                      <div className="mt-3">
                        <StaleNotice
                          compact
                          reason="小说来源已变化，请核对该素材是否仍参与当前剧情。"
                        />
                      </div>
                    ) : null}

                    <div className="mt-4 rounded-md border border-line bg-white/60 p-3">
                      <p className="text-[10px] font-bold uppercase tracking-[0.14em] text-muted">
                        最终提示词
                      </p>
                      <p className="mt-2 max-h-44 overflow-y-auto whitespace-pre-wrap text-xs leading-5 text-[#454842]">
                        {asset.compiled_prompt || asset.prompt}
                      </p>
                    </div>
                  </div>
                </div>
                <AssetImagePanel asset={asset} />
              </Card>
            );
          })}
        </div>

        <Modal
          open={Boolean(editing)}
          onClose={() => setEditing(null)}
          title={editing === "new" ? `新增${labels[type].single}` : "编辑素材"}
          description="保存档案和视觉细节后，最终提示词会自动按固定模板更新。"
          wide
          footer={
            <>
              {editing && editing !== "new" ? (
                <Button
                  variant="danger"
                  className="mr-auto"
                  busy={
                    deleteMutation.isPending &&
                    deleteMutation.variables === editing.id
                  }
                  onClick={() => {
                    if (
                      window.confirm(
                        `确认删除“${editing.name}”吗？其历史媒体版本也会一并删除。`,
                      )
                    ) {
                      deleteMutation.mutate(editing.id, {
                        onSuccess: () => setEditing(null),
                      });
                    }
                  }}
                >
                  <Trash2 className="h-4 w-4" />
                  删除
                </Button>
              ) : null}
              <Button onClick={() => setEditing(null)}>取消</Button>
              <Button
                variant="primary"
                busy={saveMutation.isPending}
                disabled={!form.name?.trim() || !profileValid}
                onClick={() => saveMutation.mutate()}
              >
                保存素材
              </Button>
            </>
          }
        >
          <div className="grid gap-5 lg:grid-cols-2">
            <div className="space-y-5">
              <div className="grid gap-4 sm:grid-cols-2">
                <div>
                  <Label htmlFor="asset-name">名称</Label>
                  <Input
                    id="asset-name"
                    value={form.name || ""}
                    onChange={(event) =>
                      setForm((current) => ({
                        ...current,
                        name: event.target.value,
                      }))
                    }
                  />
                </div>
                <div>
                  <Label htmlFor="asset-importance">重要程度</Label>
                  <Select
                    id="asset-importance"
                    value={form.importance || "normal"}
                    onChange={(event) =>
                      setForm((current) => ({
                        ...current,
                        importance: event.target.value as Asset["importance"],
                      }))
                    }
                  >
                    <option value="normal">普通素材</option>
                    <option value="core">核心素材</option>
                  </Select>
                </div>
              </div>
              <JsonEditor
                label="素材档案"
                value={parseJson(form.profile_json, {})}
                onChange={(value) =>
                  setForm((current) => ({
                    ...current,
                    profile_json: value as Record<string, unknown>,
                  }))
                }
                onValidityChange={setProfileValid}
                rows={19}
              />
            </div>
            <div className="space-y-4">
              <div>
                <Label htmlFor="asset-prompt">视觉细节补充</Label>
                <Textarea
                  id="asset-prompt"
                  rows={7}
                  value={form.prompt || ""}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      prompt: event.target.value,
                    }))
                  }
                />
              </div>
              <div>
                <Label htmlFor="asset-negative">负向提示词</Label>
                <Textarea
                  id="asset-negative"
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
              <div>
                <Label htmlFor="asset-consistency">
                  一致性关键词（每行一个）
                </Label>
                <Textarea
                  id="asset-consistency"
                  rows={4}
                  value={parseJson<string[]>(form.consistency_json, []).join("\n")}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      consistency_json: event.target.value
                        .split("\n")
                        .map((item) => item.trim())
                        .filter(Boolean),
                    }))
                  }
                />
              </div>
              <div>
                <Label htmlFor="asset-composition">构图建议</Label>
                <Textarea
                  id="asset-composition"
                  rows={3}
                  value={form.composition || ""}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      composition: event.target.value,
                    }))
                  }
                />
              </div>
            </div>
          </div>
        </Modal>
      </div>
    </ProjectRequired>
  );
}
