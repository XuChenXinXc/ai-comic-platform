import {
  AlertTriangle,
  FileQuestion,
  LoaderCircle,
  RefreshCw,
} from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "../lib/utils";
import { Badge, Button, Card } from "./ui";

const statusLabels: Record<string, string> = {
  draft: "草稿",
  generating: "生成中",
  ready: "待确认",
  confirmed: "已确认",
  queued: "排队中",
  running: "执行中",
  waiting_provider: "等待服务商",
  downloading: "下载中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
};

export function StatusBadge({
  status,
  className,
}: {
  status: string;
  className?: string;
}) {
  return (
    <Badge
      className={cn(
        status === "confirmed" || status === "completed"
          ? "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-300"
          : status === "failed" || status === "interrupted"
            ? "border-red-200 bg-red-50 text-red-700 dark:border-red-500/30 dark:bg-red-500/10 dark:text-red-300"
            : [
                  "queued",
                  "running",
                  "generating",
                  "waiting_provider",
                  "downloading",
                ].includes(status)
              ? "border-sky-200 bg-sky-50 text-sky-700 dark:border-sky-500/30 dark:bg-sky-500/10 dark:text-sky-300"
              : "",
        className,
      )}
    >
      {statusLabels[status] || status}
    </Badge>
  );
}

export function StaleNotice({
  reason = "上游内容已经变化，当前结果仍可使用，但建议检查或重新生成。",
  compact,
}: {
  reason?: string;
  compact?: boolean;
}) {
  return (
    <div
      className={cn(
        "flex gap-2 border border-sky-200 bg-sky-50 text-sky-800 dark:border-sky-500/25 dark:bg-sky-500/10 dark:text-sky-300",
        compact ? "rounded-xl px-3 py-2 text-xs" : "rounded-2xl px-4 py-3 text-sm",
      )}
    >
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
      <span>{reason}</span>
    </div>
  );
}

export function LoadingState({ label = "正在读取工作区…" }: { label?: string }) {
  return (
    <div className="flex min-h-48 items-center justify-center gap-3 text-sm text-muted">
      <LoaderCircle className="h-5 w-5 animate-spin text-vermilion" />
      {label}
    </div>
  );
}

export function ErrorState({
  title = "内容加载失败",
  message,
  onRetry,
}: {
  title?: string;
  message: string;
  onRetry?: () => void;
}) {
  return (
    <Card className="flex min-h-52 flex-col items-center justify-center border-red-100 bg-red-50/40 p-8 text-center dark:border-red-500/20 dark:bg-red-500/[0.06]">
      <div className="flex h-14 w-14 items-center justify-center rounded-full bg-red-500/10">
        <AlertTriangle className="h-6 w-6 text-red-500" />
      </div>
      <h3 className="mt-4 text-base font-semibold text-ink">{title}</h3>
      <p className="mt-2 max-w-xl text-sm leading-6 text-muted">{message}</p>
      {onRetry ? (
        <Button className="mt-5" onClick={onRetry}>
          <RefreshCw className="h-4 w-4" />
          重新加载
        </Button>
      ) : null}
    </Card>
  );
}

export function EmptyState({
  title,
  description,
  action,
  compact,
}: {
  title: string;
  description: string;
  action?: ReactNode;
  compact?: boolean;
}) {
  return (
    <Card
      className={cn(
        "flex flex-col items-center justify-center border-dashed border-black/15 bg-transparent text-center shadow-none hover:border-vermilion/40 dark:border-white/20 dark:bg-transparent dark:shadow-none",
        compact ? "min-h-32 p-5" : "min-h-64 p-10",
      )}
    >
      <div
        className={cn(
          "flex items-center justify-center rounded-[18px] bg-vermilion/10 text-vermilion",
          compact ? "h-9 w-9" : "h-14 w-14",
        )}
      >
        <FileQuestion className={compact ? "h-4 w-4" : "h-6 w-6"} />
      </div>
      <h3 className="mt-4 text-base font-semibold text-ink md:text-lg">
        {title}
      </h3>
      <p className="mt-2 max-w-lg text-sm leading-6 text-muted">{description}</p>
      {action ? <div className="mt-5">{action}</div> : null}
    </Card>
  );
}

export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow: string;
  title: string;
  description: string;
  actions?: ReactNode;
}) {
  return (
    <header className="mb-9 flex flex-col justify-between gap-5 md:flex-row md:items-end">
      <div className="max-w-3xl">
        <p className="mb-2.5 text-[12px] font-semibold uppercase tracking-[0.16em] text-vermilion">
          {eyebrow}
        </p>
        <h1 className="text-[32px] font-semibold leading-[1.05] tracking-[-0.04em] text-ink md:text-[40px]">
          {title}
        </h1>
        <p className="mt-3 text-sm leading-6 text-muted md:text-[15px]">
          {description}
        </p>
      </div>
      {actions ? (
        <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>
      ) : null}
    </header>
  );
}

export function FieldError({ children }: { children?: ReactNode }) {
  if (!children) return null;
  return <p className="mt-1.5 text-xs text-[#e0442e]">{children}</p>;
}
