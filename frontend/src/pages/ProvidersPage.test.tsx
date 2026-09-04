import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { api } from "../api/client";
import type { ApiProvider } from "../types/api";
import { ProvidersPage } from "./ProvidersPage";

vi.mock("sonner", () => ({
  toast: { error: vi.fn(), success: vi.fn(), warning: vi.fn() },
}));

function makeProvider(
  overrides: Partial<ApiProvider> = {},
): ApiProvider {
  return {
    id: "provider-1",
    kind: "text",
    name: "文字模型 A",
    adapter: "mock",
    base_url: "",
    api_key_env: "",
    api_key_configured: false,
    api_key_source: "none",
    model: "mock-v1",
    params: {},
    timeout_sec: 120,
    max_retries: 2,
    enabled: false,
    is_default: false,
    created_at: "",
    updated_at: "",
    ...overrides,
  };
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <ProvidersPage />
    </QueryClientProvider>,
  );
}

describe("Provider 直接 API Key", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api.providers, "list").mockResolvedValue([]);
  });

  test("test_new_provider_form_accepts_direct_api_key", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "添加接口" }));
    const keyInput = screen.getByLabelText("API Key");
    expect(keyInput).toHaveAttribute("type", "password");
    await user.type(keyInput, "direct-provider-secret");
    expect(keyInput).toHaveValue("direct-provider-secret");
  });
});

describe("Provider 编辑与状态展示", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api.providers, "list").mockResolvedValue([]);
  });

  test("test_provider_edit_modal_disables_kind_select", async () => {
    const user = userEvent.setup();
    vi.mocked(api.providers.list).mockResolvedValue([
      makeProvider({ enabled: true }),
    ]);
    renderPage();

    await user.click(
      await screen.findByRole("button", { name: "编辑 文字模型 A" }),
    );
    // 更新接口不提交 kind，编辑模式下接口类型必须禁用
    expect(screen.getByLabelText("接口类型")).toBeDisabled();
  });

  test("test_provider_new_modal_kind_select_is_enabled", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "添加接口" }));
    expect(screen.getByLabelText("接口类型")).toBeEnabled();
  });

  test("test_provider_card_shows_disabled_badge_for_disabled_provider", async () => {
    vi.mocked(api.providers.list).mockResolvedValue([makeProvider()]);
    renderPage();

    // 停用状态用普通徽标展示，不再向状态枚举组件传中文
    expect(await screen.findByText("已停用")).toBeInTheDocument();
  });
});
