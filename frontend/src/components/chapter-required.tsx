import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api } from "../api/client";
import { useChapter } from "../hooks/use-chapter";
import { useProject } from "../hooks/use-project";
import { formatError } from "../lib/utils";
import { EmptyState, LoadingState } from "./status";
import { Button } from "./ui";

/**
 * 章节守卫：正文、剧本、分镜都归属章节，没有章节时先引导创建。
 * 素材库属于整本小说，不受此限制。
 */
export function ChapterRequired({ children }: { children: React.ReactNode }) {
  const queryClient = useQueryClient();
  const { activeProjectId } = useProject();
  const { activeChapter, chapters, isLoading, selectChapter } = useChapter();

  const createMutation = useMutation({
    mutationFn: () => api.chapters.create(activeProjectId, { title: "第1章" }),
    onSuccess: async (chapter) => {
      await queryClient.invalidateQueries({
        queryKey: ["chapters", activeProjectId],
      });
      selectChapter(chapter.id);
      toast.success("已创建第1章");
    },
    onError: (error) => toast.error(formatError(error)),
  });

  if (isLoading) return <LoadingState label="正在读取章节…" />;
  if (!chapters.length || !activeChapter) {
    return (
      <EmptyState
        title="先创建一个章节"
        description="正文、剧本与分镜都归属于章节；素材库属于整本小说，跨章节共用。"
        action={
          <Button
            variant="primary"
            busy={createMutation.isPending}
            onClick={() => createMutation.mutate()}
          >
            创建第1章
          </Button>
        }
      />
    );
  }
  return children;
}
