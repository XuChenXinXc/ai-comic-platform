import {
  AlertTriangle,
  Check,
  Download,
  Film,
  ImageOff,
  RotateCcw,
  Trash2,
} from "lucide-react";
import type { MediaVersion } from "../types/api";
import { mediaUrl } from "../api/client";
import { cn, formatDate, parseJson } from "../lib/utils";
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
  const visibleVersions = versions.filter((version) => version.status !== "failed");
  const failedVersions = versions
    .filter((version) => version.status === "failed")
    .sort((a, b) => b.version_no - a.version_no);
  const current =
    visibleVersions.find((version) => version.is_current) ||
    visibleVersions.find((version) => version.status === "completed") ||
    null;
  // 关键帧视觉质检结果（后端写入 provenance_json.keyframe_qa）
  const qa = current
    ? parseJson<{
        keyframe_qa?: {
          passed?: boolean;
          score?: number;
          issues?: string[];
        };
      }>(current.provenance_json ?? "", {}).keyframe_qa
    : null;

  if (!versions.length) {
    return (
      <EmptyState
        compact
        title={mediaKind === "image" ? "还没有图像版本" : "还没有视频版本"}
        description="可以生成新版本，或上传本地文件作为当前版本。"
      />
    );
  }

  const failureNotice = failedVersions.length ? (
    <div className="space-y-2">
      {failedVersions.map((version) => (
        <div
          key={version.id}
          role="alert"
          className="rounded-xl border border-red-200 bg-red-50 px-3 py-2 text-[#c93c30] dark:border-red-500/25 dark:bg-red-500/10 dark:text-red-300"
        >
          <div className="flex gap-2">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <div className="min-w-0 flex-1">
              <p className="text-xs font-bold">V{version.version_no} 生成失败</p>
              <p className="mt-0.5 break-words text-xs leading-5">
                {version.error || "生成失败，请在任务中心查看详情。"}
              </p>
            </div>
          </div>
          <div className="mt-2 flex justify-end">
            <Button
              variant="ghost"
              size="sm"
              className="min-h-7 px-2 text-[#c93c30] dark:text-red-400"
              busy={deletingId === version.id}
              onClick={() => onDelete(version)}
              aria-label={`删除失败版本 ${version.version_no}`}
              title="删除失败版本"
            >
              <Trash2 className="h-3 w-3" />
            </Button>
          </div>
        </div>
      ))}
    </div>
  ) : null;

  if (!visibleVersions.length) return <div className="space-y-3">{failureNotice}</div>;

  return (
    <div className="space-y-3">
      {failureNotice}
      <div className="relative overflow-hidden rounded-2xl border border-black/[0.06] bg-[#0b0b0d] dark:border-white/10">
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
            {qa && typeof qa.score === "number" ? (
              <Badge
                className={cn(
                  "border-white/15 text-white backdrop-blur",
                  qa.passed ? "bg-emerald-700/80" : "bg-red-700/80",
                )}
                title={qa.issues?.join("；") || ""}
              >
                {qa.passed ? "质检通过" : "质检未通过"} · {qa.score}
              </Badge>
            ) : null}
            <StatusBadge status={current.status} className="backdrop-blur" />
          </div>
        ) : null}
      </div>

      {qa && !qa.passed && qa.issues?.length ? (
        <div
          role="alert"
          className="rounded-xl border border-amber-300 bg-amber-50 px-3 py-2 text-amber-900 dark:border-amber-500/25 dark:bg-amber-500/10 dark:text-amber-300"
        >
          <p className="text-xs font-bold">质检发现的问题</p>
          <ul className="mt-1 list-disc space-y-0.5 pl-4 text-xs leading-5">
            {qa.issues.map((issue) => (
              <li key={issue}>{issue}</li>
            ))}
          </ul>
          <p className="mt-1 text-[11px] text-muted">
            已自动换 seed 重新生成；仍不理想可点击「生成新版本」重试。
          </p>
        </div>
      ) : null}

      {current?.is_stale ? (
        <StaleNotice reason={current.stale_reason} compact />
      ) : null}
      {current?.warnings?.map((warning) => (
        <StaleNotice key={warning} reason={warning} compact />
      ))}

      <div className="flex gap-2 overflow-x-auto pb-1" aria-label="媒体版本">
        {[...visibleVersions]
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
                  "relative w-32 shrink-0 overflow-hidden rounded-xl border bg-paper",
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
                      imageClassName="aspect-video w-full bg-[#0b0b0d] object-cover"
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
                    <div className="flex aspect-video items-center justify-center bg-[#0b0b0d] text-white/60">
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
                    className="min-h-7 flex-1 rounded-none px-1 text-[#c93c30] dark:text-red-400"
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
