import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Save, Upload, WandSparkles } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { api, mediaUrl, queryKeys } from "../api/client";
import { formatError, parseJson } from "../lib/utils";
import type {
  Asset,
  MediaVersion,
  ReferenceBinding,
  Shot,
} from "../types/api";
import { MediaVersions } from "./media-versions";
import {
  ErrorState,
  LoadingState,
  StaleNotice,
  StatusBadge,
} from "./status";
import { Badge, Button, Card, Label, Textarea } from "./ui";

export function ShotMediaCard({
  shot,
  kind,
  assetMap,
  selected,
  onToggle,
}: {
  shot: Shot;
  kind: "keyframe" | "video";
  assetMap: Map<string, Asset>;
  selected: boolean;
  onToggle: (checked: boolean) => void;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [prompt, setPrompt] = useState(
    kind === "keyframe" ? shot.image_prompt : shot.video_prompt,
  );
  const queryKey =
    kind === "keyframe"
      ? queryKeys.keyframes(shot.id)
      : queryKeys.videos(shot.id);
  const query = useQuery({
    queryKey,
    queryFn: () =>
      kind === "keyframe"
        ? api.shots.keyframes.list(shot.id)
        : api.shots.videos.list(shot.id),
  });
  const keyframesQuery = useQuery({
    queryKey: queryKeys.keyframes(shot.id),
    queryFn: () => api.shots.keyframes.list(shot.id),
    enabled: kind === "video",
  });

  useEffect(() => {
    setPrompt(kind === "keyframe" ? shot.image_prompt : shot.video_prompt);
  }, [kind, shot.image_prompt, shot.video_prompt]);

  const invalidate = () => queryClient.invalidateQueries({ queryKey });

  const generateMutation = useMutation({
    mutationFn: () =>
      kind === "keyframe"
        ? api.shots.keyframes.generate(shot.id)
        : api.shots.videos.generate(shot.id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: queryKeys.jobs(shot.project_id),
      });
      toast.success(
        `分镜 ${shot.shot_no} ${kind === "keyframe" ? "关键帧" : "视频"}任务已入队`,
      );
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const uploadMutation = useMutation({
    mutationFn: (file: File) =>
      kind === "keyframe"
        ? api.shots.keyframes.upload(shot.id, file)
        : api.shots.videos.upload(shot.id, file),
    onSuccess: async () => {
      await invalidate();
      if (kind === "keyframe") {
        await queryClient.invalidateQueries({
          queryKey: queryKeys.keyframes(shot.id),
        });
      }
      toast.success("文件已上传并设为当前版本");
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const currentMutation = useMutation({
    mutationFn: (version: MediaVersion) =>
      kind === "keyframe"
        ? api.shots.keyframes.current(version.id)
        : api.shots.videos.current(version.id),
    onSuccess: async () => {
      await invalidate();
      toast.success("已切换当前版本");
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const deleteMutation = useMutation({
    mutationFn: (version: MediaVersion) =>
      kind === "keyframe"
        ? api.shots.keyframes.remove(version.id)
        : api.shots.videos.remove(version.id),
    onSuccess: async () => {
      await invalidate();
      toast.success("版本已删除");
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const promptMutation = useMutation({
    mutationFn: () =>
      api.shots.update(shot.id, {
        [kind === "keyframe" ? "image_prompt" : "video_prompt"]: prompt,
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: queryKeys.shots(shot.project_id),
      });
      toast.success("提示词已保存，旧媒体会显示来源变化");
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const characterNames = parseJson<string[]>(shot.character_ids_json, [])
    .map((id) => assetMap.get(id)?.name)
    .filter(Boolean);
  const sceneName = shot.scene_asset_id
    ? assetMap.get(shot.scene_asset_id)?.name
    : shot.scene_name;
  const bindings = parseJson<ReferenceBinding[]>(
    shot.reference_bindings_json,
    [],
  );
  const currentKeyframe = (keyframesQuery.data || []).find(
    (version) => version.is_current && version.status === "completed",
  );
  const promptMissing = !prompt.trim();
  const videoMissingKeyframe = kind === "video" && !currentKeyframe;

  return (
    <Card className="overflow-hidden">
      <div className="flex flex-col gap-3 border-b border-line px-5 py-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex min-w-0 items-start gap-3">
          <label className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded bg-sidebar text-xs font-bold text-white">
            <input
              type="checkbox"
              className="sr-only"
              checked={selected}
              onChange={(event) => onToggle(event.target.checked)}
              aria-label={`选择分镜 ${shot.shot_no}`}
            />
            {String(shot.shot_no).padStart(2, "0")}
          </label>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="truncate font-bold">{sceneName || "未绑定场景"}</h2>
              <StatusBadge status={shot.status} />
              <Badge>{shot.duration_sec}s</Badge>
            </div>
            <p className="mt-1 line-clamp-2 text-xs leading-5 text-muted">
              {shot.action || shot.frame_description}
            </p>
            {characterNames.length ? (
              <p className="mt-1 truncate text-[11px] text-muted">
                {characterNames.join(" · ")}
              </p>
            ) : null}
          </div>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          <input
            ref={fileRef}
            type="file"
            accept={
              kind === "keyframe"
                ? "image/png,image/jpeg,image/webp"
                : "video/mp4,video/webm,video/quicktime"
            }
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
            上传替换
          </Button>
          <Button
            size="sm"
            variant="primary"
            busy={generateMutation.isPending}
            disabled={promptMissing || videoMissingKeyframe}
            onClick={() => generateMutation.mutate()}
          >
            <WandSparkles className="h-4 w-4" />
            生成新版本
          </Button>
        </div>
      </div>

      <div className="grid xl:grid-cols-[minmax(280px,0.82fr)_minmax(0,1.35fr)]">
        <div className="space-y-4 border-b border-line p-5 xl:border-b-0 xl:border-r">
          <div>
            <div className="mb-2 flex items-center justify-between gap-2">
              <Label className="mb-0">
                {kind === "keyframe" ? "关键帧图片提示词" : "视频执行提示词"}
              </Label>
              <Button
                size="sm"
                variant="ghost"
                busy={promptMutation.isPending}
                disabled={
                  prompt ===
                  (kind === "keyframe" ? shot.image_prompt : shot.video_prompt)
                }
                onClick={() => promptMutation.mutate()}
              >
                <Save className="h-3.5 w-3.5" />
                保存
              </Button>
            </div>
            <Textarea
              rows={kind === "keyframe" ? 9 : 12}
              value={prompt}
              placeholder={
                kind === "keyframe"
                  ? "先在分镜页生成提示词，或直接在这里编辑…"
                  : "按时间段描述动作、镜头、环境动态与禁止事项…"
              }
              onChange={(event) => setPrompt(event.target.value)}
            />
          </div>
          {shot.negative_prompt ? (
            <div className="rounded-md border border-line bg-white/55 p-3">
              <p className="text-[10px] font-bold uppercase tracking-[0.14em] text-muted">
                禁止事项
              </p>
              <p className="mt-2 line-clamp-4 text-xs leading-5 text-[#454842]">
                {shot.negative_prompt}
              </p>
            </div>
          ) : null}
          {kind === "keyframe" && bindings.length === 0 ? (
            <StaleNotice
              compact
              reason="尚未绑定素材参考图，将以纯文字降级生成；可到分镜编辑中补充明确版本绑定。"
            />
          ) : null}
          {videoMissingKeyframe && !keyframesQuery.isLoading ? (
            <div>
              <StaleNotice
                compact
                reason="缺少已完成的当前关键帧，视频生成暂不可用。"
              />
              <Button
                className="mt-2"
                size="sm"
                onClick={() => navigate("/keyframes")}
              >
                前往生成关键帧
              </Button>
            </div>
          ) : null}
        </div>

        <div className="min-w-0 p-5">
          {query.isLoading ? (
            <LoadingState
              label={`正在读取${kind === "keyframe" ? "关键帧" : "视频"}版本…`}
            />
          ) : null}
          {query.error ? (
            <ErrorState
              title="媒体版本加载失败"
              message={formatError(query.error)}
              onRetry={() => void query.refetch()}
            />
          ) : null}
          {query.data ? (
            <MediaVersions
              versions={query.data}
              mediaKind={kind === "keyframe" ? "image" : "video"}
              alt={`分镜 ${shot.shot_no} ${kind === "keyframe" ? "关键帧" : "视频"}`}
              selectingId={
                currentMutation.isPending
                  ? currentMutation.variables?.id
                  : undefined
              }
              deletingId={
                deleteMutation.isPending
                  ? deleteMutation.variables?.id
                  : undefined
              }
              onSelect={(version) => {
                if (!version.is_current) currentMutation.mutate(version);
              }}
              onDelete={(version) => {
                if (
                  window.confirm(
                    `确认删除分镜 ${shot.shot_no} 的 V${version.version_no} ${
                      kind === "keyframe" ? "关键帧" : "视频"
                    }吗？`,
                  )
                ) {
                  deleteMutation.mutate(version);
                }
              }}
              onDownload={(version) => {
                if (kind === "video") {
                  void api.shots.videos
                    .download(
                      version.id,
                      `shot-${String(shot.shot_no).padStart(3, "0")}-v${version.version_no}.mp4`,
                    )
                    .catch((error) => toast.error(formatError(error)));
                  return;
                }
                const url = mediaUrl(version);
                if (!url) return;
                const anchor = document.createElement("a");
                anchor.href = url;
                anchor.download = `shot-${String(shot.shot_no).padStart(3, "0")}-v${version.version_no}.png`;
                anchor.click();
              }}
            />
          ) : null}
        </div>
      </div>
    </Card>
  );
}
