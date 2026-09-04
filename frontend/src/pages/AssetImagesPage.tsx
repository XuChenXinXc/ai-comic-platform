import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil, Upload, WandSparkles } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Navigate } from "react-router";
import { toast } from "sonner";
import { api, mediaUrl, queryKeys } from "../api/client";
import { MediaVersions } from "../components/media-versions";
import { ErrorState, LoadingState } from "../components/status";
import { Button, Modal, Textarea } from "../components/ui";
import { formatError, parseJson } from "../lib/utils";
import type { Asset, MediaVersion } from "../types/api";

const visualProfileFields: Record<Asset["asset_type"], string[]> = {
  character: [
    "appearance",
    "face",
    "hair",
    "eyes",
    "body",
    "costume",
    "immutable_features",
  ],
  scene: [
    "layout",
    "architecture",
    "space_keywords",
    "regions",
    "furnishings",
    "main_view",
  ],
  prop: [
    "description",
    "silhouette",
    "front_structure",
    "material",
    "special_features",
  ],
};

function hasVisualValue(value: unknown): boolean {
  if (typeof value === "string") return Boolean(value.trim());
  if (Array.isArray(value)) return value.some(hasVisualValue);
  if (value && typeof value === "object") {
    return Object.values(value).some(hasVisualValue);
  }
  return value !== null && value !== undefined;
}

function canGenerateAssetImage(asset: Asset): boolean {
  if (hasVisualValue(asset.prompt)) return true;
  const profile = asset.profile ?? parseJson<Record<string, unknown>>(asset.profile_json, {});
  // 未知素材类型时回退为空数组，避免整张卡片渲染崩溃。
  const fields = visualProfileFields[asset.asset_type] ?? [];
  return fields.some((field) => hasVisualValue(profile[field]));
}

export function AssetImagePanel({ asset }: { asset: Asset }) {
  const queryClient = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [editingPrompt, setEditingPrompt] = useState(false);
  const [referenceMode, setReferenceMode] = useState(false);
  const canGenerate = canGenerateAssetImage(asset);
  const query = useQuery({
    queryKey: queryKeys.assetImages(asset.id),
    queryFn: () => api.assets.images.list(asset.id),
  });
  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: queryKeys.assetImages(asset.id) });
  // 上传/设当前/删除都会更新素材的当前图片；素材卡片主图来自 assets 查询，
  // 需要同时失效两者，否则素材列表中的主图不会刷新。
  const invalidateWithAssets = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: queryKeys.assetImages(asset.id) }),
      queryClient.invalidateQueries({
        queryKey: queryKeys.assets(asset.project_id),
      }),
    ]);
  const currentVersion = query.data?.find((item) => item.is_current);
  const currentPrompt = asset.prompt_override || currentVersion?.prompt_snapshot || "";

  const promptMutation = useMutation({
    mutationFn: (value: string) =>
      api.assets.update(asset.id, { prompt_override: value }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: queryKeys.assets(asset.project_id),
      });
      toast.success("已保存图片提示词，下次生成将使用该提示词");
    },
    onError: (error) => toast.error(formatError(error)),
  });
  const savePrompt = (value: string) => {
    const next = value.trim();
    promptMutation.mutate(next, {
      onSuccess: () => {
        setEditingPrompt(false);
        if (next) invalidate();
      },
    });
  };

  const generateMutation = useMutation({
    mutationFn: () =>
      api.assets.images.generate(asset.id, { reference_mode: referenceMode }),
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
      await invalidateWithAssets();
      toast.success("图片已上传并设为当前版本");
    },
    onError: (error) => toast.error(formatError(error)),
  });
  const currentMutation = useMutation({
    mutationFn: (version: MediaVersion) =>
      api.assets.images.current(version.id),
    onSuccess: async () => {
      await invalidateWithAssets();
      toast.success("已切换当前版本");
    },
    onError: (error) => toast.error(formatError(error)),
  });
  const deleteMutation = useMutation({
    mutationFn: (version: MediaVersion) => api.assets.images.remove(version.id),
    onSuccess: async () => {
      await invalidateWithAssets();
      toast.success("版本已删除");
    },
    onError: (error) => toast.error(formatError(error)),
  });

  return (
    <div className="border-t border-line bg-[#f8f6f0] p-5">
      <div className="mb-4 flex flex-col justify-between gap-3 sm:flex-row sm:items-center">
        <div>
          <p className="text-xs font-bold uppercase tracking-[0.14em] text-muted">
            素材图片与历史版本
          </p>
          <p className="mt-1 text-xs leading-5 text-muted">
            生成和上传都会保留历史版本，成功版本自动成为当前版本。
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          <label
            className="flex cursor-pointer items-center gap-1.5 text-xs text-muted"
            title="单张干净设定图：无多视图展板、无资料卡文字，适合写意水墨等与白底展板冲突的画风"
          >
            <input
              type="checkbox"
              checked={referenceMode}
              onChange={(event) => setReferenceMode(event.target.checked)}
              className="h-4 w-4 accent-[#d95c3a]"
            />
            单图参考模式
          </label>
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
            disabled={!canGenerate}
            onClick={() => generateMutation.mutate()}
          >
            <WandSparkles className="h-4 w-4" />
            生成新版本
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setEditingPrompt(true)}
          >
            <Pencil className="h-4 w-4" />
            编辑图片提示词
          </Button>
        </div>
      </div>

      {asset.prompt_override ? (
        <div className="mb-3 rounded-md border border-line bg-white/60 p-3">
          <p className="text-[10px] font-bold uppercase tracking-[0.14em] text-muted">
            已保存的图片提示词覆盖
          </p>
          <p className="mt-1 max-h-32 overflow-y-auto whitespace-pre-wrap text-xs leading-5 text-[#454842]">
            {asset.prompt_override}
          </p>
        </div>
      ) : null}

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
            if (version.status === "failed") {
              deleteMutation.mutate(version);
              return;
            }
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

      <PromptOverrideModal
        open={editingPrompt}
        title={`编辑 ${asset.name} 图片提示词`}
        initial={currentPrompt}
        busy={promptMutation.isPending}
        onClose={() => setEditingPrompt(false)}
        onSave={savePrompt}
      />
    </div>
  );
}

function PromptOverrideModal({
  open,
  title,
  initial,
  busy,
  onClose,
  onSave,
}: {
  open: boolean;
  title: string;
  initial: string;
  busy: boolean;
  onClose: () => void;
  onSave: (value: string) => void;
}) {
  const [value, setValue] = useState(initial);
  useEffect(() => {
    if (open) setValue(initial);
  }, [open, initial]);
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={title}
      description="保存后，下次生成新版本将直接使用该提示词，不再按模板自动编译；清空并保存可恢复自动编译。"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            取消
          </Button>
          <Button variant="primary" busy={busy} onClick={() => onSave(value)}>
            保存
          </Button>
        </>
      }
    >
      <Textarea
        value={value}
        rows={12}
        onChange={(event) => setValue(event.target.value)}
        placeholder="输入素材图提示词…"
      />
    </Modal>
  );
}

export function AssetImagesPage() {
  return <Navigate to="/assets" replace />;
}
