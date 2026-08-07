import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { api } from "../api/client";
import type { Asset, MediaVersion } from "../types/api";
import { AssetImagePanel } from "./AssetImagesPage";

vi.mock("sonner", () => ({
  toast: { error: vi.fn(), success: vi.fn() },
}));

const baseAsset: Asset = {
  id: "asset-1",
  project_id: "project-1",
  asset_type: "character",
  name: "小明",
  profile_json: {},
  profile: {},
  prompt: "",
  compiled_prompt: "以素材资料为准的角色模板提示词",
  negative_prompt: "文字，水印",
  consistency_json: [],
  consistency_keywords: [],
  composition: "角色设定图",
  importance: "core",
  status: "ready",
  source_signature: "source-1",
  own_signature: "asset-signature-1",
  revision: 1,
  created_at: "2026-07-30T00:00:00Z",
  updated_at: "2026-07-30T00:00:00Z",
};

function renderPanel(asset: Asset) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AssetImagePanel asset={asset} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api.assets.images, "list").mockResolvedValue([]);
});

describe("素材图片真实内容门禁", () => {
  test("test_asset_with_only_compiled_template_disables_generation", () => {
    renderPanel(baseAsset);

    expect(screen.getByRole("button", { name: "生成新版本" })).toBeDisabled();
  });

  test("test_asset_with_explicit_prompt_enables_generation", () => {
    renderPanel({
      ...baseAsset,
      prompt: "黑色短发，灰色风衣，左眉有一道浅疤。",
    });

    expect(screen.getByRole("button", { name: "生成新版本" })).toBeEnabled();
  });

  test("test_asset_with_visual_profile_enables_generation", () => {
    renderPanel({
      ...baseAsset,
      compiled_prompt: "",
      profile_json: { appearance: "黑色短发，左眉有一道浅疤。" },
      profile: { appearance: "黑色短发，左眉有一道浅疤。" },
    });

    expect(screen.getByRole("button", { name: "生成新版本" })).toBeEnabled();
  });
});

describe("素材图片失败记录删除", () => {
  test("test_delete_failed_asset_image_removes_without_confirmation", async () => {
    const user = userEvent.setup();
    const failedVersion: MediaVersion = {
      id: "image-failed",
      asset_id: baseAsset.id,
      version_no: 2,
      file_path: null,
      prompt_snapshot: "失败提示词",
      negative_prompt_snapshot: "",
      model: "image-model",
      status: "failed",
      is_current: false,
      source: "generated",
      input_signature: "failed-signature",
      error: "生成失败",
      created_at: "2026-08-03T00:00:00Z",
    };
    vi.mocked(api.assets.images.list).mockResolvedValue([failedVersion]);
    const removeSpy = vi
      .spyOn(api.assets.images, "remove")
      .mockResolvedValue(undefined);
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);

    renderPanel(baseAsset);
    await user.click(
      await screen.findByRole("button", { name: "删除失败版本 2" }),
    );

    await waitFor(() => expect(removeSpy).toHaveBeenCalledWith("image-failed"));
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  test("test_delete_completed_asset_image_keeps_confirmation", async () => {
    const user = userEvent.setup();
    const completedVersion: MediaVersion = {
      id: "image-completed",
      asset_id: baseAsset.id,
      version_no: 1,
      file_path: "角色/角色-小明-v001.png",
      prompt_snapshot: "成功提示词",
      negative_prompt_snapshot: "",
      model: "image-model",
      status: "completed",
      is_current: false,
      source: "generated",
      input_signature: "completed-signature",
      created_at: "2026-08-03T00:00:00Z",
    };
    vi.mocked(api.assets.images.list).mockResolvedValue([completedVersion]);
    const removeSpy = vi
      .spyOn(api.assets.images, "remove")
      .mockResolvedValue(undefined);
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);

    renderPanel(baseAsset);
    await user.click(
      await screen.findByRole("button", { name: "删除版本 1" }),
    );

    expect(confirmSpy).toHaveBeenCalledOnce();
    await waitFor(() => expect(removeSpy).toHaveBeenCalledWith("image-completed"));
  });
});

describe("素材图片提示词编辑", () => {
  test("test_edit_prompt_shows_current_snapshot_and_saves_override", async () => {
    const user = userEvent.setup();
    const currentVersion: MediaVersion = {
      id: "image-current",
      asset_id: baseAsset.id,
      version_no: 1,
      file_path: "character/林风.png",
      prompt_snapshot: "自动编译的角色提示词",
      negative_prompt_snapshot: "",
      model: "image-model",
      status: "completed",
      is_current: true,
      source: "generated",
      input_signature: "signature-1",
      created_at: "2026-08-03T00:00:00Z",
    };
    vi.mocked(api.assets.images.list).mockResolvedValue([currentVersion]);
    const updateSpy = vi
      .spyOn(api.assets, "update")
      .mockResolvedValue({ ...baseAsset, prompt_override: "优化后的白底提示词" });

    renderPanel(baseAsset);
    await user.click(
      await screen.findByRole("button", { name: "编辑图片提示词" }),
    );

    const textarea = await screen.findByPlaceholderText("输入素材图提示词…");
    expect((textarea as HTMLTextAreaElement).value).toBe("自动编译的角色提示词");
    await user.clear(textarea);
    await user.type(textarea, "优化后的白底提示词");
    await user.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() =>
      expect(updateSpy).toHaveBeenCalledWith(baseAsset.id, {
        prompt_override: "优化后的白底提示词",
      }),
    );
  });

  test("test_asset_with_prompt_override_shows_saved_block", () => {
    renderPanel({
      ...baseAsset,
      prompt_override: "用户保存的白底提示词",
    });

    expect(screen.getByText("已保存的图片提示词覆盖")).toBeTruthy();
    expect(screen.getByText("用户保存的白底提示词")).toBeTruthy();
  });
});
