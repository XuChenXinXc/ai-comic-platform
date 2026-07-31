import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { api } from "../api/client";
import type { Asset, Shot } from "../types/api";
import { AssetsPage } from "./AssetsPage";
import { ShotsPage } from "./ShotsPage";

const projectState = vi.hoisted(() => ({ id: "project-1" }));

vi.mock("sonner", () => ({
  toast: { error: vi.fn(), success: vi.fn(), warning: vi.fn() },
}));
vi.mock("../hooks/use-project", () => ({
  useProject: () => ({
    activeProject: {
      id: projectState.id,
      name: "验收项目",
      source_text: "测试小说",
      source_revision: 1,
      source_signature: "source-1",
      default_duration_sec: 6,
    },
    activeProjectId: projectState.id,
  }),
}));
vi.mock("../components/project-required", () => ({
  ProjectRequired: ({ children }: { children: ReactNode }) => children,
}));
vi.mock("../components/shot-media-card", () => ({
  ShotMediaCard: ({
    shot,
    selected,
    onToggle,
  }: {
    shot: Shot;
    selected: boolean;
    onToggle: (checked: boolean) => void;
  }) => (
    <button
      type="button"
      aria-label={`选择分镜 ${shot.shot_no}`}
      aria-pressed={selected}
      onClick={() => onToggle(!selected)}
    >
      选择分镜
    </button>
  ),
}));

const asset = {
  id: "asset-1",
  project_id: "project-1",
  asset_type: "character",
  name: "林昭",
  prompt: "角色提示词",
  compiled_prompt: "角色完整设定模板",
} as Asset;
const shot = {
  id: "shot-1",
  project_id: "project-1",
  shot_no: 1,
  shot_order: 1,
  action: "推开旧剧院的门",
  image_prompt: "",
} as Shot;

function renderPage(page: ReactNode) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>{page}</QueryClientProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
  projectState.id = "project-1";
  vi.spyOn(api.assets, "list").mockImplementation(async (projectId) =>
    projectId === "project-1" ? [asset] : [],
  );
  vi.spyOn(api.shots, "list").mockImplementation(async (projectId) =>
    projectId === "project-1" ? [shot] : [],
  );
  vi.spyOn(api.assets.images, "list").mockResolvedValue([]);
});

describe("切换项目清空批量选择", () => {
  test.each([
    ["素材库", <AssetsPage />, "checkbox", "选择 林昭", /生成所选素材图（1）/],
    [
      "分镜页",
      <ShotsPage />,
      "checkbox",
      "选择分镜 1",
      /生成所选关键帧（1）/,
    ],
  ])(
    "test_%s_switch_project_clears_selection",
    async (_, page, selectionRole, selectionLabel, selectedButtonName) => {
      const view = renderPage(page);
      await userEvent.click(
        await screen.findByRole(selectionRole as "button" | "checkbox", {
          name: selectionLabel,
        }),
      );
      expect(
        screen.getByRole("button", { name: selectedButtonName }),
      ).toBeInTheDocument();

      projectState.id = "project-2";
      view.rerender(
        <MemoryRouter>
          <QueryClientProvider
            client={
              new QueryClient({
                defaultOptions: {
                  queries: { retry: false },
                  mutations: { retry: false },
                },
              })
            }
          >
            {page}
          </QueryClientProvider>
        </MemoryRouter>,
      );

      await waitFor(() => {
        expect(
          screen.queryByRole("button", { name: selectedButtonName }),
        ).not.toBeInTheDocument();
      });
    },
  );
});
