import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2,
  CircleSlash,
  Pencil,
  PlugZap,
  Plus,
  Trash2,
} from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";
import { api, queryKeys } from "../api/client";
import {
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
} from "../components/status";
import {
  Badge,
  Button,
  Card,
  Input,
  Label,
  Modal,
  Select,
  Tabs,
  Textarea,
} from "../components/ui";
import { formatError, parseJson, prettyJson } from "../lib/utils";
import type {
  ApiProvider,
  ProviderInput,
  ProviderKind,
} from "../types/api";

const adapterOptions: Record<ProviderKind, Array<{ value: string; label: string }>> =
  {
    text: [
      { value: "mock", label: "Mock（本地验收）" },
      { value: "openai_compatible", label: "OpenAI Compatible" },
    ],
    image: [
      { value: "mock", label: "Mock（本地验收）" },
      { value: "http_image", label: "通用 HTTP Image" },
    ],
    video: [
      { value: "mock", label: "Mock（本地验收）" },
      { value: "http_video", label: "通用 HTTP Video" },
    ],
    tts: [
      { value: "mock", label: "Mock（本地验收）" },
      { value: "openai_tts", label: "OpenAI TTS" },
      { value: "http_tts", label: "通用 HTTP TTS" },
    ],
  };

function blankProvider(kind: ProviderKind): ProviderInput {
  let name = "模型";
  if (kind === "text") name = "文字模型";
  else if (kind === "image") name = "图片模型";
  else if (kind === "video") name = "视频模型";
  else if (kind === "tts") name = "TTS 语音模型";

  return {
    kind,
    name,
    adapter: "mock",
    base_url: "",
    api_key_env: "",
    api_key: "",
    model: "mock-v1",
    params: {},
    timeout_sec: 120,
    max_retries: 2,
    enabled: true,
    is_default: false,
  };
}

