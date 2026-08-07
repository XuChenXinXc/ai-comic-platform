import { useQuery } from "@tanstack/react-query";
import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { api } from "../api/client";
import type { Chapter } from "../types/api";
import { useProject } from "./use-project";

const STORAGE_KEY = "ai-comic-platform:selected-chapter";

interface ChapterContextValue {
  chapters: Chapter[];
  activeChapter: Chapter | null;
  activeChapterId: string;
  selectChapter: (id: string) => void;
  isLoading: boolean;
  error: unknown;
  refetch: () => void;
}

const ChapterContext = createContext<ChapterContextValue | null>(null);

/** 按项目分别记住上次选中的章节，切项目时不会串到别的书。 */
function readStored(projectId: string) {
  if (!projectId) return "";
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    const map = raw ? (JSON.parse(raw) as Record<string, string>) : {};
    return map[projectId] || "";
  } catch {
    return "";
  }
}

function writeStored(projectId: string, chapterId: string) {
  if (!projectId) return;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    const map = raw ? (JSON.parse(raw) as Record<string, string>) : {};
    if (chapterId) {
      map[projectId] = chapterId;
    } else {
      delete map[projectId];
    }
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(map));
  } catch {
    // localStorage 不可用时忽略：章节选择退化为仅当前会话有效
  }
}

export function ChapterProvider({ children }: { children: ReactNode }) {
  const { activeProjectId } = useProject();
  const [activeChapterId, setActiveChapterId] = useState("");

  const chaptersQuery = useQuery({
    queryKey: ["chapters", activeProjectId],
    queryFn: () => api.chapters.list(activeProjectId),
    enabled: Boolean(activeProjectId),
  });
  const chapters = useMemo(
    () => chaptersQuery.data || [],
    [chaptersQuery.data],
  );
  const refetchChapters = chaptersQuery.refetch;

  // 切项目时恢复该项目上次选中的章节
  useEffect(() => {
    setActiveChapterId(readStored(activeProjectId));
  }, [activeProjectId]);

  // 章节列表变化后校正选中项：无效则回落到第一章
  useEffect(() => {
    if (!chapters.length) {
      if (activeChapterId) setActiveChapterId("");
      return;
    }
    if (!chapters.some((item) => item.id === activeChapterId)) {
      setActiveChapterId(chapters[0].id);
    }
  }, [activeChapterId, chapters]);

  useEffect(() => {
    writeStored(activeProjectId, activeChapterId);
  }, [activeProjectId, activeChapterId]);

  const value = useMemo<ChapterContextValue>(
    () => ({
      chapters,
      activeChapter:
        chapters.find((item) => item.id === activeChapterId) || null,
      activeChapterId,
      selectChapter: setActiveChapterId,
      isLoading: chaptersQuery.isLoading,
      error: chaptersQuery.error,
      refetch: () => {
        void refetchChapters();
      },
    }),
    [
      activeChapterId,
      chapters,
      chaptersQuery.error,
      chaptersQuery.isLoading,
      refetchChapters,
    ],
  );

  return (
    <ChapterContext.Provider value={value}>{children}</ChapterContext.Provider>
  );
}

export function useChapter() {
  const value = useContext(ChapterContext);
  if (!value) throw new Error("useChapter 必须在 ChapterProvider 内使用");
  return value;
}
