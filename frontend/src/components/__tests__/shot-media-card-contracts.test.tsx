import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { api } from "../../api/client";
import type { MediaVersion, ReferenceBinding, Shot } from "../../types/api";
import { ShotMediaCard } from "../shot-media-card";

vi.mock("sonner", () => ({
  toast: { error: vi.fn(), success: vi.fn() },
}));

const currentKeyframe: MediaVersion = {
  id: "keyframe-1",
  shot_id: "shot-1",
  version_no: 1,
  file_url: "/media/keyframe-1.png",
  prompt_snapshot: "首帧",
  negative_prompt_snapshot: "",
  model: "image-model",
  status: "completed",
  is_current: true,
  source: "generated",
  input_signature: "keyframe-signature",
  created_at: "2026-07-30T00:00:00Z",
};

const sceneBinding: ReferenceBinding = {
  role: "scene_layout",
  asset_id: "scene-1",
  asset_image_id: "scene-image-1",
};

const baseShot: Shot = {
  id: "shot-1",
  project_id: "project-1",
  shot_no: 1,
  shot_order: 1,
  source_type: "screenplay",
  source_ref: "scene-1",
  source_signature: "source-1",
  scene_asset_id: "scene-1",
  character_ids_json: [],
  prop_ids_json: [],
  positions_json: [],
  start_state: "小明站在门外。",
  action: "小明推开房门。",
  expressions_json: [],
  dialogue_json: [],
  narration: "",
  shot_type: "中景",
  camera_angle: "平视",
  camera_move: "固定",
  duration_sec: 6,
  end_state: "小明进入屋内。",
  next_hook: "",
  frame_description: "小明站在旧屋门口。",
  image_prompt: "",
  compiled_image_prompt: "模板编译出的关键帧提示词",
  video_prompt: "",
  compiled_video_prompt: "模板编译出的视频提示词",
  negative_prompt: "文字，水印",
  reference_bindings_json: [sceneBinding],
  status: "ready",
  own_signature: "shot-signature-1",
  revision: 1,
  current_keyframe: currentKeyframe,
  created_at: "2026-07-30T00:00:00Z",
  updated_at: "2026-07-30T00:00:00Z",
};

function renderCard(shot: Shot, kind: "keyframe" | "video") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <ShotMediaCard
          shot={shot}
          kind={kind}
          assetMap={new Map()}
          selected={false}
          onToggle={vi.fn()}
        />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api.shots.keyframes, "list").mockResolvedValue([]);
  vi.spyOn(api.shots.videos, "list").mockResolvedValue([]);
});

describe("分镜媒体显式提示词门禁", () => {
  test("test_keyframe_without_explicit_image_prompt_disables_generation", () => {
    renderCard(baseShot, "keyframe");

    expect(screen.getByRole("button", { name: "生成新版本" })).toBeDisabled();
  });

  test("test_keyframe_with_explicit_image_prompt_enables_generation", () => {
    renderCard(
      { ...baseShot, image_prompt: "小明站在门前的稳定首帧。" },
      "keyframe",
    );

    expect(screen.getByRole("button", { name: "生成新版本" })).toBeEnabled();
  });

  test("test_video_without_explicit_video_prompt_disables_generation", () => {
    renderCard(baseShot, "video");

    expect(screen.getByRole("button", { name: "生成新版本" })).toBeDisabled();
  });

  test("test_video_with_explicit_video_prompt_enables_generation", () => {
    renderCard(
      { ...baseShot, video_prompt: "0-2 秒：小明推开房门。" },
      "video",
    );

    expect(screen.getByRole("button", { name: "生成新版本" })).toBeEnabled();
  });

  test("test_keyframe_without_asset_reference_disables_generation_and_prompts_binding", () => {
    renderCard(
      {
        ...baseShot,
        image_prompt: "小明站在门前的稳定首帧。",
        reference_bindings_json: [],
      },
      "keyframe",
    );

    expect(screen.getByRole("button", { name: "生成新版本" })).toBeDisabled();
    expect(screen.getByText(/请先到分镜编辑中绑定素材/)).toBeInTheDocument();
    expect(screen.queryByText(/纯文字降级/)).not.toBeInTheDocument();
  });

  test("test_video_without_asset_reference_disables_generation_and_prompts_binding", () => {
    renderCard(
      {
        ...baseShot,
        video_prompt: "0-2 秒：小明推开房门。",
        reference_bindings_json: [],
      },
      "video",
    );

    expect(screen.getByRole("button", { name: "生成新版本" })).toBeDisabled();
    expect(screen.getByText(/请先到分镜编辑中绑定素材/)).toBeInTheDocument();
  });

  test.each(["keyframe", "video"] as const)(
    "test_%s_without_scene_asset_disables_generation",
    (kind) => {
      renderCard(
        {
          ...baseShot,
          scene_asset_id: null,
          image_prompt: "小明站在门前的稳定首帧。",
          video_prompt: "0-2 秒：小明推开房门。",
        },
        kind,
      );

      expect(screen.getByRole("button", { name: "生成新版本" })).toBeDisabled();
      expect(screen.getByText(/请先到分镜编辑中绑定场景素材/)).toBeInTheDocument();
    },
  );

  test.each(["keyframe", "video"] as const)(
    "test_%s_with_asset_binding_without_current_image_disables_generation",
    (kind) => {
      renderCard(
        {
          ...baseShot,
          image_prompt: "小明站在门前的稳定首帧。",
          video_prompt: "0-2 秒：小明推开房门。",
          reference_bindings_json: [
            { role: "scene_layout", asset_id: "scene-1" },
          ],
        },
        kind,
      );

      expect(screen.getByRole("button", { name: "生成新版本" })).toBeDisabled();
    },
  );

  test.each(["keyframe", "video"] as const)(
    "test_%s_with_selected_asset_missing_reference_disables_generation",
    (kind) => {
      renderCard(
        {
          ...baseShot,
          character_ids_json: ["character-1"],
          image_prompt: "小明站在门前的稳定首帧。",
          video_prompt: "0-2 秒：小明推开房门。",
        },
        kind,
      );

      expect(screen.getByRole("button", { name: "生成新版本" })).toBeDisabled();
    },
  );
});
