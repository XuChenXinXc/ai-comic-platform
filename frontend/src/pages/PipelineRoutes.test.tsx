import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet } from "react-router";
import { afterEach, describe, expect, test, vi } from "vitest";
import { App } from "../App";
import { ApiError, api } from "../api/client";
import type {
  DynamicChecklistResponse,
  DynamicStageKey,
  DynamicWorkflowAction,
  DynamicWorkflowRun,
  DynamicWorkflowStage,
} from "../types/api";

vi.mock("../components/app-shell", () => ({
  AppShell: () => {
    return <div data-testid="app-shell"><Outlet /></div>;
  },
}));

vi.mock("./CustomShotsPage", () => ({
  CustomShotsPage: () => <div>custom-shots-route</div>,
}));

const stageKeys: DynamicStageKey[] = [
  "0-source",
  "1-character-bios",
  "2-rhythm",
  "3-script",
  "4-storyboard",
  "5a-image-prompts",
  "5b-video-prompts",
  "6-delivery",
];

function gate(status: "ready" | "warning" | "blocked" = "ready") {
  return {
    status,
    passed: status === "ready",
    diagnostics: [],
    can_confirm: status === "ready",
  } as DynamicWorkflowStage["gate_result"];
}

function stage(
  stageKey: DynamicStageKey,
  status: DynamicWorkflowStage["status"],
): DynamicWorkflowStage {
  return {
    id: `stage-${stageKey}`,
    workflow_run_id: "run-1",
    stage_key: stageKey,
    ordinal: stageKeys.indexOf(stageKey),
    status,
    input_signature: `input-${stageKey}`,
    raw_output: { source: stageKey },
    normalized_output: { normalized: stageKey },
    draft_output: { draft: stageKey },
    output_refs: [],
    gate_result: gate(status === "ready" ? "ready" : "blocked"),
    attempt: 1,
    error: "",
    created_at: "2026-08-18T00:00:00Z",
    updated_at: "2026-08-18T00:00:00Z",
  };
}

