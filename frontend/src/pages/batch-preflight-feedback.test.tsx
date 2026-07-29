import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { api } from "../api/client";
import type { Asset, Job, Shot } from "../types/api";
import { AssetImagesPage } from "./AssetImagesPage";
import { AssetsPage } from "./AssetsPage";
import { KeyframesPage } from "./KeyframesPage";
import { ShotsPage } from "./ShotsPage";
import { VideosPage } from "./VideosPage";

const toastMock = vi.hoisted(() => ({
  error: vi.fn(),
  success: vi.fn(),
  warning: vi.fn(),
}));

vi.mock("sonner", () => ({ toast: toastMock }));
vi.mock("../hooks/use-project", () => ({
  useProject: () => ({
    activeProject: { id: "project-1", name: "验收项目" },
    activeProjectId: "project-1",
  }),
}));
vi.mock("../components/project-required", () => ({
  ProjectRequired: ({ children }: { children: ReactNode }) => children,
}));
vi.mock("../components/shot-media-card", () => ({
  ShotMediaCard: () => null,
}));

const shots = [
  {
    id: "shot-1",
    project_id: "project-1",
    shot_no: 1,
    shot_order: 1,
  },
  {
    id: "shot-2",
    project_id: "project-1",
    shot_no: 2,
    shot_order: 2,
  },
] as Shot[];

const queuedJob = {
  id: "job-1",
  project_id: "project-1",
  job_type: "generate_media",
  target_type: "shot",
  provider_kind: "image",
  status: "queued",
  progress_current: 0,
  progress_total: 1,
  message: "已入队",
  retry_count: 0,
  created_at: "2026-07-28T10:00:00Z",
  updated_at: "2026-07-28T10:00:00Z",
} as Job;

const assets = [
  {
    id: "asset-1",
    project_id: "project-1",
    asset_type: "character",
    name: "林昭",
    prompt: "角色定妆图",
  },
  {
    id: "asset-2",
    project_id: "project-1",
    asset_type: "character",
    name: "顾临",
    prompt: "",
  },
] as Asset[];

function renderPage(page: ReactNode) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>{page}</QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
  toastMock.error.mockReset();
  toastMock.success.mockReset();
  toastMock.warning.mockReset();
  vi.spyOn(api.shots, "list").mockResolvedValue(shots);
  vi.spyOn(api.assets, "list").mockResolvedValue([]);
  vi.spyOn(api.assets.images, "list").mockResolvedValue([]);
});

describe("批量生成预检反馈", () => {
  test("test_keyframe_batch_with_preflight_failure_shows_queued_count_and_reason", async () => {
    vi.spyOn(api.shots.keyframes, "batch").mockResolvedValue({
      batch_id: "batch-1",
      jobs: [queuedJob],
      preflight_failures: [
        { target_id: "shot-2", error: "关键帧提示词为空" },
      ],
    });
    renderPage(<KeyframesPage />);

    await userEvent.click(
      await screen.findByRole("button", { name: "生成全部关键帧" }),
    );

    await waitFor(() => {
      expect(toastMock.warning).toHaveBeenCalledWith(
        "已入队 1 个关键帧任务，跳过 1 项",
        { description: "分镜 2：关键帧提示词为空" },
      );
    });
  });

  test("test_video_batch_with_preflight_failure_shows_queued_count_and_reason", async () => {
    vi.spyOn(api.shots.videos, "batch").mockResolvedValue({
      batch_id: "batch-2",
      jobs: [queuedJob],
      preflight_failures: [
        { target_id: "shot-2", error: "请先生成或上传关键帧" },
      ],
    });
    renderPage(<VideosPage />);

    await userEvent.click(
      await screen.findByRole("button", { name: "生成全部视频" }),
    );

    await waitFor(() => {
      expect(toastMock.warning).toHaveBeenCalledWith(
        "已入队 1 个视频任务，跳过 1 项",
        { description: "分镜 2：请先生成或上传关键帧" },
      );
    });
  });

  test("test_asset_image_batch_with_preflight_failure_shows_queued_count_and_reason", async () => {
    vi.mocked(api.assets.list).mockResolvedValue(assets);
    vi.spyOn(api.assets.images, "batch").mockResolvedValue({
      batch_id: "batch-3",
      jobs: [queuedJob],
      preflight_failures: [
        { target_id: "asset-2", error: "图片提示词为空" },
      ],
    });
    renderPage(<AssetImagesPage />);

    await userEvent.click(
      await screen.findByRole("button", { name: "批量生成角色图" }),
    );

    await waitFor(() => {
      expect(toastMock.warning).toHaveBeenCalledWith(
        "已入队 1 个素材图片任务，跳过 1 项",
        { description: "素材 顾临：图片提示词为空" },
      );
    });
  });

  test("test_asset_prompt_batch_with_preflight_failure_shows_queued_count_and_reason", async () => {
    vi.mocked(api.assets.list).mockResolvedValue(assets);
    vi.spyOn(api.assets, "batchPrompts").mockResolvedValue({
      batch_id: "batch-4",
      jobs: [queuedJob],
      preflight_failures: [
        { target_id: "asset-2", error: "素材档案不完整" },
      ],
    });
    renderPage(<AssetsPage />);

    await userEvent.click(
      await screen.findByRole("button", { name: "批量补齐提示词" }),
    );

    await waitFor(() => {
      expect(toastMock.warning).toHaveBeenCalledWith(
        "已入队 1 个素材提示词任务，跳过 1 项",
        { description: "素材 顾临：素材档案不完整" },
      );
    });
  });

  test("test_shot_prompt_batch_with_preflight_failure_shows_queued_count_and_reason", async () => {
    vi.spyOn(api.shots, "batchPrompts").mockResolvedValue({
      batch_id: "batch-5",
      jobs: [queuedJob],
      preflight_failures: [
        { target_id: "shot-2", error: "分镜动作为空" },
      ],
    });
    renderPage(<ShotsPage />);

    await userEvent.click(
      await screen.findByRole("button", { name: "批量补齐提示词" }),
    );

    await waitFor(() => {
      expect(toastMock.warning).toHaveBeenCalledWith(
        "已入队 1 个分镜提示词任务，跳过 1 项",
        { description: "分镜 2：分镜动作为空" },
      );
    });
  });
});
