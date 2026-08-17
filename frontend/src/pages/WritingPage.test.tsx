import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { ApiError, api, queryKeys } from "../api/client";
import type {
  ContentRecord,
  ScreenplayContent,
} from "../types/api";
import { WritingPage } from "./WritingPage";

const projectContext = vi.hoisted(() => ({
  activeProject: {
    id: "chapter-1",
    name: "验收项目",
    source_text: "测试小说原文",
    style_prompt: "二维国风漫剧",
    source_revision: 1,
    source_signature: "source-1",
  },
  activeProjectId: "chapter-1",
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
vi.mock("../hooks/use-chapter", () => ({
  useChapter: () => {
    const chapterId =
      projectContext.activeProjectId === "chapter-1" ? "chapter-1" : "chapter-2";
    return {
      chapters: [
        { id: "chapter-1", project_id: "project-1", chapter_no: 1, title: "第一章" },
        { id: "chapter-2", project_id: "project-2", chapter_no: 1, title: "第一章" },
      ],
      activeChapter: {
        id: chapterId,
        project_id: projectContext.activeProjectId,
        chapter_no: 1,
        title: "第一章",
        source_signature: chapterId === "chapter-1" ? "source-1" : "source-2",
      },
      activeChapterId: chapterId,
      selectChapter: vi.fn(),
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    };
  },
}));
vi.mock("../components/chapter-required", () => ({
  ChapterRequired: ({ children }: { children: ReactNode }) => children,
}));
vi.mock("../components/chapter-selector", () => ({
  ChapterSelector: () => null,
}));

function screenplayRecord(
  revision: number,
  title: string,
): ContentRecord<ScreenplayContent> {
  return { id: "test-content", chapter_id: "chapter-1", project_id: "test-project",
    content_json: { title, logline: "", scenes: [] },
    status: "ready",
    source_signature: "source-1",
    revision,
    updated_at: `2026-07-28T11:00:0${revision}Z`,
  };
}

function renderWritingPage(queryClient: QueryClient) {
  return render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <WritingPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );
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
      style_prompt: "二维国风漫剧",
      source_revision: 1,
      source_signature: "source-1",
    };
    projectContext.activeProjectId = "chapter-1";
    vi.spyOn(api.analysis, "get").mockRejectedValue(
      new ApiError("分析不存在", 404),
    );
    vi.spyOn(api.screenplay, "get").mockRejectedValue(
      new ApiError("剧本不存在", 404),
    );
  });

  test("test_writing_page_only_shows_screenplay_without_analysis_tab", async () => {
    vi.mocked(api.screenplay.get).mockResolvedValue(
      screenplayRecord(1, "唯一剧本"),
    );
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });
    renderWritingPage(queryClient);

    expect(await screen.findByLabelText("剧本标题")).toHaveValue("唯一剧本");
    expect(screen.queryByRole("tab", { name: "小说分析" })).not.toBeInTheDocument();
    expect(screen.queryByText("小说分析")).not.toBeInTheDocument();
    expect(api.analysis.get).not.toHaveBeenCalled();
  });

  test("test_style_prompt_is_above_screenplay_metadata", async () => {
    vi.mocked(api.screenplay.get).mockResolvedValue(
      screenplayRecord(1, "唯一剧本"),
    );
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });
    renderWritingPage(queryClient);

    const stylePrompt = await screen.findByLabelText("统一画风 STYLE_ANCHOR");
    const titleInput = await screen.findByLabelText("剧本标题");

    expect(stylePrompt).toHaveValue("二维国风漫剧");
    expect(
      stylePrompt.compareDocumentPosition(titleInput) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  test("test_saving_screenplay_persists_style_prompt", async () => {
    vi.mocked(api.screenplay.get).mockResolvedValue(
      screenplayRecord(1, "唯一剧本"),
    );
    const updateProject = vi
      .spyOn(api.projects, "update")
      .mockResolvedValue({
        ...projectContext.activeProject,
        style_prompt: "新画风",
      } as never);
    vi.spyOn(api.screenplay, "update").mockResolvedValue(
      screenplayRecord(2, "唯一剧本"),
    );
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });
    renderWritingPage(queryClient);

    const stylePrompt = await screen.findByLabelText("统一画风 STYLE_ANCHOR");
    await userEvent.clear(stylePrompt);
    await userEvent.type(stylePrompt, "新画风");
    await userEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => {
      expect(updateProject).toHaveBeenCalledWith("chapter-1", {
        style_prompt: "新画风",
      });
    });
  });

  test("test_screenplay_dialogue_with_line_field_displays_dialogue_text", async () => {
    vi.mocked(api.screenplay.get).mockResolvedValue({
      ...screenplayRecord(1, "对白兼容剧本"),
      content_json: {
        title: "对白兼容剧本",
        logline: "",
        scenes: [
          {
            scene_no: 1,
            scene_name: "门口",
            characters: ["小明"],
            dialogue: [{ speaker: "小明", line: "你终于来了。" }],
            narration: [],
          },
        ],
      },
    });
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });
    renderWritingPage(queryClient);

    expect(
      await screen.findByDisplayValue("小明：你终于来了。"),
    ).toBeInTheDocument();
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
    renderWritingPage(queryClient);

    await userEvent.click(
      await screen.findByRole("tab", { name: /剧本生成/ }),
    );
    const titleInput = await screen.findByLabelText("剧本标题");
    await userEvent.clear(titleInput);
    await userEvent.type(titleInput, "本地未保存剧本");

    serverRecord = screenplayRecord(2, "服务端新剧本");
    await queryClient.invalidateQueries({
      queryKey: queryKeys.screenplay("chapter-1"),
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
      style_prompt: "二维国风漫剧",
      source_revision: 1,
      source_signature: "source-1",
    };
    projectContext.activeProjectId = "chapter-1";
    vi.spyOn(api.screenplay, "get").mockRejectedValue(
      new ApiError("剧本不存在", 404),
    );
  });

  test("test_edit_after_save_started_preserves_newer_local_screenplay", async () => {
    let serverRecord = screenplayRecord(1, "服务端旧剧本");
    vi.mocked(api.screenplay.get).mockImplementation(async () => serverRecord);
    const pendingSave = deferred<ContentRecord<ScreenplayContent>>();
    vi.spyOn(api.screenplay, "update").mockImplementation(
      async () => pendingSave.promise,
    );
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });
    renderWritingPage(queryClient);
    const titleInput = await screen.findByLabelText("剧本标题");
    fireEvent.change(titleInput, {
      target: { value: "提交版本" },
    });
    await userEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(screen.getByRole("button", { name: "保存" })).toBeDisabled();
    fireEvent.change(titleInput, {
      target: { value: "保存期间的新编辑" },
    });

    serverRecord = screenplayRecord(2, "提交版本");
    pendingSave.resolve(serverRecord);

    await waitFor(() => {
      expect(queryClient.isMutating()).toBe(0);
    });
    expect(screen.getByRole("button", { name: "保存" })).not.toBeDisabled();
    expect(api.screenplay.update).toHaveBeenCalledWith("chapter-1", {
      content_json: { title: "提交版本", logline: "", scenes: [] },
      status: "ready",
    });
    expect(titleInput).toHaveValue("保存期间的新编辑");
  });

  test("test_save_from_previous_project_does_not_overwrite_current_project", async () => {
    const records = {
      "chapter-1": screenplayRecord(1, "项目 A 服务端剧本"),
      "chapter-2": {
        ...screenplayRecord(1, "项目 B 服务端剧本"),
        chapter_id: "chapter-2",
        project_id: "project-2",
        source_signature: "source-2",
      },
    };
    vi.mocked(api.screenplay.get).mockImplementation(
      async (projectId) => records[projectId as keyof typeof records],
    );
    const pendingSave = deferred<ContentRecord<ScreenplayContent>>();
    vi.spyOn(api.screenplay, "update").mockImplementation(
      async () => pendingSave.promise,
    );
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });
    const view = renderWritingPage(queryClient);
    const titleInput = await screen.findByLabelText("剧本标题");
    fireEvent.change(titleInput, {
      target: { value: "项目 A 提交剧本" },
    });
    await userEvent.click(screen.getByRole("button", { name: "保存" }));

    projectContext.activeProject = {
      id: "project-2",
      name: "项目 B",
      source_text: "项目 B 小说",
      style_prompt: "项目 B 画风",
      source_revision: 1,
      source_signature: "source-2",
    };
    projectContext.activeProjectId = "project-2";
    view.rerender(
      <MemoryRouter>
        <QueryClientProvider client={queryClient}>
          <WritingPage />
        </QueryClientProvider>
      </MemoryRouter>,
    );
    const projectBTitle = await screen.findByLabelText("剧本标题");
    await waitFor(() => {
      expect(projectBTitle).toHaveValue("项目 B 服务端剧本");
    });
    fireEvent.change(projectBTitle, {
      target: { value: "项目 B 本地编辑" },
    });

    pendingSave.resolve(screenplayRecord(2, "项目 A 提交剧本"));

    await waitFor(() => {
      expect(queryClient.isMutating()).toBe(0);
    });
    expect(api.screenplay.update).toHaveBeenCalledWith("chapter-1", {
      content_json: { title: "项目 A 提交剧本", logline: "", scenes: [] },
      status: "ready",
    });
    expect(projectBTitle).toHaveValue("项目 B 本地编辑");
  });
});
