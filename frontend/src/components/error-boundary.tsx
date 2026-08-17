import { Component, ErrorInfo } from "react";
import type { ReactNode } from "react";
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
        <div className="flex min-h-screen items-center justify-center bg-gray-50 px-4">
          <div className="w-full max-w-md rounded-lg border border-red-200 bg-white p-6 shadow-lg">
            <div className="mb-4 flex items-center gap-3">
              <div className="flex h-12 w-12 items-center justify-center rounded-full bg-red-100">
                <svg
                  className="h-6 w-6 text-red-600"
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
                <h1 className="text-lg font-semibold text-gray-900">页面出错了</h1>
                <p className="text-sm text-gray-600">应用遇到了一个意外错误</p>
              </div>
            </div>

            <div className="mb-4 rounded-md bg-gray-50 p-3">
              <p className="text-sm font-mono text-gray-700 break-words">
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
                <summary className="cursor-pointer text-sm text-gray-600 hover:text-gray-900">
                  查看详细错误信息
                </summary>
                <pre className="mt-2 overflow-auto rounded-md bg-gray-900 p-3 text-xs text-gray-100">
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
