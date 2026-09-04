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
import { formatError } from "../lib/utils";
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
        className="border-border bg-card hover:bg-accent flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm font-medium transition-colors"
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <BookOpen className="text-muted-foreground h-3.5 w-3.5" />
        <span className="max-w-[180px] truncate">
          {activeChapter
            ? `第${activeChapter.chapter_no}章 ${activeChapter.title}`
            : "选择章节"}
        </span>
        <ChevronDown className="text-muted-foreground h-3.5 w-3.5" />
      </button>

      {open && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} />
          <div className="border-border bg-card absolute left-0 top-full z-20 mt-1 min-w-[240px] rounded-md border shadow-lg">
            <div className="max-h-64 overflow-y-auto p-1">
              {chapters.map((item, index) => (
                <div
                  key={item.id}
                  role="button"
                  tabIndex={0}
                  className={`hover:bg-accent flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-sm ${
                    item.id === activeChapterId ? "bg-accent font-medium" : ""
                  }`}
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
                  <span className="text-muted-foreground w-5 shrink-0 text-xs">
                    {item.chapter_no}
                  </span>
                  <span className="truncate">
                    {item.title || `第${item.chapter_no}章`}
                  </span>
                  {Boolean(item.shot_count) && (
                    <span className="text-muted-foreground shrink-0 text-xs">
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
                      className="rounded p-0.5 hover:bg-accent disabled:opacity-30"
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
                      className="rounded p-0.5 hover:bg-accent disabled:opacity-30"
                      title="下移"
                      aria-label={`下移第${item.chapter_no}章`}
                      onClick={() => moveChapter(index, 1)}
                    >
                      <ChevronDown className="h-3.5 w-3.5" />
                    </button>
                    <button
                      type="button"
                      disabled={deleteMutation.isPending}
                      className="rounded p-0.5 text-[#9b342c] hover:bg-accent disabled:opacity-30"
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
            <div className="border-border border-t p-1">
              <button
                type="button"
                disabled={createMutation.isPending}
                className="hover:bg-accent flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-sm disabled:opacity-50"
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
