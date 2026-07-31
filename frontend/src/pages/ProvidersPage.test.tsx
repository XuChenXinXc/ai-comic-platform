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
