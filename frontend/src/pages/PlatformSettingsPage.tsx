import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FolderOpen, Info } from "lucide-react";
import { toast } from "sonner";
import { api, queryKeys } from "../api/client";
import {
  ErrorState,
  LoadingState,
  PageHeader,
} from "../components/status";
import { Button, Card, Input, Label } from "../components/ui";
import { formatError } from "../lib/utils";

export function PlatformSettingsPage() {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: queryKeys.projectStorage,
    queryFn: () => api.settings.projectStorage.get(),
  });
  const selectMutation = useMutation({
    mutationFn: () => api.settings.projectStorage.select(),
    onSuccess: (result) => {
      if (!result.selected) {
        toast.info("已取消选择，当前目录未更改");
        return;
      }
      queryClient.setQueryData(queryKeys.projectStorage, {
        path: result.path,
        source: result.source,
      });
      toast.success("项目存储目录已更新");
    },
    onError: (error) => toast.error(formatError(error)),
  });

  return (
    <div>
      <PageHeader
        eyebrow="Workspace / Settings"
        title="平台设置"
        description="管理平台级项目存储位置。"
      />

      {query.isLoading ? <LoadingState label="正在读取平台设置…" /> : null}
      {query.error ? (
        <ErrorState
          title="平台设置加载失败"
          message={formatError(query.error)}
          onRetry={() => void query.refetch()}
        />
      ) : null}
      {query.data ? (
        <Card className="max-w-3xl p-5 sm:p-6">
          <h2 className="text-lg font-bold text-ink">项目存储</h2>
          <p className="mt-1 text-sm leading-6 text-muted">
            新建项目及其图片、视频素材将保存在这个目录下。
          </p>

          <div className="mt-5">
            <Label htmlFor="project-storage-path">当前项目存储目录</Label>
            <div className="flex flex-col gap-3 sm:flex-row">
              <Input
                id="project-storage-path"
                className="font-mono"
                readOnly
                value={query.data.path}
              />
              <Button
                type="button"
                variant="primary"
                className="shrink-0"
                busy={selectMutation.isPending}
                onClick={() => selectMutation.mutate()}
              >
                <FolderOpen className="h-4 w-4" />
                选择目录
              </Button>
            </div>
          </div>

          <div className="mt-5 flex gap-2 rounded-md border border-[#d9ae7f] bg-[#fff7ec] px-4 py-3 text-sm leading-6 text-[#75471f]">
            <Info className="mt-1 h-4 w-4 shrink-0" />
            <p>仅影响之后新建项目，已有项目不会自动搬迁。</p>
          </div>
        </Card>
      ) : null}
    </div>
  );
}
