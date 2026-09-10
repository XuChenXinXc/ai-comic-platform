import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router";
import { beforeEach, expect, test, vi } from "vitest";
import { api } from "../api/client";
import type { Project } from "../types/api";
import { ProjectPage } from "./ProjectPage";

const initialProject = vi.hoisted(() => ({
  id: "project-1",
  name: "固定生产规格项目",
  storage_path: "D:\\漫画项目\\project-1",
  source_text: "小说原文",
  source_signature: "source-1",
  source_revision: 1,
  style_prompt: "二维国风漫剧",
  aspect_ratio: "16:9",
  image_size: "3840x2160",
  video_size: "16:9",
  default_duration_sec: 6,
  default_text_provider_id: null,
  default_image_provider_id: null,
  default_video_provider_id: null,
  is_free_create: false,
  created_at: "2026-07-29T00:00:00Z",
  updated_at: "2026-07-29T00:00:00Z",
}));

const projectContext = vi.hoisted(() => ({
  activeProject: { ...initialProject } as Project | null,
  activeProjectId: "project-1",
  selectProject: vi.fn(),
  projects: [] as Project[],
  isLoading: false,
  error: null,
}));

vi.mock("sonner", () => ({
  toast: { error: vi.fn(), success: vi.fn(), warning: vi.fn() },
}));

vi.mock("../hooks/use-project", () => ({
  useProject: () => projectContext,
}));

/** 记录当前 URL 搜索参数，便于断言删除后的跳转行为。 */
function LocationProbe() {
  const { search } = useLocation();
  return <div data-testid="location">{search}</div>;
}

function renderProjectPage(initialEntries: string[] = ["/project"]) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  const view = render(
    <MemoryRouter initialEntries={initialEntries}>
      <QueryClientProvider client={queryClient}>
        <ProjectPage />
        <LocationProbe />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  const rerender = () => {
    view.rerender(
      <MemoryRouter initialEntries={initialEntries}>
        <QueryClientProvider client={queryClient}>
          <ProjectPage />
          <LocationProbe />
        </QueryClientProvider>
      </MemoryRouter>,
    );
  };
  return { rerender };
}

beforeEach(() => {
  vi.restoreAllMocks();
  // 重置共享上下文，避免上个测试修改的表单状态泄漏。
  projectContext.activeProject = { ...initialProject };
  projectContext.activeProjectId = "project-1";
  projectContext.projects = [];
  projectContext.selectProject = vi.fn();
  vi.spyOn(api.providers, "list").mockResolvedValue([]);
});

test("test_project_page_hides_production_baseline_controls", async () => {
  renderProjectPage();

  expect(
    await screen.findByRole("heading", { name: "项目小说" }),
  ).toBeInTheDocument();
  expect(screen.queryByText("生产基准")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("统一画风 STYLE_ANCHOR")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("画面比例")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("默认镜头时长")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("图片尺寸")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("视频尺寸 / 比例")).not.toBeInTheDocument();
});

test("test_project_page_displays_storage_path", async () => {
  renderProjectPage();

  await screen.findByRole("heading", { name: "项目小说" });
  expect(screen.getByLabelText("项目存储路径")).toHaveValue(
    "D:\\漫画项目\\project-1",
  );
});

test("test_project_update_omits_source_text_payload", async () => {
  const user = userEvent.setup();
  const updateSpy = vi
    .spyOn(api.projects, "update")
    .mockResolvedValue({ ...initialProject } as never);

  renderProjectPage();

  const nameInput = await screen.findByLabelText("项目名称");
  await user.clear(nameInput);
  await user.type(nameInput, "新项目名");
  await user.click(screen.getByRole("button", { name: "保存修改" }));

  await waitFor(() => expect(updateSpy).toHaveBeenCalledTimes(1));
  const payload = updateSpy.mock.calls[0][1];
  // 编辑模式不携带 source_text，避免后端重算签名清空存量正文。
  expect(payload).not.toHaveProperty("source_text");
  expect(payload.name).toBe("新项目名");
});

test("test_dirty_project_form_survives_projects_refetch", async () => {
  const user = userEvent.setup();
  const { rerender } = renderProjectPage();

  const nameInput = await screen.findByLabelText("项目名称");
  expect(nameInput).toHaveValue("固定生产规格项目");
  await user.clear(nameInput);
  await user.type(nameInput, "编辑中的项目名");

  // 模拟任务终态后 use-job-polling 失效 projects 查询：activeProject 换成新引用。
  projectContext.activeProject = { ...initialProject };
  rerender();

  // 用户正在编辑的表单不被服务器值静默覆盖。
  expect(nameInput).toHaveValue("编辑中的项目名");
});

