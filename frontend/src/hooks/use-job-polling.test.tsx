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

const runningJob: Job = {
  ...completedJob,
  id: "job-2",
  status: "running",
  message: "任务执行中",
};

function createWrapper(queryClient: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("任务轮询", () => {
  test("test_initial_mount_with_terminal_jobs_skips_invalidation", async () => {
    // 挂载时已存在的终态任务是历史完成任务：只记录基线，不触发全量失效。
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    vi.spyOn(api.jobs, "list").mockResolvedValue([completedJob]);

    const { result } = renderHook(() => useJobPolling("project-1"), {
      wrapper: createWrapper(queryClient),
    });

    await waitFor(() => {
      expect(result.current.data).toEqual([completedJob]);
    });
    // 留出时间让基线记录 effect 运行完毕。
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(invalidateSpy).not.toHaveBeenCalled();
  });

  test("test_new_terminal_job_invalidates_current_project_queries", async () => {
    // 挂载后任务从 running 变为 completed：触发失效，且按 projectId 维度过滤。
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    let callCount = 0;
    vi.spyOn(api.jobs, "list").mockImplementation(async () => {
      callCount += 1;
      return callCount === 1 ? [runningJob] : [completedJob];
    });

    const { result } = renderHook(() => useJobPolling("project-1"), {
      wrapper: createWrapper(queryClient),
    });

    await waitFor(() => {
      expect(result.current.data).toEqual([runningJob]);
    });
    await result.current.refetch();
    await waitFor(() => {
      expect(result.current.data).toEqual([completedJob]);
    });

    await waitFor(() => {
      expect(invalidateSpy).toHaveBeenCalledWith({
        predicate: expect.any(Function),
      });
    });

    const predicateCall = invalidateSpy.mock.calls.find(
      (args) => typeof args[0]?.predicate === "function",
    );
    expect(predicateCall).toBeTruthy();
    const predicate = predicateCall![0]!.predicate as (
      query: { queryKey: readonly unknown[] },
    ) => boolean;
    // 项目维度的键精确匹配当前项目。
    expect(predicate({ queryKey: ["assets", "project-1", "all"] })).toBe(true);
    expect(predicate({ queryKey: ["chapters", "project-1"] })).toBe(true);
    expect(predicate({ queryKey: ["projects", "project-1"] })).toBe(true);
    expect(
      predicate({ queryKey: ["projects", "project-1", "scene-views"] }),
    ).toBe(true);
    // projects 全局列表仍然刷新。
    expect(predicate({ queryKey: ["projects"] })).toBe(true);
    // 其他项目的缓存不再被失效。
    expect(predicate({ queryKey: ["assets", "project-2", "all"] })).toBe(false);
    expect(predicate({ queryKey: ["chapters", "project-2"] })).toBe(false);
    expect(predicate({ queryKey: ["projects", "project-2"] })).toBe(false);
    // 章节/分镜维度无法映射项目，保持前缀失效。
    expect(predicate({ queryKey: ["shots", "chapter-1"] })).toBe(true);
    expect(predicate({ queryKey: ["keyframes", "shot-1"] })).toBe(true);
    expect(predicate({ queryKey: ["videos", "shot-1"] })).toBe(true);
    expect(predicate({ queryKey: ["analysis", "chapter-1"] })).toBe(true);
    expect(predicate({ queryKey: ["screenplay", "chapter-1"] })).toBe(true);
    // 无关前缀不失效。
    expect(predicate({ queryKey: ["jobs", "project-1"] })).toBe(false);
    expect(predicate({ queryKey: ["providers"] })).toBe(false);
  });
});
