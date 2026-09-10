import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle,
  ChevronDown,
  ChevronUp,
  Mic,
  Plus,
  RefreshCw,
  Save,
  Trash2,
  WandSparkles,
} from "lucide-react";
import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useNavigate } from "react-router";
import { toast } from "sonner";
import { ApiError, api, queryKeys } from "../api/client";
import { JsonEditor } from "../components/json-editor";
import { ProjectRequired } from "../components/project-required";
import {
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
  StaleNotice,
  StatusBadge,
} from "../components/status";
import {
  Badge,
  Button,
  Card,
  Input,
  Label,
  Select,
  Tabs,
  Textarea,
} from "../components/ui";
import { useChapter } from "../hooks/use-chapter";
import { useProject } from "../hooks/use-project";
import { formatError, parseJson } from "../lib/utils";
import type {
  ContentRecord,
  ScreenplayContent,
  ScreenplayScene,
  VisualBible,
} from "../types/api";
import { ChapterRequired } from "../components/chapter-required";
import { ChapterSelector } from "../components/chapter-selector";
async function nullable<T>(promise: Promise<T>) {
  try {
    return await promise;
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
}

function recordContent<T>(record?: ContentRecord<T> | null, fallback?: T): T {
  if (!record) return fallback as T;
  return parseJson(record.content ?? record.content_json, fallback as T);
}

const emptyScreenplay: ScreenplayContent = {
  title: "",
  logline: "",
  scenes: [],
};

type WritingTab = "analysis" | "screenplay";

function recordVersion(record?: ContentRecord<unknown> | null) {
  if (!record) return "missing";
  return [
    record.project_id,
    record.revision,
    record.updated_at,
    record.status,
  ].join(":");
}

function screenplayContent(
  record?: ContentRecord<ScreenplayContent> | null,
): ScreenplayContent {
  const next = recordContent(record, emptyScreenplay);
  return {
    ...emptyScreenplay,
    ...next,
    scenes: Array.isArray(next?.scenes) ? next.scenes : [],
  };
}

// 对白行按首个冒号切分说话人：仅当冒号前文本短（≤12 字符）且不含句末标点
// （。！？，;）时才视为 speaker，否则整句作台词、speaker 为空。
const SPEAKER_TERMINAL_PUNCTUATION = /[。！？，;]/;

function parseDialogueLine(line: string): { speaker: string; text: string } {
  const colonIndex = line.search(/[：:]/);
  if (colonIndex > 0) {
    const candidate = line.slice(0, colonIndex).trim();
    const text = line.slice(colonIndex + 1).trim();
    if (
      candidate &&
      candidate.length <= 12 &&
      !SPEAKER_TERMINAL_PUNCTUATION.test(candidate)
    ) {
      return { speaker: candidate, text };
    }
  }
  return { speaker: "", text: line.trim() };
}

// —— 视觉圣经 VisualBible 编辑面板 ——

// 三态布尔与 boolean | null | undefined 互转：未设置 / 禁用 / 启用。
function toTriState(value: boolean | null | undefined): string {
  if (value === null || value === undefined) return "";
  return value ? "true" : "false";
}

function fromTriState(value: string): boolean | undefined {
  if (value === "") return undefined;
  return value === "true";
}

/** 删除对象中值为 undefined 的键，仅保留已设置字段。 */
function omitUnset<T extends Record<string, unknown>>(source: T): Partial<T> {
  const result: Partial<T> = {};
  (Object.keys(source) as Array<keyof T>).forEach((key) => {
    if (source[key] !== undefined) result[key] = source[key];
  });
  return result;
}

/** 将编辑草稿压缩为仅含已设置字段的结构体；全空时返回 null（等价自动推导）。 */
function compactVisualBible(draft: VisualBible | null): VisualBible | null {
  if (!draft) return null;
  const pickString = (value: string | null | undefined) =>
    value === null || value === undefined || value === "" ? undefined : value;
  const pickBool = (value: boolean | null | undefined) =>
    value === null || value === undefined ? undefined : value;
  const pickNumber = (value: number | null | undefined) =>
    value === null || value === undefined || Number.isNaN(value)
      ? undefined
      : value;

  const line = omitUnset({
    type: pickString(draft.line?.type),
    thickness: pickString(draft.line?.thickness),
    continuity: pickString(draft.line?.continuity),
  });
  const coloring = omitUnset({
    method: pickString(draft.coloring?.method),
    saturation: pickString(draft.coloring?.saturation),
    color_count: pickString(draft.coloring?.color_count),
  });
  const shading = omitUnset({
    method: pickString(draft.shading?.method),
    gradient: pickString(draft.shading?.gradient),
    realistic_shading: pickBool(draft.shading?.realistic_shading),
  });
  const material = omitUnset({
    method: pickString(draft.material?.method),
    pbr: pickBool(draft.material?.pbr),
    photographic_reflection: pickBool(draft.material?.photographic_reflection),
  });
  const lighting = omitUnset({
    realism: pickString(draft.lighting?.realism),
    volumetric: pickBool(draft.lighting?.volumetric),
    cinematic_glow: pickBool(draft.lighting?.cinematic_glow),
  });
  const depthOfField = omitUnset({
    enabled: pickBool(draft.depth_of_field?.enabled),
  });
  const realism = omitUnset({
    character: pickNumber(draft.realism?.character),
    environment: pickNumber(draft.realism?.environment),
  });

  const bible: VisualBible = {};
  if (Object.keys(line).length) bible.line = line;
  if (Object.keys(coloring).length) bible.coloring = coloring;
  if (Object.keys(shading).length) bible.shading = shading;
  if (Object.keys(material).length) bible.material = material;
  if (Object.keys(lighting).length) bible.lighting = lighting;
  if (Object.keys(depthOfField).length) bible.depth_of_field = depthOfField;
  if (Object.keys(realism).length) bible.realism = realism;
  if (draft.texture !== undefined && draft.texture !== null && draft.texture !== "") {
    bible.texture = draft.texture;
  }
  const forbiddenStyles = (draft.forbidden_styles || []).filter(Boolean);
  if (forbiddenStyles.length) bible.forbidden_styles = forbiddenStyles;
  return Object.keys(bible).length ? bible : null;
}

function VisualBibleSection({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <section>
      <h4 className="mb-3 text-xs font-semibold uppercase tracking-wide text-muted">
        {title}
      </h4>
      <div className="grid gap-4 sm:grid-cols-2">{children}</div>
    </section>
  );
}

function VisualBibleText({
  id,
  label,
  value,
  placeholder,
  onChange,
}: {
  id: string;
  label: string;
  value: string;
  placeholder?: string;
  onChange: (value: string) => void;
}) {
  return (
    <div>
      <Label htmlFor={id}>{label}</Label>
      <Input
        id={id}
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
      />
    </div>
  );
}

function VisualBibleTriState({
  id,
  label,
  value,
  onChange,
}: {
  id: string;
  label: string;
  value: boolean | null | undefined;
  onChange: (value: boolean | undefined) => void;
}) {
  return (
    <div>
      <Label htmlFor={id}>{label}</Label>
      <Select
        id={id}
        value={toTriState(value)}
        onChange={(event) => onChange(fromTriState(event.target.value))}
      >
        <option value="">未设置</option>
        <option value="false">禁用</option>
        <option value="true">启用</option>
      </Select>
    </div>
  );
}

function VisualBibleNumber({
  id,
  label,
  value,
  onChange,
}: {
  id: string;
  label: string;
  value: number | null | undefined;
  onChange: (value: number | undefined) => void;
}) {
  return (
    <div>
      <Label htmlFor={id}>{label}</Label>
      <Input
        id={id}
        type="number"
        min={0}
        max={1}
        step={0.1}
        value={value ?? ""}
        onChange={(event) => {
          const raw = event.target.value;
          onChange(raw === "" ? undefined : Number(raw));
        }}
      />
    </div>
  );
}

/** 自动推导模式下的只读回显：展示最终会编译进提示词的规范原文。 */
function VisualBibleDerivedPreview({ projectId }: { projectId: string }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["projects", projectId, "visual-bible-preview"],
    queryFn: () => api.projects.visualBiblePreview(projectId),
    enabled: Boolean(projectId),
    staleTime: 30_000,
  });
  if (isLoading) {
    return (
      <div className="rounded-xl border border-line px-3 py-2.5 text-xs leading-5 text-muted">
        正在推导当前画风的结构化约束…
      </div>
    );
  }
  if (isError || !data) {
    return (
      <div className="rounded-xl border border-amber-200 bg-amber-50 px-3 py-2.5 text-xs leading-5 text-amber-800 dark:border-amber-500/25 dark:bg-amber-500/10 dark:text-amber-300">
        回显加载失败：无法获取当前画风的结构化推导结果，请刷新页面重试。
      </div>
    );
  }
  return (
    <div className="rounded-xl border border-sky-200 bg-sky-50 px-3 py-2.5 text-xs leading-5 text-sky-800 dark:border-sky-500/25 dark:bg-sky-500/10 dark:text-sky-300">
      <p className="mb-1 font-semibold">
        当前自动推导结果（依据「统一画风」，未保存也生效）
      </p>
      {!data.style_prompt?.trim() ? (
        <p>
          项目尚未保存统一画风：填写画风并点击「保存」后，这里会显示该画风自动推导出的结构化约束。
        </p>
      ) : data.rendered ? (
        <p>
          {data.rendered}
        </p>
      ) : (
        <p>
          当前画风未命中二维/水墨/写实/3D 类关键词，无结构化推导，仅使用画风原文。
        </p>
      )}
    </div>
  );
}

