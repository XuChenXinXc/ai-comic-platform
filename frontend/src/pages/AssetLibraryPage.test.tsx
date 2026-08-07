import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router";
import { beforeEach, expect, test, vi } from "vitest";
import { api } from "../api/client";
import type { Asset } from "../types/api";
import { AssetImagesPage } from "./AssetImagesPage";
import { AssetsPage } from "./AssetsPage";

vi.mock("sonner", () => ({
  toast: { error: vi.fn(), success: vi.fn(), warning: vi.fn() },
}));
vi.mock("../hooks/use-project", () => ({
  useProject: () => ({
    activeProject: {
      id: "project-1",
      name: "素材库测试",
      source_signature: "source-1",
    },
    activeProjectId: "project-1",
  }),
}));
vi.mock("../components/project-required", () => ({
  ProjectRequired: ({ children }: { children: ReactNode }) => children,
}));
vi.mock("../hooks/use-chapter", () => ({
  useChapter: () => ({
    chapters: [
      { id: "chapter-1", project_id: "project-1", chapter_no: 1, title: "第一章" },
    ],
    activeChapter: {
      id: "chapter-1",
      project_id: "project-1",
      chapter_no: 1,
      title: "第一章",
      source_signature: "source-1",
    },
    activeChapterId: "chapter-1",
    selectChapter: vi.fn(),
    isLoading: false,
    error: null,
    refetch: vi.fn(),
  }),
}));
vi.mock("../components/chapter-required", () => ({
  ChapterRequired: ({ children }: { children: ReactNode }) => children,
}));
vi.mock("../components/chapter-selector", () => ({
  ChapterSelector: () => null,
}));

const asset = {
  id: "asset-1",
  project_id: "project-1",
  asset_type: "character",
  name: "林昭",
  profile_json: { identity: "调查记者" },
  profile: { identity: "调查记者" },
  prompt: "黑色短发，深色风衣",
  compiled_prompt: "画面美术风格：完整角色基础设定稿",
  negative_prompt: "身份漂移",
  consistency_json: ["黑色短发"],
  consistency_keywords: ["黑色短发"],
  composition: "三视图",
  importance: "core",
  status: "ready",
  source_signature: "source-1",
  own_signature: "asset-1",
  revision: 1,
  current_image: {
    id: "asset-image-1",
    asset_id: "asset-1",
    version_no: 1,
    file_url: "/media/project-1/assets/asset-1/v001.png",
    prompt_snapshot: "角色设定图",
    negative_prompt_snapshot: "",
    model: "gpt-image-2",
    status: "completed",
    is_current: true,
    input_signature: "asset-image-1",
    source: "generated",
    created_at: "2026-07-29T00:00:00Z",
  },
  created_at: "2026-07-29T00:00:00Z",
  updated_at: "2026-07-29T00:00:00Z",
} as Asset;

function renderPage(page: ReactNode, initialEntry = "/assets") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <QueryClientProvider client={queryClient}>{page}</QueryClientProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api.assets, "list").mockResolvedValue([asset]);
  vi.spyOn(api.assets.images, "list").mockResolvedValue([]);
});

test("test_asset_library_shows_compiled_prompt_and_image_actions_without_prompt_buttons", async () => {
  renderPage(<AssetsPage />);

  expect(
    await screen.findByRole("heading", { name: "素材库" }),
  ).toBeInTheDocument();
  expect(
    await screen.findByText("画面美术风格：完整角色基础设定稿"),
  ).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "生成新版本" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "批量生成角色图" })).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: /生成.*提示词|补齐提示词/ }),
  ).not.toBeInTheDocument();
});

test("test_asset_library_uses_list_profile_fields_in_card_summary", async () => {
  vi.mocked(api.assets.list).mockResolvedValue([
    {
      ...asset,
      profile_json: {
        immutable_features: ["左眉浅疤", "旧怀表"],
      },
      profile: {
        immutable_features: ["左眉浅疤", "旧怀表"],
      },
    },
  ]);

  renderPage(<AssetsPage />);

  expect(await screen.findByText(/左眉浅疤.*旧怀表/)).toBeInTheDocument();
  expect(screen.queryByText("尚未填写完整素材描述")).not.toBeInTheDocument();
});

test("test_click_asset_image_opens_large_preview", async () => {
  const user = userEvent.setup();
  renderPage(<AssetsPage />);

  await user.click(
    await screen.findByRole("button", { name: "放大林昭当前参考图" }),
  );

  expect(
    screen.getByRole("dialog", { name: "林昭当前参考图" }),
  ).toBeInTheDocument();
  expect(screen.getByAltText("林昭当前参考图大图")).toBeInTheDocument();
});

test("test_asset_images_route_redirects_to_asset_library", async () => {
  renderPage(
    <Routes>
      <Route path="/asset-images" element={<AssetImagesPage />} />
      <Route path="/assets" element={<div>素材库目标页</div>} />
    </Routes>,
    "/asset-images",
  );

  expect(await screen.findByText("素材库目标页")).toBeInTheDocument();
});
