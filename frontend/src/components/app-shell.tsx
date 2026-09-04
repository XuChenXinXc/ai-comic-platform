import {
  BookOpenText,
  Boxes,
  Film,
  FolderCog,
  ListChecks,
  Mic,
  PenLine,
  Settings2,
  Sparkles,
  Video,
  Workflow,
} from "lucide-react";
import { NavLink, Outlet, useNavigate } from "react-router";
import { formatError } from "../lib/utils";
import { useJobPolling, isJobActive } from "../hooks/use-job-polling";
import { useProject } from "../hooks/use-project";
import { useChapter } from "../hooks/use-chapter";
import { Badge, Select } from "./ui";
import { TaskLoadingBar } from "./task-loading-bar";

interface ProductionStep {
  to: string;
  label: string;
  short: string;
  icon: React.ComponentType<{ className?: string }>;
  requiresChapter?: boolean;
  requiresProject?: boolean;
}

const productionSteps: ProductionStep[] = [
  { to: "/project", label: "项目小说", short: "项目", icon: BookOpenText },
  { to: "/writing", label: "剧本生成", short: "剧本", icon: Sparkles },
  { to: "/assets", label: "素材库", short: "素材库", icon: Boxes },
  { to: "/shots", label: "分镜视频", short: "分镜", icon: Film },
  { to: "/audio", label: "配音工作台", short: "配音", icon: Mic, requiresChapter: true },
  { to: "/composition", label: "视频合成", short: "合成", icon: Video, requiresProject: true },
  { to: "/custom-shots", label: "自由制作", short: "自由", icon: PenLine },
  { to: "/free-create", label: "动态漫工作区", short: "动态漫", icon: Workflow },
];

