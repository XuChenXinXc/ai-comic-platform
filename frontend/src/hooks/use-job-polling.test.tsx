import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, test, vi } from "vitest";
import { api } from "../api/client";
import type { Job } from "../types/api";
import { useJobPolling } from "./use-job-polling";

const completedJob: Job = {
  id: "job-1",
  project_id: "project-1",
  job_type: "generate_analysis",
  target_type: "analysis",
  provider_kind: "text",
  status: "completed",
  progress_current: 1,
  progress_total: 1,
  message: "任务完成",
  retry_count: 0,
  created_at: "2026-07-28T10:00:00Z",
  updated_at: "2026-07-28T10:00:01Z",
};

afterEach(() => {
  vi.restoreAllMocks();
});

describe("任务轮询", () => {
  test("test_first_observed_terminal_job_invalidates_generated_content_queries", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    vi.spyOn(api.jobs, "list").mockResolvedValue([completedJob]);

    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>
        {children}
      </QueryClientProvider>
    );
    const { result } = renderHook(() => useJobPolling("project-1"), {
      wrapper,
    });

    await waitFor(() => {
      expect(result.current.data).toEqual([completedJob]);
    });
    await waitFor(() => {
      expect(invalidateSpy).toHaveBeenCalledWith({
        predicate: expect.any(Function),
      });
    });
  });
});
