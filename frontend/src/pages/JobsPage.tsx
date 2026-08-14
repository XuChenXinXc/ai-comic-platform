import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Ban,
  Eye,
  LoaderCircle,
  RefreshCw,
  RotateCcw,
} from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";
import { api, queryKeys } from "../api/client";
import {
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
  StatusBadge,
} from "../components/status";
import {
  Badge,
  Button,
  Card,
  Modal,
  Progress,
  Select,
  Tabs,
} from "../components/ui";
import { isJobActive } from "../hooks/use-job-polling";
import { JOB_TYPE_LABELS } from "../lib/job-labels";
import { formatDate, formatError, prettyJson } from "../lib/utils";
import type { Job } from "../types/api";

export function JobsPage() {
  const queryClient = useQueryClient();
  const projectsQuery = useQuery({
    queryKey: ["projects", "all-kinds"],
    queryFn: async () => [
      ...(await api.projects.list({ kind: "regular" })),
      ...(await api.projects.list({ kind: "free" })),
    ],
  });
  const projects = projectsQuery.data ?? [];
  const [projectFilter, setProjectFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState("all");
  const [typeFilter, setTypeFilter] = useState("all");
  const [detail, setDetail] = useState<Job | null>(null);

  const queryProjectId = projectFilter === "all" ? undefined : projectFilter;
  const query = useQuery({
    queryKey: queryKeys.jobs(queryProjectId),
    queryFn: () =>
      api.jobs.list({
        project_id: queryProjectId,
      }),
    refetchInterval: (result) =>
      (result.state.data || []).some((job) => isJobActive(job.status))
        ? 1_500
        : 10_000,
  });
  const jobs = useMemo(
    () =>
      (query.data || []).filter(
        (job) =>
          (statusFilter === "all" ||
            (statusFilter === "active"
              ? isJobActive(job.status)
              : job.status === statusFilter)) &&
          (typeFilter === "all" || job.job_type === typeFilter),
      ),
    [query.data, statusFilter, typeFilter],
  );
  const jobTypes = useMemo(
    () => [...new Set((query.data || []).map((job) => job.job_type))].sort(),
    [query.data],
  );
  const batches = useMemo(() => {
    const grouped = new Map<string, Job[]>();
    (query.data || []).forEach((job) => {
      if (!job.batch_id) return;
      grouped.set(job.batch_id, [...(grouped.get(job.batch_id) || []), job]);
    });
    return [...grouped.entries()]
      .filter(([, batchJobs]) => batchJobs.length > 1)
      .sort(
        (a, b) =>
          new Date(b[1][0].created_at).getTime() -
          new Date(a[1][0].created_at).getTime(),
      )
      .slice(0, 4);
  }, [query.data]);

  const mutation = useMutation({
    mutationFn: ({
      action,
      id,
    }: {
      action: "retry" | "cancel";
      id: string;
    }) =>
      action === "retry" ? api.jobs.retry(id) : api.jobs.cancel(id),
    onSuccess: async (_, variables) => {
      await queryClient.invalidateQueries({ queryKey: ["jobs"] });
      toast.success(variables.action === "retry" ? "任务已重新入队" : "任务已取消");
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const batchRetryMutation = useMutation({
    mutationFn: (variables: {
      batchId: string;
      jobs: Job[];
    }) => Promise.all(variables.jobs.map((job) => api.jobs.retry(job.id))),
    onSuccess: async (retried) => {
      await queryClient.invalidateQueries({ queryKey: ["jobs"] });
      toast.success(`已重新入队 ${retried.length} 个失败项`);
    },
    onError: (error) => toast.error(formatError(error)),
  });

  return (
    <div>
      <PageHeader
        eyebrow="Workspace / Job control"
        title="任务中心"
        description="所有外部调用都在这里持久化、轮询和诊断。批量任务逐项执行，失败项不会回滚已完成结果。"
        actions={
          <Button onClick={() => void query.refetch()}>
            <RefreshCw className="h-4 w-4" />
            立即刷新
          </Button>
        }
      />

      <Card className="mb-6 flex flex-col gap-4 p-4 lg:flex-row lg:items-center lg:justify-between">
        <div className="flex flex-wrap gap-3">
          <Select
            aria-label="按项目筛选"
            value={projectFilter}
            className="w-52"
            onChange={(event) => setProjectFilter(event.target.value)}
          >
            <option value="all">全部项目</option>
            {projects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.name}
                {project.is_free_create ? "（自由制作）" : ""}
              </option>
            ))}
          </Select>
          <Select
            aria-label="按任务类型筛选"
            value={typeFilter}
            className="w-52"
            onChange={(event) => setTypeFilter(event.target.value)}
          >
            <option value="all">全部任务类型</option>
            {jobTypes.map((type) => (
              <option key={type} value={type}>
                {JOB_TYPE_LABELS[type] || type}
              </option>
            ))}
          </Select>
        </div>
        <div className="flex items-center gap-2 text-xs text-muted">
          {query.isFetching ? (
            <LoaderCircle className="h-4 w-4 animate-spin text-vermilion" />
          ) : (
            <span className="h-2 w-2 rounded-full bg-[#6f8c71]" />
          )}
          {query.isFetching ? "正在同步任务状态" : "任务状态自动轮询"}
        </div>
      </Card>

      <div className="mb-6">
        <Tabs
          value={statusFilter}
          onChange={setStatusFilter}
          items={[
            { value: "all", label: "全部", count: query.data?.length || 0 },
            {
              value: "active",
              label: "进行中",
              count:
                query.data?.filter((job) => isJobActive(job.status)).length || 0,
            },
            {
              value: "failed",
              label: "失败",
              count:
                query.data?.filter((job) => job.status === "failed").length || 0,
            },
            {
              value: "completed",
              label: "已完成",
              count:
                query.data?.filter((job) => job.status === "completed").length ||
                0,
            },
          ]}
        />
      </div>

      {batches.length && statusFilter === "all" && typeFilter === "all" ? (
        <section className="mb-7">
          <h2 className="mb-3 text-sm font-bold">最近批次</h2>
          <div className="grid gap-3 lg:grid-cols-2 2xl:grid-cols-4">
            {batches.map(([batchId, batchJobs]) => {
              const completed = batchJobs.filter(
                (job) => job.status === "completed",
              ).length;
              const failed = batchJobs.filter((job) =>
                ["failed", "interrupted"].includes(job.status),
              ).length;
              const progress = Math.round(
                ((completed + failed) / batchJobs.length) * 100,
              );
              return (
                <Card key={batchId} className="p-4">
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <p className="font-mono text-[10px] text-muted">
                        {batchId.slice(0, 12)}
                      </p>
                      <h3 className="mt-1 text-sm font-bold">
                        {JOB_TYPE_LABELS[batchJobs[0].job_type] ||
                          batchJobs[0].job_type}
                      </h3>
                    </div>
                    {failed ? (
                      <Badge className="border-[#e0a39e] bg-[#fff0ee] text-[#9b342c]">
                        {failed} 失败
                      </Badge>
                    ) : null}
                  </div>
                  <div className="mt-4">
                    <Progress value={progress} />
                    <div className="mt-2 flex justify-between text-[11px] text-muted">
                      <span>
                        {completed} 成功 / {failed} 失败
                      </span>
                      <span>{batchJobs.length} 项</span>
                    </div>
                    {failed ? (
                      <Button
                        className="mt-3 w-full"
                        size="sm"
                        busy={
                          batchRetryMutation.isPending &&
                          batchRetryMutation.variables?.batchId === batchId
                        }
                        onClick={() =>
                          batchRetryMutation.mutate({
                            batchId,
                            jobs: batchJobs.filter((job) =>
                              ["failed", "interrupted"].includes(job.status),
                            ),
                          })
                        }
                      >
                        <RotateCcw className="h-4 w-4" />
                        仅重试失败项
                      </Button>
                    ) : null}
                  </div>
                </Card>
              );
            })}
          </div>
        </section>
      ) : null}

      {query.isLoading ? <LoadingState label="正在读取任务记录…" /> : null}
      {query.error ? (
        <ErrorState
          message={formatError(query.error)}
          onRetry={() => void query.refetch()}
        />
      ) : null}
      {!query.isLoading && !query.error && !jobs.length ? (
        <EmptyState
          title="没有符合条件的任务"
          description={
            query.data?.length
              ? "调整项目、类型或状态筛选条件。"
              : "从任一生产页面发起生成后，任务会显示在这里。"
          }
        />
      ) : null}

      <div className="space-y-3">
        {jobs.map((job) => {
          const progress = job.progress_total
            ? Math.round((job.progress_current / job.progress_total) * 100)
            : isJobActive(job.status)
              ? 8
              : job.status === "completed"
                ? 100
                : 0;
          return (
            <Card key={job.id} className="p-4 sm:p-5">
              <div className="grid gap-4 xl:grid-cols-[minmax(230px,0.8fr)_minmax(220px,0.9fr)_minmax(200px,1fr)_auto] xl:items-center">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <h2 className="truncate text-sm font-bold">
                      {JOB_TYPE_LABELS[job.job_type] || job.job_type}
                    </h2>
                    <StatusBadge status={job.status} />
                  </div>
                  <p className="mt-1 truncate font-mono text-[10px] text-muted">
                    {job.id}
                  </p>
                </div>
                <div className="min-w-0 text-xs">
                  <p className="truncate font-semibold text-[#444741]">
                    {job.target_name ||
                      `${job.target_type || "项目"} ${job.target_id?.slice(0, 8) || ""}`}
                  </p>
                  <p className="mt-1 truncate text-muted">
                    {job.provider_name ||
                      job.provider_kind ||
                      "未指定 Provider"}
                    {job.retry_count ? ` · 已重试 ${job.retry_count} 次` : ""}
                  </p>
                </div>
                <div>
                  <Progress value={progress} />
                  <div className="mt-2 flex justify-between gap-2 text-[10px] text-muted">
                    <span className="truncate">
                      {job.error || job.message || "等待状态更新"}
                    </span>
                    <span className="shrink-0">
                      {job.progress_current}/{job.progress_total}
                    </span>
                  </div>
                </div>
                <div className="flex justify-end gap-1">
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label="查看任务详情"
                    onClick={() => setDetail(job)}
                  >
                    <Eye className="h-4 w-4" />
                  </Button>
                  {["failed", "interrupted"].includes(job.status) ? (
                    <Button
                      size="sm"
                      busy={
                        mutation.isPending &&
                        mutation.variables?.action === "retry" &&
                        mutation.variables.id === job.id
                      }
                      onClick={() =>
                        mutation.mutate({ action: "retry", id: job.id })
                      }
                    >
                      <RotateCcw className="h-4 w-4" />
                      重试
                    </Button>
                  ) : null}
                  {job.status === "queued" ? (
                    <Button
                      size="sm"
                      variant="ghost"
                      className="text-[#9b342c]"
                      busy={
                        mutation.isPending &&
                        mutation.variables?.action === "cancel" &&
                        mutation.variables.id === job.id
                      }
                      onClick={() =>
                        mutation.mutate({ action: "cancel", id: job.id })
                      }
                    >
                      <Ban className="h-4 w-4" />
                      取消
                    </Button>
                  ) : null}
                </div>
              </div>
            </Card>
          );
        })}
      </div>

      <Modal
        open={Boolean(detail)}
        onClose={() => setDetail(null)}
        title="任务诊断"
        description={
          detail
            ? `${JOB_TYPE_LABELS[detail.job_type] || detail.job_type} · ${detail.id}`
            : ""
        }
        footer={<Button onClick={() => setDetail(null)}>关闭</Button>}
      >
        {detail ? (
          <div className="space-y-5">
            <dl className="grid gap-4 text-sm sm:grid-cols-2">
              <div>
                <dt className="text-xs text-muted">状态</dt>
                <dd className="mt-1">
                  <StatusBadge status={detail.status} />
                </dd>
              </div>
              <div>
                <dt className="text-xs text-muted">Provider task ID</dt>
                <dd className="mt-1 break-all font-mono text-xs">
                  {detail.provider_task_id || "—"}
                </dd>
              </div>
              <div>
                <dt className="text-xs text-muted">提交时间</dt>
                <dd className="mt-1">{formatDate(detail.created_at)}</dd>
              </div>
              <div>
                <dt className="text-xs text-muted">完成时间</dt>
                <dd className="mt-1">{formatDate(detail.finished_at)}</dd>
              </div>
            </dl>
            {detail.error ? (
              <div className="rounded-md border border-[#e1b3ad] bg-[#fff3f0] p-4 text-sm leading-6 text-[#943c33]">
                {detail.error}
              </div>
            ) : null}
            <div>
              <p className="mb-2 text-xs font-bold text-muted">结果 / 恢复信息</p>
              <pre className="max-h-80 overflow-auto rounded-md border border-line bg-[#222622] p-4 text-xs leading-5 text-[#e7e4db]">
                {prettyJson(detail.result_json || {})}
              </pre>
            </div>
          </div>
        ) : null}
      </Modal>
    </div>
  );
}
