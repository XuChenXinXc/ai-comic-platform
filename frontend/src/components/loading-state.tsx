/**
 * 加载状态组件
 *
 * 统一的加载、错误、空状态显示
 */

import { Button } from "./ui";

interface LoadingStateProps {
  isLoading?: boolean;
  isError?: boolean;
  error?: Error | null;
  isEmpty?: boolean;
  loadingMessage?: string;
  errorMessage?: string;
  emptyMessage?: string;
  onRetry?: () => void;
  children: React.ReactNode;
}

export function LoadingState({
  isLoading = false,
  isError = false,
  error = null,
  isEmpty = false,
  loadingMessage = "加载中...",
  errorMessage,
  emptyMessage = "暂无数据",
  onRetry,
  children,
}: LoadingStateProps) {
  if (isLoading) {
    return (
      <div className="flex min-h-[200px] items-center justify-center">
        <div className="text-center">
          <div className="mb-4 inline-block h-12 w-12 animate-spin rounded-full border-4 border-gray-300 border-t-blue-500" />
          <p className="text-sm text-gray-600">{loadingMessage}</p>
        </div>
      </div>
    );
  }

  if (isError) {
    const displayErrorMessage =
      errorMessage || error?.message || "加载失败，请重试";

    return (
      <div className="flex min-h-[200px] items-center justify-center">
        <div className="text-center">
          <div className="mb-4 flex justify-center">
            <div className="flex h-16 w-16 items-center justify-center rounded-full bg-red-100">
              <svg
                className="h-8 w-8 text-red-600"
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
                />
              </svg>
            </div>
          </div>
          <p className="mb-4 text-sm text-gray-700">{displayErrorMessage}</p>
          {onRetry && (
            <Button onClick={onRetry} variant="secondary">
              重试
            </Button>
          )}
        </div>
      </div>
    );
  }

  if (isEmpty) {
    return (
      <div className="flex min-h-[200px] items-center justify-center">
        <div className="text-center">
          <div className="mb-4 flex justify-center">
            <svg
              className="h-16 w-16 text-gray-400"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7m16 0v5a2 2 0 01-2 2H6a2 2 0 01-2-2v-5m16 0h-2.586a1 1 0 00-.707.293l-2.414 2.414a1 1 0 01-.707.293h-3.172a1 1 0 01-.707-.293l-2.414-2.414A1 1 0 006.586 13H4"
              />
            </svg>
          </div>
          <p className="text-sm text-gray-600">{emptyMessage}</p>
        </div>
      </div>
    );
  }

  return <>{children}</>;
}

/**
 * 内联加载指示器
 *
 * 用于按钮等小区域的加载状态
 */
interface SpinnerProps {
  size?: "sm" | "md" | "lg";
  className?: string;
}

export function Spinner({ size = "md", className = "" }: SpinnerProps) {
  const sizeClasses = {
    sm: "h-4 w-4 border-2",
    md: "h-6 w-6 border-2",
    lg: "h-8 w-8 border-4",
  };

  return (
    <div
      className={`animate-spin rounded-full border-gray-300 border-t-blue-500 ${sizeClasses[size]} ${className}`}
    />
  );
}

/**
 * 骨架屏组件
 *
 * 占位加载效果
 */
interface SkeletonProps {
  width?: string;
  height?: string;
  className?: string;
}

export function Skeleton({
  width = "100%",
  height = "1rem",
  className = "",
}: SkeletonProps) {
  return (
    <div
      className={`animate-pulse rounded bg-gray-200 ${className}`}
      style={{ width, height }}
    />
  );
}

/**
 * 卡片骨架屏
 */
export function CardSkeleton() {
  return (
    <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
      <Skeleton width="60%" height="1.5rem" className="mb-2" />
      <Skeleton width="100%" height="1rem" className="mb-2" />
      <Skeleton width="100%" height="1rem" className="mb-2" />
      <Skeleton width="80%" height="1rem" />
    </div>
  );
}
