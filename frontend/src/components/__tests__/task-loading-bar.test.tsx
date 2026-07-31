import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, test } from "vitest";
import type { Job } from "../../types/api";
import { TaskLoadingBar } from "../task-loading-bar";

function makeJob(overrides: Partial<Job> = {}): Job {
  return {
    id: "job-1",
    project_id: "project-1",
    job_type: "generate_keyframe",
    target_type: "keyframe",
    target_id: "keyframe-1",
    provider_kind: "image",
    provider_id: "mock-image",
    status: "running",
    progress_current: 0,
    progress_total: 1,
    message: "任务执行中",
    retry_count: 0,
    created_at: "2026-07-29T00:00:00Z",
    updated_at: "2026-07-29T00:00:01Z",
    ...overrides,
  };
}

function renderBar(jobs: Job[]) {
  return render(
    <MemoryRouter>
      <TaskLoadingBar jobs={jobs} />
    </MemoryRouter>,
  );
}

describe("TaskLoadingBar", () => {
  test("test_without_active_jobs_hides_status_bar", () => {
    renderBar([makeJob({ status: "completed" })]);

    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  test("test_single_active_job_shows_type_status_and_target", () => {
    renderBar([
      makeJob({
        target_name: "分镜 3",
        status: "waiting_provider",
        message: "等待图片服务商返回结果",
      }),
    ]);

    expect(screen.getByRole("status")).toHaveTextContent("关键帧生成");
    expect(screen.getByRole("status")).toHaveTextContent("分镜 3");
    expect(screen.getByRole("status")).toHaveTextContent("等待服务商");
  });

  test("test_single_batch_shows_terminal_item_progress", () => {
    renderBar([
      makeJob({ id: "job-1", batch_id: "batch-1", status: "completed" }),
      makeJob({ id: "job-2", batch_id: "batch-1", status: "running" }),
      makeJob({ id: "job-3", batch_id: "batch-1", status: "queued" }),
    ]);

    expect(screen.getByRole("status")).toHaveTextContent("2 个任务处理中");
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "33");
    expect(screen.getByRole("status")).toHaveTextContent("批次进度 1/3");
  });

  test("test_status_bar_links_to_jobs_page", () => {
    renderBar([makeJob()]);

    expect(
      screen.getByRole("link", { name: "查看任务中心" }),
    ).toHaveAttribute("href", "/jobs");
  });
});
