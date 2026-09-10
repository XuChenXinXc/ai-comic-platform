/**
 * 章节选择器：显示当前章节、支持切换与新建。
 * 挂在需要章节维度的页面顶部（写作、分镜、关键帧、视频）。
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { BookOpen, ChevronDown, ChevronUp, Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { api, queryKeys } from "../api/client";
import { useChapter } from "../hooks/use-chapter";
import { useProject } from "../hooks/use-project";
import { cn, formatError } from "../lib/utils";
import { Button } from "./ui";

export function ChapterSelector() {
  const queryClient = useQueryClient();
  const { activeProjectId } = useProject();
  const {
    chapters,
    activeChapter,
    activeChapterId,
    selectChapter,
    refetch,
  } = useChapter();
  const [open, setOpen] = useState(false);

  const createMutation = useMutation({
    mutationFn: () =>
      api.chapters.create(activeProjectId, {
        title: `第${chapters.length + 1}章`,
      }),
    onSuccess: async (chapter) => {
      await queryClient.invalidateQueries({
        queryKey: queryKeys.chapters(activeProjectId),
      });
      selectChapter(chapter.id);
      setOpen(false);
      toast.success(`已创建「${chapter.title}」`);
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const reorderMutation = useMutation({
    mutationFn: (ids: string[]) =>
      api.chapters.reorder(activeProjectId, ids),
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: queryKeys.chapters(activeProjectId),
      });
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const deleteMutation = useMutation({
    mutationFn: async (chapterId: string) => {
      const preview = await api.chapters.deletePreview(chapterId);
      const message =
        `确认删除第${preview.chapter_no}章「${preview.title || "未命名"}」？\n` +
        `将删除 ${preview.shot_count} 个分镜、${preview.keyframe_count} 张关键帧、` +
        `${preview.video_count} 个视频及其媒体文件。\n` +
        "素材库属于整本小说，不会被删除。";
      if (!window.confirm(message)) return { deleted: false };
      await api.chapters.remove(chapterId);
      return { deleted: true };
    },
    onSuccess: async (result) => {
      if (!result.deleted) return;
      await queryClient.invalidateQueries({
        queryKey: queryKeys.chapters(activeProjectId),
      });
      refetch();
      toast.success("章节已删除");
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const moveChapter = (index: number, direction: -1 | 1) => {
    const target = index + direction;
    if (target < 0 || target >= chapters.length) return;
    const next = [...chapters];
    [next[index], next[target]] = [next[target], next[index]];
    reorderMutation.mutate(next.map((item) => item.id));
  };

  if (!activeProjectId) return null;

  if (!chapters.length) {
    return (
      <Button
        variant="secondary"
        busy={createMutation.isPending}
        onClick={() => createMutation.mutate()}
      >
        <Plus className="h-4 w-4" />
        新建第一章
      </Button>
    );
  }

  return (
    <div className="relative">
      <button
        type="button"
        className="flex h-10 items-center gap-1.5 rounded-full border border-black/10 bg-white/70 px-4 text-sm font-medium shadow-sm transition hover:border-black/25 dark:border-white/15 dark:bg-white/10 dark:text-white"
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <BookOpen className="h-3.5 w-3.5 text-muted" />
        <span className="max-w-[180px] truncate">
          {activeChapter
            ? `第${activeChapter.chapter_no}章 ${activeChapter.title}`
            : "选择章节"}
        </span>
        <ChevronDown className="h-3.5 w-3.5 text-muted" />
      </button>

      {open && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} />
          <div className="absolute left-0 top-full z-20 mt-1 min-w-[260px] overflow-hidden rounded-2xl border border-black/[0.06] bg-paper shadow-apple dark:border-white/10 dark:bg-[#1a1a1b]">
            <div className="max-h-64 overflow-y-auto p-1">
              {chapters.map((item, index) => (
                <div
                  key={item.id}
                  role="button"
                  tabIndex={0}
                  className={cn(
                    "flex w-full items-center gap-2 rounded-xl px-2.5 py-2 text-left text-sm text-ink transition hover:bg-black/5 dark:hover:bg-white/10",
                    item.id === activeChapterId &&
                      "bg-black/5 font-medium dark:bg-white/10",
                  )}
                  onClick={() => {
                    selectChapter(item.id);
                    setOpen(false);
                  }}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      selectChapter(item.id);
                      setOpen(false);
                    }
                  }}
                >
                  <span className="w-5 shrink-0 text-xs text-muted">
                    {item.chapter_no}
                  </span>
                  <span className="truncate">
                    {item.title || `第${item.chapter_no}章`}
                  </span>
                  {Boolean(item.shot_count) && (
                    <span className="shrink-0 text-xs text-muted">
                      {item.shot_count} 镜
                    </span>
                  )}
                  <span
                    className="ml-auto flex items-center gap-0.5"
                    onClick={(event) => event.stopPropagation()}
                  >
                    <button
                      type="button"
                      disabled={index === 0 || reorderMutation.isPending}
                      className="rounded-full p-1 hover:bg-black/10 disabled:opacity-30 dark:hover:bg-white/15"
                      title="上移"
                      aria-label={`上移第${item.chapter_no}章`}
                      onClick={() => moveChapter(index, -1)}
                    >
                      <ChevronUp className="h-3.5 w-3.5" />
                    </button>
                    <button
                      type="button"
                      disabled={
                        index === chapters.length - 1 ||
                        reorderMutation.isPending
                      }
                      className="rounded-full p-1 hover:bg-black/10 disabled:opacity-30 dark:hover:bg-white/15"
                      title="下移"
                      aria-label={`下移第${item.chapter_no}章`}
                      onClick={() => moveChapter(index, 1)}
                    >
                      <ChevronDown className="h-3.5 w-3.5" />
                    </button>
                    <button
                      type="button"
                      disabled={deleteMutation.isPending}
                      className="rounded-full p-1 text-[#c93c30] hover:bg-red-50 disabled:opacity-30 dark:hover:bg-red-500/10"
                      title="删除章节"
                      aria-label={`删除第${item.chapter_no}章`}
                      onClick={() => deleteMutation.mutate(item.id)}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </span>
                </div>
              ))}
            </div>
            <div className="border-t border-black/[0.06] p-1 dark:border-white/10">
              <button
                type="button"
                disabled={createMutation.isPending}
                className="flex w-full items-center gap-2 rounded-xl px-2.5 py-2 text-left text-sm font-medium text-vermilion transition hover:bg-black/5 disabled:opacity-50 dark:hover:bg-white/10"
                onClick={() => createMutation.mutate()}
              >
                <Plus className="h-3.5 w-3.5" />
                新建章节
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
