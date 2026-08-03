import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, expect, test, vi } from "vitest";
import { api } from "../api/client";
import { ProjectPage } from "./ProjectPage";

const projectContext = vi.hoisted(() => ({
  activeProject: {
    id: "project-1",
    name: "固定生产规格项目",
    storage_path: "D:\\漫画项目\\project-1",
    source_text: "小说原文",
    source_signature: "source-1",
    source_revision: 1,
    style_prompt: "二维国风漫剧",
    aspect_ratio: "16:9",
    image_size: "3840x2160",
    video_size: "16:9",
    default_duration_sec: 6,
    default_text_provider_id: null,
    default_image_provider_id: null,
    default_video_provider_id: null,
    created_at: "2026-07-29T00:00:00Z",
    updated_at: "2026-07-29T00:00:00Z",
  },
  activeProjectId: "project-1",
  selectProject: vi.fn(),
  projects: [],
  isLoading: false,
  error: null,
}));

vi.mock("sonner", () => ({
  toast: { error: vi.fn(), success: vi.fn(), warning: vi.fn() },
}));

vi.mock("../hooks/use-project", () => ({
  useProject: () => projectContext,
}));

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api.providers, "list").mockResolvedValue([]);
});

test("test_project_page_hides_production_baseline_controls", async () => {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <ProjectPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );

  expect(
    await screen.findByRole("heading", { name: "项目小说" }),
  ).toBeInTheDocument();
  expect(screen.queryByText("生产基准")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("统一画风 STYLE_ANCHOR")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("画面比例")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("默认镜头时长")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("图片尺寸")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("视频尺寸 / 比例")).not.toBeInTheDocument();
});

test("test_project_page_displays_storage_path", async () => {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <ProjectPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );

  await screen.findByRole("heading", { name: "项目小说" });
  expect(screen.getByLabelText("项目存储路径")).toHaveValue(
    "D:\\漫画项目\\project-1",
  );
});
