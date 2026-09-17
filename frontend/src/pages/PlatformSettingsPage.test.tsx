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
    vi.spyOn(api.settings.cameraPolicy, "get").mockResolvedValue({
      policy: "克制",
      options: ["克制", "标准", "丰富"],
    });
    vi.spyOn(api.settings.defaultStylePrompt, "get").mockResolvedValue({
      style_prompt: "水墨国风画风",
    });
    vi.spyOn(api.settings.providerAllowPrivateHosts, "get").mockResolvedValue({
      enabled: false,
      source: "default",
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

  test("test_camera_policy_load_displays_current_and_hint", async () => {
    renderPage();

    expect(await screen.findByLabelText("当前策略")).toHaveValue("克制");
    expect(screen.getAllByText(/定镜优先/).length).toBeGreaterThan(0);
  });

  test("test_camera_policy_change_updates_policy_and_shows_success", async () => {
    const user = userEvent.setup();
    vi.spyOn(api.settings.cameraPolicy, "set").mockResolvedValue({
      policy: "标准",
      options: ["克制", "标准", "丰富"],
    });
    renderPage();

    await user.selectOptions(
      await screen.findByLabelText("当前策略"),
      "标准",
    );

    expect(await screen.findByLabelText("当前策略")).toHaveValue("标准");
    expect(toast.success).toHaveBeenCalledWith("运镜策略已更新");
  });

  test("test_default_style_prompt_load_displays_current_value", async () => {
    renderPage();

    expect(
      await screen.findByLabelText("默认统一画风 STYLE_ANCHOR"),
    ).toHaveValue("水墨国风画风");
  });

  test("test_default_style_prompt_save_updates_value_and_shows_success", async () => {
    const user = userEvent.setup();
    vi.spyOn(api.settings.defaultStylePrompt, "set").mockResolvedValue({
      style_prompt: "赛博朋克写实风",
    });
    renderPage();

    const textarea = await screen.findByLabelText(
      "默认统一画风 STYLE_ANCHOR",
    );
    await user.clear(textarea);
    await user.type(textarea, "赛博朋克写实风");

    const saveButton = screen.getByRole("button", { name: "保存画风" });
    expect(saveButton).toBeEnabled();
    await user.click(saveButton);

    await waitFor(() =>
      expect(api.settings.defaultStylePrompt.set).toHaveBeenCalledWith(
        "赛博朋克写实风",
      ),
    );
    expect(toast.success).toHaveBeenCalledWith("默认画风已更新");
  });

  test("test_provider_allow_private_hosts_load_displays_current_value", async () => {
    vi.spyOn(api.settings.providerAllowPrivateHosts, "get").mockResolvedValue({
      enabled: true,
      source: "database",
    });
    renderPage();

    expect(
      await screen.findByLabelText("允许连接本地/内网 Provider"),
    ).toBeChecked();
  });

  test("test_provider_allow_private_hosts_toggle_saves_and_shows_success", async () => {
    const user = userEvent.setup();
    vi.spyOn(api.settings.providerAllowPrivateHosts, "set").mockResolvedValue({
      enabled: true,
      source: "database",
    });
    renderPage();

    const checkbox = await screen.findByLabelText(
      "允许连接本地/内网 Provider",
    );
    expect(checkbox).not.toBeChecked();

    await user.click(checkbox);

    expect(await screen.findByLabelText("允许连接本地/内网 Provider")).toBeChecked();
    expect(api.settings.providerAllowPrivateHosts.set).toHaveBeenCalledWith(
      true,
    );
    expect(toast.success).toHaveBeenCalledWith("已允许连接本地/内网 Provider");
  });

  test("test_provider_allow_private_hosts_save_failure_shows_error", async () => {
    const user = userEvent.setup();
    vi.spyOn(api.settings.providerAllowPrivateHosts, "set").mockRejectedValue(
      new Error("保存失败"),
    );
    renderPage();

    await user.click(
      await screen.findByLabelText("允许连接本地/内网 Provider"),
    );

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("保存失败"),
    );
    expect(
      screen.getByLabelText("允许连接本地/内网 Provider"),
    ).not.toBeChecked();
  });
});
