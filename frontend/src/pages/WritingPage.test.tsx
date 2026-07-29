import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { ApiError, api, queryKeys } from "../api/client";
import type {
  ContentRecord,
  ScreenplayContent,
} from "../types/api";
import { WritingPage } from "./WritingPage";

const projectContext = vi.hoisted(() => ({
  activeProject: {
    id: "project-1",
    name: "验收项目",
    source_text: "测试小说原文",
    source_revision: 1,
    source_signature: "source-1",
  },
  activeProjectId: "project-1",
}));

vi.mock("sonner", () => ({
  toast: { error: vi.fn(), success: vi.fn(), warning: vi.fn() },
}));
vi.mock("../hooks/use-project", () => ({
  useProject: () => projectContext,
}));
vi.mock("../components/project-required", () => ({
  ProjectRequired: ({ children }: { children: ReactNode }) => children,
}));

function analysisRecord(
  revision: number,
  summary: string,
): ContentRecord<Record<string, unknown>> {
  return {
    project_id: "project-1",
    content_json: { summary },
    status: "ready",
    source_signature: "source-1",
    revision,
    updated_at: `2026-07-28T10:00:0${revision}Z`,
  };
}

function screenplayRecord(
  revision: number,
  title: string,
): ContentRecord<ScreenplayContent> {
  return {
    project_id: "project-1",
    content_json: { title, logline: "", scenes: [] },
    status: "ready",
    source_signature: "source-1",
    revision,
    updated_at: `2026-07-28T11:00:0${revision}Z`,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((complete) => {
    resolve = complete;
  });
  return { promise, resolve };
}

describe("文字工作台服务端版本冲突", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    projectContext.activeProject = {
      id: "project-1",
      name: "验收项目",
      source_text: "测试小说原文",
      source_revision: 1,
      source_signature: "source-1",
    };
    projectContext.activeProjectId = "project-1";
    vi.spyOn(api.analysis, "get").mockRejectedValue(
      new ApiError("分析不存在", 404),
    );
    vi.spyOn(api.screenplay, "get").mockRejectedValue(
      new ApiError("剧本不存在", 404),
    );
  });

  test("test_server_revision_while_analysis_dirty_preserves_local_until_user_loads_server", async () => {
    let serverRecord = analysisRecord(1, "服务端旧稿");
    vi.spyOn(api.analysis, "get").mockImplementation(async () => serverRecord);
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <WritingPage />
      </QueryClientProvider>,
    );

    const editor = await screen.findByRole("textbox");
    fireEvent.change(editor, {
      target: { value: '{"summary":"本地未保存"}' },
    });

    serverRecord = analysisRecord(2, "服务端新稿");
    await queryClient.invalidateQueries({
      queryKey: queryKeys.analysis("project-1"),
    });

    expect(
      await screen.findByText("服务端已有新版本"),
    ).toBeInTheDocument();
    expect((editor as HTMLTextAreaElement).value).toContain("本地未保存");

    await userEvent.click(
      screen.getByRole("button", { name: "保留本地编辑" }),
    );
    await waitFor(() => {
      expect(
        screen.queryByText("服务端已有新版本"),
      ).not.toBeInTheDocument();
    });
    expect((editor as HTMLTextAreaElement).value).toContain("本地未保存");

    serverRecord = analysisRecord(3, "服务端更新稿");
    await queryClient.invalidateQueries({
      queryKey: queryKeys.analysis("project-1"),
    });
    await userEvent.click(
      await screen.findByRole("button", { name: "载入服务端版本" }),
    );

    await waitFor(() => {
      expect((editor as HTMLTextAreaElement).value).toContain("服务端更新稿");
    });
    expect((editor as HTMLTextAreaElement).value).not.toContain("本地未保存");
  });

  test("test_server_revision_while_screenplay_dirty_preserves_local_until_user_loads_server", async () => {
    let serverRecord = screenplayRecord(1, "服务端旧剧本");
    vi.mocked(api.screenplay.get).mockImplementation(async () => serverRecord);
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <WritingPage />
      </QueryClientProvider>,
    );

    await userEvent.click(
      await screen.findByRole("tab", { name: /剧本生成/ }),
    );
    const titleInput = await screen.findByLabelText("剧本标题");
    await userEvent.clear(titleInput);
    await userEvent.type(titleInput, "本地未保存剧本");

    serverRecord = screenplayRecord(2, "服务端新剧本");
    await queryClient.invalidateQueries({
      queryKey: queryKeys.screenplay("project-1"),
    });

    expect(
      await screen.findByText("服务端已有新版本"),
    ).toBeInTheDocument();
    expect(titleInput).toHaveValue("本地未保存剧本");

    await userEvent.click(
      screen.getByRole("button", { name: "载入服务端版本" }),
    );
    await waitFor(() => {
      expect(titleInput).toHaveValue("服务端新剧本");
    });
  });
});

