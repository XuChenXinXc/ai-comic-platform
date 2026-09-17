import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FolderOpen, Info, Save } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { api, queryKeys } from "../api/client";
import {
  ErrorState,
  LoadingState,
  PageHeader,
} from "../components/status";
import { Button, Card, Input, Label, Select, Textarea } from "../components/ui";
import { formatError } from "../lib/utils";

const CAMERA_POLICY_HINTS: Record<string, string> = {
  克制: "定镜优先：只有明确叙事动机才用运镜，漏加不提醒（推荐）",
  标准: "动机驱动：多插警告、漏加给建议，双向提醒",
  丰富: "倾向运镜：每镜默认带运镜，多插仅提醒、漏加主动补建议",
};

const PRIVATE_HOSTS_SOURCE_HINTS: Record<string, string> = {
  database: "当前值来自平台设置页保存的配置。",
  environment:
    "当前值来自 .env 的 APP_PROVIDER_ALLOW_PRIVATE_HOSTS；在页面保存后会以页面配置为准。",
  default: "当前使用默认值：禁止连接内网地址。",
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
  const defaultStylePromptQuery = useQuery({
    queryKey: queryKeys.defaultStylePrompt,
    queryFn: () => api.settings.defaultStylePrompt.get(),
  });
  const allowPrivateHostsQuery = useQuery({
    queryKey: queryKeys.providerAllowPrivateHosts,
    queryFn: () => api.settings.providerAllowPrivateHosts.get(),
  });
  const [defaultStylePrompt, setDefaultStylePrompt] = useState("");
  const [isStylePromptDirty, setIsStylePromptDirty] = useState(false);

  useEffect(() => {
    if (defaultStylePromptQuery.data && !isStylePromptDirty) {
      setDefaultStylePrompt(defaultStylePromptQuery.data.style_prompt || "");
    }
  }, [defaultStylePromptQuery.data, isStylePromptDirty]);

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
  const defaultStylePromptMutation = useMutation({
    mutationFn: (style_prompt: string) =>
      api.settings.defaultStylePrompt.set(style_prompt),
    onSuccess: (result) => {
      queryClient.setQueryData(queryKeys.defaultStylePrompt, result);
      setDefaultStylePrompt(result.style_prompt || "");
      setIsStylePromptDirty(false);
      toast.success("默认画风已更新");
    },
    onError: (error) => toast.error(formatError(error)),
  });
  const allowPrivateHostsMutation = useMutation({
    mutationFn: (enabled: boolean) =>
      api.settings.providerAllowPrivateHosts.set(enabled),
    onSuccess: (result) => {
      queryClient.setQueryData(queryKeys.providerAllowPrivateHosts, result);
      toast.success(
        result.enabled
          ? "已允许连接本地/内网 Provider"
          : "已禁止连接本地/内网 Provider",
      );
    },
    onError: (error) => toast.error(formatError(error)),
  });

  return (
    <div>
      <PageHeader
        eyebrow="Workspace / Settings"
        title="平台设置"
        description="管理平台级项目存储位置、运镜策略与默认画风。"
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

          <div className="mt-5 flex gap-2 rounded-xl border border-amber-200 dark:border-amber-500/30 bg-amber-50 dark:bg-amber-500/10 px-4 py-3 text-sm leading-6 text-amber-800 dark:text-amber-300">
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

      {defaultStylePromptQuery.isLoading ? (
        <LoadingState label="正在读取默认画风…" />
      ) : null}
      {defaultStylePromptQuery.data !== undefined ? (
        <Card className="mt-6 max-w-3xl p-5 sm:p-6">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <h2 className="text-lg font-bold text-ink">默认画风</h2>
              <p className="mt-1 text-sm leading-6 text-muted">
                新建项目时，若未在创建时指定画风，将自动回填该默认画风（留空表示不设默认值）。
              </p>
            </div>
            <Button
              type="button"
              variant="primary"
              className="shrink-0 self-start sm:self-auto"
              busy={defaultStylePromptMutation.isPending}
              disabled={!isStylePromptDirty}
              onClick={() =>
                defaultStylePromptMutation.mutate(defaultStylePrompt)
              }
            >
              <Save className="h-4 w-4" />
              保存画风
            </Button>
          </div>

          <div className="mt-5">
            <Label htmlFor="default-style-prompt">
              默认统一画风 STYLE_ANCHOR
            </Label>
            <Textarea
              id="default-style-prompt"
              rows={4}
              value={defaultStylePrompt}
              placeholder="例如：现代国风水墨动画风格，粗笔焦墨，宣纸微黄底色，工笔重彩与留白结合…"
              onChange={(event) => {
                setDefaultStylePrompt(event.target.value);
                setIsStylePromptDirty(true);
              }}
            />
            <p className="mt-2 text-xs leading-5 text-muted">
              仅对之后新建的项目生效；已有项目的画风请在各项目的【剧本生成】页面单独调整。
            </p>
          </div>
        </Card>
      ) : null}

      {allowPrivateHostsQuery.isLoading ? (
        <LoadingState label="正在读取本地 Provider 开关…" />
      ) : null}
      {allowPrivateHostsQuery.data ? (
        <Card className="mt-6 max-w-3xl p-5 sm:p-6">
          <h2 className="text-lg font-bold text-ink">本地 / 内网 Provider</h2>
          <p className="mt-1 text-sm leading-6 text-muted">
            默认拒绝 Provider 连接 127.0.0.1、局域网等内网地址，避免无鉴权的接口配置被当作内网探测入口。
            需要在 Ollama、ComfyUI 等本机服务上使用时再开启；开启后下一次连接测试或生成即生效，无需重启。
          </p>

          <div className="mt-5 flex items-center gap-3">
            <input
              id="provider-allow-private-hosts"
              type="checkbox"
              className="h-4 w-4 accent-vermilion"
              checked={allowPrivateHostsQuery.data.enabled}
              disabled={allowPrivateHostsMutation.isPending}
              onChange={(event) =>
                allowPrivateHostsMutation.mutate(event.target.checked)
              }
            />
            <Label htmlFor="provider-allow-private-hosts" className="mb-0">
              允许连接本地/内网 Provider
            </Label>
          </div>
          <p className="mt-2 text-xs leading-5 text-muted">
            {PRIVATE_HOSTS_SOURCE_HINTS[allowPrivateHostsQuery.data.source] ??
              PRIVATE_HOSTS_SOURCE_HINTS.default}
          </p>
        </Card>
      ) : null}
    </div>
  );
}
