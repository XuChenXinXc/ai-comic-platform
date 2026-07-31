import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { api } from "../api/client";
import type { Asset } from "../types/api";
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
