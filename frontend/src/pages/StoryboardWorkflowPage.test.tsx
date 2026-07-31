import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { api } from "../api/client";
import type { Shot } from "../types/api";
import { KeyframesPage } from "./KeyframesPage";
import { ShotsPage } from "./ShotsPage";
import { VideosPage } from "./VideosPage";

vi.mock("sonner", () => ({
  toast: { error: vi.fn(), success: vi.fn(), warning: vi.fn() },
}));
vi.mock("../hooks/use-project", () => ({
  useProject: () => ({
    activeProject: {
      id: "project-1",
      name: "统一分镜项目",
      source_signature: "source-1",
      default_duration_sec: 6,
    },
    activeProjectId: "project-1",
  }),
}));
vi.mock("../components/project-required", () => ({
  ProjectRequired: ({ children }: { children: ReactNode }) => children,
}));
vi.mock("../components/shot-media-card", () => ({
  ShotMediaCard: ({
    shot,
    kind,
  }: {
    shot: Shot;
    kind: "keyframe" | "video";
  }) => (
    <div
      aria-label={`分镜 ${shot.shot_no} ${
        kind === "keyframe" ? "关键帧区块" : "视频区块"
      }`}
    />
  ),
}));

const shot: Shot = {
  id: "shot-1",
  project_id: "project-1",
  shot_no: 1,
  shot_order: 1,
  source_type: "novel",
  source_ref: "novel",
  source_signature: "source-1",
  character_ids_json: [],
  prop_ids_json: [],
  positions_json: [],
  start_state: "主角站在门外。",
  expressions_json: [],
  dialogue_json: [],
  action: "主角推开房门。",
  narration: "",
  frame_description: "主角站在旧屋门口。",
  image_prompt: "主角站在旧屋门口，动作发生前的稳定起始状态。",
  compiled_image_prompt: "【当前分镜素材参考图】\n首帧关键构图：主角站在旧屋门口。",
  video_prompt: "0-2秒：主角推开房门。",
  compiled_video_prompt: "【执行】\n0-2秒：主角推开房门。\n【镜头与声音】",
  negative_prompt: "身份漂移，服装变化，文字，水印",
  duration_sec: 6,
  shot_type: "中景",
  camera_angle: "平视",
  camera_move: "固定",
  end_state: "主角进入屋内。",
  next_hook: "",
  reference_bindings_json: [],
  status: "ready",
  own_signature: "shot-signature-1",
  revision: 1,
  created_at: "2026-07-29T00:00:00Z",
  updated_at: "2026-07-29T00:00:00Z",
};

function renderShotsPage() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <MemoryRouter initialEntries={["/shots"]}>
      <QueryClientProvider client={queryClient}>
        <ShotsPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api.shots, "list").mockResolvedValue([shot]);
  vi.spyOn(api.assets, "list").mockResolvedValue([]);
  vi.spyOn(api.shots, "extract").mockResolvedValue({
    id: "job-extract",
  } as never);
});

describe("分镜视频统一页面", () => {
  test("test_shot_page_contains_keyframe_and_video_generation", async () => {
    renderShotsPage();

    expect(
      await screen.findByRole("heading", { name: "分镜视频" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "生成全部关键帧" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "生成全部视频" }),
    ).toBeInTheDocument();
    expect(
      await screen.findByText(/【当前分镜素材参考图】/),
    ).toBeInTheDocument();
    expect(await screen.findByText(/【镜头与声音】/)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /生成.*提示词|补齐.*提示词/ }),
    ).not.toBeInTheDocument();
    expect(
      await screen.findByLabelText("分镜 1 关键帧区块"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("分镜 1 视频区块")).toBeInTheDocument();
  });

  test("test_shot_extraction_has_single_screenplay_source_without_selector", async () => {
    const user = userEvent.setup();
    renderShotsPage();

    await screen.findByRole("heading", { name: "分镜视频" });
    expect(screen.queryByText("分镜来源")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "从小说提取" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "从当前剧本提取" }),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /提取/ }));

    await waitFor(() => {
      expect(api.shots.extract).toHaveBeenCalledWith("project-1", {
        scope: "all",
      });
    });
  });

  test("test_shot_card_displays_dialogue_aliases_with_full_text", async () => {
    vi.mocked(api.shots.list).mockResolvedValue([
      {
        ...shot,
        dialogue_json: [
          { speaker: "小明", line: "门外有人。" },
          { character: "小红", content: "别出声。" },
          { name: "店主", text: "别碰那个盒子。" },
        ],
      },
    ]);

    renderShotsPage();

    const label = await screen.findByText("对白 / 旁白");
    const dialogue = label.nextElementSibling;
    expect(dialogue).toHaveTextContent("小明：门外有人。");
    expect(dialogue).toHaveTextContent("小红：别出声。");
    expect(dialogue).toHaveTextContent("店主：别碰那个盒子。");
  });

  test("test_shot_card_displays_positions_gaze_and_expressions", async () => {
    vi.mocked(api.shots.list).mockResolvedValue([
      {
        ...shot,
        positions_json: [
          {
            character: "小明",
            position: "画面左侧",
            facing: "面向右侧",
            gaze: "看向小红",
          },
        ],
        expressions_json: [
          { character: "小明", expression: "紧张警觉" },
          { name: "小红", emotion: "压低声音" },
        ],
      },
    ]);

    renderShotsPage();

    await screen.findByText("主角推开房门。");
    expect(document.body).toHaveTextContent("画面左侧");
    expect(document.body).toHaveTextContent("面向右侧");
    expect(document.body).toHaveTextContent("看向小红");
    expect(document.body).toHaveTextContent("紧张警觉");
    expect(document.body).toHaveTextContent("压低声音");
  });

  test.each([
    ["/keyframes", <KeyframesPage />],
    ["/videos", <VideosPage />],
  ])("test_legacy_route_%s_redirects_to_shots", async (path, page) => {
    render(
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/keyframes" element={page} />
          <Route path="/videos" element={page} />
          <Route path="/shots" element={<h1>统一分镜视频</h1>} />
        </Routes>
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("heading", { name: "统一分镜视频" }),
    ).toBeInTheDocument();
  });
});
