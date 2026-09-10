import {
  BookOpenText,
  Boxes,
  Film,
  FolderCog,
  Menu,
  Mic,
  Moon,
  PenLine,
  Plus,
  Settings2,
  Sparkles,
  Sun,
  Video,
  X,
} from "lucide-react";
import { useEffect, useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router";
import { useChapter } from "../hooks/use-chapter";
import { isJobActive, useJobPolling } from "../hooks/use-job-polling";
import { useProject } from "../hooks/use-project";
import { useTheme } from "../hooks/use-theme";
import { cn, formatError } from "../lib/utils";
import { TaskLoadingBar } from "./task-loading-bar";
import { Select } from "./ui";

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
];

export function AppShell() {
  const navigate = useNavigate();
  const { dark, toggle } = useTheme();
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  // 桌面/移动导航只渲染其中一套：媒体查询类在 jsdom 中不生效，
  // 若两套同时挂在 DOM 会导致无障碍树出现重复链接（测试与读屏都会出错）。
  const [isDesktop, setIsDesktop] = useState<boolean>(() => {
    if (typeof window === "undefined") return true;
    if (typeof window.matchMedia !== "function") return true;
    return window.matchMedia("(min-width: 1024px)").matches;
  });
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const query = window.matchMedia("(min-width: 1024px)");
    const onChange = (event: MediaQueryListEvent) => setIsDesktop(event.matches);
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);
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

  const stepTarget = (item: ProductionStep) => {
    let linkTo = item.to;
    let isDisabled = false;
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
    return { linkTo, isDisabled };
  };

  const navItemClass = ({ isActive }: { isActive: boolean }) =>
    cn(
      "relative shrink-0 rounded-full px-3 py-2 text-[13px] font-medium text-black/55 transition hover:text-black",
      "dark:text-white/50 dark:hover:text-white",
      isActive && "text-black dark:text-white",
    );

  const renderStep = (item: ProductionStep, mobile: boolean) => {
    const { linkTo, isDisabled } = stepTarget(item);
    if (mobile) {
      return (
        <NavLink
          key={item.to}
          to={linkTo}
          aria-label={item.label}
          aria-disabled={isDisabled || undefined}
          tabIndex={isDisabled ? -1 : undefined}
          onClick={(event) => {
            if (isDisabled) {
              event.preventDefault();
              return;
            }
            setMobileMenuOpen(false);
          }}
          className={({ isActive }) =>
            cn(
              "rounded-2xl px-3 py-3 text-left text-sm font-semibold transition",
              isDisabled
                ? "cursor-not-allowed bg-black/5 text-black/30 dark:bg-white/10 dark:text-white/30"
                : isActive
                  ? "bg-black text-white dark:bg-white dark:text-black"
                  : "bg-black/5 text-ink hover:bg-black/10 dark:bg-white/10 dark:text-white dark:hover:bg-white/15",
            )
          }
        >
          {item.label}
        </NavLink>
      );
    }
    return (
      <NavLink
        key={item.to}
        to={linkTo}
        aria-label={item.label}
        aria-disabled={isDisabled || undefined}
        tabIndex={isDisabled ? -1 : undefined}
        onClick={(event) => {
          if (isDisabled) event.preventDefault();
        }}
        className={({ isActive }) =>
          cn(
            navItemClass({ isActive }),
            isDisabled && "pointer-events-none opacity-40",
          )
        }
      >
        {({ isActive }) => (
          <>
            {item.label}
            {isActive ? (
              <span className="absolute -bottom-2.5 left-1/2 h-[2px] w-5 -translate-x-1/2 rounded-full bg-vermilion" />
            ) : null}
          </>
        )}
      </NavLink>
    );
  };

  return (
    <div className="min-h-screen bg-canvas text-ink">
      <header className="glass sticky top-0 z-50">
        <div className="mx-auto flex h-[68px] max-w-[1440px] items-center gap-3 px-5 lg:gap-4 lg:px-8">
          <button
            type="button"
            className="mr-1 inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-black/5 bg-white/70 text-black lg:hidden dark:border-white/10 dark:bg-white/10 dark:text-white"
            aria-label="打开导航"
            onClick={() => setMobileMenuOpen((value) => !value)}
          >
            {mobileMenuOpen ? <X className="h-5 w-5" /> : <Menu className="h-5 w-5" />}
          </button>

          <NavLink
            to="/project"
            className="min-w-[88px] shrink-0"
            onClick={() => setMobileMenuOpen(false)}
          >
            <span className="text-[21px] font-semibold tracking-[-0.035em]">
              漫制局
            </span>
          </NavLink>

          {isDesktop ? (
            <nav
              className="nav-scroll hidden min-w-0 flex-1 overflow-x-auto lg:flex"
              aria-label="生产步骤导航"
            >
              {/* mx-auto：内容放得下时居中，放不下时左对齐且可横向滚动，
                  避免 justify-center 溢出后左侧不可达（点击被 Logo 遮挡）。 */}
              <div className="mx-auto flex items-center gap-1">
                {productionSteps.map((item) => renderStep(item, false))}
              </div>
            </nav>
          ) : null}

          <div className="ml-auto flex shrink-0 items-center gap-2">
            <div className="hidden items-center gap-2 md:flex">
              <Select
                aria-label="选择项目"
                value={activeProjectId}
                onChange={(event) => selectProject(event.target.value)}
                className="h-10 max-w-[200px] rounded-full border-black/10 bg-white/70 text-[13px] shadow-sm hover:border-black/25 dark:border-white/15 dark:bg-white/10 dark:text-white"
              >
                {!projects.length ? <option value="">尚无项目</option> : null}
                {projects.map((project) => (
                  <option key={project.id} value={project.id}>
                    {project.name}
                  </option>
                ))}
              </Select>
              <button
                type="button"
                className="pill inline-flex h-10 items-center gap-1.5 rounded-full border border-black/10 bg-white px-3 text-[13px] font-medium shadow-sm hover:bg-black hover:text-white dark:border-white/15 dark:bg-white/10 dark:hover:bg-white dark:hover:text-black"
                onClick={() => navigate("/project?create=1")}
              >
                <Plus className="h-4 w-4" />
                新建
              </button>
            </div>

            <NavLink
              to="/jobs"
              aria-label={
                activeJobs
                  ? `任务中心，${activeJobs} 个任务进行中`
                  : "任务中心"
              }
              className="pill relative hidden h-10 items-center gap-2 rounded-full border border-black/10 bg-white px-4 text-[13px] font-medium shadow-sm hover:bg-black hover:text-white md:inline-flex dark:border-white/15 dark:bg-white/10 dark:hover:bg-white dark:hover:text-black"
            >
              任务中心
              {activeJobs ? (
                <span className="badge-pop inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-red-500 px-1.5 text-[11px] font-semibold text-white">
                  {activeJobs}
                </span>
              ) : null}
            </NavLink>

            <NavLink
              to="/providers"
              aria-label="接口配置"
              title="接口配置"
              className={({ isActive }) =>
                cn(
                  "pill hidden h-10 w-10 items-center justify-center rounded-full border border-black/10 bg-white shadow-sm hover:bg-black hover:text-white xl:inline-flex xl:w-auto xl:gap-2 xl:px-3 dark:border-white/15 dark:bg-white/10 dark:hover:bg-white dark:hover:text-black",
                  isActive && "bg-black text-white dark:bg-white dark:text-black",
                )
              }
            >
              <Settings2 className="h-4 w-4" />
              <span className="hidden text-[13px] font-medium xl:inline">
                接口配置
              </span>
            </NavLink>

            <NavLink
              to="/settings"
              aria-label="平台设置"
              title="平台设置"
              className={({ isActive }) =>
                cn(
                  "pill hidden h-10 w-10 items-center justify-center rounded-full border border-black/10 bg-white shadow-sm hover:bg-black hover:text-white xl:inline-flex xl:w-auto xl:gap-2 xl:px-3 dark:border-white/15 dark:bg-white/10 dark:hover:bg-white dark:hover:text-black",
                  isActive && "bg-black text-white dark:bg-white dark:text-black",
                )
              }
            >
              <FolderCog className="h-4 w-4" />
              <span className="hidden text-[13px] font-medium xl:inline">
                平台设置
              </span>
            </NavLink>

            <button
              type="button"
              className="pill inline-flex h-10 w-10 items-center justify-center rounded-full border border-black/10 bg-white shadow-sm hover:bg-black hover:text-white dark:border-white/15 dark:bg-white/10 dark:hover:bg-white dark:hover:text-black"
              aria-label="切换明暗主题"
              onClick={toggle}
            >
              {dark ? <Sun className="h-[18px] w-[18px]" /> : <Moon className="h-[18px] w-[18px]" />}
            </button>
          </div>
        </div>

        {mobileMenuOpen ? (
          <div className="border-t border-black/5 px-5 py-4 lg:hidden dark:border-white/10">
            <div className="grid grid-cols-1 gap-2">
              <div className="flex items-center gap-2">
                <Select
                  aria-label="选择项目"
                  value={activeProjectId}
                  onChange={(event) => selectProject(event.target.value)}
                  className="h-10 flex-1 rounded-full border-black/10 bg-white/70 text-[13px] shadow-sm dark:border-white/15 dark:bg-white/10 dark:text-white"
                >
                  {!projects.length ? <option value="">尚无项目</option> : null}
                  {projects.map((project) => (
                    <option key={project.id} value={project.id}>
                      {project.name}
                    </option>
                  ))}
                </Select>
                <button
                  type="button"
                  className="rounded-full border border-black/10 px-3 py-2.5 text-[13px] font-medium dark:border-white/15"
                  onClick={() => {
                    setMobileMenuOpen(false);
                    navigate("/project?create=1");
                  }}
                >
                  新建
                </button>
              </div>
            </div>
            {activeProject ? (
              <p className="mt-3 truncate text-xs text-muted">
                小说修订 R{activeProject.source_revision}
              </p>
            ) : null}
            {error ? (
              <p className="mt-2 text-xs text-[#e0442e]">{formatError(error)}</p>
            ) : null}
          </div>
        ) : null}

        {/* 移动端常驻：生产步骤横向滚动 + 工具入口，保证窄屏下导航始终可达。 */}
        {!isDesktop ? (
          <div className="lg:hidden">
            <nav
              className="nav-scroll flex items-center gap-1 overflow-x-auto px-4 py-2"
              aria-label="生产步骤导航"
            >
              {productionSteps.map((item) => renderStep(item, true))}
            </nav>
            <div className="grid grid-cols-3 gap-2 px-4 pb-3">
              <NavLink
                to="/jobs"
                className="rounded-2xl border border-black/10 px-3 py-2.5 text-center text-[13px] font-medium dark:border-white/15"
                onClick={() => setMobileMenuOpen(false)}
              >
                任务中心
              </NavLink>
              <NavLink
                to="/providers"
                className="rounded-2xl border border-black/10 px-3 py-2.5 text-center text-[13px] font-medium dark:border-white/15"
                onClick={() => setMobileMenuOpen(false)}
              >
                接口配置
              </NavLink>
              <NavLink
                to="/settings"
                className="rounded-2xl border border-black/10 px-3 py-2.5 text-center text-[13px] font-medium dark:border-white/15"
                onClick={() => setMobileMenuOpen(false)}
              >
                平台设置
              </NavLink>
            </div>
          </div>
        ) : null}
      </header>

      <main className="min-w-0">
        <div className="mx-auto w-full max-w-[1440px] px-5 py-6 lg:px-8 lg:py-8">
          <TaskLoadingBar jobs={jobsQuery.data || []} />
          <Outlet />
        </div>
      </main>
    </div>
  );
}

