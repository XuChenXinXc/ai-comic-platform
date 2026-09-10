import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { api, queryKeys } from "../../api/client";
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
      chapter_id: "test-chapter",
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
  transition_from_prev: "scene_change",
  frame_description: "小明站在旧屋门口。",
  image_prompt: "",
  image_prompt_override: "",
  final_image_prompt: "模板编译出的关键帧提示词",
  compiled_image_prompt: "模板编译出的关键帧提示词",
  video_prompt: "",
  video_prompt_override: "",
  final_video_prompt: "模板编译出的视频提示词",
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

function renderCard(
  shot: Shot,
  kind: "keyframe" | "video",
  queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  }),
) {
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
  test("test_keyframe_shows_final_prompt_and_editable_override_field", () => {
    renderCard(baseShot, "keyframe");

    expect(screen.getByText("最终关键帧提示词")).toBeInTheDocument();
    expect(screen.getByDisplayValue("模板编译出的关键帧提示词")).toBeInTheDocument();
    expect(screen.queryByText("导演补充（可选）")).not.toBeInTheDocument();
    expect(screen.queryByText("最终 V2 关键帧提示词")).not.toBeInTheDocument();
  });

  test("test_video_shows_final_prompt_and_editable_override_field", () => {
    renderCard(baseShot, "video");

    expect(screen.getByText("最终视频提示词")).toBeInTheDocument();
    expect(screen.getByDisplayValue("模板编译出的视频提示词")).toBeInTheDocument();
    expect(screen.queryByText("导演补充（可选）")).not.toBeInTheDocument();
    expect(screen.queryByText("最终 V2 视频提示词")).not.toBeInTheDocument();
  });

  test("test_keyframe_save_writes_image_prompt_override", async () => {
    const user = userEvent.setup();
    const updateSpy = vi
      .spyOn(api.shots, "update")
      .mockResolvedValue(baseShot as never);

    renderCard(baseShot, "keyframe");

    const textarea = screen.getByRole("textbox");
    await user.clear(textarea);
    await user.type(textarea, "新的最终关键帧提示词");
    await user.click(screen.getByRole("button", { name: "保存" }));

    expect(updateSpy).toHaveBeenCalledWith("shot-1", {
      image_prompt_override: "新的最终关键帧提示词",
    });
  });

  test("test_save_prompt_invalidates_chapter_shots_query", async () => {
    // 分镜列表查询键是 chapterId 维度：保存提示词后必须失效对应键。
    const user = userEvent.setup();
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    vi.spyOn(api.shots, "update").mockResolvedValue(baseShot as never);

    renderCard(baseShot, "keyframe", queryClient);

    const textarea = screen.getByRole("textbox");
    await user.clear(textarea);
    await user.type(textarea, "新的最终关键帧提示词");
    await user.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => {
      expect(invalidateSpy).toHaveBeenCalledWith({
        queryKey: queryKeys.shots("test-chapter"),
      });
    });
  });

  test("test_video_save_writes_video_prompt_override", async () => {
    const user = userEvent.setup();
    const updateSpy = vi
      .spyOn(api.shots, "update")
      .mockResolvedValue(baseShot as never);

    renderCard(baseShot, "video");

    const textarea = screen.getByRole("textbox");
    await user.clear(textarea);
    await user.type(textarea, "新的视频最终提示词");
    await user.click(screen.getByRole("button", { name: "保存" }));

    expect(updateSpy).toHaveBeenCalledWith("shot-1", {
      video_prompt_override: "新的视频最终提示词",
    });
  });

  test("test_clear_saved_override_restores_compiled_prompt", async () => {
    const user = userEvent.setup();
    vi.spyOn(api.shots, "update").mockResolvedValue({
      ...baseShot,
      image_prompt_override: "",
      final_image_prompt: "模板编译出的关键帧提示词",
    });

    renderCard(
      {
        ...baseShot,
        image_prompt_override: "人工覆盖的关键帧提示词",
        final_image_prompt: "人工覆盖的关键帧提示词",
      },
      "keyframe",
    );

    const textarea = screen.getByRole("textbox");
    await user.clear(textarea);
    await user.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() =>
      expect(textarea).toHaveValue("模板编译出的关键帧提示词"),
    );
  });

  test("test_edit_during_save_preserves_newer_local_prompt", async () => {
    const user = userEvent.setup();
    let resolveUpdate: (shot: Shot) => void = () => undefined;
    vi.spyOn(api.shots, "update").mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveUpdate = resolve;
        }),
    );

    renderCard(baseShot, "keyframe");

    const textarea = screen.getByRole("textbox");
    await user.clear(textarea);
    await user.type(textarea, "已提交的提示词");
    await user.click(screen.getByRole("button", { name: "保存" }));
    expect(screen.getByRole("button", { name: "生成新版本" })).toBeDisabled();
    await user.type(textarea, "，继续编辑");
    resolveUpdate({
      ...baseShot,
      image_prompt_override: "已提交的提示词",
      final_image_prompt: "已提交的提示词",
    });

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "保存" })).toBeEnabled(),
    );
    await waitFor(() =>
      expect(textarea).toHaveValue("已提交的提示词，继续编辑"),
    );
  });

  test("test_dirty_prompt_survives_remote_shot_refresh", async () => {
    const user = userEvent.setup();
    const queryClient = new QueryClient();
    const view = renderCard(baseShot, "keyframe", queryClient);
    const textarea = screen.getByRole("textbox");

    await user.clear(textarea);
    await user.type(textarea, "尚未保存的关键帧提示词");
    view.rerender(
      <MemoryRouter>
        <QueryClientProvider client={queryClient}>
          <ShotMediaCard
            shot={{
              ...baseShot,
              final_image_prompt: "后台刷新的新提示词",
              compiled_image_prompt: "后台刷新的新提示词",
            }}
            kind="keyframe"
            assetMap={new Map()}
            selected={false}
            onToggle={vi.fn()}
          />
        </QueryClientProvider>
      </MemoryRouter>,
    );

    expect(screen.getByDisplayValue("尚未保存的关键帧提示词")).toBeInTheDocument();
  });

  test("test_generate_with_dirty_prompt_saves_before_enqueue", async () => {
    const user = userEvent.setup();
    const updateSpy = vi
      .spyOn(api.shots, "update")
      .mockResolvedValue({
        ...baseShot,
        image_prompt_override: "生成前保存的关键帧提示词",
        final_image_prompt: "生成前保存的关键帧提示词",
      });
    const generateSpy = vi
      .spyOn(api.shots.keyframes, "generate")
      .mockResolvedValue({} as never);

    renderCard(baseShot, "keyframe");
    const textarea = screen.getByRole("textbox");
    await user.clear(textarea);
    await user.type(textarea, "生成前保存的关键帧提示词");
    await user.click(screen.getByRole("button", { name: "生成新版本" }));

    await waitFor(() =>
      expect(generateSpy).toHaveBeenCalledWith("shot-1", {
        auto_prepare_references: true,
      }),
    );
    expect(updateSpy).toHaveBeenCalledWith("shot-1", {
      image_prompt_override: "生成前保存的关键帧提示词",
    });
    expect(updateSpy.mock.invocationCallOrder[0]).toBeLessThan(
      generateSpy.mock.invocationCallOrder[0],
    );
  });

  test("test_generate_with_compiled_only_prompt_saves_override_before_enqueue", async () => {
    const user = userEvent.setup();
    const updateSpy = vi.spyOn(api.shots, "update").mockResolvedValue({
      ...baseShot,
      image_prompt_override: "模板编译出的关键帧提示词",
      final_image_prompt: "模板编译出的关键帧提示词",
    });
    const generateSpy = vi
      .spyOn(api.shots.keyframes, "generate")
      .mockResolvedValue({} as never);

    renderCard(baseShot, "keyframe");
    await user.click(screen.getByRole("button", { name: "生成新版本" }));

    await waitFor(() =>
      expect(generateSpy).toHaveBeenCalledWith("shot-1", {
        auto_prepare_references: true,
      }),
    );
    expect(updateSpy).toHaveBeenCalledWith("shot-1", {
      image_prompt_override: "模板编译出的关键帧提示词",
    });
    expect(updateSpy.mock.invocationCallOrder[0]).toBeLessThan(
      generateSpy.mock.invocationCallOrder[0],
    );
  });

  test("test_keyframe_without_explicit_image_prompt_disables_generation", () => {
    renderCard(
      {
        ...baseShot,
        image_prompt: "",
        image_prompt_override: "",
        final_image_prompt: "",
        compiled_image_prompt: "",
      },
      "keyframe",
    );

    expect(screen.getByRole("button", { name: "生成新版本" })).toBeDisabled();
  });

  test("test_keyframe_with_explicit_image_prompt_enables_generation", () => {
    renderCard(
      {
        ...baseShot,
        image_prompt: "小明站在门前的稳定首帧。",
        image_prompt_override: "",
        final_image_prompt: "",
        compiled_image_prompt: "",
      },
      "keyframe",
    );

    expect(screen.getByRole("button", { name: "生成新版本" })).toBeEnabled();
  });

  test("test_video_without_explicit_video_prompt_disables_generation", () => {
    renderCard(
      {
        ...baseShot,
        video_prompt: "",
        video_prompt_override: "",
        final_video_prompt: "",
        compiled_video_prompt: "",
      },
      "video",
    );

    expect(screen.getByRole("button", { name: "生成新版本" })).toBeDisabled();
  });

  test("test_video_with_explicit_video_prompt_enables_generation", () => {
    renderCard(
      {
        ...baseShot,
        video_prompt: "0-2 秒：小明推开房门。",
        video_prompt_override: "",
        final_video_prompt: "",
        compiled_video_prompt: "",
      },
      "video",
    );

    expect(screen.getByRole("button", { name: "生成新版本" })).toBeEnabled();
  });

  test.each([
    ["keyframe", { image_prompt_override: "已保存的最终关键帧提示词" }],
    ["video", { video_prompt_override: "已保存的最终视频提示词" }],
  ] as const)("test_%s_with_saved_final_override_enables_generation", (kind, patch) => {
    renderCard({ ...baseShot, ...patch }, kind);

    expect(screen.getByRole("button", { name: "生成新版本" })).toBeEnabled();
  });

  test("test_keyframe_without_asset_reference_disables_generation_and_prompts_binding", () => {
    renderCard(
      {
        ...baseShot,
        image_prompt_override: "小明站在门前的稳定首帧。",
        reference_bindings_json: [],
      },
      "keyframe",
    );

    expect(screen.getByRole("button", { name: "生成新版本" })).toBeDisabled();
    expect(screen.getByText(/素材参考图未就绪/)).toBeInTheDocument();
    expect(screen.queryByText(/纯文字降级/)).not.toBeInTheDocument();
  });

  test("test_video_without_asset_reference_disables_generation_and_prompts_binding", () => {
    renderCard(
      {
        ...baseShot,
        video_prompt_override: "0-2 秒：小明推开房门。",
        reference_bindings_json: [],
      },
      "video",
    );

    expect(screen.getByRole("button", { name: "生成新版本" })).toBeDisabled();
    expect(screen.getByText(/素材参考图未就绪/)).toBeInTheDocument();
  });

  test.each(["keyframe", "video"] as const)(
    "test_%s_without_scene_asset_disables_generation",
    (kind) => {
      renderCard(
        {
        ...baseShot,
        image_prompt: "",
        image_prompt_override: "小明站在门前的稳定首帧。",
        final_image_prompt: "",
        compiled_image_prompt: "",
        video_prompt: "",
        video_prompt_override: "0-2 秒：小明推开房门。",
        final_video_prompt: "",
        compiled_video_prompt: "",
        scene_asset_id: null,
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
        image_prompt: "",
        image_prompt_override: "小明站在门前的稳定首帧。",
        final_image_prompt: "",
        compiled_image_prompt: "",
        video_prompt: "",
        video_prompt_override: "0-2 秒：小明推开房门。",
        final_video_prompt: "",
        compiled_video_prompt: "",
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
          image_prompt: "",
          image_prompt_override: "小明站在门前的稳定首帧。",
          final_image_prompt: "",
          compiled_image_prompt: "",
          video_prompt: "",
          video_prompt_override: "0-2 秒：小明推开房门。",
          final_video_prompt: "",
          compiled_video_prompt: "",
        },
        kind,
      );

      expect(screen.getByRole("button", { name: "生成新版本" })).toBeDisabled();
    },
  );

  test("test_delete_failed_keyframe_removes_without_confirmation", async () => {
    const user = userEvent.setup();
    const failedVersion: MediaVersion = {
      ...currentKeyframe,
      id: "keyframe-failed",
      version_no: 2,
      file_path: null,
      file_url: undefined,
      status: "failed",
      is_current: false,
      error: "关键帧生成失败",
    };
    vi.mocked(api.shots.keyframes.list).mockResolvedValue([failedVersion]);
    const removeSpy = vi
      .spyOn(api.shots.keyframes, "remove")
      .mockResolvedValue(undefined);
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);

    renderCard(baseShot, "keyframe");
    await user.click(
      await screen.findByRole("button", { name: "删除失败版本 2" }),
    );

    await waitFor(() =>
      expect(removeSpy).toHaveBeenCalledWith("keyframe-failed"),
    );
    expect(confirmSpy).not.toHaveBeenCalled();
  });
});
