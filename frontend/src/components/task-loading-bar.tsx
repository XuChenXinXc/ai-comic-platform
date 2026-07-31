import { LoaderCircle, ListChecks } from "lucide-react";
import { Link } from "react-router-dom";
import { isJobActive } from "../hooks/use-job-polling";
import { jobTypeLabel } from "../lib/job-labels";
import type { Job } from "../types/api";
import { StatusBadge } from "./status";
import { Progress } from "./ui";

const TERMINAL_STATUSES = new Set([
  "completed",
  "failed",
  "cancelled",
  "interrupted",
]);

function batchProgress(jobs: Job[], activeJobs: Job[]) {
  const batchIds = new Set(
    activeJobs.map((job) => job.batch_id).filter((value): value is string => Boolean(value)),
  );
  if (batchIds.size !== 1) return null;
  const [batchId] = batchIds;
  if (!activeJobs.every((job) => job.batch_id === batchId)) return null;
  const batchJobs = jobs.filter((job) => job.batch_id === batchId);
  if (batchJobs.length <= 1) return null;
  const processed = batchJobs.filter((job) => TERMINAL_STATUSES.has(job.status)).length;
  return {
    label: `批次进度 ${processed}/${batchJobs.length}`,
    value: Math.round((processed / batchJobs.length) * 100),
  };
}

function jobProgress(job: Job) {
  if (job.progress_total <= 1) return null;
  return {
    label: `任务进度 ${job.progress_current}/${job.progress_total}`,
    value: Math.round((job.progress_current / job.progress_total) * 100),
  };
}

export function TaskLoadingBar({ jobs }: { jobs: Job[] }) {
  const activeJobs = jobs.filter((job) => isJobActive(job.status));
  if (!activeJobs.length) return null;

  const singleJob = activeJobs.length === 1 ? activeJobs[0] : null;
  const progress = batchProgress(jobs, activeJobs) || (singleJob ? jobProgress(singleJob) : null);
  const title = singleJob
    ? jobTypeLabel(singleJob.job_type)
    : `${activeJobs.length} 个任务处理中`;
  const description = progress?.label
    ? `${progress.label} · ${singleJob?.message || "后台任务持续执行中"}`
    : singleJob
      ? [singleJob.target_name, singleJob.message].filter(Boolean).join(" · ")
      : [...new Set(activeJobs.map((job) => jobTypeLabel(job.job_type)))].join("、");

  return (
    <aside
      role="status"
      aria-live="polite"
      className="sticky top-0 z-10 mb-5 overflow-hidden rounded-lg border border-[#e2aa88] bg-[#fff8f2]/95 shadow-sm backdrop-blur"
    >
      <div className="flex items-center gap-3 px-4 py-3">
        <LoaderCircle className="h-5 w-5 shrink-0 animate-spin text-vermilion" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-sm font-bold text-ink">{title}</p>
            {singleJob ? <StatusBadge status={singleJob.status} /> : null}
          </div>
          <p className="mt-0.5 truncate text-xs text-muted">
            {description || "后台任务持续执行中"}
          </p>
        </div>
        <Link
          to="/jobs"
          aria-label="查看任务中心"
          className="flex shrink-0 items-center gap-1.5 rounded-md border border-[#d9ae7f] bg-white px-3 py-2 text-xs font-bold text-[#7b431d] hover:bg-[#fff3e5]"
        >
          <ListChecks className="h-4 w-4" />
          <span className="hidden sm:inline">任务中心</span>
        </Link>
      </div>
      <div className="px-4 pb-3">
        {progress ? (
          <Progress value={progress.value} />
        ) : (
          <div
            role="progressbar"
            aria-label="任务正在处理"
            className="h-1.5 overflow-hidden rounded-full bg-black/10"
          >
            <div className="h-full w-1/2 animate-pulse rounded-full bg-vermilion" />
          </div>
        )}
      </div>
    </aside>
  );
}
