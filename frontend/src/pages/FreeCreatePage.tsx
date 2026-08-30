import { ArrowRight, Clock3, Plus, RefreshCw, Workflow } from "lucide-react";
import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router";
import { api, getDynamicRunTitle } from "../api/client";
import { Button, Card } from "../components/ui";
import { ErrorState, LoadingState } from "../components/status";
import { formatDate, formatError } from "../lib/utils";
import type { DynamicWorkflowRun, Project } from "../types/api";

const statusLabels: Record<string, string> = {
  pending: "待开始",
  queued: "排队中",
  running: "运行中",
  blocked: "已暂停",
  ready: "待确认",
  confirmed: "已完成",
  stale: "需要检查",
  partial_failed: "部分失败",
  failed: "失败",
  interrupted: "已中断",
  cancelled: "已取消",
};

function RunStatus({ status }: { status: string }) {
  const label = statusLabels[status] || status;
  return (
    <span
      role="status"
      aria-label={`运行状态：${label}`}
      className="inline-flex items-center gap-1.5 rounded-full border border-white/15 bg-white/[0.07] px-2.5 py-1 text-[11px] font-bold text-[#f7c4a5]"
    >
      <span className="h-1.5 w-1.5 rounded-full bg-[#f29a70]" aria-hidden="true" />
      {label}
    </span>
  );
}