function makeWorkspace({
  currentStage = "0-source",
  revision = 7,
  selectedStatus = "ready",
  delivery = false,
  runStatus = "ready",
  projectId = "project-1",
}: {
  currentStage?: DynamicStageKey;
  revision?: number;
  selectedStatus?: DynamicWorkflowStage["status"];
  delivery?: boolean;
  runStatus?: DynamicWorkflowRun["status"];
  projectId?: string;
} = {}): DynamicWorkflowAction {
  const stages = stageKeys.map((key) => {
    if (delivery) {
      if (key === "6-delivery") return stage(key, selectedStatus);
      if (key === "5a-image-prompts" || key === "5b-video-prompts") {
        return stage(key, "confirmed");
      }
    }
    return stage(key, key === currentStage ? selectedStatus : "pending");
  });

  const run: DynamicWorkflowRun = {
    id: "run-1",
    project_id: projectId,
    chapter_id: "chapter-1",
    title: "雨夜旧案",
    chapter_title: "第一章 雨巷",
    kind: "dynamic_comic_0_6",
    schema_version: "1",
    status: runStatus,
    current_stage: currentStage,
    source_kind: "full_text",
    source_snapshot: { text: "一段原文" },
    config_snapshot: { target_duration_sec: 30, aspect_ratio: "9:16" },
    asset_mapping: [],
    input_signature: "signature-1",
    revision,
    error: "",
    created_at: "2026-08-18T00:00:00Z",
    updated_at: "2026-08-18T00:00:00Z",
  };
  return { run, stages };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("PipelineWorkspace routes", () => {
  test("free-create 入口只读取 free 项目，不自动 POST", async () => {
    const listProjects = vi.spyOn(api.projects, "list").mockResolvedValue([]);
    const createFreeCreate = vi.spyOn(api.dynamicRuns, "createFreeCreate");

    renderAt("/free-create");

    expect(await screen.findByText("还没有自由制作项目")).toBeInTheDocument();
    expect(listProjects).toHaveBeenCalledWith({ kind: "free" });
    expect(createFreeCreate).not.toHaveBeenCalled();
  });

  test("运行列表兼容旧 title 字段时显示 source_snapshot 中的运行标题", async () => {
    const run = makeWorkspace().run;
    run.title = "自由制作项目";
    run.source_snapshot = { ...run.source_snapshot, title: "雨夜旧案·第一集" };
    vi.spyOn(api.dynamicRuns, "list").mockResolvedValue([run]);

    renderAt("/free-create/project-1");

    expect(await screen.findByRole("heading", { name: "雨夜旧案·第一集" })).toBeInTheDocument();
  });

  test("新建表单要求原文或梗概二选一，并在成功后导航到工作区", async () => {
    const user = userEvent.setup();
    const workspace = makeWorkspace();
    workspace.run.status = "queued";
    const createFreeCreate = vi
      .spyOn(api.dynamicRuns, "createFreeCreate")
      .mockResolvedValue(workspace);
    vi.spyOn(api.dynamicRuns, "getWithStages").mockResolvedValue(workspace);

    renderAt("/free-create/pipeline/new");

    await user.click(screen.getByRole("button", { name: "创建并打开工作区" }));
    expect(await screen.findByText("请填写原文")).toBeInTheDocument();
    expect(createFreeCreate).not.toHaveBeenCalled();

    await user.type(screen.getByLabelText("运行标题"), "雨夜旧案");
    await user.type(screen.getByLabelText("章节标题"), "第一章 雨巷");
    await user.type(screen.getByLabelText("原文"), "一段原文");
    await user.click(screen.getByRole("checkbox", { name: /创建后自动运行/ }));
    await user.click(screen.getByRole("button", { name: "创建并打开工作区" }));

    expect(await screen.findByText("产物预览")).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "状态：排队中" })).toBeInTheDocument();
    expect(createFreeCreate).toHaveBeenCalledWith(
      expect.objectContaining({
        title: "雨夜旧案",
        chapter_title: "第一章 雨巷",
        source_text: "一段原文",
        target_duration_sec: 30,
        aspect_ratio: "9:16",
        auto_run: true,
      }),
    );
  });

  test("workspace 初次只调用 getWithStages，并显示八个阶段和状态", async () => {
    const workspace = makeWorkspace();
    const getWithStages = vi
      .spyOn(api.dynamicRuns, "getWithStages")
      .mockResolvedValue(workspace);

    renderAt("/free-create/project-1/pipeline/run-1");

    expect(await screen.findByText("产物预览")).toBeInTheDocument();
    expect(getWithStages).toHaveBeenCalledTimes(1);
    for (const key of stageKeys) {
      expect(screen.getByRole("button", { name: new RegExp(`${key}：`) })).toBeInTheDocument();
    }
    expect(screen.getAllByRole("status").length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByRole("status", { name: "状态：待确认" })[0]).toBeInTheDocument();
  });

  test("运行或阶段处于活动状态时会轮询工作区", async () => {
    const running = makeWorkspace({ runStatus: "running", selectedStatus: "running" });
    const finished = makeWorkspace({ revision: 8, selectedStatus: "ready" });
    const getWithStages = vi
      .spyOn(api.dynamicRuns, "getWithStages")
      .mockResolvedValueOnce(running)
      .mockResolvedValueOnce(finished);

    renderAt("/free-create/project-1/pipeline/run-1");

    expect(await screen.findByText("产物预览")).toBeInTheDocument();
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 1600));
    });

    expect(getWithStages).toHaveBeenCalledTimes(2);
    expect(screen.getByText("修订 R8")).toBeInTheDocument();
  });

  test("轮询不覆盖 dirty draft，保存成功后接受服务端草稿", async () => {
    const user = userEvent.setup();
    const initial = makeWorkspace({ runStatus: "running", selectedStatus: "running" });
    initial.stages[0].draft_output = { server: "initial" };
    const polled = makeWorkspace({ runStatus: "running", selectedStatus: "running", revision: 8 });
    polled.stages[0].draft_output = { server: "updated" };
    const saved = makeWorkspace({ runStatus: "ready", selectedStatus: "ready", revision: 9 });
    saved.stages[0].draft_output = { server: "saved" };
    vi.spyOn(api.dynamicRuns, "getWithStages")
      .mockResolvedValueOnce(initial)
      .mockResolvedValue(polled);
    const saveDraft = vi.spyOn(api.dynamicRuns, "saveDraft").mockResolvedValue(saved);

    renderAt("/free-create/project-1/pipeline/run-1/stage/0-source");
    const draft = await screen.findByRole("textbox", { name: "0-source draft JSON" });
    fireEvent.change(draft, { target: { value: '{"local":true}' } });

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 1600));
    });

    expect(draft).toHaveValue('{"local":true}');
    expect(screen.getByText(/服务端草稿已更新，已保留本地未保存内容/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /保存草稿/ }));
    await waitFor(() => expect(saveDraft).toHaveBeenCalled());
    expect(draft).toHaveValue('{\n  "server": "saved"\n}');
    expect(screen.queryByText(/未保存更改。轮询/)).not.toBeInTheDocument();
  });

  test("切换阶段后仍保留原阶段的 dirty draft", async () => {
    const user = userEvent.setup();
    const workspace = makeWorkspace();
    vi.spyOn(api.dynamicRuns, "getWithStages").mockResolvedValue(workspace);

    renderAt("/free-create/project-1/pipeline/run-1/stage/0-source");
    const sourceDraft = await screen.findByRole("textbox", { name: "0-source draft JSON" });
    fireEvent.change(sourceDraft, { target: { value: '{"kept":true}' } });
    await user.click(screen.getByRole("button", { name: /1-character-bios：/ }));
    await screen.findByRole("textbox", { name: "1-character-bios draft JSON" });
    await user.click(screen.getByRole("button", { name: /0-source：/ }));

    expect(await screen.findByRole("textbox", { name: "0-source draft JSON" })).toHaveValue(
      '{"kept":true}',
    );
  });

  test("partial_failed 显示失败阶段并可恢复重试，恢复后回到排队状态", async () => {
    const user = userEvent.setup();
    const partialFailed = makeWorkspace({
      runStatus: "partial_failed",
      selectedStatus: "partial_failed",
      revision: 7,
    });
    partialFailed.run.error = "运行中有阶段失败";
    partialFailed.stages[0].error = "provider job failed";
    const queued = makeWorkspace({ runStatus: "queued", selectedStatus: "queued", revision: 8 });
    vi.spyOn(api.dynamicRuns, "getWithStages").mockResolvedValue(partialFailed);
    const resume = vi.spyOn(api.dynamicRuns, "resume").mockResolvedValue(queued);

    renderAt("/free-create/project-1/pipeline/run-1");

    expect(await screen.findAllByRole("status", { name: "状态：部分失败" })).toHaveLength(2);
    expect(screen.getByRole("alert", { name: "运行失败诊断" })).toHaveTextContent(
      "provider job failed",
    );
    const resumeButton = screen.getByRole("button", { name: "恢复" });
    expect(resumeButton).toBeEnabled();
    await user.click(resumeButton);

    await waitFor(() => expect(resume).toHaveBeenCalledWith("run-1", 7));
    expect(await screen.findAllByRole("status", { name: "状态：排队中" })).toHaveLength(2);
    expect(screen.getByRole("button", { name: "开始运行" })).toBeDisabled();
  });

  test("stale 和 failed 运行不能进入阶段确认流程", async () => {
    const getWithStages = vi.spyOn(api.dynamicRuns, "getWithStages");
    for (const runStatus of ["stale", "failed"] as const) {
      const workspace = makeWorkspace({ runStatus, selectedStatus: "ready" });
      getWithStages.mockResolvedValue(workspace);

      const rendered = renderAt("/free-create/project-1/pipeline/run-1/stage/0-source");

      const confirm = await screen.findByRole("button", { name: "确认 0-source" });
      expect(confirm).toBeDisabled();
      rendered.unmount();
    }
  });

  test("轮询网络失败时保留 dirty draft 并显示非破坏性错误", async () => {
    const initial = makeWorkspace({ runStatus: "running", selectedStatus: "running" });
    const getWithStages = vi
      .spyOn(api.dynamicRuns, "getWithStages")
      .mockResolvedValueOnce(initial)
      .mockRejectedValueOnce(new Error("网络暂不可用"));

    renderAt("/free-create/project-1/pipeline/run-1/stage/0-source");
    const draft = await screen.findByRole("textbox", { name: "0-source draft JSON" });
    fireEvent.change(draft, { target: { value: '{"offline":true}' } });

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 1600));
    });

    expect(getWithStages).toHaveBeenCalledTimes(2);
    expect(draft).toHaveValue('{"offline":true}');
    expect(screen.getByText(/轮询更新失败：网络暂不可用/)).toBeInTheDocument();
  });

  test("组件卸载后轮询 timer 不再更新工作区", async () => {
    const initial = makeWorkspace({ runStatus: "running", selectedStatus: "running" });
    const pendingRefresh = deferred<DynamicWorkflowAction>();
    const getWithStages = vi
      .spyOn(api.dynamicRuns, "getWithStages")
      .mockResolvedValueOnce(initial)
      .mockReturnValue(pendingRefresh.promise);

    const rendered = renderAt("/free-create/project-1/pipeline/run-1");
    await screen.findByText("产物预览");
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 1600));
    });
    expect(getWithStages).toHaveBeenCalledTimes(2);

    rendered.unmount();
    pendingRefresh.resolve(makeWorkspace({ revision: 99, runStatus: "ready" }));
    await act(async () => {
      await Promise.resolve();
    });
    expect(getWithStages).toHaveBeenCalledTimes(2);
  });

  test("保存草稿和确认阶段都携带当前 revision", async () => {
    const user = userEvent.setup();
    const workspace = makeWorkspace({ revision: 7 });
    vi.spyOn(api.dynamicRuns, "getWithStages").mockResolvedValue(workspace);
    const saveDraft = vi.spyOn(api.dynamicRuns, "saveDraft").mockResolvedValue(workspace);
    const confirmStage = vi.spyOn(api.dynamicRuns, "confirmStage").mockResolvedValue(workspace);

    renderAt("/free-create/project-1/pipeline/run-1/stage/0-source");
    const draft = await screen.findByRole("textbox", { name: "0-source draft JSON" });
    fireEvent.change(draft, { target: { value: '{"edited":true}' } });
    await user.click(screen.getByRole("button", { name: /保存草稿/ }));

    await waitFor(() => expect(saveDraft).toHaveBeenCalledWith(
      "run-1",
      "0-source",
      expect.objectContaining({
        expected_revision: 7,
        draft_output: { edited: true },
      }),
    ));
    await user.click(screen.getByRole("button", { name: "确认 0-source" }));
    await waitFor(() => expect(confirmStage).toHaveBeenCalledWith("run-1", "0-source", 7));
  });

  test("409 revision 冲突显示刷新提示", async () => {
    const user = userEvent.setup();
    const workspace = makeWorkspace({ revision: 4 });
    vi.spyOn(api.dynamicRuns, "getWithStages").mockResolvedValue(workspace);
    vi.spyOn(api.dynamicRuns, "saveDraft").mockRejectedValue(
      new ApiError("revision conflict", 409),
    );

    renderAt("/free-create/project-1/pipeline/run-1/stage/0-source");
    await screen.findByText("产物预览");
    await user.click(screen.getByRole("button", { name: /保存草稿/ }));

    expect(await screen.findByRole("alert", { name: "" })).toHaveTextContent("内容已更新，请刷新");
  });

  test("保存阻塞草稿不回传旧 gate，并使用动作响应刷新门禁", async () => {
    const user = userEvent.setup();
    const blocked = makeWorkspace();
    blocked.stages[0].gate_result = gate("blocked");
    const refreshed = makeWorkspace({ revision: 8 });
    vi.spyOn(api.dynamicRuns, "getWithStages").mockResolvedValue(blocked);
    const saveDraft = vi.spyOn(api.dynamicRuns, "saveDraft").mockResolvedValue(refreshed);

    renderAt("/free-create/project-1/pipeline/run-1/stage/0-source");
    const draft = await screen.findByRole("textbox", { name: "0-source draft JSON" });
    expect(screen.getByRole("button", { name: /确认 0-source/ })).toBeDisabled();
    fireEvent.change(draft, { target: { value: '{"fixed":true}' } });
    await user.click(screen.getByRole("button", { name: /保存草稿/ }));

    await waitFor(() => expect(saveDraft).toHaveBeenCalled());
    expect(saveDraft.mock.calls[0][2]).not.toHaveProperty("gate_result");
    expect(screen.getByRole("button", { name: "确认 0-source" })).toBeEnabled();
  });

  test("非法 stageKey 显示错误而不回退到当前阶段", async () => {
    vi.spyOn(api.dynamicRuns, "getWithStages").mockResolvedValue(makeWorkspace());

    renderAt("/free-create/project-1/pipeline/run-1/stage/not-a-stage");

    expect(await screen.findByText("动态漫阶段参数无效")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "0-source draft JSON" })).not.toBeInTheDocument();
  });

  test("run.project_id 与路由 projectId 不一致时拒绝打开工作区", async () => {
    vi.spyOn(api.dynamicRuns, "getWithStages").mockResolvedValue(
      makeWorkspace({ projectId: "project-2" }),
    );

    renderAt("/free-create/project-1/pipeline/run-1");

    expect(await screen.findByText("当前运行不属于该项目，无法打开工作区。")).toBeInTheDocument();
  });

  test("进行中的旧刷新响应不能覆盖动作返回的新 revision", async () => {
    const user = userEvent.setup();
    const initial = makeWorkspace();
    const saved = makeWorkspace({ revision: 8 });
    const pendingRefresh = deferred<DynamicWorkflowAction>();
    const getWithStages = vi
      .spyOn(api.dynamicRuns, "getWithStages")
      .mockResolvedValueOnce(initial)
      .mockReturnValueOnce(pendingRefresh.promise);
    const saveDraft = vi.spyOn(api.dynamicRuns, "saveDraft").mockResolvedValue(saved);

    renderAt("/free-create/project-1/pipeline/run-1/stage/0-source");
    const draft = await screen.findByRole("textbox", { name: "0-source draft JSON" });
    await user.click(screen.getByRole("button", { name: "刷新" }));
    expect(screen.getByRole("button", { name: "刷新" })).toBeDisabled();
    fireEvent.change(draft, { target: { value: '{"new":true}' } });
    await user.click(screen.getByRole("button", { name: /保存草稿/ }));

    await waitFor(() => expect(saveDraft).toHaveBeenCalled());
    expect(screen.getByText("修订 R8")).toBeInTheDocument();
    await act(async () => {
      pendingRefresh.resolve(initial);
      await Promise.resolve();
    });
    expect(getWithStages).toHaveBeenCalledTimes(2);
    expect(screen.getByText("修订 R8")).toBeInTheDocument();
  });

  test("运行已取消时禁止编辑和确认", async () => {
    const cancelled = makeWorkspace({ runStatus: "cancelled", selectedStatus: "cancelled" });
    vi.spyOn(api.dynamicRuns, "getWithStages").mockResolvedValue(cancelled);
    const saveDraft = vi.spyOn(api.dynamicRuns, "saveDraft");

    renderAt("/free-create/project-1/pipeline/run-1/stage/0-source");

    const draft = await screen.findByRole("textbox", { name: "0-source draft JSON" });
    expect(draft).toBeDisabled();
    expect(screen.getByRole("button", { name: /保存草稿/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /确认 0-source/ })).toBeDisabled();
    expect(saveDraft).not.toHaveBeenCalled();
  });

  test("交付阻塞时禁用严格导出，勾选二次确认后提供部分导出", async () => {
    const user = userEvent.setup();
    const workspace = makeWorkspace({ currentStage: "6-delivery", selectedStatus: "ready", delivery: true });
    const checklist: DynamicChecklistResponse = {
      checklist: [
        {
          key: "videos",
          label: "分镜视频",
          status: "missing",
          completed: 0,
          total: 1,
          reason: "分镜视频尚未完成",
        },
      ],
      preflight: {
        workflow_run_id: "run-1",
        project_id: "project-1",
        chapter_id: "chapter-1",
        strict: true,
        allow_partial: false,
        status: "blocked",
        items: [],
        missing: [
          {
            kind: "video",
            id: "video-1",
            status: "missing",
            reason: "视频缺失",
            required: true,
          },
        ],
        failed: [],
        skipped: [],
        estimated_jobs: 0,
        input_signature: "signature-1",
        provenance: {},
      },
    };
    vi.spyOn(api.dynamicRuns, "getWithStages").mockResolvedValue(workspace);
    vi.spyOn(api.dynamicRuns, "checklist").mockResolvedValue(checklist);
    const exportZip = vi.spyOn(api.dynamicRuns, "exportZip").mockResolvedValue();
    vi.stubGlobal("confirm", vi.fn().mockReturnValue(true));

    renderAt("/free-create/project-1/pipeline/run-1/stage/6-delivery");
    expect(await screen.findByText("严格导出已阻止。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "严格导出 ZIP（当前被阻止）" })).toBeDisabled();

    await user.click(screen.getByRole("checkbox", { name: "允许部分导出" }));
    const partialButton = screen.getByRole("button", { name: "部分导出 ZIP" });
    expect(partialButton).toBeEnabled();
    await user.click(partialButton);

    await waitFor(() => expect(exportZip).toHaveBeenCalledWith("run-1", {
      strict: false,
      allow_partial: true,
    }));
  });

  test("保留 CustomShotsPage 的 custom-shots 路由注册", async () => {
    renderAt("/custom-shots");
    expect(await screen.findByText("custom-shots-route")).toBeInTheDocument();
  });
});
