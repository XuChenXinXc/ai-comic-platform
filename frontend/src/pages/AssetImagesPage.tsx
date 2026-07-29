import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ImagePlus, Upload, WandSparkles } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { api, mediaUrl, queryKeys } from "../api/client";
import { MediaVersions } from "../components/media-versions";
import { ProjectRequired } from "../components/project-required";
import {
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
  StatusBadge,
} from "../components/status";
import { Badge, Button, Card, Tabs } from "../components/ui";
import { useProject } from "../hooks/use-project";
import { formatError } from "../lib/utils";
import type { Asset, AssetType, MediaVersion } from "../types/api";

const typeLabels: Record<AssetType, string> = {
  character: "角色",
  scene: "场景",
  prop: "物品",
};

function AssetImageCard({
  asset,
  selected,
  onToggle,
}: {
  asset: Asset;
  selected: boolean;
  onToggle: (checked: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const query = useQuery({
    queryKey: queryKeys.assetImages(asset.id),
    queryFn: () => api.assets.images.list(asset.id),
  });
  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: queryKeys.assetImages(asset.id) });

  const generateMutation = useMutation({
    mutationFn: () => api.assets.images.generate(asset.id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: queryKeys.jobs(asset.project_id),
      });
      toast.success(`${asset.name} 图片任务已入队`);
    },
    onError: (error) => toast.error(formatError(error)),
  });
  const uploadMutation = useMutation({
    mutationFn: (file: File) => api.assets.images.upload(asset.id, file),
    onSuccess: async () => {
      await invalidate();
      toast.success("图片已上传并设为当前版本");
    },
    onError: (error) => toast.error(formatError(error)),
  });
  const currentMutation = useMutation({
    mutationFn: (version: MediaVersion) =>
      api.assets.images.current(version.id),
    onSuccess: async () => {
      await invalidate();
      toast.success("已切换当前版本");
    },
    onError: (error) => toast.error(formatError(error)),
  });
  const deleteMutation = useMutation({
    mutationFn: (version: MediaVersion) => api.assets.images.remove(version.id),
    onSuccess: async () => {
      await invalidate();
      toast.success("版本已删除");
    },
    onError: (error) => toast.error(formatError(error)),
  });

  return (
    <Card className="overflow-hidden">
      <div className="flex flex-col justify-between gap-3 border-b border-line px-5 py-4 sm:flex-row sm:items-start">
        <div className="flex min-w-0 items-start gap-3">
          <input
            type="checkbox"
            checked={selected}
            onChange={(event) => onToggle(event.target.checked)}
            className="mt-1 accent-vermilion"
            aria-label={`选择 ${asset.name}`}
          />
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="truncate font-bold">{asset.name}</h2>
              {asset.importance === "core" ? (
                <Badge className="border-[#d9ae7f] bg-[#fff3e5] text-[#8b4b1e]">
                  核心
                </Badge>
              ) : null}
              <StatusBadge status={asset.status} />
            </div>
            <p className="mt-1 line-clamp-2 text-xs leading-5 text-muted">
              {asset.prompt || "尚未生成图片提示词，请先在素材页补齐。"}
            </p>
          </div>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          <input
            ref={fileRef}
            type="file"
            accept="image/png,image/jpeg,image/webp"
            className="hidden"
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) uploadMutation.mutate(file);
              event.currentTarget.value = "";
            }}
          />
          <Button
            size="sm"
            busy={uploadMutation.isPending}
            onClick={() => fileRef.current?.click()}
          >
            <Upload className="h-4 w-4" />
            上传
          </Button>
          <Button
            size="sm"
            variant="primary"
            busy={generateMutation.isPending}
            disabled={!asset.prompt}
            onClick={() => generateMutation.mutate()}
          >
            <WandSparkles className="h-4 w-4" />
            生成新版本
          </Button>
        </div>
      </div>
      <div className="p-5">
        {query.isLoading ? <LoadingState label="正在读取图片版本…" /> : null}
        {query.error ? (
          <ErrorState
            title="图片版本加载失败"
            message={formatError(query.error)}
            onRetry={() => void query.refetch()}
          />
        ) : null}
        {query.data ? (
          <MediaVersions
            versions={query.data}
            mediaKind="image"
            alt={`${asset.name} 参考图`}
            selectingId={
              currentMutation.isPending ? currentMutation.variables?.id : undefined
            }
            deletingId={
              deleteMutation.isPending ? deleteMutation.variables?.id : undefined
            }
            onSelect={(version) => {
              if (!version.is_current) currentMutation.mutate(version);
            }}
            onDelete={(version) => {
              if (
                window.confirm(
                  `确认删除 ${asset.name} 的 V${version.version_no} 图片吗？`,
                )
              ) {
                deleteMutation.mutate(version);
              }
            }}
            onDownload={(version) => {
              const url = mediaUrl(version);
              if (!url) return;
              const anchor = document.createElement("a");
              anchor.href = url;
              anchor.download = `${asset.name}-v${version.version_no}.png`;
              anchor.click();
            }}
          />
        ) : null}
      </div>
    </Card>
  );
}