export function FreeCreatePage() {
  const { projectId } = useParams<{ projectId?: string }>();
  const navigate = useNavigate();
  const [projects, setProjects] = useState<Project[]>([]);
  const [runs, setRuns] = useState<DynamicWorkflowRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");

    const load = projectId
      ? api.dynamicRuns.list(projectId).then((items) => {
          if (!cancelled) setRuns(items);
        })
      : api.projects.list({ kind: "free" }).then((items) => {
          if (!cancelled) setProjects(items);
        });

    load
      .catch((loadError: unknown) => {
        if (!cancelled) setError(formatError(loadError));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [projectId]);

  const newRunPath = projectId
    ? `/free-create/${projectId}/pipeline/new`
    : "/free-create/pipeline/new";

  if (loading) {
    return (
      <div className="min-h-screen bg-[#111715] px-5 py-8 text-white sm:px-8">
        <LoadingState label="正在读取动态漫工作区…" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="min-h-screen bg-[#111715] px-5 py-8 sm:px-8">
        <ErrorState
          title="动态漫列表读取失败"
          message={error}
          onRetry={() => window.location.reload()}
        />
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-[#111715] text-white">
      <header className="border-b border-white/10 bg-[#151d1a]">
        <div className="mx-auto flex max-w-[1440px] items-center justify-between gap-5 px-5 py-5 sm:px-8 lg:px-12">
          <div>
            <p className="text-[10px] font-bold uppercase tracking-[0.28em] text-[#f29a70]">
              Pipeline workspace
            </p>
            <div className="mt-1 flex items-baseline gap-3">
              <h1 className="text-2xl font-extrabold tracking-[-0.05em]">动态漫工作区</h1>
              <span className="text-xs text-white/40">独立制作</span>
            </div>
          </div>
          <Button
            variant="dark"
            className="border-[#f29a70]/40 text-[#ffd6c2] hover:bg-[#f29a70]/15"
            onClick={() => navigate(newRunPath)}
          >
            <Plus className="h-4 w-4" />
            新建动态漫
          </Button>
        </div>
      </header>

      <main className="mx-auto max-w-[1440px] px-5 py-10 sm:px-8 lg:px-12 lg:py-14">
        {!projectId ? (
          <>
            <section className="max-w-2xl">
              <p className="text-xs font-bold uppercase tracking-[0.2em] text-[#f29a70]">
                Free create
              </p>
              <h2 className="mt-3 text-4xl font-extrabold tracking-[-0.05em] sm:text-5xl">
                从一段内容开始，逐阶段完成。
              </h2>
              <p className="mt-5 text-sm leading-7 text-white/55 sm:text-base">
                这里的运行与常规项目分开保存。先选择一个自由制作项目，或直接新建一轮动态漫。
              </p>
            </section>

            <section className="mt-12" aria-labelledby="free-projects-title">
              <div className="mb-4 flex items-end justify-between gap-4">
                <div>
                  <h2 id="free-projects-title" className="text-lg font-bold">
                    自由制作项目
                  </h2>
                  <p className="mt-1 text-sm text-white/45">只读取已有项目，不会因为访问页面而新建项目。</p>
                </div>
                <span className="text-xs text-white/35">{projects.length} 个项目</span>
              </div>

              {projects.length ? (
                <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                  {projects.map((project) => (
                    <button
                      type="button"
                      key={project.id}
                      className="group rounded-xl border border-white/10 bg-[#1b2521] p-5 text-left transition hover:-translate-y-0.5 hover:border-[#f29a70]/60 hover:bg-[#202c27] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#f29a70]"
                      onClick={() => navigate(`/free-create/${project.id}`)}
                    >
                      <div className="flex items-start justify-between gap-4">
                        <span className="flex h-10 w-10 items-center justify-center rounded-lg border border-[#f29a70]/25 bg-[#f29a70]/10 text-[#f29a70]">
                          <Workflow className="h-5 w-5" />
                        </span>
                        <ArrowRight className="h-4 w-4 text-white/25 transition group-hover:translate-x-1 group-hover:text-[#f29a70]" />
                      </div>
                      <h3 className="mt-7 truncate text-base font-bold">{project.name}</h3>
                      <p className="mt-2 text-xs leading-5 text-white/45">
                        最近更新于 {formatDate(project.updated_at)}
                      </p>
                    </button>
                  ))}
                </div>
              ) : (
                <Card className="border-white/10 bg-[#1b2521] p-8 text-white">
                  <div className="flex flex-col items-start gap-5 sm:flex-row sm:items-center sm:justify-between">
                    <div>
                      <h3 className="font-bold">还没有自由制作项目</h3>
                      <p className="mt-2 text-sm leading-6 text-white/45">
                        可以直接新建一轮动态漫，系统只会在提交表单后创建对应资源。
                      </p>
                    </div>
                    <Button variant="dark" onClick={() => navigate(newRunPath)}>
                      新建动态漫
                      <ArrowRight className="h-4 w-4" />
                    </Button>
                  </div>
                </Card>
              )}
            </section>
          </>
        ) : (
          <>
            <section className="flex flex-col justify-between gap-6 border-b border-white/10 pb-8 lg:flex-row lg:items-end">
              <div>
                <button
                  type="button"
                  className="mb-4 text-xs font-bold text-[#f29a70] hover:text-white"
                  onClick={() => navigate("/free-create")}
                >
                  ← 返回自由制作项目
                </button>
                <p className="text-xs font-bold uppercase tracking-[0.2em] text-[#f29a70]">
                  Dynamic runs
                </p>
                <h2 className="mt-3 text-4xl font-extrabold tracking-[-0.05em]">
                  运行列表
                </h2>
                <p className="mt-3 text-sm leading-6 text-white/50">
                  项目 ID：{projectId}。运行创建后才会进入这里。
                </p>
              </div>
              <Button
                variant="dark"
                className="self-start border-[#f29a70]/40 text-[#ffd6c2] hover:bg-[#f29a70]/15 lg:self-auto"
                onClick={() => navigate(newRunPath)}
              >
                <Plus className="h-4 w-4" />
                新建动态漫
              </Button>
            </section>

            {runs.length ? (
              <section className="mt-10 grid gap-4 md:grid-cols-2 xl:grid-cols-3" aria-label="动态漫运行列表">
                {runs.map((run) => (
                  <button
                    type="button"
                    key={run.id}
                    className="group min-h-52 rounded-xl border border-white/10 bg-[#1b2521] p-5 text-left transition hover:-translate-y-0.5 hover:border-[#f29a70]/60 hover:bg-[#202c27] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#f29a70]"
                    onClick={() => navigate(`/free-create/${projectId}/pipeline/${run.id}`)}
                  >
                    <div className="flex items-start justify-between gap-4">
                      <span className="flex h-10 w-10 items-center justify-center rounded-lg border border-white/10 bg-white/[0.04] text-[#f29a70]">
                        <Workflow className="h-5 w-5" />
                      </span>
                      <RunStatus status={run.status} />
                    </div>
                    <h3 className="mt-7 truncate text-base font-bold">
                      {getDynamicRunTitle(run)}
                    </h3>
                    <div className="mt-3 flex flex-wrap gap-x-4 gap-y-2 text-xs text-white/45">
                      <span className="inline-flex items-center gap-1.5">
                        <Clock3 className="h-3.5 w-3.5" />
                        {formatDate(run.updated_at)}
                      </span>
                      <span>修订 R{run.revision}</span>
                    </div>
                    <span className="mt-5 inline-flex items-center gap-1 text-xs font-bold text-[#f29a70]">
                      打开工作区
                      <ArrowRight className="h-3.5 w-3.5 transition group-hover:translate-x-1" />
                    </span>
                  </button>
                ))}
              </section>
            ) : (
              <Card className="mt-10 border-white/10 bg-[#1b2521] p-10 text-white">
                <div className="mx-auto max-w-xl text-center">
                  <Workflow className="mx-auto h-9 w-9 text-[#f29a70]" />
                  <h3 className="mt-5 text-lg font-bold">这个项目还没有运行</h3>
                  <p className="mt-2 text-sm leading-6 text-white/45">
                    新建后会先保存输入快照，再进入 8 个阶段的工作区。
                  </p>
                  <Button variant="dark" className="mt-6" onClick={() => navigate(newRunPath)}>
                    新建动态漫
                    <ArrowRight className="h-4 w-4" />
                  </Button>
                </div>
              </Card>
            )}
          </>
        )}

        <button
          type="button"
          className="mt-10 inline-flex items-center gap-2 text-xs font-semibold text-white/35 hover:text-white"
          onClick={() => window.location.reload()}
        >
          <RefreshCw className="h-3.5 w-3.5" />
          重新读取
        </button>
      </main>
    </div>
  );
}
