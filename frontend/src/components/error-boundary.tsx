import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";
import { Button } from "./ui";

interface Props {
  children: ReactNode;
  fallback?: (error: Error, resetError: () => void) => ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    console.error("ErrorBoundary caught an error:", error, errorInfo);
  }

  resetError = () => {
    this.setState({ hasError: false, error: null });
  };

  render() {
    if (this.state.hasError && this.state.error) {
      if (this.props.fallback) {
        return this.props.fallback(this.state.error, this.resetError);
      }

      return (
        <div className="flex min-h-screen items-center justify-center bg-canvas px-4">
          <div className="w-full max-w-md rounded-card border border-black/[0.06] bg-paper p-6 shadow-apple dark:border-white/10 dark:bg-[#101010] dark:shadow-appleDark">
            <div className="mb-4 flex items-center gap-3">
              <div className="flex h-12 w-12 items-center justify-center rounded-full bg-red-500/10">
                <svg
                  className="h-6 w-6 text-red-500"
                  fill="none"
                  stroke="currentColor"
                  viewBox="0 0 24 24"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"
                  />
                </svg>
              </div>
              <div>
                <h1 className="text-lg font-semibold text-ink">页面出错了</h1>
                <p className="text-sm text-muted">应用遇到了一个意外错误</p>
              </div>
            </div>

            <div className="mb-4 rounded-xl bg-black/[0.03] p-3 dark:bg-white/5">
              <p className="text-sm font-mono text-muted break-words">
                {this.state.error.message}
              </p>
            </div>

            <div className="flex gap-2">
              <Button onClick={this.resetError} className="flex-1">
                重试
              </Button>
              <Button
                variant="secondary"
                onClick={() => window.location.reload()}
                className="flex-1"
              >
                刷新页面
              </Button>
            </div>

            {process.env.NODE_ENV === "development" && this.state.error.stack && (
              <details className="mt-4">
                <summary className="cursor-pointer text-sm text-muted hover:text-ink">
                  查看详细错误信息
                </summary>
                <pre className="mt-2 overflow-auto rounded-xl bg-black p-3 text-xs text-white/85">
                  {this.state.error.stack}
                </pre>
              </details>
            )}
          </div>
        </div>
      );
    }

    return this.props.children;
  }
}
