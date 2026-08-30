import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, test, vi } from "vitest";
import { AppShell } from "./app-shell";

vi.mock("../hooks/use-job-polling", () => ({
  isJobActive: () => false,
  useJobPolling: () => ({ data: [] }),
}));

vi.mock("../hooks/use-project", () => ({
  useProject: () => ({
    projects: [],
    activeProject: null,
    activeProjectId: "",
    selectProject: vi.fn(),
    error: null,
  }),
}));

vi.mock("../hooks/use-chapter", () => ({
  useChapter: () => ({ activeChapterId: "" }),
}));

describe("AppShell 动态漫导航", () => {
  test("test_app_shell_shows_dynamic_workspace_and_preserves_custom_shots_link", () => {
    render(
      <MemoryRouter initialEntries={["/project"]}>
        <AppShell />
      </MemoryRouter>,
    );

    const dynamicWorkspaceLink = screen.getByRole("link", {
      name: "动态漫工作区",
    });
    expect(dynamicWorkspaceLink).toHaveAttribute("href", "/free-create");
    expect(screen.getByText("动态漫工作区")).toBeInTheDocument();

    const customShotsLink = screen.getByRole("link", { name: "自由制作" });
    expect(customShotsLink).toHaveAttribute("href", "/custom-shots");
  });
});
