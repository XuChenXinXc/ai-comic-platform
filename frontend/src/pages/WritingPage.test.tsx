import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { toast } from "sonner";
import { ApiError, api, queryKeys } from "../api/client";
import type {
  ContentRecord,
  ScreenplayContent,
} from "../types/api";
import { WritingPage } from "./WritingPage";

const projectContext = vi.hoisted<{
  activeProject: {
    id: string;
    name: string;
    source_text: string;
    style_prompt: string;
    source_revision: number;
    source_signature: string;
    visual_bible?: unknown;
  };
  activeProjectId: string;
}>(() => ({
  activeProject: {
    id: "chapter-1",
    name: "验收项目",
    source_text: "测试小说原文",
    style_prompt: "二维国风漫剧",
    source_revision: 1,
    source_signature: "source-1",
    visual_bible: null,
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

describe("文字工作台对白切分", () => {
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
    vi.spyOn(api.screenplay, "get").mockResolvedValue({
      ...screenplayRecord(1, "对白切分剧本"),
      content_json: {
        title: "对白切分剧本",
        logline: "",
        scenes: [
          {
            scene_no: 1,
            scene_name: "门口",
            characters: ["小明"],
            dialogue: [],
            narration: [],
          },
        ],
      },
    } as never);
  });

  test("test_dialogue_split_requires_short_punctuation_free_speaker_prefix", async () => {
    const updateSpy = vi.spyOn(api.screenplay, "update").mockResolvedValue(
      screenplayRecord(2, "对白切分剧本"),
    );
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });
    renderWritingPage(queryClient);

    // 定位场次卡片中的对白 textarea（Label 未通过 htmlFor 关联）。
    const dialogueLabel = await screen.findByText("对白（每行一条）");
    const dialogueTextarea = dialogueLabel.parentElement!.querySelector(
      "textarea",
    ) as HTMLTextAreaElement;
    fireEvent.change(dialogueTextarea, {
      target: {
        value: [
          "小明：你终于来了。",
          "这一整天从早到晚忙得他头昏脑涨：先开会再赶稿",
          "他停顿了一下，说：你好",
          "没有冒号的整句",
          "一二三四五六七八九十一二：正文",
        ].join("\n"),
      },
    });
    await userEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(updateSpy).toHaveBeenCalledTimes(1));
    const payload = updateSpy.mock.calls[0][1];
    // update 的 content_json 类型是 unknown，按 screenplay 结构断言解析结果。
    const screenplay = payload.content_json as {
      scenes: { dialogue: Array<{ speaker: string; text: string }> }[];
    };
    expect(screenplay.scenes[0].dialogue).toEqual([
      // 短且无句末标点的冒号前缀识别为说话人。
      { speaker: "小明", text: "你终于来了。" },
      // 冒号前 15 个字符（>12）：整句作台词。
      { speaker: "", text: "这一整天从早到晚忙得他头昏脑涨：先开会再赶稿" },
      // 冒号前含句末标点（，）：整句作台词。
      { speaker: "", text: "他停顿了一下，说：你好" },
      // 无冒号：整句作台词。
      { speaker: "", text: "没有冒号的整句" },
      // 冒号前正好 12 个字符：仍视为说话人。
      { speaker: "一二三四五六七八九十一二", text: "正文" },
    ]);
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

describe("视觉圣经 VisualBible 编辑面板", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    projectContext.activeProject = {
      id: "project-1",
      name: "验收项目",
      source_text: "测试小说原文",
      style_prompt: "二维国风漫剧",
      source_revision: 1,
      source_signature: "source-1",
      visual_bible: null,
    };
    projectContext.activeProjectId = "chapter-1";
    vi.spyOn(api.analysis, "get").mockRejectedValue(
      new ApiError("分析不存在", 404),
    );
    vi.spyOn(api.screenplay, "get").mockResolvedValue(
      screenplayRecord(1, "视觉圣经剧本"),
    );
  });

  function createQueryClient() {
    return new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });
  }

  async function openPanel() {
    await screen.findByLabelText("统一画风 STYLE_ANCHOR");
    await userEvent.click(screen.getByText("视觉圣经 VisualBible"));
  }

  test("test_visual_bible_panel_collapsed_by_default_with_auto_badge", async () => {
    renderWritingPage(createQueryClient());

    await screen.findByLabelText("统一画风 STYLE_ANCHOR");
    expect(screen.getByText("视觉圣经 VisualBible")).toBeInTheDocument();
    expect(screen.getByText("自动推导")).toBeInTheDocument();
    expect(screen.queryByText("已自定义")).not.toBeInTheDocument();
    // 默认收起：面板内容不渲染，字段不可见。
    expect(screen.queryByLabelText("类型 type")).not.toBeInTheDocument();

    await userEvent.click(screen.getByText("视觉圣经 VisualBible"));
    expect(screen.getByLabelText("类型 type")).toBeInTheDocument();
  });

  test("test_visual_bible_save_builds_structured_payload", async () => {
    const updateProject = vi.spyOn(api.projects, "update").mockResolvedValue({
      ...projectContext.activeProject,
      visual_bible: {
        line: { type: "粗笔焦墨" },
        shading: { realistic_shading: true },
        realism: { character: 0.8 },
        forbidden_styles: ["低多边形", "赛博朋克"],
      },
    } as never);
    renderWritingPage(createQueryClient());
    await openPanel();

    await userEvent.type(screen.getByLabelText("类型 type"), "粗笔焦墨");
    await userEvent.selectOptions(
      screen.getByLabelText("写实明暗 realistic_shading"),
      "true",
    );
    fireEvent.change(screen.getByLabelText("角色 character（0-1）"), {
      target: { value: "0.8" },
    });
    // jsdom 的键盘事件会丢失顿号字符，改用直接赋值触发 change。
    fireEvent.change(
      screen.getByLabelText("禁用风格 forbidden_styles（逗号或顿号分隔）"),
      { target: { value: "低多边形、赛博朋克" } },
    );
    await userEvent.click(
      screen.getByRole("button", { name: "保存视觉圣经" }),
    );

    await waitFor(() => {
      expect(updateProject).toHaveBeenCalledWith("chapter-1", {
        visual_bible: {
          line: { type: "粗笔焦墨" },
          shading: { realistic_shading: true },
          realism: { character: 0.8 },
          forbidden_styles: ["低多边形", "赛博朋克"],
        },
      });
    });
  });

  test("test_visual_bible_reset_sends_null", async () => {
    projectContext.activeProject = {
      ...projectContext.activeProject,
      visual_bible: { texture: "宣纸颗粒" },
    };
    const updateProject = vi.spyOn(api.projects, "update").mockResolvedValue({
      ...projectContext.activeProject,
      visual_bible: null,
    } as never);
    renderWritingPage(createQueryClient());

    await screen.findByLabelText("统一画风 STYLE_ANCHOR");
    expect(screen.getByText("已自定义")).toBeInTheDocument();
    await userEvent.click(screen.getByText("视觉圣经 VisualBible"));
    await userEvent.click(
      screen.getByRole("button", { name: "重置为自动推导" }),
    );

    await waitFor(() => {
      expect(updateProject).toHaveBeenCalledWith("chapter-1", {
        visual_bible: null,
      });
    });
  });

  test("test_visual_bible_rejects_out_of_range_realism", async () => {
    const updateProject = vi.spyOn(api.projects, "update").mockResolvedValue({
      ...projectContext.activeProject,
    } as never);
    renderWritingPage(createQueryClient());
    await openPanel();

    fireEvent.change(screen.getByLabelText("角色 character（0-1）"), {
      target: { value: "1.5" },
    });
    await userEvent.click(
      screen.getByRole("button", { name: "保存视觉圣经" }),
    );

    expect(updateProject).not.toHaveBeenCalled();
    expect(toast.error).toHaveBeenCalledWith(
      "写实度·角色 必须是 0 到 1 之间的数值",
    );
  });

  test("test_visual_bible_inputs_show_derived_preview_values", async () => {
    vi.spyOn(api.projects, "visualBiblePreview").mockResolvedValue({
      style_prompt: "二维国风漫剧",
      visual_bible: null,
      derived: {
        line: { type: "粗笔焦墨", thickness: "中粗" },
        coloring: { method: "湿墨铺色", saturation: "低至中" },
        shading: { method: "墨色明暗块", realistic_shading: false },
        material: { method: "绘画概括", pbr: false },
        lighting: { realism: "写意" },
        depth_of_field: { enabled: false },
        realism: { character: 0.8, environment: 0.7 },
        texture: "宣纸、水墨晕染、粗笔笔触",
        forbidden_styles: ["3D", "PBR"],
      },
      rendered: "结构化画风规范：线条：类型「粗笔焦墨」。",
    });
    renderWritingPage(createQueryClient());
    await openPanel();

    // 表单输入框回显当前画风的自动推导值，而不是空白。
    await waitFor(() => {
      expect(screen.getByLabelText("类型 type")).toHaveValue("粗笔焦墨");
    });
    expect(screen.getByLabelText("粗细 thickness")).toHaveValue("中粗");
    expect(screen.getByLabelText("饱和度 saturation")).toHaveValue("低至中");
    expect(
      screen.getByLabelText("写实明暗 realistic_shading"),
    ).toHaveValue("false");
    expect(screen.getByLabelText("PBR pbr")).toHaveValue("false");
    expect(screen.getByLabelText("角色 character（0-1）")).toHaveValue(0.8);
    expect(screen.getByLabelText("质感 texture")).toHaveValue(
      "宣纸、水墨晕染、粗笔笔触",
    );
    expect(
      screen.getByLabelText("禁用风格 forbidden_styles（逗号或顿号分隔）"),
    ).toHaveValue("3D、PBR");
  });
});
