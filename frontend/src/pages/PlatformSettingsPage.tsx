import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FolderOpen, Info } from "lucide-react";
import { toast } from "sonner";
import { api, queryKeys } from "../api/client";
import {
  ErrorState,
  LoadingState,
  PageHeader,
} from "../components/status";
import { Button, Card, Input, Label, Select } from "../components/ui";
import { formatError } from "../lib/utils";

const CAMERA_POLICY_HINTS: Record<string, string> = {
  克制: "定镜优先：只有明确叙事动机才用运镜，漏加不提醒（推荐）",
  标准: "动机驱动：多插警告、漏加给建议，双向提醒",
  丰富: "倾向运镜：每镜默认带运镜，多插仅提醒、漏加主动补建议",
};

export function PlatformSettingsPage() {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: queryKeys.projectStorage,
    queryFn: () => api.settings.projectStorage.get(),
  });
  const cameraPolicyQuery = useQuery({
    queryKey: queryKeys.cameraPolicy,
    queryFn: () => api.settings.cameraPolicy.get(),
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
  const cameraPolicyMutation = useMutation({
    mutationFn: (policy: string) => api.settings.cameraPolicy.set(policy),
    onSuccess: (result) => {
      queryClient.setQueryData(queryKeys.cameraPolicy, result);
      toast.success("运镜策略已更新");
    },
    onError: (error) => toast.error(formatError(error)),
  });

  return (
    <div>
      <PageHeader
        eyebrow="Workspace / Settings"
        title="平台设置"
        description="管理平台级项目存储位置与运镜策略。"
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

      {cameraPolicyQuery.isLoading ? (
        <LoadingState label="正在读取运镜策略…" />
      ) : null}
      {cameraPolicyQuery.data ? (
        <Card className="mt-6 max-w-3xl p-5 sm:p-6">
          <h2 className="text-lg font-bold text-ink">运镜策略</h2>
          <p className="mt-1 text-sm leading-6 text-muted">
            控制分镜生成时「加不加运镜」的激进程度。默认克制：定镜优先，
            运镜必须自证叙事动机；该用运镜却漏加时只在标准/丰富档给建议，
            由你在分镜页一键采纳，平台不会静默修改分镜。
          </p>

          <div className="mt-5 max-w-sm">
            <Label htmlFor="camera-policy">当前策略</Label>
            <Select
              id="camera-policy"
              value={cameraPolicyQuery.data.policy}
              disabled={cameraPolicyMutation.isPending}
              onChange={(event) =>
                cameraPolicyMutation.mutate(event.target.value)
              }
            >
              {cameraPolicyQuery.data.options.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </Select>
            <p className="mt-2 text-xs leading-5 text-muted">
              {CAMERA_POLICY_HINTS[cameraPolicyQuery.data.policy] ??
                "未知策略，按克制执行"}
            </p>
          </div>
        </Card>
      ) : null}
    </div>
  );
}
