import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { api } from "../api/client";
import type { Asset, Shot } from "../types/api";
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

function assetWithImage(
  id: string,
  name: string,
  assetType: Asset["asset_type"],
  imageId: string,
): Asset {
  return {
    id,
    project_id: "project-1",
    asset_type: assetType,
    name,
    profile_json: {},
    prompt: `${name}提示词`,
    compiled_prompt: `${name}最终提示词`,
    negative_prompt: "",
    consistency_json: [],
    composition: "",
    importance: "core",
    status: "ready",
    source_signature: "source-1",
    own_signature: `${id}-signature`,
    revision: 1,
    current_image: {
      id: imageId,
      asset_id: id,
      version_no: 1,
      file_url: `/media/${imageId}.png`,
      prompt_snapshot: `${name}参考图`,
      negative_prompt_snapshot: "",
      model: "image-model",
      status: "completed",
      is_current: true,
      input_signature: `${imageId}-signature`,
      source: "generated",
      created_at: "2026-07-29T00:00:00Z",
    },
    created_at: "2026-07-29T00:00:00Z",
    updated_at: "2026-07-29T00:00:00Z",
  };
}

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
      screen.queryByRole("button", { name: /生成.*提示词|补齐.*提示词/ }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("最终视频执行提示词")).not.toBeInTheDocument();
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

  test("test_shot_edit_modal_hides_legacy_prompt_fields", async () => {
    const user = userEvent.setup();
    renderShotsPage();

    await user.click(await screen.findByRole("button", { name: "编辑" }));

    expect(screen.getByText("负向提示词 / 禁止事项")).toBeInTheDocument();
    expect(screen.queryByText("关键帧图片提示词")).not.toBeInTheDocument();
    expect(screen.queryByText("视频执行提示词")).not.toBeInTheDocument();
  });

  test("test_shot_card_with_bound_assets_displays_only_bound_images", async () => {
    vi.mocked(api.assets.list).mockResolvedValue([
      assetWithImage("scene-1", "青石巷", "scene", "scene-image-1"),
      assetWithImage("character-1", "林舟", "character", "character-image-1"),
      assetWithImage("prop-1", "银色怀表", "prop", "prop-image-1"),
      assetWithImage("character-2", "未出场角色", "character", "character-image-2"),
    ]);
    vi.mocked(api.shots.list).mockResolvedValue([
      {
        ...shot,
        scene_asset_id: "scene-1",
        character_ids_json: ["character-1"],
        prop_ids_json: ["prop-1"],
        reference_bindings_json: [
          {
            role: "scene_layout",
            asset_id: "scene-1",
            asset_image_id: "scene-image-1",
          },
          {
            role: "character_identity",
            asset_id: "character-1",
            asset_image_id: "character-image-1",
          },
          {
            role: "prop_identity",
            asset_id: "prop-1",
            asset_image_id: "prop-image-1",
          },
        ],
      },
    ]);

    renderShotsPage();

    const boundAssets = await screen.findByRole("region", {
      name: "分镜 1 已绑定素材",
    });
    expect(
      within(boundAssets).getByRole("button", { name: "放大青石巷场景参考图" }),
    ).toBeInTheDocument();
    expect(
      within(boundAssets).getByRole("button", { name: "放大林舟角色参考图" }),
    ).toBeInTheDocument();
    expect(
      within(boundAssets).getByRole("button", { name: "放大银色怀表物品参考图" }),
    ).toBeInTheDocument();
    expect(within(boundAssets).queryByText("未出场角色")).not.toBeInTheDocument();
  });

  test("test_shot_card_without_reference_images_displays_semantic_asset_bindings", async () => {
    vi.mocked(api.assets.list).mockResolvedValue([
      assetWithImage("scene-1", "青石巷", "scene", "scene-image-1"),
      assetWithImage("character-1", "林舟", "character", "character-image-1"),
      assetWithImage("prop-1", "银色怀表", "prop", "prop-image-1"),
      assetWithImage("character-2", "未出场角色", "character", "character-image-2"),
    ]);
    vi.mocked(api.shots.list).mockResolvedValue([
      {
        ...shot,
        scene_asset_id: "scene-1",
        character_ids_json: ["character-1"],
        prop_ids_json: ["prop-1"],
        reference_bindings_json: [
          {
            role: "character_identity",
            asset_id: "character-2",
            asset_image_id: "character-image-2",
          },
        ],
      },
    ]);

    renderShotsPage();

    const assetTags = await screen.findByRole("group", {
      name: "分镜 1 素材标签",
    });
    const sceneTag = within(assetTags).getByText("场景 · 青石巷");
    const characterTag = within(assetTags).getByText("角色 · 林舟");
    const propTag = within(assetTags).getByText("物品 · 银色怀表");
    expect(sceneTag.className).toBe(characterTag.className);
    expect(propTag.className).toBe(characterTag.className);

    const boundAssets = screen.getByRole("region", {
      name: "分镜 1 已绑定素材",
    });
    expect(within(boundAssets).getByText("青石巷")).toBeInTheDocument();
    expect(within(boundAssets).getByText("林舟")).toBeInTheDocument();
    expect(within(boundAssets).getByText("银色怀表")).toBeInTheDocument();
    expect(within(boundAssets).getAllByText("尚无绑定图")).toHaveLength(3);
    expect(within(boundAssets).queryByRole("button")).not.toBeInTheDocument();
    expect(within(boundAssets).queryByText("未出场角色")).not.toBeInTheDocument();
  });

  test("test_shot_card_with_mismatched_bound_image_does_not_display_current_image", async () => {
    vi.mocked(api.assets.list).mockResolvedValue([
      assetWithImage("scene-1", "青石巷", "scene", "scene-image-current"),
    ]);
    vi.mocked(api.shots.list).mockResolvedValue([
      {
        ...shot,
        scene_asset_id: "scene-1",
        reference_bindings_json: [
          {
            role: "scene_layout",
            asset_id: "scene-1",
            asset_image_id: "scene-image-bound",
          },
        ],
      },
    ]);

    renderShotsPage();

    const boundAssets = await screen.findByRole("region", {
      name: "分镜 1 已绑定素材",
    });
    expect(within(boundAssets).getByText("绑定图不可用")).toBeInTheDocument();
    expect(
      within(boundAssets).queryByRole("button", {
        name: "放大青石巷场景参考图",
      }),
    ).not.toBeInTheDocument();
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