describe("文字工作台延迟保存", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    projectContext.activeProject = {
      id: "project-1",
      name: "验收项目",
      source_text: "测试小说原文",
      source_revision: 1,
      source_signature: "source-1",
    };
    projectContext.activeProjectId = "project-1";
    vi.spyOn(api.screenplay, "get").mockRejectedValue(
      new ApiError("剧本不存在", 404),
    );
  });

  test("test_edit_after_save_started_preserves_newer_local_analysis", async () => {
    let serverRecord = analysisRecord(1, "服务端旧稿");
    vi.spyOn(api.analysis, "get").mockImplementation(async () => serverRecord);
    const pendingSave = deferred<ContentRecord<Record<string, unknown>>>();
    vi.spyOn(api.analysis, "update").mockImplementation(
      async () => pendingSave.promise,
    );
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <WritingPage />
      </QueryClientProvider>,
    );
    const editor = await screen.findByRole("textbox");
    fireEvent.change(editor, {
      target: { value: '{"summary":"提交版本"}' },
    });
    await userEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(
      screen.getByRole("button", { name: "确认当前内容" }),
    ).toBeDisabled();
    fireEvent.change(editor, {
      target: { value: '{"summary":"保存期间的新编辑"}' },
    });

    serverRecord = analysisRecord(2, "提交版本");
    pendingSave.resolve(serverRecord);

    await waitFor(() => {
      expect(queryClient.isMutating()).toBe(0);
    });
    expect(
      screen.getByRole("button", { name: "确认当前内容" }),
    ).not.toBeDisabled();
    expect(api.analysis.update).toHaveBeenCalledWith("project-1", {
      content_json: { summary: "提交版本" },
      status: "ready",
    });
    expect(editor).toHaveValue(
      JSON.stringify({ summary: "保存期间的新编辑" }, null, 2),
    );
  });

  test("test_save_from_previous_project_does_not_overwrite_current_project", async () => {
    const records = {
      "project-1": analysisRecord(1, "项目 A 服务端稿"),
      "project-2": {
        ...analysisRecord(1, "项目 B 服务端稿"),
        project_id: "project-2",
        source_signature: "source-2",
      },
    };
    vi.spyOn(api.analysis, "get").mockImplementation(
      async (projectId) => records[projectId as keyof typeof records],
    );
    const pendingSave = deferred<ContentRecord<Record<string, unknown>>>();
    vi.spyOn(api.analysis, "update").mockImplementation(
      async () => pendingSave.promise,
    );
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });
    const view = render(
      <QueryClientProvider client={queryClient}>
        <WritingPage />
      </QueryClientProvider>,
    );
    const editor = await screen.findByRole("textbox");
    fireEvent.change(editor, {
      target: { value: '{"summary":"项目 A 提交稿"}' },
    });
    await userEvent.click(screen.getByRole("button", { name: "保存" }));

    projectContext.activeProject = {
      id: "project-2",
      name: "项目 B",
      source_text: "项目 B 小说",
      source_revision: 1,
      source_signature: "source-2",
    };
    projectContext.activeProjectId = "project-2";
    view.rerender(
      <QueryClientProvider client={queryClient}>
        <WritingPage />
      </QueryClientProvider>,
    );
    const projectBEditor = await screen.findByRole("textbox");
    await waitFor(() => {
      expect(projectBEditor).toHaveValue(
        JSON.stringify({ summary: "项目 B 服务端稿" }, null, 2),
      );
    });
    fireEvent.change(projectBEditor, {
      target: { value: '{"summary":"项目 B 本地编辑"}' },
    });

    pendingSave.resolve(analysisRecord(2, "项目 A 提交稿"));

    await waitFor(() => {
      expect(queryClient.isMutating()).toBe(0);
    });
    expect(api.analysis.update).toHaveBeenCalledWith("project-1", {
      content_json: { summary: "项目 A 提交稿" },
      status: "ready",
    });
    expect(projectBEditor).toHaveValue(
      JSON.stringify({ summary: "项目 B 本地编辑" }, null, 2),
    );
  });
});