export function AssetImagesPage() {
  const queryClient = useQueryClient();
  const { activeProjectId } = useProject();
  const [type, setType] = useState<AssetType>("character");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const query = useQuery({
    queryKey: queryKeys.assets(activeProjectId),
    queryFn: () => api.assets.list(activeProjectId),
    enabled: Boolean(activeProjectId),
  });
  const assets = useMemo(() => query.data || [], [query.data]);
  const filtered = useMemo(
    () => assets.filter((asset) => asset.asset_type === type),
    [assets, type],
  );

  useEffect(() => {
    setSelected(new Set());
  }, [activeProjectId]);

  const batchMutation = useMutation({
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
            const asset = assets.find((item) => item.id === target_id);
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

  return (
    <ProjectRequired>
      <div>
        <PageHeader
          eyebrow="Step 04 / Reference images"
          title="素材图片"
          description="建立角色、场景与物品的统一参考库。生成和上传都会保留历史版本，成功版本自动成为当前版本。"
          actions={
            <Button
              variant="primary"
              busy={batchMutation.isPending}
              disabled={!filtered.length}
              onClick={() => batchMutation.mutate()}
            >
              <ImagePlus className="h-4 w-4" />
              {selected.size
                ? `生成所选（${selected.size}）`
                : `批量生成${typeLabels[type]}图`}
            </Button>
          }
        />

        <div className="mb-6 flex flex-col justify-between gap-3 lg:flex-row lg:items-center">
          <Tabs
            value={type}
            onChange={(value) => {
              setType(value as AssetType);
              setSelected(new Set());
            }}
            items={(Object.keys(typeLabels) as AssetType[]).map((item) => ({
              value: item,
              label: typeLabels[item],
              count: assets.filter((asset) => asset.asset_type === item).length,
            }))}
          />
          {filtered.length ? (
            <label className="flex items-center gap-2 text-xs font-semibold text-muted">
              <input
                type="checkbox"
                className="accent-vermilion"
                checked={
                  filtered.length > 0 &&
                  filtered.every((asset) => selected.has(asset.id))
                }
                onChange={(event) =>
                  setSelected(
                    event.target.checked
                      ? new Set(filtered.map((asset) => asset.id))
                      : new Set(),
                  )
                }
              />
              全选当前分类
            </label>
          ) : null}
        </div>

        {query.isLoading ? <LoadingState label="正在读取素材图片…" /> : null}
        {query.error ? (
          <ErrorState
            message={formatError(query.error)}
            onRetry={() => void query.refetch()}
          />
        ) : null}
        {!query.isLoading && !query.error && !filtered.length ? (
          <EmptyState
            title={`没有可生成的${typeLabels[type]}素材`}
            description="请先在“素材与提示词”页面提取或新增素材，并生成图片提示词。"
          />
        ) : null}

        <div className="grid gap-5 2xl:grid-cols-2">
          {filtered.map((asset) => (
            <AssetImageCard
              key={asset.id}
              asset={asset}
              selected={selected.has(asset.id)}
              onToggle={(checked) =>
                setSelected((current) => {
                  const next = new Set(current);
                  if (checked) next.add(asset.id);
                  else next.delete(asset.id);
                  return next;
                })
              }
            />
          ))}
        </div>
      </div>
    </ProjectRequired>
  );
}
