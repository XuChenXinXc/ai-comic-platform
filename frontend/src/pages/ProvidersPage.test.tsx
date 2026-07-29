import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { api } from "../api/client";
import { ProvidersPage } from "./ProvidersPage";

vi.mock("sonner", () => ({
  toast: { error: vi.fn(), success: vi.fn(), warning: vi.fn() },
}));

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

describe("Provider 密钥环境变量提示", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api.providers, "list").mockResolvedValue([]);
  });

  test("test_new_provider_key_placeholder_follows_provider_kind", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "添加接口" }));
    const keyInput = screen.getByRole("textbox", {
      name: "API Key 环境变量名",
    });
    expect(keyInput).toHaveAttribute("placeholder", "TEXT_API_KEY");

    await user.selectOptions(
      screen.getByRole("combobox", { name: "接口类型" }),
      "image",
    );

    expect(keyInput).toHaveAttribute("placeholder", "IMAGE_API_KEY");
  });
});
