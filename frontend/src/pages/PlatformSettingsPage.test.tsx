import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { toast } from "sonner";
import { api } from "../api/client";
import { PlatformSettingsPage } from "./PlatformSettingsPage";

vi.mock("sonner", () => ({
  toast: { error: vi.fn(), info: vi.fn(), success: vi.fn() },
}));

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <PlatformSettingsPage />
    </QueryClientProvider>,
  );
}

describe("平台设置", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api.settings.projectStorage, "get").mockResolvedValue({
      path: "D:\\旧项目目录",
      source: "database",
    });
  });

  test("test_project_storage_load_displays_current_path_and_scope_notice", async () => {
    renderPage();

    expect(await screen.findByLabelText("当前项目存储目录")).toHaveValue(
      "D:\\旧项目目录",
    );
    expect(
      screen.getByText("仅影响之后新建项目，已有项目不会自动搬迁。"),
    ).toBeVisible();
  });

  test("test_project_storage_select_success_updates_path_and_shows_success", async () => {
    const user = userEvent.setup();
    vi.spyOn(api.settings.projectStorage, "select").mockResolvedValue({
      selected: true,
      path: "E:\\新项目目录",
      source: "database",
    });
    renderPage();

    await user.click(await screen.findByRole("button", { name: "选择目录" }));

    expect(await screen.findByLabelText("当前项目存储目录")).toHaveValue(
      "E:\\新项目目录",
    );
    expect(toast.success).toHaveBeenCalledWith("项目存储目录已更新");
  });

  test("test_project_storage_select_cancel_keeps_path_and_shows_info", async () => {
    const user = userEvent.setup();
    vi.spyOn(api.settings.projectStorage, "select").mockResolvedValue({
      selected: false,
      path: "D:\\旧项目目录",
      source: "database",
    });
    renderPage();

    await user.click(await screen.findByRole("button", { name: "选择目录" }));

    await waitFor(() =>
      expect(toast.info).toHaveBeenCalledWith("已取消选择，当前目录未更改"),
    );
    expect(screen.getByLabelText("当前项目存储目录")).toHaveValue(
      "D:\\旧项目目录",
    );
    expect(toast.error).not.toHaveBeenCalled();
  });

  test("test_project_storage_select_failure_keeps_path_and_shows_error", async () => {
    const user = userEvent.setup();
    vi.spyOn(api.settings.projectStorage, "select").mockRejectedValue(
      new Error("所选目录不可写"),
    );
    renderPage();

    await user.click(await screen.findByRole("button", { name: "选择目录" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("所选目录不可写"),
    );
    expect(screen.getByLabelText("当前项目存储目录")).toHaveValue(
      "D:\\旧项目目录",
    );
  });

  test("test_project_storage_select_pending_disables_button", async () => {
    const user = userEvent.setup();
    vi.spyOn(api.settings.projectStorage, "select").mockImplementation(
      () => new Promise(() => undefined),
    );
    renderPage();
    const button = await screen.findByRole("button", { name: "选择目录" });

    await user.click(button);

    expect(button).toBeDisabled();
  });
});