export function ProvidersPage() {
  const queryClient = useQueryClient();
  const [kindFilter, setKindFilter] = useState<ProviderKind | "all">("all");
  const [editing, setEditing] = useState<ApiProvider | "new" | null>(null);
  const [form, setForm] = useState<ProviderInput>(blankProvider("text"));
  const [paramsText, setParamsText] = useState("{}");
  const [paramsError, setParamsError] = useState("");
  const query = useQuery({
    queryKey: queryKeys.providers,
    queryFn: () => api.providers.list(),
  });
  const providers = useMemo(
    () =>
      (query.data || []).filter(
        (provider) => kindFilter === "all" || provider.kind === kindFilter,
      ),
    [kindFilter, query.data],
  );

  const closeModal = () => {
    setEditing(null);
    setParamsError("");
  };

  const supportsVision = Boolean(
    ((form.params as Record<string, unknown> | undefined) ?? {})[
      "supports_vision"
    ],
  );

  const toggleSupportsVision = (checked: boolean) => {
    const nextParams: Record<string, unknown> = {
      ...(form.params as Record<string, unknown>),
      supports_vision: checked,
    };
    setForm((current) => ({ ...current, params: nextParams }));
    setParamsText(prettyJson(nextParams));
    setParamsError("");
  };

  const saveMutation = useMutation({
    mutationFn: () =>
      editing === "new"
        ? api.providers.create(form)
        : api.providers.update((editing as ApiProvider).id, form),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.providers });
      toast.success(editing === "new" ? "接口已添加" : "接口配置已保存");
      closeModal();
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const testMutation = useMutation({
    mutationFn: (id: string) => api.providers.test(id),
    onSuccess: async (result) => {
      toast[result.ok ? "success" : "error"](result.message);
      await queryClient.invalidateQueries({ queryKey: queryKeys.providers });
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.providers.remove(id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.providers });
      toast.success("接口已删除");
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const openNew = (kind: ProviderKind = "text") => {
    const value = blankProvider(kind);
    setForm(value);
    setParamsText("{}");
    setEditing("new");
  };

  const openEdit = (provider: ApiProvider) => {
    const params = parseJson(
      provider.params_json ?? provider.params,
      {} as Record<string, unknown>,
    );
    setForm({
      kind: provider.kind,
      name: provider.name,
      adapter: provider.adapter,
      base_url: provider.base_url,
      api_key_env: provider.api_key_env,
      api_key: "",
      model: provider.model,
      params,
      timeout_sec: provider.timeout_sec,
      max_retries: provider.max_retries,
      enabled: provider.enabled,
      is_default: provider.is_default,
    });
    setParamsText(prettyJson(params));
    setEditing(provider);
  };

  return (
    <div>
      <PageHeader
        eyebrow="Workspace / Providers"
        title="接口配置"
        description="文字、图片与视频 Provider 独立配置。API Key 直接加密保存在本机，不需要手动维护环境变量。"
        actions={
          <Button variant="primary" onClick={() => openNew()}>
            <Plus className="h-4 w-4" />
            添加接口
          </Button>
        }
      />

      <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
        <Tabs
          value={kindFilter}
          onChange={(value) => setKindFilter(value as ProviderKind | "all")}
          items={[
            { value: "all", label: "全部", count: query.data?.length || 0 },
            {
              value: "text",
              label: "文字模型",
              count: query.data?.filter((item) => item.kind === "text").length || 0,
            },
            {
              value: "image",
              label: "图片模型",
              count: query.data?.filter((item) => item.kind === "image").length || 0,
            },
            {
              value: "video",
              label: "视频模型",
              count: query.data?.filter((item) => item.kind === "video").length || 0,
            },
            {
              value: "tts",
              label: "TTS 语音",
              count: query.data?.filter((item) => item.kind === "tts").length || 0,
            },
          ]}
        />
        <p className="text-xs text-muted">
          推荐先保留三类 Mock 默认项，确保无密钥也可验收。
        </p>
      </div>

      {query.isLoading ? <LoadingState label="正在读取接口配置…" /> : null}
      {query.error ? (
        <ErrorState
          message={formatError(query.error)}
          onRetry={() => void query.refetch()}
        />
      ) : null}
      {!query.isLoading && !query.error && !providers.length ? (
        <EmptyState
          title="没有符合条件的接口"
          description="添加一个 Provider，或切换到其他类型。Mock 适配器不需要密钥。"
          action={
            <Button variant="primary" onClick={() => openNew(kindFilter === "all" ? "text" : kindFilter)}>
              添加接口
            </Button>
          }
        />
      ) : null}

      <div className="grid gap-4 xl:grid-cols-2">
        {providers.map((provider) => (
          <Card key={provider.id} className="p-5 sm:p-6">
            <div className="flex items-start justify-between gap-4">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <h2 className="truncate text-lg font-bold">{provider.name}</h2>
                  {provider.is_default ? (
                    <Badge className="border-amber-200 dark:border-amber-500/30 bg-amber-50 dark:bg-amber-500/10 text-amber-700 dark:text-amber-300">
                      默认
                    </Badge>
                  ) : null}
                  {/* 用 Badge 展示停用状态，不再向 StatusBadge 传入非任务状态的枚举值 */}
                  {!provider.enabled ? (
                    <Badge className="border-black/10 dark:border-white/10 bg-black/5 dark:bg-white/10 text-muted">
                      已停用
                    </Badge>
                  ) : null}
                </div>
                <p className="mt-1 font-mono text-xs text-muted">
                  {provider.adapter} · {provider.model || "未指定模型"}
                </p>
              </div>
              <div className="flex gap-1">
                <Button
                  size="icon"
                  variant="ghost"
                  aria-label={`编辑 ${provider.name}`}
                  onClick={() => openEdit(provider)}
                >
                  <Pencil className="h-4 w-4" />
                </Button>
                <Button
                  size="icon"
                  variant="ghost"
                  className="text-[#c93c30] dark:text-red-400"
                  busy={
                    deleteMutation.isPending &&
                    deleteMutation.variables === provider.id
                  }
                  aria-label={`删除 ${provider.name}`}
                  onClick={() => {
                    if (
                      window.confirm(
                        `确认删除接口“${provider.name}”吗？已引用它的历史任务仍会保留。`,
                      )
                    ) {
                      deleteMutation.mutate(provider.id);
                    }
                  }}
                >
                  <Trash2 className="h-4 w-4" />
                </Button>
              </div>
            </div>

            <dl className="mt-5 grid gap-3 border-y border-line py-4 text-sm sm:grid-cols-2">
              <div>
                <dt className="text-xs text-muted">类型</dt>
                <dd className="mt-1 font-semibold">
                  {provider.kind === "text"
                    ? "文字"
                    : provider.kind === "image"
                      ? "图片"
                      : "视频"}
                </dd>
              </div>
              <div>
                <dt className="text-xs text-muted">API Key 状态</dt>
                <dd className="mt-1 truncate font-mono text-xs font-semibold">
                  {provider.api_key_configured
                    ? provider.api_key_source === "direct"
                      ? "已配置 API Key"
                      : provider.api_key_env
                        ? `环境变量：${provider.api_key_env}`
                        : "已配置"
                    : "未配置"}
                </dd>
              </div>
              <div className="sm:col-span-2">
                <dt className="text-xs text-muted">Base URL</dt>
                <dd className="mt-1 truncate font-mono text-xs font-semibold">
                  {provider.base_url || "本地 Mock"}
                </dd>
              </div>
            </dl>

            <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
              <div className="flex min-w-0 items-center gap-2 text-xs">
                {provider.last_test_ok === true ? (
                  <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-600 dark:text-emerald-400" />
                ) : provider.last_test_ok === false ? (
                  <CircleSlash className="h-4 w-4 shrink-0 text-red-500" />
                ) : (
                  <PlugZap className="h-4 w-4 shrink-0 text-muted" />
                )}
                <span className="truncate text-muted">
                  {provider.last_test_message || "尚未测试连接"}
                </span>
              </div>
              <Button
                size="sm"
                busy={
                  testMutation.isPending &&
                  testMutation.variables === provider.id
                }
                onClick={() => testMutation.mutate(provider.id)}
              >
                <PlugZap className="h-4 w-4" />
                测试连接
              </Button>
            </div>
          </Card>
        ))}
      </div>

      <Modal
        open={Boolean(editing)}
        onClose={closeModal}
        title={editing === "new" ? "添加接口" : "编辑接口"}
        description="适配器决定协议差异；默认参数会与单次生成参数合并。"
        footer={
          <>
            <Button onClick={closeModal}>取消</Button>
            <Button
              variant="primary"
              busy={saveMutation.isPending}
              disabled={!form.name.trim() || Boolean(paramsError)}
              onClick={() => saveMutation.mutate()}
            >
              保存接口
            </Button>
          </>
        }
      >
        <div className="grid gap-5 sm:grid-cols-2">
          <div>
            <Label htmlFor="provider-kind">接口类型</Label>
            <Select
              id="provider-kind"
              value={form.kind}
              // 更新接口不提交 kind，编辑模式禁用选择，避免界面与实际不一致
              disabled={editing !== "new"}
              onChange={(event) => {
                const kind = event.target.value as ProviderKind;
                setForm((current) => ({
                  ...current,
                  kind,
                  adapter: "mock",
                }));
              }}
            >
              <option value="text">文字模型</option>
              <option value="image">图片模型</option>
              <option value="video">视频模型</option>
              <option value="tts">TTS 语音</option>
            </Select>
          </div>
          <div>
            <Label htmlFor="provider-name">配置名称</Label>
            <Input
              id="provider-name"
              value={form.name}
              onChange={(event) =>
                setForm((current) => ({ ...current, name: event.target.value }))
              }
            />
          </div>
          <div>
            <Label htmlFor="provider-adapter">适配器</Label>
            <Select
              id="provider-adapter"
              value={form.adapter}
              onChange={(event) =>
                setForm((current) => ({
                  ...current,
                  adapter: event.target.value,
                }))
              }
            >
              {adapterOptions[form.kind].map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </Select>
          </div>
          <div>
            <Label htmlFor="provider-model">模型名称</Label>
            <Input
              id="provider-model"
              value={form.model}
              placeholder="例如 gpt-4.1-mini"
              onChange={(event) =>
                setForm((current) => ({ ...current, model: event.target.value }))
              }
            />
          </div>
          <div className="sm:col-span-2">
            <Label htmlFor="provider-base-url">Base URL</Label>
            <Input
              id="provider-base-url"
              value={form.base_url}
              placeholder="https://api.example.com/v1"
              onChange={(event) =>
                setForm((current) => ({
                  ...current,
                  base_url: event.target.value,
                }))
              }
            />
          </div>
          <div className="sm:col-span-2">
            <Label htmlFor="provider-api-key">API Key</Label>
            <Input
              id="provider-api-key"
              type="password"
              value={form.api_key || ""}
              placeholder="直接输入 API Key"
              autoComplete="new-password"
              spellCheck={false}
              onChange={(event) =>
                setForm((current) => ({
                  ...current,
                  api_key: event.target.value,
                }))
              }
            />
            <p className="mt-1.5 text-xs text-muted">
              直接填写密钥。保存后不会再次显示；编辑时留空表示保留已保存的 Key。
            </p>
          </div>
          <div>
            <Label htmlFor="provider-timeout">超时（秒）</Label>
            <Input
              id="provider-timeout"
              type="number"
              min={1}
              value={form.timeout_sec}
              onChange={(event) =>
                setForm((current) => ({
                  ...current,
                  timeout_sec: Number(event.target.value),
                }))
              }
            />
          </div>
          <div>
            <Label htmlFor="provider-retries">最大重试次数</Label>
            <Input
              id="provider-retries"
              type="number"
              min={0}
              value={form.max_retries}
              onChange={(event) =>
                setForm((current) => ({
                  ...current,
                  max_retries: Number(event.target.value),
                }))
              }
            />
          </div>
          {form.kind === "text" ? (
            <label className="flex items-start gap-3 rounded-xl border border-line bg-paper px-3 py-3 text-sm font-semibold sm:col-span-2">
              <input
                type="checkbox"
                className="mt-0.5"
                checked={supportsVision}
                onChange={(event) => toggleSupportsVision(event.target.checked)}
              />
              <span>
                启用视觉能力（supports_vision）
                <span className="mt-1 block text-xs font-normal leading-5 text-muted">
                  接口需支持图片输入。启用后，关键帧生成完成会自动做视觉质检
                  （身份一致 / 画风合规 / 画面可用性），不通过自动换 seed 重生成。
                </span>
              </span>
            </label>
          ) : null}
          <div className="sm:col-span-2">
            <Label htmlFor="provider-params">默认请求参数（JSON）</Label>
            <Textarea
              id="provider-params"
              rows={7}
              className="font-mono text-xs"
              value={paramsText}
              onChange={(event) => {
                setParamsText(event.target.value);
                try {
                  const parsed = JSON.parse(event.target.value) as Record<
                    string,
                    unknown
                  >;
                  setForm((current) => ({ ...current, params: parsed }));
                  setParamsError("");
                } catch (error) {
                  setParamsError(formatError(error));
                }
              }}
            />
            {paramsError ? (
              <p className="mt-1.5 text-xs text-[#e0442e]">{paramsError}</p>
            ) : null}
          </div>
          <label className="flex items-center gap-3 rounded-xl border border-line bg-paper px-3 py-3 text-sm font-semibold">
            <input
              type="checkbox"
              checked={form.enabled}
              onChange={(event) =>
                setForm((current) => ({
                  ...current,
                  enabled: event.target.checked,
                }))
              }
            />
            启用此接口
          </label>
          <label className="flex items-center gap-3 rounded-xl border border-line bg-paper px-3 py-3 text-sm font-semibold">
            <input
              type="checkbox"
              checked={form.is_default}
              onChange={(event) =>
                setForm((current) => ({
                  ...current,
                  is_default: event.target.checked,
                }))
              }
            />
            设为该类型默认接口
          </label>
        </div>
      </Modal>
    </div>
  );
}