export function AppShell() {
  const navigate = useNavigate();
  const {
    projects,
    activeProjectId,
    activeProject,
    selectProject,
    error,
  } = useProject();
  const { activeChapterId } = useChapter();
  const jobsQuery = useJobPolling(activeProjectId);
  const activeJobs = (jobsQuery.data || []).filter((job) =>
    isJobActive(job.status),
  ).length;

  return (
    <div className="min-h-screen bg-canvas text-ink lg:grid lg:grid-cols-[278px_minmax(0,1fr)]">
      <aside className="relative z-20 bg-sidebar text-white lg:sticky lg:top-0 lg:h-screen lg:overflow-y-auto">
        <div className="flex items-center justify-between px-5 py-5 lg:block lg:px-6 lg:pb-4 lg:pt-7">
          <div>
            <p className="text-[10px] font-bold uppercase tracking-[0.28em] text-white/45">
              Local production desk
            </p>
            <div className="mt-1.5 flex items-baseline gap-2">
              <span className="text-2xl font-extrabold tracking-[-0.05em]">
                漫制局
              </span>
              <span className="text-xs text-white/55">V1</span>
            </div>
          </div>
          <div className="flex gap-2 lg:hidden">
            <NavLink
              to="/settings"
              className="rounded-md border border-white/10 p-2.5 text-white/70 hover:bg-white/10 hover:text-white"
              aria-label="平台设置"
            >
              <FolderCog className="h-5 w-5" />
            </NavLink>
            <NavLink
              to="/providers"
              className="rounded-md border border-white/10 p-2.5 text-white/70 hover:bg-white/10 hover:text-white"
              aria-label="接口配置"
            >
              <Settings2 className="h-5 w-5" />
            </NavLink>
            <NavLink
              to="/jobs"
              className="relative rounded-md border border-white/10 p-2.5 text-white/70 hover:bg-white/10 hover:text-white"
              aria-label="任务中心"
            >
              <ListChecks className="h-5 w-5" />
              {activeJobs ? (
                <span className="absolute -right-1 -top-1 flex h-5 min-w-5 items-center justify-center rounded-full bg-vermilion px-1 text-[10px] font-bold text-white">
                  {activeJobs}
                </span>
              ) : null}
            </NavLink>
          </div>
        </div>

        <div className="border-y border-white/10 px-5 py-4 lg:mx-4 lg:rounded-md lg:border">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-[10px] font-bold uppercase tracking-[0.15em] text-white/45">
              当前项目
            </span>
            <button
              type="button"
              className="text-xs font-semibold text-[#ff9c78] hover:text-white"
              onClick={() => navigate("/project?create=1")}
            >
              新建
            </button>
          </div>
          <Select
            aria-label="选择项目"
            value={activeProjectId}
            onChange={(event) => selectProject(event.target.value)}
            className="border-white/10 bg-white/10 text-white focus:border-[#ff9c78]"
          >
            {!projects.length ? <option value="">尚无项目</option> : null}
            {projects.map((project) => (
              <option key={project.id} value={project.id} className="text-ink">
                {project.name}
              </option>
            ))}
          </Select>
          {activeProject ? (
            <p className="mt-2 truncate text-xs text-white/45">
              小说修订 R{activeProject.source_revision}
            </p>
          ) : null}
          {error ? (
            <p className="mt-2 text-xs text-[#ffb29b]">{formatError(error)}</p>
          ) : null}
        </div>

        <nav
          className="flex gap-2 overflow-x-auto px-4 py-3 lg:block lg:space-y-1 lg:px-4 lg:py-5"
          aria-label="四步生产轨道"
        >
          {productionSteps.map((item, index) => {
            const Icon = item.icon;
            let linkTo = item.to;
            let isDisabled = false;

            // 为需要参数的路由动态生成 URL
            if (item.requiresChapter && activeProjectId && activeChapterId) {
              linkTo = `${item.to}/${activeProjectId}/${activeChapterId}`;
            } else if (item.requiresChapter && !activeChapterId) {
              isDisabled = true;
            }

            if (item.requiresProject && activeProjectId) {
              linkTo = `${item.to}/${activeProjectId}`;
            } else if (item.requiresProject && !activeProjectId) {
              isDisabled = true;
            }

            return (
              <NavLink
                key={item.to}
                to={linkTo}
                aria-label={item.label}
                aria-disabled={isDisabled || undefined}
                tabIndex={isDisabled ? -1 : undefined}
                className={({ isActive }) =>
                  [
                    "group flex min-w-max items-center gap-3 rounded-md border px-3 py-2.5 text-sm font-semibold transition lg:w-full",
                    isDisabled
                      ? "cursor-not-allowed border-white/5 bg-white/[0.02] text-white/25 opacity-50"
                      : isActive
                        ? "border-[#e87c59]/40 bg-vermilion text-white"
                        : "border-white/10 text-white/62 hover:border-white/20 hover:bg-white/[0.07] hover:text-white",
                  ].join(" ")
                }
                onClick={(e) => {
                  if (isDisabled) {
                    e.preventDefault();
                  }
                }}
              >
                <span className="flex h-6 w-6 items-center justify-center rounded border border-current/20 text-[10px]">
                  {index + 1}
                </span>
                <Icon className="h-4 w-4 lg:hidden" />
                <span className="hidden lg:inline">{item.label}</span>
                <span className="lg:hidden">{item.short}</span>
              </NavLink>
            );
          })}
        </nav>

        <div className="hidden border-t border-white/10 px-4 py-4 lg:block">
          <NavLink
            to="/settings"
            className={({ isActive }) =>
              `flex items-center gap-3 rounded-md px-3 py-2.5 text-sm font-semibold ${
                isActive
                  ? "bg-white/12 text-white"
                  : "text-white/58 hover:bg-white/[0.07] hover:text-white"
              }`
            }
          >
            <FolderCog className="h-4 w-4" />
            平台设置
          </NavLink>
          <NavLink
            to="/providers"
            className={({ isActive }) =>
              `mt-1 flex items-center gap-3 rounded-md px-3 py-2.5 text-sm font-semibold ${
                isActive
                  ? "bg-white/12 text-white"
                  : "text-white/58 hover:bg-white/[0.07] hover:text-white"
              }`
            }
          >
            <Settings2 className="h-4 w-4" />
            接口配置
          </NavLink>
          <NavLink
            to="/jobs"
            className={({ isActive }) =>
              `mt-1 flex items-center gap-3 rounded-md px-3 py-2.5 text-sm font-semibold ${
                isActive
                  ? "bg-white/12 text-white"
                  : "text-white/58 hover:bg-white/[0.07] hover:text-white"
              }`
            }
          >
            <ListChecks className="h-4 w-4" />
            任务中心
            {activeJobs ? (
              <Badge className="ml-auto border-[#ff9c78]/40 bg-vermilion/20 text-[#ffb39a]">
                {activeJobs}
              </Badge>
            ) : null}
          </NavLink>
        </div>
      </aside>

      <main className="min-w-0">
        <div className="mx-auto w-full max-w-[1680px] px-4 py-6 sm:px-6 lg:px-9 lg:py-9 xl:px-12">
          <TaskLoadingBar jobs={jobsQuery.data || []} />
          <Outlet />
        </div>
      </main>
    </div>
  );
}