export function WritingPage() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { activeProject, activeProjectId } = useProject();
  const { activeChapterId, activeChapter } = useChapter();
  const [tab, setTab] = useState<WritingTab>("screenplay");
  const [analysis, setAnalysis] = useState<unknown>({});
  const [analysisValid, setAnalysisValid] = useState(true);
  const [screenplay, setScreenplay] =
    useState<ScreenplayContent>(emptyScreenplay);
  const [stylePrompt, setStylePrompt] = useState("");
  const [stylePromptDirty, setStylePromptDirty] = useState(false);
  const [visualBible, setVisualBible] = useState<VisualBible | null>(null);
  const [visualBibleDirty, setVisualBibleDirty] = useState(false);
  const [visualBibleOpen, setVisualBibleOpen] = useState(false);
  const [chapterSource, setChapterSource] = useState("");
  const [chapterSourceDirty, setChapterSourceDirty] = useState(false);
  const [dirty, setDirty] = useState<Record<WritingTab, boolean>>({
    analysis: false,
    screenplay: false,
  });
  const [serverUpdateAvailable, setServerUpdateAvailable] = useState<
    Record<WritingTab, boolean>
  >({
    analysis: false,
    screenplay: false,
  });
  const hydratedVersion = useRef<Record<WritingTab, string>>({
    analysis: "",
    screenplay: "",
  });
  const editRevision = useRef<Record<WritingTab, number>>({
    analysis: 0,
    screenplay: 0,
  });
  const saveInFlight = useRef(false);
  const activeProjectIdRef = useRef(activeProjectId);
  activeProjectIdRef.current = activeProjectId;
  // 记录上一个章节：切换章节时把未保存草稿保存到原章节，避免串写
  const previousChapterIdRef = useRef(activeChapterId);
  // 正文输入实时镜像：切换章节时用于把旧章节未保存正文存回原章节。
  const chapterSourceRef = useRef(chapterSource);
  chapterSourceRef.current = chapterSource;
  // 画风输入 dirty 实时镜像：保存请求在途时判断是否有新输入，避免被响应回填覆盖。
  const stylePromptDirtyRef = useRef(stylePromptDirty);
  stylePromptDirtyRef.current = stylePromptDirty;
  // 视觉圣经 dirty 实时镜像：保存请求在途时判断是否有新编辑，避免被响应回填覆盖。
  const visualBibleDirtyRef = useRef(visualBibleDirty);
  visualBibleDirtyRef.current = visualBibleDirty;
  // 各章节已加载内容的状态（按 chapterId 记忆）：切章自动保存草稿时保持
  // confirmed 不被静默降级为 ready。
  const statusByChapterRef = useRef<Record<string, Record<WritingTab, string>>>({});

  const analysisQuery = useQuery({
    queryKey: queryKeys.analysis(activeChapterId),
    queryFn: () => nullable(api.analysis.get(activeChapterId)),
    enabled: Boolean(activeChapterId && tab === "analysis"),
  });
  const screenplayQuery = useQuery({
    queryKey: queryKeys.screenplay(activeChapterId),
    queryFn: () => nullable(api.screenplay.get(activeChapterId)),
    enabled: Boolean(activeChapterId),
  });

  useLayoutEffect(() => {
    setAnalysis({});
    setScreenplay(emptyScreenplay);
    setStylePrompt("");
    setStylePromptDirty(false);
    setVisualBible(null);
    setVisualBibleDirty(false);
    setVisualBibleOpen(false);
    setDirty({ analysis: false, screenplay: false });
    setServerUpdateAvailable({ analysis: false, screenplay: false });
    setAnalysisValid(true);
    hydratedVersion.current = { analysis: "", screenplay: "" };
    editRevision.current = { analysis: 0, screenplay: 0 };
  }, [activeProjectId]);

  useEffect(() => {
    if (!stylePromptDirty) {
      setStylePrompt(activeProject?.style_prompt || "");
    }
  }, [activeProject?.style_prompt, stylePromptDirty]);

  useEffect(() => {
    if (!visualBibleDirty) {
      setVisualBible(activeProject?.visual_bible ?? null);
    }
  }, [activeProject?.visual_bible, visualBibleDirty]);

  useEffect(() => {
    // 只随章节切换重置正文；保存后服务端 source_text 变化不应覆盖本地内容。
    setChapterSource(activeChapter?.source_text || "");
    setChapterSourceDirty(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeChapter?.id]);

  useEffect(() => {
    if (
      !activeChapterId ||
      (analysisQuery.data?.chapter_id &&
        analysisQuery.data.chapter_id !== activeChapterId)
    ) {
      return;
    }
    const version = recordVersion(analysisQuery.data);
    if (dirty.analysis) {
      if (
        hydratedVersion.current.analysis &&
        hydratedVersion.current.analysis !== version
      ) {
        setServerUpdateAvailable((current) =>
          current.analysis ? current : { ...current, analysis: true },
        );
      }
      return;
    }
    setAnalysis(recordContent(analysisQuery.data, {}));
    hydratedVersion.current.analysis = version;
    if (analysisQuery.data?.chapter_id) {
      const byChapter = statusByChapterRef.current;
      byChapter[analysisQuery.data.chapter_id] = {
        ...byChapter[analysisQuery.data.chapter_id],
        analysis: analysisQuery.data.status || "",
      };
    }
    setServerUpdateAvailable((current) =>
      current.analysis ? { ...current, analysis: false } : current,
    );
  }, [activeChapterId, analysisQuery.data, dirty.analysis]);

  useEffect(() => {
    if (
      !activeChapterId ||
      (screenplayQuery.data?.chapter_id &&
        screenplayQuery.data.chapter_id !== activeChapterId)
    ) {
      return;
    }
    const version = recordVersion(screenplayQuery.data);
    if (dirty.screenplay) {
      if (
        hydratedVersion.current.screenplay &&
        hydratedVersion.current.screenplay !== version
      ) {
        setServerUpdateAvailable((current) =>
          current.screenplay ? current : { ...current, screenplay: true },
        );
      }
      return;
    }
    setScreenplay(screenplayContent(screenplayQuery.data));
    hydratedVersion.current.screenplay = version;
    if (screenplayQuery.data?.chapter_id) {
      const byChapter = statusByChapterRef.current;
      byChapter[screenplayQuery.data.chapter_id] = {
        ...byChapter[screenplayQuery.data.chapter_id],
        screenplay: screenplayQuery.data.status || "",
      };
    }
    setServerUpdateAvailable((current) =>
      current.screenplay ? { ...current, screenplay: false } : current,
    );
  }, [activeChapterId, dirty.screenplay, screenplayQuery.data]);

  const currentRecord =
    tab === "analysis" ? analysisQuery.data : screenplayQuery.data;
  const hasCurrentContent =
    tab === "analysis"
      ? Boolean(
          analysis &&
            typeof analysis === "object" &&
            Object.keys(analysis as Record<string, unknown>).length,
        )
      : Boolean(
          screenplay.scenes.length ||
            screenplay.title?.trim() ||
            screenplay.logline?.trim(),
        );
  const stale = Boolean(
    currentRecord &&
      (currentRecord.is_stale ||
        (currentRecord.source_signature &&
          currentRecord.source_signature !== activeChapter?.source_signature)),
  );

  const stylePromptMutation = useMutation({
    mutationFn: ({
      projectId,
      value,
    }: {
      projectId: string;
      value: string;
    }) => api.projects.update(projectId, { style_prompt: value }),
    onSuccess: async (project, { projectId }) => {
      if (activeProjectIdRef.current === projectId) {
        // 仅在保存期间没有继续输入时才回填，避免覆盖在途编辑。
        if (!stylePromptDirtyRef.current) {
          setStylePrompt(project.style_prompt);
          setStylePromptDirty(false);
        }
      }
      await queryClient.invalidateQueries({ queryKey: queryKeys.projects });
      // 回显块读取已保存画风：显式失效预览查询，避免 30 秒缓存内看到旧推导。
      await queryClient.invalidateQueries({
        queryKey: ["projects", projectId, "visual-bible-preview"],
      });
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const visualBibleMutation = useMutation({
    mutationFn: ({
      projectId,
      value,
    }: {
      projectId: string;
      value: VisualBible | null;
    }) => api.projects.update(projectId, { visual_bible: value }),
    onSuccess: async (project, { projectId }) => {
      if (activeProjectIdRef.current === projectId) {
        // 仅在保存期间没有继续编辑时才回填，避免覆盖在途编辑。
        if (!visualBibleDirtyRef.current) {
          setVisualBible(project.visual_bible ?? null);
          setVisualBibleDirty(false);
        }
      }
      await queryClient.invalidateQueries({ queryKey: queryKeys.projects });
      // 视觉圣经保存/重置后同步刷新自动推导回显。
      await queryClient.invalidateQueries({
        queryKey: ["projects", projectId, "visual-bible-preview"],
      });
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const generateMutation = useMutation({
    mutationFn: ({
      chapterId,
      contentType,
    }: {
      chapterId: string;
      contentType: WritingTab;
    }) =>
      contentType === "analysis"
        ? api.analysis.generate(chapterId)
        : api.screenplay.generate(chapterId),
    onSuccess: async (result, { contentType }) => {
      void result;
      await queryClient.invalidateQueries({
        queryKey: queryKeys.jobs(activeProjectId),
      });
      toast.success(
        contentType === "analysis"
          ? "小说分析已进入任务队列"
          : "剧本生成已进入任务队列",
      );
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const saveMutation = useMutation({
    mutationFn: ({
      chapterId,
      status,
      contentType,
      content,
    }: {
      chapterId: string;
      status: "ready" | "confirmed";
      contentType: WritingTab;
      content: unknown;
      editRevision: number;
    }) =>
      contentType === "analysis"
        ? api.analysis.update(chapterId, {
            content_json: content,
            status,
          })
        : api.screenplay.update(chapterId, {
            content_json: content,
            status,
          }),
    onSuccess: async (
      record,
      { chapterId, status, contentType, editRevision: submittedRevision },
    ) => {
      const queryKey =
        contentType === "analysis"
          ? queryKeys.analysis(chapterId)
          : queryKeys.screenplay(chapterId);
      const isCurrentChapter = activeChapterId === chapterId;
      const hasNewerEdits =
        editRevision.current[contentType] !== submittedRevision;
      const byChapter = statusByChapterRef.current;
      byChapter[chapterId] = {
        ...byChapter[chapterId],
        [contentType]: status,
      };

      if (isCurrentChapter) {
        hydratedVersion.current[contentType] = recordVersion(record);
        if (!hasNewerEdits) {
          if (contentType === "analysis") {
            setAnalysis(recordContent(record, {}));
            setAnalysisValid(true);
          } else {
            setScreenplay(
              screenplayContent(
                record as ContentRecord<ScreenplayContent>,
              ),
            );
          }
          setDirty((current) => ({ ...current, [contentType]: false }));
          setServerUpdateAvailable((current) => ({
            ...current,
            [contentType]: false,
          }));
        }
      }

      queryClient.setQueryData(queryKey, record);
      await queryClient.invalidateQueries({
        queryKey,
      });
      toast.success(status === "confirmed" ? "内容已确认" : "修改已保存");
    },
    onError: (error) => toast.error(formatError(error)),
    onSettled: () => {
      saveInFlight.current = false;
    },
  });

  // 切换章节时：若存在未保存草稿，先保存到原章节，再重置本地草稿状态，
  // 避免把上一章内容串写到新章节。
  useEffect(() => {
    const previous = previousChapterIdRef.current;
    previousChapterIdRef.current = activeChapterId;
    if (!previous || previous === activeChapterId) return;
    // 未保存的「小说原文」存回原章节，避免切章静默丢稿。
    if (chapterSourceDirty && chapterSourceRef.current) {
      void api.chapters
        .update(previous, { source_text: chapterSourceRef.current })
        .then(() =>
          queryClient.invalidateQueries({ queryKey: ["chapters", activeProjectId] }),
        )
        .catch((error) =>
          toast.error(`章节正文保存失败：${formatError(error)}`),
        );
    }
    const pendingTab: WritingTab | null = dirty.analysis
      ? "analysis"
      : dirty.screenplay
        ? "screenplay"
        : null;
    if (pendingTab) {
      // 保持原章节原状态：confirmed 的剧本不被静默降级为 ready。
      const previousStatus =
        statusByChapterRef.current[previous]?.[pendingTab] || "";
      saveMutation.mutate({
        chapterId: previous,
        status: previousStatus === "confirmed" ? "confirmed" : "ready",
        contentType: pendingTab,
        content: pendingTab === "analysis" ? analysis : screenplay,
        editRevision: editRevision.current[pendingTab],
      });
    }
    // 无论是否保存，都把本地草稿清空，防止旧章节内容残留显示在新章节上。
    setAnalysis({});
    setAnalysisValid(true);
    setScreenplay(emptyScreenplay);
    setDirty({ analysis: false, screenplay: false });
    setServerUpdateAvailable({ analysis: false, screenplay: false });
    setChapterSourceDirty(false);
    hydratedVersion.current = { analysis: "", screenplay: "" };
    editRevision.current = { analysis: 0, screenplay: 0 };
    // 该 effect 只在章节切换时运行，刻意不依赖 dirty/analysis/screenplay。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeChapterId]);

  const sceneMutation = useMutation({
    mutationFn: ({
      chapterId,
      sceneNo,
    }: {
      chapterId: string;
      sceneNo: number;
    }) => api.screenplay.regenerateScene(chapterId, sceneNo),
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: queryKeys.jobs(activeProjectId),
      });
      toast.success("单场重生成已进入任务队列");
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const markDirty = (contentType: WritingTab) => {
    editRevision.current[contentType] += 1;
    setDirty((current) =>
      current[contentType] ? current : { ...current, [contentType]: true },
    );
  };

  const persistStylePrompt = async () => {
    if (!stylePromptDirty || stylePromptMutation.isPending) {
      return !stylePromptMutation.isPending;
    }
    try {
      await stylePromptMutation.mutateAsync({
        projectId: activeProjectId,
        value: stylePrompt,
      });
      return true;
    } catch {
      return false;
    }
  };

  const saveCurrent = async (status: "ready" | "confirmed") => {
    if (saveInFlight.current) return;
    saveInFlight.current = true;
    if (!(await persistStylePrompt())) {
      saveInFlight.current = false;
      return;
    }
    saveMutation.mutate({
      chapterId: activeChapterId,
      status,
      contentType: tab,
      content: tab === "analysis" ? analysis : screenplay,
      editRevision: editRevision.current[tab],
    });
  };

  const generateCurrent = async () => {
    if (!(await persistStylePrompt())) return;
    generateMutation.mutate({
      chapterId: activeChapterId,
      contentType: tab,
    });
  };

  const editScreenplay = (
    update: (current: ScreenplayContent) => ScreenplayContent,
  ) => {
    setScreenplay(update);
    markDirty("screenplay");
  };

  const updateScene = (
    index: number,
    patch: Partial<ScreenplayScene>,
  ) => {
    editScreenplay((current) => ({
      ...current,
      scenes: current.scenes.map((scene, sceneIndex) =>
        sceneIndex === index ? { ...scene, ...patch } : scene,
      ),
    }));
  };

  const moveScene = (index: number, direction: -1 | 1) => {
    editScreenplay((current) => {
      const next = [...current.scenes];
      const target = index + direction;
      if (target < 0 || target >= next.length) return current;
      [next[index], next[target]] = [next[target], next[index]];
      return {
        ...current,
        scenes: next.map((scene, sceneIndex) => ({
          ...scene,
          scene_no: sceneIndex + 1,
        })),
      };
    });
  };

  const keepLocalEdits = () => {
    hydratedVersion.current[tab] = recordVersion(currentRecord);
    setServerUpdateAvailable((current) => ({
      ...current,
      [tab]: false,
    }));
  };

  const loadServerVersion = () => {
    if (tab === "analysis") {
      setAnalysis(recordContent(analysisQuery.data, {}));
      setAnalysisValid(true);
    } else {
      setScreenplay(screenplayContent(screenplayQuery.data));
    }
    hydratedVersion.current[tab] = recordVersion(currentRecord);
    setDirty((current) => ({ ...current, [tab]: false }));
    setServerUpdateAvailable((current) => ({
      ...current,
      [tab]: false,
    }));
  };

  const chapterSourceMutation = useMutation({
    mutationFn: () =>
      api.chapters.update(activeChapterId, {
        source_text: chapterSource,
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: ["chapters", activeProjectId],
      });
      setChapterSourceDirty(false);
      toast.success("章节正文已保存");
    },
    onError: (error) => toast.error(formatError(error)),
  });

  // —— 视觉圣经：自动推导 + 可编辑 ——
  const hasVisualBible = Boolean(
    activeProject?.visual_bible &&
      Object.keys(activeProject.visual_bible).length > 0,
  );
  // 自动推导预览：与回显块共用同一查询（React Query 共享缓存）。
  // 表单输入框用它回显「未手动设置的字段」的推导值，保存时不会误存。
  const visualBiblePreviewQuery = useQuery({
    queryKey: ["projects", activeProjectId, "visual-bible-preview"],
    queryFn: () => api.projects.visualBiblePreview(activeProjectId ?? ""),
    enabled: Boolean(activeProjectId),
    staleTime: 30_000,
  });
  const derivedPreview = visualBiblePreviewQuery.data?.derived;

  const editVisualBible = (
    update: (current: VisualBible) => VisualBible,
  ) => {
    setVisualBible((current) => update(current ?? {}));
    setVisualBibleDirty(true);
  };

  const editLine = (patch: Partial<NonNullable<VisualBible["line"]>>) =>
    editVisualBible((current) => ({
      ...current,
      line: { ...current.line, ...patch },
    }));
  const editColoring = (patch: Partial<NonNullable<VisualBible["coloring"]>>) =>
    editVisualBible((current) => ({
      ...current,
      coloring: { ...current.coloring, ...patch },
    }));
  const editShading = (patch: Partial<NonNullable<VisualBible["shading"]>>) =>
    editVisualBible((current) => ({
      ...current,
      shading: { ...current.shading, ...patch },
    }));
  const editMaterial = (patch: Partial<NonNullable<VisualBible["material"]>>) =>
    editVisualBible((current) => ({
      ...current,
      material: { ...current.material, ...patch },
    }));
  const editLighting = (patch: Partial<NonNullable<VisualBible["lighting"]>>) =>
    editVisualBible((current) => ({
      ...current,
      lighting: { ...current.lighting, ...patch },
    }));
  const editDepthOfField = (
    patch: Partial<NonNullable<VisualBible["depth_of_field"]>>,
  ) =>
    editVisualBible((current) => ({
      ...current,
      depth_of_field: { ...current.depth_of_field, ...patch },
    }));
  const editRealism = (patch: Partial<NonNullable<VisualBible["realism"]>>) =>
    editVisualBible((current) => ({
      ...current,
      realism: { ...current.realism, ...patch },
    }));

  const validateVisualBibleRealism = (draft: VisualBible | null) => {
    const invalid = (value: number | null | undefined) =>
      value !== null &&
      value !== undefined &&
      (Number.isNaN(value) || value < 0 || value > 1);
    if (invalid(draft?.realism?.character)) {
      toast.error("写实度·角色 必须是 0 到 1 之间的数值");
      return false;
    }
    if (invalid(draft?.realism?.environment)) {
      toast.error("写实度·环境 必须是 0 到 1 之间的数值");
      return false;
    }
    return true;
  };

  const saveVisualBible = async () => {
    if (visualBibleMutation.isPending) return;
    if (!validateVisualBibleRealism(visualBible)) return;
    const payload = compactVisualBible(visualBible);
    setVisualBibleDirty(false);
    try {
      await visualBibleMutation.mutateAsync({
        projectId: activeProjectId,
        value: payload,
      });
      toast.success(payload ? "视觉圣经已保存" : "已重置为自动推导");
    } catch {
      // 失败提示由 mutation 的 onError 统一处理；保留本地编辑等待重试。
      setVisualBibleDirty(true);
    }
  };

  const resetVisualBible = async () => {
    if (visualBibleMutation.isPending) return;
    setVisualBible(null);
    setVisualBibleDirty(false);
    try {
      await visualBibleMutation.mutateAsync({
        projectId: activeProjectId,
        value: null,
      });
      toast.success("已重置为自动推导");
    } catch {
      // 重置失败时恢复服务端值，避免界面与服务端状态不一致。
      setVisualBible(activeProject?.visual_bible ?? null);
      setVisualBibleDirty(false);
    }
  };

  return (
    <ProjectRequired>
      <ChapterRequired>
      <div>
        <PageHeader
          eyebrow="Step 02 / Writing"
          title="剧本生成"
          description="生成剧本时会在后台自动完成小说分析，再使用本次分析结果生成结构化场次剧本。"
          actions={
            <>
              <ChapterSelector />
              {screenplayQuery.data &&
                (screenplayQuery.data.status === "confirmed" ||
                  screenplayQuery.data.status === "ready") &&
                activeProjectId &&
                activeChapterId &&
                navigate && (
                  <Button
                    variant="secondary"
                    onClick={() =>
                      navigate(`/audio/${activeProjectId}/${activeChapterId}`)
                    }
                  >
                    <Mic className="h-4 w-4" />
                    配音工作台
                  </Button>
                )}
              <Button
                busy={
                  stylePromptMutation.isPending ||
                  (generateMutation.isPending &&
                    generateMutation.variables?.chapterId === activeChapterId &&
                    generateMutation.variables.contentType === tab)
                }
                onClick={() => void generateCurrent()}
              >
                {hasCurrentContent ? (
                  <RefreshCw className="h-4 w-4" />
                ) : (
                  <WandSparkles className="h-4 w-4" />
                )}
                {hasCurrentContent ? "重新生成" : "开始生成"}
              </Button>
              <Button
                busy={
                  stylePromptMutation.isPending ||
                  (saveMutation.isPending &&
                    saveMutation.variables?.chapterId === activeChapterId &&
                    saveMutation.variables?.status === "ready" &&
                    saveMutation.variables.contentType === tab)
                }
                disabled={
                  saveMutation.isPending ||
                  stylePromptMutation.isPending ||
                  (tab === "analysis" && !analysisValid)
                }
                onClick={() => void saveCurrent("ready")}
              >
                <Save className="h-4 w-4" />
                保存
              </Button>
              {tab === "screenplay" && hasCurrentContent ? (
                <Button
                  variant="primary"
                  busy={
                    stylePromptMutation.isPending ||
                    (saveMutation.isPending &&
                      saveMutation.variables?.chapterId === activeChapterId &&
                      saveMutation.variables?.status === "confirmed" &&
                      saveMutation.variables.contentType === tab)
                  }
                  disabled={
                    saveMutation.isPending || stylePromptMutation.isPending
                  }
                  onClick={() => void saveCurrent("confirmed")}
                >
                  <CheckCircle className="h-4 w-4" />
                  确认剧本
                </Button>
              ) : null}
            </>
          }
        />

        <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
          <Tabs
            value={tab}
            onChange={(value) => setTab(value as typeof tab)}
            items={[
              {
                value: "screenplay",
                label: "剧本生成",
                count: screenplay.scenes.length,
              },
            ]}
          />
          {currentRecord ? (
            <div className="flex items-center gap-2">
              <StatusBadge status={currentRecord.status} />
              <span className="text-xs text-muted">
                修订 R{currentRecord.revision}
              </span>
            </div>
          ) : null}
        </div>

        {stale ? (
          <div className="mb-5">
            <StaleNotice reason="小说正文已修改，当前文字结果基于旧来源。请核对后保留，或重新生成。" />
          </div>
        ) : null}

        {serverUpdateAvailable[tab] ? (
          <div
            role="alert"
            className="mb-5 flex flex-col gap-3 rounded-2xl border border-sky-200 bg-sky-50 px-4 py-4 sm:flex-row sm:items-center sm:justify-between dark:border-sky-500/25 dark:bg-sky-500/10"
          >
            <div>
              <h2 className="text-sm font-semibold text-sky-800 dark:text-sky-300">
                服务端已有新版本
              </h2>
              <p className="mt-1 text-xs leading-5 text-sky-700/80 dark:text-sky-300/80">
                后台任务已返回新结果。你的本地编辑仍保留，选择后再决定是否替换。
              </p>
            </div>
            <div className="flex shrink-0 flex-wrap gap-2">
              <Button size="sm" onClick={keepLocalEdits}>
                保留本地编辑
              </Button>
              <Button size="sm" variant="primary" onClick={loadServerVersion}>
                载入服务端版本
              </Button>
            </div>
          </div>
        ) : null}

        <div className="grid min-w-0 gap-6 xl:grid-cols-[minmax(300px,0.72fr)_minmax(0,1.55fr)]">
          <Card className="h-fit overflow-hidden xl:sticky xl:top-9">
            <div className="flex items-center justify-between border-b border-line px-5 py-4">
              <div>
                <h2 className="font-bold">小说原文</h2>
                <p className="mt-1 text-xs text-muted">
                  章节正文 · 修订 R{activeChapter?.source_revision ?? 0}
                </p>
              </div>
              <div className="flex items-center gap-3">
                <span className="text-xs text-muted">
                  {chapterSource.length.toLocaleString("zh-CN")} 字符
                </span>
                <Button
                  size="sm"
                  busy={chapterSourceMutation.isPending}
                  disabled={!chapterSourceDirty}
                  onClick={() => chapterSourceMutation.mutate()}
                >
                  <Save className="h-4 w-4" />
                  保存正文
                </Button>
              </div>
            </div>
            <Textarea
              rows={18}
              className="max-h-[70vh] resize-y rounded-none border-0 border-b border-line bg-transparent px-5 py-5 text-sm leading-7 text-ink dark:bg-transparent"
              value={chapterSource}
              placeholder="粘贴本章小说原文…"
              onChange={(event) => {
                setChapterSource(event.target.value);
                setChapterSourceDirty(true);
              }}
            />
          </Card>

          <div className="min-w-0">
            {tab === "analysis" ? (
              <>
                {analysisQuery.isLoading ? (
                  <LoadingState label="正在读取小说分析…" />
                ) : null}
                {analysisQuery.error ? (
                  <ErrorState
                    message={formatError(analysisQuery.error)}
                    onRetry={() => void analysisQuery.refetch()}
                  />
                ) : null}
                {!analysisQuery.isLoading && !analysisQuery.error ? (
                  <Card className="p-5 sm:p-6">
                    {!analysisQuery.data &&
                    Object.keys((analysis as Record<string, unknown>) || {})
                      .length === 0 ? (
                      <EmptyState
                        compact
                        title="还没有小说分析"
                        description="启动文字任务后，结构化结果会自动回填；任务失败不会覆盖你已保存的内容。"
                        action={
                          <Button
                            variant="primary"
                            busy={
                              stylePromptMutation.isPending ||
                              (generateMutation.isPending &&
                                generateMutation.variables?.chapterId ===
                                  activeChapterId &&
                                generateMutation.variables.contentType === tab)
                            }
                            onClick={() => void generateCurrent()}
                          >
                            生成小说分析
                          </Button>
                        }
                      />
                    ) : (
                      <JsonEditor
                        key={activeProjectId}
                        label="故事概述、人物关系、剧情结构与制作风险"
                        value={analysis}
                        onChange={(value) => {
                          setAnalysis(value);
                          markDirty("analysis");
                        }}
                        onValidityChange={(valid) => {
                          setAnalysisValid(valid);
                          if (!valid) markDirty("analysis");
                        }}
                        rows={28}
                      />
                    )}
                    {analysisQuery.data?.error ? (
                      <p className="mt-4 rounded-xl border border-red-200 bg-red-50 p-3 text-xs leading-5 text-red-700 dark:border-red-500/25 dark:bg-red-500/10 dark:text-red-300">
                        {analysisQuery.data.error}
                      </p>
                    ) : null}
                  </Card>
                ) : null}
              </>
            ) : (
              <>
                {screenplayQuery.isLoading ? (
                  <LoadingState label="正在读取剧本…" />
                ) : null}
                {screenplayQuery.error ? (
                  <ErrorState
                    message={formatError(screenplayQuery.error)}
                    onRetry={() => void screenplayQuery.refetch()}
                  />
                ) : null}
                {!screenplayQuery.isLoading && !screenplayQuery.error ? (
                  <div className="space-y-4">
                    <Card className="space-y-5 p-5 sm:p-6">
                      <div>
                        <Label htmlFor="style-prompt">统一画风 STYLE_ANCHOR</Label>
                        <Textarea
                          id="style-prompt"
                          value={stylePrompt}
                          rows={4}
                          placeholder="描述整个项目唯一且稳定的画面语言…"
                          onChange={(event) => {
                            setStylePrompt(event.target.value);
                            setStylePromptDirty(true);
                          }}
                        />
                        <p className="mt-2 text-xs leading-5 text-muted">
                          该画风会编译进所有素材图、关键帧和视频提示词。
                        </p>
                      </div>
                      <div className="border-t border-line pt-4">
                        <button
                          type="button"
                          aria-expanded={visualBibleOpen}
                          onClick={() => setVisualBibleOpen((open) => !open)}
                          className="flex w-full cursor-pointer items-center justify-between gap-3 rounded-2xl border border-black/[0.06] bg-black/[0.02] px-4 py-3 text-left transition hover:bg-black/[0.04] dark:border-white/10 dark:bg-white/5 dark:hover:bg-white/[0.08]"
                        >
                          <span className="flex items-center gap-2">
                            <span className="text-sm font-bold">
                              视觉圣经 VisualBible
                            </span>
                            {hasVisualBible ? (
                              <Badge className="border-green-200 bg-green-50 text-green-700 dark:border-green-500/25 dark:bg-green-500/10 dark:text-green-300">
                                已自定义
                              </Badge>
                            ) : (
                              <Badge>自动推导</Badge>
                            )}
                          </span>
                          <ChevronDown
                            className={`h-4 w-4 shrink-0 text-muted transition-transform ${
                              visualBibleOpen ? "rotate-180" : ""
                            }`}
                          />
                        </button>
                        {visualBibleOpen ? (
                          <div className="mt-4 space-y-5">
                            <p className="text-xs leading-5 text-muted">
                              结构化画风规范会编译进素材图、关键帧与视频提示词；输入框显示当前画风的自动推导值，手动修改并保存后才会覆盖推导。
                            </p>
                            {!hasVisualBible && activeProjectId ? (
                              <VisualBibleDerivedPreview projectId={activeProjectId} />
                            ) : null}
                            <VisualBibleSection title="线条 line">
                              <VisualBibleText
                                id="vb-line-type"
                                label="类型 type"
                                value={
                                  visualBible?.line?.type ??
                                  derivedPreview?.line?.type ??
                                  ""
                                }
                                placeholder="如：粗笔焦墨"
                                onChange={(value) => editLine({ type: value })}
                              />
                              <VisualBibleText
                                id="vb-line-thickness"
                                label="粗细 thickness"
                                value={
                                  visualBible?.line?.thickness ??
                                  derivedPreview?.line?.thickness ??
                                  ""
                                }
                                onChange={(value) =>
                                  editLine({ thickness: value })
                                }
                              />
                              <VisualBibleText
                                id="vb-line-continuity"
                                label="连贯性 continuity"
                                value={
                                  visualBible?.line?.continuity ??
                                  derivedPreview?.line?.continuity ??
                                  ""
                                }
                                onChange={(value) =>
                                  editLine({ continuity: value })
                                }
                              />
                            </VisualBibleSection>
                            <VisualBibleSection title="上色 coloring">
                              <VisualBibleText
                                id="vb-coloring-method"
                                label="方法 method"
                                value={
                                  visualBible?.coloring?.method ??
                                  derivedPreview?.coloring?.method ??
                                  ""
                                }
                                onChange={(value) =>
                                  editColoring({ method: value })
                                }
                              />
                              <VisualBibleText
                                id="vb-coloring-saturation"
                                label="饱和度 saturation"
                                value={
                                  visualBible?.coloring?.saturation ??
                                  derivedPreview?.coloring?.saturation ??
                                  ""
                                }
                                onChange={(value) =>
                                  editColoring({ saturation: value })
                                }
                              />
                              <VisualBibleText
                                id="vb-coloring-color-count"
                                label="色彩数量 color_count"
                                value={
                                  visualBible?.coloring?.color_count ??
                                  derivedPreview?.coloring?.color_count ??
                                  ""
                                }
                                onChange={(value) =>
                                  editColoring({ color_count: value })
                                }
                              />
                            </VisualBibleSection>
                            <VisualBibleSection title="明暗 shading">
                              <VisualBibleText
                                id="vb-shading-method"
                                label="方法 method"
                                value={
                                  visualBible?.shading?.method ??
                                  derivedPreview?.shading?.method ??
                                  ""
                                }
                                onChange={(value) =>
                                  editShading({ method: value })
                                }
                              />
                              <VisualBibleText
                                id="vb-shading-gradient"
                                label="渐变 gradient"
                                value={
                                  visualBible?.shading?.gradient ??
                                  derivedPreview?.shading?.gradient ??
                                  ""
                                }
                                onChange={(value) =>
                                  editShading({ gradient: value })
                                }
                              />
                              <VisualBibleTriState
                                id="vb-shading-realistic"
                                label="写实明暗 realistic_shading"
                                value={
                                  visualBible?.shading?.realistic_shading ??
                                  derivedPreview?.shading?.realistic_shading ??
                                  null
                                }
                                onChange={(value) =>
                                  editShading({ realistic_shading: value })
                                }
                              />
                            </VisualBibleSection>
                            <VisualBibleSection title="材质 material">
                              <VisualBibleText
                                id="vb-material-method"
                                label="方法 method"
                                value={
                                  visualBible?.material?.method ??
                                  derivedPreview?.material?.method ??
                                  ""
                                }
                                onChange={(value) =>
                                  editMaterial({ method: value })
                                }
                              />
                              <VisualBibleTriState
                                id="vb-material-pbr"
                                label="PBR pbr"
                                value={
                                  visualBible?.material?.pbr ??
                                  derivedPreview?.material?.pbr ??
                                  null
                                }
                                onChange={(value) => editMaterial({ pbr: value })}
                              />
                              <VisualBibleTriState
                                id="vb-material-reflection"
                                label="摄影级反射 photographic_reflection"
                                value={
                                  visualBible?.material
                                    ?.photographic_reflection ??
                                  derivedPreview?.material
                                    ?.photographic_reflection ??
                                  null
                                }
                                onChange={(value) =>
                                  editMaterial({
                                    photographic_reflection: value,
                                  })
                                }
                              />
                            </VisualBibleSection>
                            <VisualBibleSection title="光照 lighting">
                              <VisualBibleText
                                id="vb-lighting-realism"
                                label="写实度 realism"
                                value={
                                  visualBible?.lighting?.realism ??
                                  derivedPreview?.lighting?.realism ??
                                  ""
                                }
                                onChange={(value) =>
                                  editLighting({ realism: value })
                                }
                              />
                              <VisualBibleTriState
                                id="vb-lighting-volumetric"
                                label="体积光 volumetric"
                                value={
                                  visualBible?.lighting?.volumetric ??
                                  derivedPreview?.lighting?.volumetric ??
                                  null
                                }
                                onChange={(value) =>
                                  editLighting({ volumetric: value })
                                }
                              />
                              <VisualBibleTriState
                                id="vb-lighting-glow"
                                label="电影级辉光 cinematic_glow"
                                value={
                                  visualBible?.lighting?.cinematic_glow ??
                                  derivedPreview?.lighting?.cinematic_glow ??
                                  null
                                }
                                onChange={(value) =>
                                  editLighting({ cinematic_glow: value })
                                }
                              />
                            </VisualBibleSection>
                            <VisualBibleSection title="景深 depth_of_field">
                              <VisualBibleTriState
                                id="vb-dof-enabled"
                                label="启用 enabled"
                                value={
                                  visualBible?.depth_of_field?.enabled ??
                                  derivedPreview?.depth_of_field?.enabled ??
                                  null
                                }
                                onChange={(value) =>
                                  editDepthOfField({ enabled: value })
                                }
                              />
                            </VisualBibleSection>
                            <VisualBibleSection title="写实度 realism">
                              <VisualBibleNumber
                                id="vb-realism-character"
                                label="角色 character（0-1）"
                                value={
                                  visualBible?.realism?.character ??
                                  derivedPreview?.realism?.character ??
                                  null
                                }
                                onChange={(value) =>
                                  editRealism({ character: value })
                                }
                              />
                              <VisualBibleNumber
                                id="vb-realism-environment"
                                label="环境 environment（0-1）"
                                value={
                                  visualBible?.realism?.environment ??
                                  derivedPreview?.realism?.environment ??
                                  null
                                }
                                onChange={(value) =>
                                  editRealism({ environment: value })
                                }
                              />
                            </VisualBibleSection>
                            <VisualBibleSection title="质感与禁用风格 texture / forbidden_styles">
                              <VisualBibleText
                                id="vb-texture"
                                label="质感 texture"
                                value={
                                  visualBible?.texture ??
                                  derivedPreview?.texture ??
                                  ""
                                }
                                placeholder="如：宣纸颗粒、绢本质感"
                                onChange={(value) =>
                                  editVisualBible((current) => ({
                                    ...current,
                                    texture: value,
                                  }))
                                }
                              />
                              <div>
                                <Label htmlFor="vb-forbidden-styles">
                                  禁用风格 forbidden_styles（逗号或顿号分隔）
                                </Label>
                                <Input
                                  id="vb-forbidden-styles"
                                  value={(
                                    visualBible?.forbidden_styles ??
                                    derivedPreview?.forbidden_styles ??
                                    []
                                  ).join("、")}
                                  placeholder="如：低多边形、赛博朋克"
                                  onChange={(event) =>
                                    editVisualBible((current) => ({
                                      ...current,
                                      forbidden_styles: event.target.value
                                        .split(/[、,，]/)
                                        .map((item) => item.trim())
                                        .filter(Boolean),
                                    }))
                                  }
                                />
                              </div>
                            </VisualBibleSection>
                            <div className="flex flex-wrap gap-2 border-t border-line pt-4">
                              <Button
                                variant="ghost"
                                busy={visualBibleMutation.isPending}
                                onClick={() => void resetVisualBible()}
                              >
                                重置为自动推导
                              </Button>
                              <Button
                                variant="primary"
                                busy={visualBibleMutation.isPending}
                                onClick={() => void saveVisualBible()}
                              >
                                <Save className="h-4 w-4" />
                                保存视觉圣经
                              </Button>
                            </div>
                          </div>
                        ) : null}
                      </div>
                      <div className="grid gap-4 sm:grid-cols-2">
                        <div>
                          <Label htmlFor="screenplay-title">剧本标题</Label>
                          <Input
                            id="screenplay-title"
                            value={screenplay.title || ""}
                            onChange={(event) =>
                              editScreenplay((current) => ({
                                ...current,
                                title: event.target.value,
                              }))
                            }
                          />
                        </div>
                        <div>
                          <Label htmlFor="screenplay-logline">一句话梗概</Label>
                          <Input
                            id="screenplay-logline"
                            value={screenplay.logline || ""}
                            onChange={(event) =>
                              editScreenplay((current) => ({
                                ...current,
                                logline: event.target.value,
                              }))
                            }
                          />
                        </div>
                      </div>
                    </Card>

                    {!screenplay.scenes.length ? (
                      <EmptyState
                        title="还没有剧本场次"
                        description="可以整体生成剧本，也可以先手工添加一个场次再逐步完善。"
                        action={
                          <div className="flex flex-wrap justify-center gap-2">
                            <Button
                              variant="primary"
                              busy={
                                stylePromptMutation.isPending ||
                                (generateMutation.isPending &&
                                  generateMutation.variables?.chapterId ===
                                    activeChapterId &&
                                  generateMutation.variables.contentType === tab)
                              }
                              onClick={() => void generateCurrent()}
                            >
                              生成完整剧本
                            </Button>
                            <Button
                              onClick={() =>
                                editScreenplay((current) => ({
                                  ...current,
                                  scenes: [
                                    {
                                      scene_no: 1,
                                      scene_name: "新场次",
                                      characters: [],
                                      dialogue: [],
                                      narration: [],
                                    },
                                  ],
                                }))
                              }
                            >
                              手工添加场次
                            </Button>
                          </div>
                        }
                      />
                    ) : null}

                    {screenplay.scenes.map((scene, index) => (
                      <Card key={`${scene.scene_no}-${index}`} className="overflow-hidden">
                        <details open={index === 0}>
                          <summary className="flex cursor-pointer list-none items-center gap-3 px-5 py-4 hover:bg-black/[0.025]">
                            <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-ink text-xs font-bold text-white dark:bg-white dark:text-black">
                              {String(scene.scene_no).padStart(2, "0")}
                            </span>
                            <div className="min-w-0 flex-1">
                              <h3 className="truncate font-bold">
                                {scene.scene_name || "未命名场次"}
                              </h3>
                              <p className="mt-0.5 truncate text-xs text-muted">
                                {(scene.characters || []).join("、") || "尚未填写人物"}
                              </p>
                            </div>
                            <ChevronDown className="h-4 w-4 text-muted" />
                          </summary>
                          <div className="space-y-5 border-t border-line px-5 py-5 sm:px-6">
                            <div className="grid gap-4 sm:grid-cols-2">
                              <div>
                                <Label>场景名称</Label>
                                <Input
                                  value={scene.scene_name}
                                  onChange={(event) =>
                                    updateScene(index, {
                                      scene_name: event.target.value,
                                    })
                                  }
                                />
                              </div>
                              <div>
                                <Label>时间与天气</Label>
                                <Input
                                  value={scene.time_weather || ""}
                                  onChange={(event) =>
                                    updateScene(index, {
                                      time_weather: event.target.value,
                                    })
                                  }
                                />
                              </div>
                              <div className="sm:col-span-2">
                                <Label>出场人物（用顿号或逗号分隔）</Label>
                                <Input
                                  value={(scene.characters || []).join("、")}
                                  onChange={(event) =>
                                    updateScene(index, {
                                      characters: event.target.value
                                        .split(/[、,，]/)
                                        .map((item) => item.trim())
                                        .filter(Boolean),
                                    })
                                  }
                                />
                              </div>
                              <div className="sm:col-span-2">
                                <Label>场次目标</Label>
                                <Textarea
                                  rows={2}
                                  value={scene.objective || ""}
                                  onChange={(event) =>
                                    updateScene(index, {
                                      objective: event.target.value,
                                    })
                                  }
                                />
                              </div>
                              <div className="sm:col-span-2">
                                <Label>人物动作与画面叙事</Label>
                                <Textarea
                                  rows={4}
                                  value={scene.action || ""}
                                  onChange={(event) =>
                                    updateScene(index, {
                                      action: event.target.value,
                                    })
                                  }
                                />
                              </div>
                              <div>
                                <Label>情绪</Label>
                                <Input
                                  value={scene.emotion || ""}
                                  onChange={(event) =>
                                    updateScene(index, {
                                      emotion: event.target.value,
                                    })
                                  }
                                />
                              </div>
                              <div>
                                <Label>下一场衔接</Label>
                                <Input
                                  value={scene.transition_to_next || ""}
                                  onChange={(event) =>
                                    updateScene(index, {
                                      transition_to_next: event.target.value,
                                    })
                                  }
                                />
                              </div>
                              <div>
                                <Label>对白（每行一条）</Label>
                                <Textarea
                                  rows={5}
                                  value={(scene.dialogue || [])
                                    .map((item) =>
                                      typeof item === "string"
                                        ? item
                                        : `${String(item.speaker || item.character || "")}：${String(item.text || item.content || item.line || "")}`,
                                    )
                                    .join("\n")}
                                  onChange={(event) =>
                                    updateScene(index, {
                                      dialogue: event.target.value
                                        .split("\n")
                                        .filter(Boolean)
                                        .map((line) => parseDialogueLine(line)),
                                    })
                                  }
                                />
                              </div>
                              <div>
                                <Label>旁白（每行一条）</Label>
                                <Textarea
                                  rows={5}
                                  value={(scene.narration || []).join("\n")}
                                  onChange={(event) =>
                                    updateScene(index, {
                                      narration: event.target.value
                                        .split("\n")
                                        .filter(Boolean),
                                    })
                                  }
                                />
                              </div>
                            </div>
                            <div className="flex flex-wrap justify-between gap-2 border-t border-line pt-4">
                              <div className="flex gap-1">
                                <Button
                                  size="icon"
                                  variant="ghost"
                                  disabled={index === 0}
                                  aria-label="场次上移"
                                  onClick={() => moveScene(index, -1)}
                                >
                                  <ChevronUp className="h-4 w-4" />
                                </Button>
                                <Button
                                  size="icon"
                                  variant="ghost"
                                  disabled={index === screenplay.scenes.length - 1}
                                  aria-label="场次下移"
                                  onClick={() => moveScene(index, 1)}
                                >
                                  <ChevronDown className="h-4 w-4" />
                                </Button>
                                <Button
                                  variant="ghost"
                                  size="sm"
                                  busy={
                                    sceneMutation.isPending &&
                                    sceneMutation.variables?.chapterId ===
                                      activeChapterId &&
                                    sceneMutation.variables.sceneNo ===
                                      scene.scene_no
                                  }
                                  onClick={() =>
                                    sceneMutation.mutate({
                                      chapterId: activeChapterId,
                                      sceneNo: scene.scene_no,
                                    })
                                  }
                                >
                                  <RefreshCw className="h-4 w-4" />
                                  重生成本场
                                </Button>
                              </div>
                              <Button
                                variant="ghost"
                                size="sm"
                                className="text-[#c93c30] dark:text-red-400"
                                onClick={() =>
                                  editScreenplay((current) => ({
                                    ...current,
                                    scenes: current.scenes
                                      .filter((_, sceneIndex) => sceneIndex !== index)
                                      .map((item, sceneIndex) => ({
                                        ...item,
                                        scene_no: sceneIndex + 1,
                                      })),
                                  }))
                                }
                              >
                                <Trash2 className="h-4 w-4" />
                                删除场次
                              </Button>
                            </div>
                          </div>
                        </details>
                      </Card>
                    ))}

                    {screenplay.scenes.length ? (
                      <Button
                        className="w-full border-dashed"
                        onClick={() =>
                          editScreenplay((current) => ({
                            ...current,
                            scenes: [
                              ...current.scenes,
                              {
                                scene_no: current.scenes.length + 1,
                                scene_name: "新场次",
                                characters: [],
                                dialogue: [],
                                narration: [],
                              },
                            ],
                          }))
                        }
                      >
                        <Plus className="h-4 w-4" />
                        添加场次
                      </Button>
                    ) : null}
                  </div>
                ) : null}
              </>
            )}
          </div>
        </div>
      </div>
      </ChapterRequired>
    </ProjectRequired>
  );
}
