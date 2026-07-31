import {
  Check,
  Download,
  Film,
  ImageOff,
  RotateCcw,
  Trash2,
} from "lucide-react";
import type { MediaVersion } from "../types/api";
import { mediaUrl } from "../api/client";
import { cn, formatDate } from "../lib/utils";
import { ImagePreview } from "./image-preview";
import { EmptyState, StaleNotice, StatusBadge } from "./status";
import { Badge, Button } from "./ui";

export interface MediaVersionsProps {
  versions: MediaVersion[];
  mediaKind: "image" | "video";
  alt: string;
  selectingId?: string;
  deletingId?: string;
  onSelect: (version: MediaVersion) => void;
  onDelete: (version: MediaVersion) => void;
  onDownload?: (version: MediaVersion) => void;
}

export function MediaVersions({
  versions,
  mediaKind,
  alt,
  selectingId,
  deletingId,
  onSelect,
  onDelete,
  onDownload,
}: MediaVersionsProps) {
  const current =
    versions.find((version) => version.is_current) ||
    versions.find((version) => version.status === "completed") ||
    null;

  if (!versions.length) {
    return (
      <EmptyState
        compact
        title={mediaKind === "image" ? "还没有图像版本" : "还没有视频版本"}
        description="可以生成新版本，或上传本地文件作为当前版本。"
      />
    );
  }

  return (
    <div className="space-y-3">
      <div className="relative overflow-hidden rounded-lg border border-[#cdc8bd] bg-[#222622]">
        {current && mediaUrl(current) ? (
          mediaKind === "image" ? (
            <ImagePreview
              src={mediaUrl(current)}
              alt={alt}
              imageClassName="aspect-video w-full object-contain"
            />
          ) : (
            <video
              src={mediaUrl(current)}
              className="aspect-video w-full bg-black object-contain"
              controls
              preload="metadata"
            >
              您的浏览器不支持视频播放。
            </video>
          )
        ) : (
          <div className="flex aspect-video items-center justify-center text-white/55">
            {mediaKind === "image" ? (
              <ImageOff className="h-8 w-8" />
            ) : (
              <Film className="h-8 w-8" />
            )}
          </div>
        )}
        {current ? (
          <div className="absolute left-3 top-3 flex flex-wrap gap-2">
            <Badge className="border-white/15 bg-black/60 text-white backdrop-blur">
              当前 · V{String(current.version_no).padStart(2, "0")}
            </Badge>
            <StatusBadge status={current.status} className="backdrop-blur" />
          </div>
        ) : null}
      </div>

      {current?.is_stale ? (
        <StaleNotice reason={current.stale_reason} compact />
      ) : null}
      {current?.warnings?.map((warning) => (
        <StaleNotice key={warning} reason={warning} compact />
      ))}

      <div className="flex gap-2 overflow-x-auto pb-1" aria-label="媒体版本">
        {[...versions]
          .sort((a, b) => b.version_no - a.version_no)
          .map((version) => {
            const url = mediaUrl(version);
            const details = (
              <div className="space-y-1 px-2 py-2">
                <div className="flex items-center justify-between gap-1">
                  <span className="text-xs font-bold">V{version.version_no}</span>
                  {version.is_current ? (
                    <Check className="h-3.5 w-3.5 text-vermilion" />
                  ) : null}
                </div>
                <p className="truncate text-[10px] text-muted">
                  {version.source === "uploaded" ? "本地上传" : version.model || "生成"}
                </p>
                <p className="text-[10px] text-muted">
                  {formatDate(version.created_at)}
                </p>
              </div>
            );
            return (
              <div
                key={version.id}
                className={cn(
                  "relative w-32 shrink-0 overflow-hidden rounded-md border bg-white",
                  version.is_current
                    ? "border-vermilion ring-1 ring-vermilion"
                    : "border-line",
                )}
              >
                {mediaKind === "image" && url ? (
                  <>
                    <ImagePreview
                      src={url}
                      alt={`${alt} 版本 ${version.version_no}`}
                      imageClassName="aspect-video w-full bg-[#252925] object-cover"
                      loading="lazy"
                    />
                    {details}
                  </>
                ) : (
                  <button
                    type="button"
                    className="block w-full text-left"
                    onClick={() => onSelect(version)}
                    disabled={version.is_current || version.status !== "completed"}
                    aria-label={`选择版本 ${version.version_no}`}
                  >
                    <div className="flex aspect-video items-center justify-center bg-[#252925] text-white/60">
                      {mediaKind === "video" ? (
                        <Film className="h-5 w-5" />
                      ) : (
                        <ImageOff className="h-5 w-5" />
                      )}
                    </div>
                    {details}
                  </button>
                )}
                <div className="flex border-t border-line">
                  {!version.is_current ? (
                    <Button
                      variant="ghost"
                      size="sm"
                      className="min-h-7 flex-1 rounded-none px-1"
                      busy={selectingId === version.id}
                      disabled={version.status !== "completed"}
                      onClick={() => onSelect(version)}
                      aria-label={`设为当前版本 ${version.version_no}`}
                    >
                      <RotateCcw className="h-3 w-3" />
                    </Button>
                  ) : null}
                  {onDownload && version.status === "completed" ? (
                    <Button
                      variant="ghost"
                      size="sm"
                      className="min-h-7 flex-1 rounded-none px-1"
                      onClick={() => onDownload(version)}
                      aria-label={`下载版本 ${version.version_no}`}
                    >
                      <Download className="h-3 w-3" />
                    </Button>
                  ) : null}
                  <Button
                    variant="ghost"
                    size="sm"
                    className="min-h-7 flex-1 rounded-none px-1 text-[#9b342c]"
                    busy={deletingId === version.id}
                    disabled={version.is_current}
                    onClick={() => onDelete(version)}
                    aria-label={`删除版本 ${version.version_no}`}
                    title={version.is_current ? "当前版本不能删除" : "删除版本"}
                  >
                    <Trash2 className="h-3 w-3" />
                  </Button>
                </div>
              </div>
            );
          })}
      </div>
    </div>
  );
}
