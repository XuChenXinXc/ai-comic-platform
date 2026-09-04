import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ImageOff, Pencil, Trash2, WandSparkles } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { api, mediaUrl, queryKeys } from "../api/client";
import { ImagePreview } from "./image-preview";
import { Button, Modal, Textarea } from "./ui";
import { formatError } from "../lib/utils";
import type { Asset, SceneView } from "../types/api";

/** 生成中的活动状态集合；其余非 completed 状态（failed 等）按失败展示。 */
const ACTIVE_STATUSES = new Set([
  "queued",
  "running",
  "waiting_provider",
  "downloading",
]);

/** 场景素材的 Scene View 面板：展示 / 生成图片 / 编辑视角提示词 / 删除视图。 */
export function SceneViewPanel({ asset }: { asset: Asset }) {
  const queryClient = useQueryClient();
  const views = asset.scene_views ?? [];
  const [editingView, setEditingView] = useState<SceneView | null>(null);

  // 场景视图数据挂在 assets 查询上，失效 assets 即可刷新 scene_views
  const invalidate = () =>
    queryClient.invalidateQueries({
      queryKey: queryKeys.assets(asset.project_id),
    });

  const ensureMutation = useMutation({
    mutationFn: () => api.assets.sceneViews.ensure(asset.id),
    onSuccess: async () => {
      await invalidate();
      toast.success("默认场景视图已生成");
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const updateMutation = useMutation({
    mutationFn: (payload: { viewId: string; prompt: string }) =>
      api.assets.sceneViews.update(payload.viewId, { prompt: payload.prompt }),
    onSuccess: async () => {
      await invalidate();
      toast.success("视角提示词已保存");
      setEditingView(null);
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const generateMutation = useMutation({
    mutationFn: (view: SceneView) =>
      api.assets.sceneViews.generate(asset.id, view.view_key),
    onSuccess: async () => {
      // 素材图与视图图都挂在 job 轮询失效列表上，任务完成后自动刷新
      await queryClient.invalidateQueries({
        queryKey: queryKeys.jobs(asset.project_id),
      });
      toast.success("场景视图图片任务已入队");
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const removeMutation = useMutation({
    mutationFn: (viewId: string) => api.assets.sceneViews.remove(viewId),
    onSuccess: async () => {
      await invalidate();
      toast.success("场景视图已删除");
    },
    onError: (error) => toast.error(formatError(error)),
  });

  return (
    <div className="border-t border-line bg-[#f8f6f0] p-5">
      <div className="mb-4 flex flex-col justify-between gap-3 sm:flex-row sm:items-center">
        <div>
          <p className="text-xs font-bold uppercase tracking-[0.14em] text-muted">
            场景视图（Scene Views）
          </p>
          <p className="mt-1 text-xs leading-5 text-muted">
            每个视图是一张独立的场景参考图；MASTER 无参考图，其余视图以 MASTER 图为参考生成。
          </p>
        </div>
        {views.length ? (
          <Button
            size="sm"
            busy={ensureMutation.isPending}
            disabled={ensureMutation.isPending}
            onClick={() => ensureMutation.mutate()}
          >
            <WandSparkles className="h-4 w-4" />
            补齐默认视图
          </Button>
        ) : null}
      </div>

      {views.length ? (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
          {views.map((view) => {
            const url = mediaUrl(view.current_image);
            const imageReady = view.current_image?.status === "completed";
            // 仅活动状态显示「生成中」；失败/中断等终态显示「失败」，避免误导。
            const status = view.current_image?.status ?? "";
            const generating = Boolean(
              view.current_image && ACTIVE_STATUSES.has(status),
            );
            const failed = Boolean(
              view.current_image && status !== "completed" && !ACTIVE_STATUSES.has(status),
            );
            const isMaster = view.view_key === "master";
            const viewName = isMaster ? "母版" : view.name || view.view_key;
            const summary = (view.prompt_override || view.prompt || "").trim();
            const deletable = !isMaster && views.length > 1;
            return (
              <div
                key={view.id}
                className="overflow-hidden rounded-md border border-line bg-white"
              >
                <div className="relative aspect-[4/3] bg-[#252925]">
                  {url ? (
                    <ImagePreview
                      src={url}
                      alt={`${asset.name}${viewName}视图`}
                      buttonClassName="h-full"
                      imageClassName="h-full w-full object-cover"
                      loading="lazy"
                    />
                  ) : (
                    <div className="flex h-full items-center justify-center text-white/35">
                      <ImageOff className="h-6 w-6" />
                    </div>
                  )}
                  <span className="absolute left-2 top-2 rounded border border-white/20 bg-black/55 px-1.5 py-0.5 text-[10px] font-bold text-white">
                    {view.view_key}
                  </span>
                  {generating ? (
                    <span className="absolute right-2 top-2 rounded bg-black/55 px-1.5 py-0.5 text-[10px] text-white">
                      生成中
                    </span>
                  ) : null}
                  {failed ? (
                    <span className="absolute right-2 top-2 rounded bg-black/55 px-1.5 py-0.5 text-[10px] text-red-300">
                      生成失败
                    </span>
                  ) : null}
                </div>
                <div className="p-3">
                  <div className="flex items-center justify-between gap-2">
                    <p className="truncate text-xs font-bold" title={viewName}>
                      {viewName}
                    </p>
                    {imageReady ? (
                      <span className="rounded-full border border-line bg-white px-1.5 py-0.5 text-[10px] text-muted">
                        已生成
                      </span>
                    ) : null}
                  </div>
                  <p
                    className="mt-1 line-clamp-2 text-[11px] leading-4 text-muted"
                    title={summary}
                  >
                    {summary || "未填写视角提示词"}
                  </p>
                  <div className="mt-2 flex flex-wrap gap-1">
                    <Button
                      size="sm"
                      variant="ghost"
                      className="min-h-7 px-2 text-xs"
                      busy={
                        generateMutation.isPending &&
                        generateMutation.variables?.id === view.id
                      }
                      disabled={imageReady}
                      title={
                        imageReady
                          ? "该视图已有图片，可在下方素材图片区管理历史版本"
                          : undefined
                      }
                      onClick={() => generateMutation.mutate(view)}
                    >
                      <WandSparkles className="h-3 w-3" />
                      {isMaster ? "生成母版图" : "生成图片"}
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      className="min-h-7 px-2 text-xs"
                      onClick={() => setEditingView(view)}
                    >
                      <Pencil className="h-3 w-3" />
                      编辑提示词
                    </Button>
                    {deletable ? (
                      <Button
                        size="sm"
                        variant="ghost"
                        className="min-h-7 px-2 text-xs text-[#9b342c]"
                        busy={
                          removeMutation.isPending &&
                          removeMutation.variables === view.id
                        }
                        onClick={() => {
                          if (
                            window.confirm(
                              `确认删除“${viewName}”视图吗？若分镜正在引用该视图，引用会被自动置空。`,
                            )
                          ) {
                            removeMutation.mutate(view.id);
                          }
                        }}
                      >
                        <Trash2 className="h-3 w-3" />
                        删除
                      </Button>
                    ) : null}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="flex flex-col items-center justify-center gap-3 rounded-md border border-line bg-white/60 py-8">
          <ImageOff className="h-6 w-6 text-muted" />
          <p className="text-sm text-muted">尚未生成视图</p>
          <Button
            size="sm"
            variant="primary"
            busy={ensureMutation.isPending}
            onClick={() => ensureMutation.mutate()}
          >
            <WandSparkles className="h-4 w-4" />
            生成默认视图
          </Button>
        </div>
      )}

      <ViewPromptModal
        open={Boolean(editingView)}
        onClose={() => setEditingView(null)}
        view={editingView}
        busy={updateMutation.isPending}
        onSave={(prompt) => {
          if (editingView) {
            updateMutation.mutate({ viewId: editingView.id, prompt });
          }
        }}
      />
    </div>
  );
}

/** 编辑某个 Scene View 的视角提示词的小弹窗。 */
function ViewPromptModal({
  open,
  onClose,
  view,
  busy,
  onSave,
}: {
  open: boolean;
  onClose: () => void;
  view: SceneView | null;
  busy: boolean;
  onSave: (prompt: string) => void;
}) {
  const [value, setValue] = useState(view?.prompt ?? "");
  useEffect(() => {
    if (open) setValue(view?.prompt ?? "");
  }, [open, view]);
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={view ? `编辑「${view.name}」视角提示词` : "编辑视角提示词"}
      description="视角附加提示词会与场景基础提示词合并，用于生成该视角的参考图。"
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
        rows={6}
        onChange={(event) => setValue(event.target.value)}
        placeholder="输入该视角的附加提示词…"
      />
    </Modal>
  );
}