test("test_undirty_project_form_follows_server_value_refresh", async () => {
  const { rerender } = renderProjectPage();

  const nameInput = await screen.findByLabelText("项目名称");
  expect(nameInput).toHaveValue("固定生产规格项目");

  // 未编辑时，服务器数据更新（如另一页面改名）会正常回填表单。
  projectContext.activeProject = {
    ...initialProject,
    name: "服务器新名称",
  };
  rerender();

  await waitFor(() => expect(nameInput).toHaveValue("服务器新名称"));
});

test("test_switching_project_rehydrates_form", async () => {
  const user = userEvent.setup();
  const { rerender } = renderProjectPage();

  const nameInput = await screen.findByLabelText("项目名称");
  await user.clear(nameInput);
  await user.type(nameInput, "项目一草稿名");

  // 切换项目：dirty 重置并回填新项目数据。
  projectContext.activeProject = {
    ...initialProject,
    id: "project-2",
    name: "第二个项目",
  };
  projectContext.activeProjectId = "project-2";
  rerender();

  await waitFor(() => expect(nameInput).toHaveValue("第二个项目"));
});

test("test_create_form_survives_project_list_refetch", async () => {
  // 回归：创建模式下 activeProject 引用变化（后台项目列表刷新）会清空正在
  // 输入的项目名，导致「创建并保存」按钮回退为禁用。
  const user = userEvent.setup();
  projectContext.activeProject = { ...initialProject };
  projectContext.activeProjectId = "project-1";
  const { rerender } = renderProjectPage(["/project?create=1"]);

  const nameInput = await screen.findByLabelText("项目名称");
  await user.type(nameInput, "新建项目名");

  // 模拟 projects 查询刷新：activeProject 换成新引用。
  projectContext.activeProject = { ...initialProject };
  rerender();

  expect(nameInput).toHaveValue("新建项目名");
  expect(
    screen.getByRole("button", { name: "创建并保存" }),
  ).toBeEnabled();
});

test("test_delete_project_selects_next_remaining_project", async () => {
  // 回归：删除当前项目后应自动显示剩余项目列表中的下一个项目，而不是跳到新建模式。
  const user = userEvent.setup();
  vi.spyOn(window, "confirm").mockReturnValue(true);
  const removeSpy = vi
    .spyOn(api.projects, "remove")
    .mockResolvedValue(undefined);
  const second = { ...initialProject, id: "project-2", name: "第二个项目" };
  projectContext.projects = [{ ...initialProject }, second];
  projectContext.selectProject = vi.fn((id: string) => {
    projectContext.activeProjectId = id;
    const found = projectContext.projects.find((item) => item.id === id);
    if (found) {
      projectContext.activeProject = found;
    }
  });

  const { rerender } = renderProjectPage();

  await user.click(
    await screen.findByRole("button", { name: "删除当前项目" }),
  );

  await waitFor(() => expect(removeSpy).toHaveBeenCalledTimes(1));
  // 删除成功后自动选中下一个项目，并清除 create 参数。
  await waitFor(() =>
    expect(screen.getByTestId("location").textContent).toBe(""),
  );
  expect(projectContext.selectProject).toHaveBeenCalledWith("project-2");

  rerender();
  await waitFor(() =>
    expect(screen.getByLabelText("项目名称")).toHaveValue("第二个项目"),
  );
});

test("test_delete_last_project_enters_create_mode", async () => {
  // 删除最后一个项目后没有剩余项目，才进入新建模式。
  const user = userEvent.setup();
  vi.spyOn(window, "confirm").mockReturnValue(true);
  const removeSpy = vi
    .spyOn(api.projects, "remove")
    .mockResolvedValue(undefined);
  projectContext.projects = [{ ...initialProject }];
  projectContext.activeProject = { ...initialProject };
  projectContext.activeProjectId = "project-1";

  renderProjectPage();

  await user.click(
    await screen.findByRole("button", { name: "删除当前项目" }),
  );

  await waitFor(() => expect(removeSpy).toHaveBeenCalledTimes(1));
  await waitFor(() =>
    expect(screen.getByTestId("location").textContent).toBe("?create=1"),
  );
  expect(projectContext.selectProject).not.toHaveBeenCalled();
  expect(
    screen.getByRole("heading", { name: "创建漫剧项目" }),
  ).toBeInTheDocument();
});
