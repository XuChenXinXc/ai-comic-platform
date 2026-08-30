import { useQuery } from "@tanstack/react-query";
import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { api, queryKeys } from "../api/client";
import type { Project } from "../types/api";

const STORAGE_KEY = "ai-comic-platform:selected-project";

interface ProjectContextValue {
  projects: Project[];
  activeProject: Project | null;
  activeProjectId: string;
  selectProject: (id: string) => void;
  isLoading: boolean;
  error: unknown;
  refetch: () => void;
}

const ProjectContext = createContext<ProjectContextValue | null>(null);

export function ProjectProvider({ children }: { children: ReactNode }) {
  const [activeProjectId, setActiveProjectId] = useState(
    () => window.localStorage.getItem(STORAGE_KEY) || "",
  );
  const projectsQuery = useQuery({
    queryKey: queryKeys.projects,
    queryFn: () => api.projects.list({ kind: "regular" }),
  });
  const projects = useMemo(
    () => projectsQuery.data || [],
    [projectsQuery.data],
  );
  const refetchProjects = projectsQuery.refetch;

  useEffect(() => {
    if (projectsQuery.isLoading) return;
    if (!projects.length) {
      if (activeProjectId) setActiveProjectId("");
      return;
    }
    if (!projects.some((item) => item.id === activeProjectId)) {
      setActiveProjectId(projects[0].id);
    }
  }, [activeProjectId, projects, projectsQuery.isLoading]);

  useEffect(() => {
    if (activeProjectId) {
      window.localStorage.setItem(STORAGE_KEY, activeProjectId);
    } else {
      window.localStorage.removeItem(STORAGE_KEY);
    }
  }, [activeProjectId]);

  const value = useMemo<ProjectContextValue>(
    () => ({
      projects,
      activeProject:
        projects.find((item) => item.id === activeProjectId) || null,
      activeProjectId,
      selectProject: setActiveProjectId,
      isLoading: projectsQuery.isLoading,
      error: projectsQuery.error,
      refetch: () => {
        void refetchProjects();
      },
    }),
    [
      activeProjectId,
      projects,
      projectsQuery.error,
      projectsQuery.isLoading,
      refetchProjects,
    ],
  );

  return (
    <ProjectContext.Provider value={value}>{children}</ProjectContext.Provider>
  );
}

export function useProject() {
  const value = useContext(ProjectContext);
  if (!value) throw new Error("useProject 必须在 ProjectProvider 内使用");
  return value;
}
