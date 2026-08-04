import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ChevronDown,
  ChevronUp,
  Plus,
  RefreshCw,
  Save,
  Trash2,
  WandSparkles,
} from "lucide-react";
import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
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
  Button,
  Card,
  Input,
  Label,
  Tabs,
  Textarea,
} from "../components/ui";
import { useProject } from "../hooks/use-project";
import { formatError, parseJson } from "../lib/utils";
import type {
  ContentRecord,
  ScreenplayContent,
  ScreenplayScene,
} from "../types/api";

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

export function WritingPage() {
  const queryClient = useQueryClient();
  const { activeProject, activeProjectId } = useProject();
  const [tab, setTab] = useState<WritingTab>("screenplay");
  const [analysis, setAnalysis] = useState<unknown>({});
  const [analysisValid, setAnalysisValid] = useState(true);
  const [screenplay, setScreenplay] =
    useState<ScreenplayContent>(emptyScreenplay);
  const [stylePrompt, setStylePrompt] = useState("");
  const [stylePromptDirty, setStylePromptDirty] = useState(false);
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

  const analysisQuery = useQuery({
    queryKey: queryKeys.analysis(activeProjectId),
    queryFn: () => nullable(api.analysis.get(activeProjectId)),
    enabled: Boolean(activeProjectId && tab === "analysis"),
  });
  const screenplayQuery = useQuery({
    queryKey: queryKeys.screenplay(activeProjectId),
    queryFn: () => nullable(api.screenplay.get(activeProjectId)),
    enabled: Boolean(activeProjectId),
  });

  useLayoutEffect(() => {
    setAnalysis({});
    setScreenplay(emptyScreenplay);
    setStylePrompt("");
    setStylePromptDirty(false);
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
    if (
      !activeProjectId ||
      (analysisQuery.data &&
        analysisQuery.data.project_id !== activeProjectId)
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
    setServerUpdateAvailable((current) =>
      current.analysis ? { ...current, analysis: false } : current,
    );
  }, [activeProjectId, analysisQuery.data, dirty.analysis]);

  useEffect(() => {
    if (
      !activeProjectId ||
      (screenplayQuery.data &&
        screenplayQuery.data.project_id !== activeProjectId)
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
    setServerUpdateAvailable((current) =>
      current.screenplay ? { ...current, screenplay: false } : current,
    );
  }, [activeProjectId, dirty.screenplay, screenplayQuery.data]);

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
          currentRecord.source_signature !== activeProject?.source_signature)),
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
        setStylePrompt(project.style_prompt);
        setStylePromptDirty(false);
      }
      await queryClient.invalidateQueries({ queryKey: queryKeys.projects });
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const generateMutation = useMutation({
    mutationFn: ({
      projectId,
      contentType,
    }: {
      projectId: string;
      contentType: WritingTab;
    }) =>
      contentType === "analysis"
        ? api.analysis.generate(projectId)
        : api.screenplay.generate(projectId),
    onSuccess: async (_record, { projectId, contentType }) => {
      await queryClient.invalidateQueries({
        queryKey: queryKeys.jobs(projectId),
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
      projectId,
      status,
      contentType,
      content,
    }: {
      projectId: string;
      status: "ready" | "confirmed";
      contentType: WritingTab;
      content: unknown;
      editRevision: number;
    }) =>
      contentType === "analysis"
        ? api.analysis.update(projectId, {
            content_json: content,
            status,
          })
        : api.screenplay.update(projectId, {
            content_json: content,
            status,
          }),
    onSuccess: async (
      record,
      { projectId, status, contentType, editRevision: submittedRevision },
    ) => {
      const queryKey =
        contentType === "analysis"
          ? queryKeys.analysis(projectId)
          : queryKeys.screenplay(projectId);
      const isCurrentProject = activeProjectIdRef.current === projectId;
      const hasNewerEdits =
        editRevision.current[contentType] !== submittedRevision;

      if (isCurrentProject) {
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

  const sceneMutation = useMutation({
    mutationFn: ({
      projectId,
      sceneNo,
    }: {
      projectId: string;
      sceneNo: number;
    }) => api.screenplay.regenerateScene(projectId, sceneNo),
    onSuccess: async (_record, { projectId }) => {
      await queryClient.invalidateQueries({
        queryKey: queryKeys.jobs(projectId),
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
      projectId: activeProjectId,
      status,
      contentType: tab,
      content: tab === "analysis" ? analysis : screenplay,
      editRevision: editRevision.current[tab],
    });
  };

  const generateCurrent = async () => {
    if (!(await persistStylePrompt())) return;
    generateMutation.mutate({
      projectId: activeProjectId,
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

  const sourcePreview = useMemo(
    () => activeProject?.source_text || "",
    [activeProject?.source_text],
  );

  return (
    <ProjectRequired>
      <div>
        <PageHeader
          eyebrow="Step 02 / Writing"
          title="剧本生成"
          description="生成剧本时会在后台自动完成小说分析，再使用本次分析结果生成结构化场次剧本。"
          actions={
            <>
              <Button
                busy={
                  stylePromptMutation.isPending ||
                  (generateMutation.isPending &&
                    generateMutation.variables?.projectId === activeProjectId &&
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
                    saveMutation.variables?.projectId === activeProjectId &&
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
            className="mb-5 flex flex-col gap-3 rounded-md border border-[#dfb98f] bg-[#fff5e9] px-4 py-4 sm:flex-row sm:items-center sm:justify-between"
          >
            <div>
              <h2 className="text-sm font-bold text-[#73431e]">
                服务端已有新版本
              </h2>
              <p className="mt-1 text-xs leading-5 text-[#7b5637]">
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
                  只读 · 修订 R{activeProject?.source_revision}
                </p>
              </div>
              <span className="text-xs text-muted">
                {sourcePreview.length.toLocaleString("zh-CN")} 字符
              </span>
            </div>
            <div className="max-h-[70vh] overflow-y-auto whitespace-pre-wrap px-5 py-5 text-sm leading-7 text-[#3f423d]">
              {sourcePreview}
            </div>
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
                                generateMutation.variables?.projectId ===
                                  activeProjectId &&
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
                      <p className="mt-4 rounded-md border border-[#e1b3ad] bg-[#fff3f0] p-3 text-xs leading-5 text-[#943c33]">
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
                                  generateMutation.variables?.projectId ===
                                    activeProjectId &&
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
                            <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded bg-sidebar text-xs font-bold text-white">
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
                                        .map((line) => {
                                          const [speaker, ...rest] = line.split(/[：:]/);
                                          return {
                                            speaker: rest.length ? speaker.trim() : "",
                                            text: rest.length
                                              ? rest.join("：").trim()
                                              : speaker.trim(),
                                          };
                                        }),
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
                                    sceneMutation.variables?.projectId ===
                                      activeProjectId &&
                                    sceneMutation.variables.sceneNo ===
                                      scene.scene_no
                                  }
                                  onClick={() =>
                                    sceneMutation.mutate({
                                      projectId: activeProjectId,
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
                                className="text-[#9b342c]"
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
    </ProjectRequired>
  );
}
