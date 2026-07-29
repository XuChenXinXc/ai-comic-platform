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
          ? "border-[#9cb79c] bg-[#eef5eb] text-[#355d39]"
          : status === "failed" || status === "interrupted"
            ? "border-[#e0a39e] bg-[#fff0ee] text-[#9b342c]"
            : [
                  "queued",
                  "running",
                  "generating",
                  "waiting_provider",
                  "downloading",
                ].includes(status)
              ? "border-[#d9ae7f] bg-[#fff3e5] text-[#8b4b1e]"
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
        "flex gap-2 border border-[#e2aa88] bg-[#fff3e9] text-[#833f24]",
        compact ? "rounded-md px-3 py-2 text-xs" : "rounded-lg px-4 py-3 text-sm",
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
    <Card className="flex min-h-52 flex-col items-center justify-center border-[#e1b3ad] bg-[#fff8f6] p-8 text-center">
      <AlertTriangle className="mb-4 h-7 w-7 text-[#b34539]" />
      <h3 className="text-base font-bold text-ink">{title}</h3>
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
        "flex flex-col items-center justify-center border-dashed bg-white/35 text-center",
        compact ? "min-h-32 p-5" : "min-h-64 p-10",
      )}
    >
      <FileQuestion
        className={cn("text-[#92958e]", compact ? "h-5 w-5" : "h-8 w-8")}
      />
      <h3 className="mt-3 text-base font-bold text-ink">{title}</h3>
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
    <header className="mb-7 flex flex-col justify-between gap-5 border-b border-line pb-6 xl:flex-row xl:items-end">
      <div className="max-w-3xl">
        <p className="mb-2 text-[11px] font-bold uppercase tracking-[0.2em] text-vermilion">
          {eyebrow}
        </p>
        <h1 className="text-3xl font-extrabold tracking-[-0.035em] text-ink md:text-4xl">
          {title}
        </h1>
        <p className="mt-3 text-sm leading-6 text-muted md:text-base">
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
  return <p className="mt-1.5 text-xs text-[#a4362d]">{children}</p>;
}
