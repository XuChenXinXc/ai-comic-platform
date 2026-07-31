import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Upload, WandSparkles } from "lucide-react";
import { useRef } from "react";
import { Navigate } from "react-router-dom";
import { toast } from "sonner";
import { api, mediaUrl, queryKeys } from "../api/client";
import { MediaVersions } from "../components/media-versions";
import { ErrorState, LoadingState } from "../components/status";
import { Button } from "../components/ui";
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
  return visualProfileFields[asset.asset_type].some((field) =>
    hasVisualValue(profile[field]),
  );
}

export function AssetImagePanel({ asset }: { asset: Asset }) {
  const queryClient = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const canGenerate = canGenerateAssetImage(asset);
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
            disabled={!canGenerate}
            onClick={() => generateMutation.mutate()}
          >
            <WandSparkles className="h-4 w-4" />
            生成新版本
          </Button>
        </div>
      </div>

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
  );
}

export function AssetImagesPage() {
  return <Navigate to="/assets" replace />;
}
