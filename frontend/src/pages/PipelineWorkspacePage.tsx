import {
  AlertTriangle,
  ArrowLeft,
  Check,
  ChevronRight,
  Download,
  FileJson,
  LockKeyhole,
  Pause,
  Play,
  RefreshCw,
  RotateCcw,
  Save,
  ShieldAlert,
  Square,
  Workflow,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router";
import { ApiError, api, getDynamicRunTitle } from "../api/client";
import { Button, Card, Textarea } from "../components/ui";
import { ErrorState, LoadingState } from "../components/status";
import { formatDate, formatError } from "../lib/utils";
import type {
  DynamicChecklistResponse,
  DynamicDiagnostic,
  DynamicGateResult,
  DynamicJsonObject,
  DynamicPreflightItem,
  DynamicPreflightResult,
  DynamicStageKey,
  DynamicWorkflowAction,
  DynamicWorkflowRun,
  DynamicWorkflowStage,
} from "../types/api";

const STAGE_DEFINITIONS: Array<{
  key: DynamicStageKey;
  label: string;
  description: string;
  prerequisites: DynamicStageKey[];
}> = [
  {
    key: "0-source",
    label: "源文",
    description: "读取并固定本轮输入。",
    prerequisites: [],
  },
  {
    key: "1-character-bios",
    label: "角色小传",
    description: "整理角色设定和视觉锚点。",
    prerequisites: ["0-source"],
  },
  {
    key: "2-rhythm",
    label: "节奏",
    description: "安排时长、节拍和转折。",
    prerequisites: ["1-character-bios"],
  },
  {
    key: "3-script",
    label: "剧本",
    description: "生成可执行的详细剧本。",
    prerequisites: ["2-rhythm"],
  },
  {
    key: "4-storyboard",
    label: "分镜",
    description: "整理镜头和连续性。",
    prerequisites: ["3-script"],
  },
  {
    key: "5a-image-prompts",
    label: "图片提示词",
    description: "编译图片提示词和关键帧计划。",
    prerequisites: ["4-storyboard"],
  },
  {
    key: "5b-video-prompts",
    label: "视频提示词",
    description: "编译视频提示词和连续性计划。",
    prerequisites: ["4-storyboard"],
  },
  {
    key: "6-delivery",
    label: "交付",
    description: "检查素材并导出交付包。",
    prerequisites: ["5a-image-prompts", "5b-video-prompts"],
  },
];

const STAGE_KEYS = STAGE_DEFINITIONS.map((item) => item.key);
const WORKSPACE_POLL_INTERVAL_MS = 1500;

type PipelineLocationState = {
  initialWorkspace?: DynamicWorkflowAction;
};

type DraftState = {
  text: string;
  serverText: string;
  dirty: boolean;
  hasServerConflict: boolean;
};

type DraftStore = Partial<Record<DynamicStageKey, DraftState>>;

const statusLabels: Record<string, string> = {
  pending: "待处理",
  queued: "排队中",
  running: "运行中",
  blocked: "已阻塞",
  ready: "待确认",
  confirmed: "已确认",
  stale: "已过期",
  partial_failed: "部分失败",
  failed: "失败",
  interrupted: "已中断",
  cancelled: "已取消",
};

const gateLabels: Record<string, string> = {
  ready: "门禁通过",
  warning: "有警告",
  blocked: "门禁阻塞",
  partial_failed: "部分失败",
};

function formatJson(value: unknown) {
  if (value === undefined) return "{}";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2) || "{}";
  } catch {
    return "无法显示该内容";
  }
}

function displaySnapshot(value: unknown) {
  if (typeof value === "string") return value;
  return formatJson(value);
}

function StatusBadge({ status }: { status: string }) {
  const label = statusLabels[status] || status;
  const tone =
    status === "confirmed"
      ? "border-[#9cb79c] bg-[#eef5eb] text-[#355d39]"
      : ["failed", "partial_failed", "interrupted", "cancelled", "blocked"].includes(status)
        ? "border-[#e0a39e] bg-[#fff0ee] text-[#9b342c]"
        : ["queued", "running"].includes(status)
          ? "border-[#d9ae7f] bg-[#fff3e5] text-[#8b4b1e]"
          : "border-[#d9d5cc] bg-white text-[#6b6e68]";

  return (
    <span
      role="status"
      aria-label={`状态：${label}`}
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-bold leading-none ${tone}`}
    >
      <span
        aria-hidden="true"
        className={`h-1.5 w-1.5 rounded-full ${
          status === "confirmed"
            ? "bg-[#527c57]"
            : ["failed", "partial_failed", "interrupted", "cancelled", "blocked"].includes(status)
              ? "bg-[#b34539]"
              : ["queued", "running"].includes(status)
                ? "bg-[#b66a2e]"
                : "bg-[#92958e]"
        }`}
      />
      {label}
    </span>
  );
}

function GateBadge({ status }: { status: string }) {
  const label = gateLabels[status] || status;
  return (
    <span
      role="status"
      aria-label={`门禁状态：${label}`}
      className={`inline-flex items-center rounded-full border px-2.5 py-1 text-[11px] font-bold ${
        status === "ready"
          ? "border-[#9cb79c] bg-[#eef5eb] text-[#355d39]"
          : status === "warning"
            ? "border-[#dfb98f] bg-[#fff5e9] text-[#7b4b22]"
            : "border-[#e0a39e] bg-[#fff0ee] text-[#9b342c]"
      }`}
    >
      {label}
    </span>
  );
}

function getGateResult(stage: DynamicWorkflowStage): DynamicGateResult {
  const gate = stage.gate_result;
  if (!gate || typeof gate !== "object") {
    return { status: "blocked", passed: false, diagnostics: [], can_confirm: false };
  }
  return {
    status: gate.status || "blocked",
    passed: Boolean(gate.passed),
    diagnostics: Array.isArray(gate.diagnostics) ? gate.diagnostics : [],
    checked_at: gate.checked_at,
    can_confirm: Boolean(gate.can_confirm),
  };
}

function textValue(value: unknown) {
  return typeof value === "string" ? value.trim() : "";
}

function FailureSummary({
  run,
  stages,
}: {
  run: DynamicWorkflowRun;
  stages: DynamicWorkflowStage[];
}) {
  const runFailureStatuses = ["partial_failed", "failed", "interrupted"];
  const stageFailureStatuses = ["partial_failed", "failed", "interrupted"];
  const runError = textValue(run.error);
  const currentStage = stages.find((stage) => stage.stage_key === run.current_stage);
  const errorStages = stages.filter(
    (stage) => stageFailureStatuses.includes(stage.status) || textValue(stage.error),
  );
  if (
    currentStage &&
    runFailureStatuses.includes(run.status) &&
    !errorStages.some((stage) => stage.id === currentStage.id)
  ) {
    errorStages.unshift(currentStage);
  }
  if (!runFailureStatuses.includes(run.status) && !runError && !errorStages.length) {
    return null;
  }

  return (
    <div
      role="alert"
      aria-label="运行失败诊断"
      className="mt-4 rounded-lg border border-[#e1b3ad] bg-[#fff5f2] px-3 py-3 text-xs leading-5 text-[#9b342c]"
    >
      <div className="flex items-center gap-2 font-bold">
        <ShieldAlert className="h-4 w-4 shrink-0" aria-hidden="true" />
        {run.status === "partial_failed" ? "运行部分失败" : "运行错误"}
      </div>
      {runError ? <p className="mt-2">运行错误：{runError}</p> : null}
      {errorStages.length ? (
        <ul className="mt-2 space-y-1.5">
          {errorStages.map((stage) => (
            <li key={stage.id}>
              <span className="font-bold">
                {stage.stage_key} · {STAGE_DEFINITIONS.find((item) => item.key === stage.stage_key)?.label || stage.stage_key}
              </span>
              <span className="ml-2">{statusLabels[stage.status] || stage.status}</span>
              <span className="mt-0.5 block">
                阶段 / job 错误：{textValue(stage.error) || "服务端未返回错误详情"}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-2">当前失败阶段未返回错误详情。</p>
      )}
    </div>
  );
}

function stageIsLocked(
  definition: (typeof STAGE_DEFINITIONS)[number],
  stageByKey: Map<DynamicStageKey, DynamicWorkflowStage>,
) {
  return definition.prerequisites.some(
    (prerequisite) => stageByKey.get(prerequisite)?.status !== "confirmed",
  );
}

function DiagnosticList({
  level,
  diagnostics,
}: {
  level: "blocking" | "warning";
  diagnostics: DynamicDiagnostic[];
}) {
  const items = diagnostics.filter((item) => item.level === level);
  const isBlocking = level === "blocking";
  return (
    <section
      aria-label={isBlocking ? "blocking diagnostics" : "warning diagnostics"}
      className={`rounded-lg border p-4 ${
        isBlocking
          ? "border-[#e1b3ad] bg-[#fff5f2]"
          : "border-[#dfb98f] bg-[#fff9f0]"
      }`}
    >
      <div className="flex items-center gap-2">
        {isBlocking ? (
          <ShieldAlert className="h-4 w-4 text-[#a4362d]" aria-hidden="true" />
        ) : (
          <AlertTriangle className="h-4 w-4 text-[#9a662f]" aria-hidden="true" />
        )}
        <h3 className="text-sm font-bold text-ink">
          {isBlocking ? "阻塞诊断" : "警告诊断"}
        </h3>
        <span className="ml-auto text-xs font-semibold text-muted">{items.length}</span>
      </div>
      {items.length ? (
        <ul className="mt-3 space-y-2">
          {items.map((item, index) => (
            <li key={`${item.code}-${index}`} className="text-xs leading-5 text-ink">
              <span className="font-bold">{item.code}</span>
              <span className="ml-2">{item.message}</span>
              {item.path ? <span className="mt-0.5 block text-muted">位置：{item.path}</span> : null}
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-2 text-xs text-muted">没有{isBlocking ? "阻塞" : "警告"}信息。</p>
      )}
    </section>
  );
}

function preflightItems(
  preflight: DynamicPreflightResult | null,
): Array<{ category: string; item: DynamicPreflightItem }> {
  if (!preflight) return [];
  return [
    ...preflight.missing.map((item) => ({ category: "缺失", item })),
    ...preflight.failed.map((item) => ({ category: "失败", item })),
    ...preflight.skipped.map((item) => ({ category: "跳过", item })),
  ];
}

function DeliveryPanel({
  run,
  checklist,
  preflight,
  loading,
  partialExport,
  exportBusy,
  onPartialExportChange,
  onPreflight,
  onExport,
}: {
  run: DynamicWorkflowRun;
  checklist: DynamicChecklistResponse | null;
  preflight: DynamicPreflightResult | null;
  loading: boolean;
  partialExport: boolean;
  exportBusy: boolean;
  onPartialExportChange: (checked: boolean) => void;
  onPreflight: () => void;
  onExport: () => void;
}) {
  const effectivePreflight = preflight || checklist?.preflight || null;
  const issues = preflightItems(effectivePreflight);
  const strictReady = effectivePreflight?.status === "ready";
  const partialReady = Boolean(effectivePreflight);

  return (
    <section aria-label="delivery controls" className="border-t border-line pt-5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-[10px] font-bold uppercase tracking-[0.18em] text-vermilion">6-delivery</p>
          <h3 className="mt-1 text-base font-bold text-ink">交付检查</h3>
        </div>
        {effectivePreflight ? <GateBadge status={effectivePreflight.status} /> : null}
      </div>

      {loading ? (
        <p className="mt-4 text-xs text-muted">正在读取清单和预检结果…</p>
      ) : checklist?.checklist.length ? (
        <ul className="mt-4 space-y-2" aria-label="交付清单">
          {checklist.checklist.map((item) => (
            <li key={item.key} className="flex items-center gap-2 rounded-md border border-line bg-white px-3 py-2 text-xs">
              <span
                aria-hidden="true"
                className={`h-1.5 w-1.5 rounded-full ${
                  ["completed", "selected"].includes(item.status) ? "bg-[#527c57]" : "bg-[#b66a2e]"
                }`}
              />
              <span className="min-w-0 flex-1 truncate font-semibold text-ink">{item.label}</span>
              <span className="shrink-0 text-muted">{item.status}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-4 text-xs leading-5 text-muted">暂无交付清单。请先运行预检。</p>
      )}

      {effectivePreflight?.status === "blocked" ? (
        <div role="alert" className="mt-4 rounded-lg border border-[#e1b3ad] bg-[#fff5f2] p-3 text-xs leading-5 text-[#9b342c]">
          <strong>严格导出已阻止。</strong> 请处理下面列出的缺失、失败或跳过项。
        </div>
      ) : effectivePreflight?.status === "warning" ? (
        <div className="mt-4 rounded-lg border border-[#dfb98f] bg-[#fff9f0] p-3 text-xs leading-5 text-[#7b4b22]">
          严格导出仍需检查；如确需继续，请明确选择“允许部分导出”。
        </div>
      ) : null}

      {issues.length ? (
        <div className="mt-3 space-y-2" aria-label="导出缺失失败跳过原因">
          {issues.map(({ category, item }, index) => (
            <div key={`${item.kind}-${item.id}-${index}`} className="rounded-md border border-line bg-white px-3 py-2 text-xs leading-5 text-ink">
              <span className="font-bold">{category}：{item.kind}</span>
              <span className="ml-2 text-muted">{item.reason || item.status}</span>
            </div>
          ))}
        </div>
      ) : null}

      <div className="mt-5 space-y-3">
        <Button
          type="button"
          className="w-full"
          disabled={loading}
          onClick={onPreflight}
        >
          <RefreshCw className="h-4 w-4" />
          重新运行预检
        </Button>
        <Button
          type="button"
          variant="primary"
          className="w-full"
          disabled={loading || exportBusy || !strictReady || partialExport}
          busy={exportBusy && !partialExport}
          onClick={onExport}
          aria-label={strictReady ? "严格导出 ZIP" : "严格导出 ZIP（当前被阻止）"}
        >
          <Download className="h-4 w-4" />
          {strictReady ? "导出严格 ZIP" : "严格导出不可用"}
        </Button>
        <label htmlFor="allow-partial-export" className="flex cursor-pointer items-start gap-2 rounded-md border border-line bg-white px-3 py-2.5 text-xs text-ink">
          <input
            id="allow-partial-export"
            type="checkbox"
            aria-label="允许部分导出"
            checked={partialExport}
            onChange={(event) => onPartialExportChange(event.target.checked)}
            className="mt-0.5 h-4 w-4 accent-[#e8613a]"
          />
          <span>
            <span className="block font-bold">允许部分导出</span>
            <span className="mt-0.5 block leading-5 text-muted">缺失内容会在 ZIP 清单中明确标记。</span>
          </span>
        </label>
        <Button
          type="button"
          variant="secondary"
          className="w-full"
          disabled={loading || exportBusy || !partialExport || !partialReady}
          busy={exportBusy && partialExport}
          onClick={onExport}
          aria-label="部分导出 ZIP"
        >
          <Download className="h-4 w-4" />
          导出部分 ZIP
        </Button>
      </div>

      <p className="mt-3 text-[11px] leading-5 text-muted">
        导出请求只由客户端代为下载，不会在页面上显示服务端文件路径。运行修订 R{run.revision}。
      </p>
    </section>
  );
}

export function PipelineWorkspacePage() {
  const { projectId, runId, stageKey } = useParams<{
    projectId?: string;
    runId?: string;
    stageKey?: string;
  }>();
  const location = useLocation();
  const navigate = useNavigate();
  const navigationWorkspace = (location.state as PipelineLocationState | null)?.initialWorkspace;
  const initialWorkspace =
    navigationWorkspace &&
    navigationWorkspace.run.id === runId &&
    navigationWorkspace.run.project_id === projectId
      ? navigationWorkspace
      : null;
  const initialWorkspaceRef = useRef(initialWorkspace);
  const workspaceRef = useRef<DynamicWorkflowAction | null>(initialWorkspace);
  const requestSequenceRef = useRef(0);
  const actionBusyRef = useRef(false);
  const refreshingRef = useRef(false);
  const refreshTokenRef = useRef(0);
  const mountedRef = useRef(false);
  const draftStatesRef = useRef<Record<string, DraftStore>>({});
  const activeDraftRef = useRef<{ runId: string; stageKey: DynamicStageKey } | null>(null);
  const draftTextRef = useRef("{}");
  const [workspace, setWorkspace] = useState<DynamicWorkflowAction | null>(initialWorkspace);
  const [loading, setLoading] = useState(!initialWorkspace);
  const [loadError, setLoadError] = useState("");
  const [actionError, setActionError] = useState("");
  const [revisionNotice, setRevisionNotice] = useState("");
  const [pollError, setPollError] = useState("");
  const [actionBusy, setActionBusy] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [artifactTab, setArtifactTab] = useState<"raw" | "normalized" | "draft">("draft");
  const [draftText, setDraftText] = useState("{}");
  const [, setDraftStatusVersion] = useState(0);
  const [draftParseError, setDraftParseError] = useState("");
  const [checklist, setChecklist] = useState<DynamicChecklistResponse | null>(null);
  const [preflight, setPreflight] = useState<DynamicPreflightResult | null>(null);
  const [deliveryLoading, setDeliveryLoading] = useState(false);
  const [deliveryError, setDeliveryError] = useState("");
  const [partialExport, setPartialExport] = useState(false);
  const [exportBusy, setExportBusy] = useState(false);

  const updateDraftText = useCallback((value: string) => {
    draftTextRef.current = value;
    setDraftText(value);
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      requestSequenceRef.current += 1;
      refreshTokenRef.current += 1;
    };
  }, []);

  const commitWorkspace = useCallback(
    (result: DynamicWorkflowAction) => {
      if (!runId || !projectId || result.run.id !== runId || result.run.project_id !== projectId) {
        return false;
      }
      const current = workspaceRef.current;
      if (
        current &&
        current.run.id === result.run.id &&
        result.run.revision < current.run.revision
      ) {
        return false;
      }
      workspaceRef.current = result;
      setWorkspace(result);
      return true;
    },
    [projectId, runId],
  );

  const refresh = useCallback(
    async ({ background = false }: { background?: boolean } = {}) => {
      if (!runId || !projectId || actionBusyRef.current) return;
      if (refreshingRef.current) return;
      const requestSequence = ++requestSequenceRef.current;
      const refreshToken = ++refreshTokenRef.current;
      refreshingRef.current = true;
      if (!background) {
        setRefreshing(true);
        setLoadError("");
      }
      try {
        const result = await api.dynamicRuns.getWithStages(runId);
        if (
          !mountedRef.current ||
          requestSequence !== requestSequenceRef.current ||
          actionBusyRef.current
        ) {
          return;
        }
        if (result.run.project_id !== projectId) {
          workspaceRef.current = null;
          setWorkspace(null);
          setLoadError("当前运行不属于该项目，无法打开工作区。");
          return;
        }
        if (commitWorkspace(result)) {
          setRevisionNotice("");
          setPollError("");
        }
      } catch (error) {
        if (
          !mountedRef.current ||
          requestSequence !== requestSequenceRef.current ||
          actionBusyRef.current
        ) {
          return;
        }
        if (background) {
          setPollError(`轮询更新失败：${formatError(error)}，本地未保存草稿仍保留。`);
          return;
        }
        throw error;
      } finally {
        if (refreshToken === refreshTokenRef.current) {
          refreshingRef.current = false;
          if (mountedRef.current && !background) {
            setRefreshing(false);
          }
        }
      }
    },
    [commitWorkspace, projectId, runId],
  );

  useEffect(() => {
    let cancelled = false;
    requestSequenceRef.current += 1;
    refreshTokenRef.current += 1;
    refreshingRef.current = false;
    activeDraftRef.current = null;
    updateDraftText("{}");
    const seededWorkspace =
      initialWorkspaceRef.current &&
      initialWorkspaceRef.current.run.id === runId &&
      initialWorkspaceRef.current.run.project_id === projectId
        ? initialWorkspaceRef.current
        : null;
    workspaceRef.current = seededWorkspace;
    setWorkspace(seededWorkspace);
    setLoadError("");
    setPollError("");
    if (!runId) {
      setLoadError("缺少动态运行 ID");
      setLoading(false);
      return () => {
        cancelled = true;
      };
    }
    if (!projectId) {
      setLoadError("缺少项目 ID");
      setLoading(false);
      return () => {
        cancelled = true;
        requestSequenceRef.current += 1;
      };
    }
    if (seededWorkspace) {
      setLoading(false);
      return () => {
        cancelled = true;
        requestSequenceRef.current += 1;
      };
    }
    setLoading(true);
    void refresh()
      .catch((error: unknown) => {
        if (!cancelled) setLoadError(formatError(error));
      })
      .finally(() => {
        if (!cancelled && !seededWorkspace) setLoading(false);
      });
    return () => {
      cancelled = true;
      requestSequenceRef.current += 1;
    };
  }, [projectId, refresh, runId, updateDraftText]);

  const stageByKey = useMemo(() => {
    const result = new Map<DynamicStageKey, DynamicWorkflowStage>();
    (workspace?.stages || []).forEach((stage) => result.set(stage.stage_key, stage));
    return result;
  }, [workspace?.stages]);

  const hasInvalidStageKey = Boolean(stageKey && !STAGE_KEYS.includes(stageKey as DynamicStageKey));
  const selectedStageKey: DynamicStageKey =
    stageKey && !hasInvalidStageKey
      ? (stageKey as DynamicStageKey)
      : workspace?.run.current_stage && STAGE_KEYS.includes(workspace.run.current_stage)
        ? workspace.run.current_stage
        : "0-source";
  const selectedStage = stageByKey.get(selectedStageKey);
  const selectedDefinition = STAGE_DEFINITIONS.find((item) => item.key === selectedStageKey);
  const selectedGate = selectedStage ? getGateResult(selectedStage) : null;
  const blockingDiagnostics = selectedGate?.diagnostics.filter((item) => item.level === "blocking") || [];
  const isLocked = selectedDefinition ? stageIsLocked(selectedDefinition, stageByKey) : false;
  const selectedDraftState = runId
    ? draftStatesRef.current[runId]?.[selectedStageKey]
    : undefined;
  const hasDirtyDraft = Boolean(selectedDraftState?.dirty);
  const canConfirmCurrentStage = Boolean(
    workspace &&
      projectId &&
      workspace.run.project_id === projectId &&
      !["cancelled", "stale", "failed", "partial_failed", "interrupted", "confirmed"].includes(
        workspace.run.status,
      ) &&
      selectedStage &&
      selectedStage.status === "ready" &&
      selectedGate?.passed &&
      selectedGate.can_confirm &&
      blockingDiagnostics.length === 0 &&
      !isLocked &&
      !hasDirtyDraft,
  );

  const shouldPoll = Boolean(
    workspace &&
      (["queued", "running"].includes(workspace.run.status) ||
        workspace.stages.some((stage) => ["queued", "running"].includes(stage.status))),
  );

  const syncDraftWithServer = useCallback(
    (stage: DynamicWorkflowStage) => {
      if (!runId) return;
      const store = (draftStatesRef.current[runId] ||= {});
      const serverText = formatJson(stage.draft_output);
      const previous = store[stage.stage_key];
      const next: DraftState = !previous
        ? {
            text: serverText,
            serverText,
            dirty: false,
            hasServerConflict: false,
          }
        : !previous.dirty
          ? {
              text: serverText,
              serverText,
              dirty: false,
              hasServerConflict: false,
            }
          : previous.serverText === serverText
            ? previous
            : {
                ...previous,
                serverText,
                hasServerConflict: true,
              };
      store[stage.stage_key] = next;
      if (next !== previous && next.hasServerConflict) {
        setDraftStatusVersion((version) => version + 1);
      }

      const activeDraft = activeDraftRef.current;
      const isActiveStage =
        activeDraft?.runId === runId && activeDraft.stageKey === stage.stage_key;
      const stageChanged = !isActiveStage;
      activeDraftRef.current = { runId, stageKey: stage.stage_key };
      if (stageChanged) {
        updateDraftText(next.text);
        setDraftParseError("");
        setArtifactTab("draft");
      } else if (!next.dirty && draftTextRef.current !== next.text) {
        updateDraftText(next.text);
        setDraftParseError("");
      }
    },
    [runId, updateDraftText],
  );

  useEffect(() => {
    if (!runId || !shouldPoll) return;
    const timer = window.setInterval(() => {
      void refresh({ background: true });
    }, WORKSPACE_POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [refresh, runId, shouldPoll]);

  useEffect(() => {
    if (!selectedStage) return;
    syncDraftWithServer(selectedStage);
  }, [selectedStage, syncDraftWithServer]);

  useEffect(() => {
    if (!runId || hasInvalidStageKey || selectedStageKey !== "6-delivery" || !workspace) return;
    let cancelled = false;
    setDeliveryLoading(true);
    setDeliveryError("");
    api.dynamicRuns
      .checklist(runId)
      .then((result) => {
        if (!cancelled) {
          setChecklist(result);
          setPreflight(result.preflight);
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) setDeliveryError(formatError(error));
      })
      .finally(() => {
        if (!cancelled) setDeliveryLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [hasInvalidStageKey, runId, selectedStageKey, workspace]);

  const handleOperationError = (error: unknown) => {
    if (!mountedRef.current) return;
    if (error instanceof ApiError && error.status === 409) {
      setRevisionNotice("内容已更新，请刷新。未保存草稿已保留，保存前请确认。");
      setActionError("");
      return;
    }
    setActionError(formatError(error));
  };

  const runOperation = async (
    name: string,
    operation: (revision: number) => Promise<DynamicWorkflowAction>,
  ): Promise<DynamicWorkflowAction | null> => {
    if (!workspace || !projectId || workspace.run.project_id !== projectId) {
      setActionError("当前运行不属于该项目，拒绝执行操作。");
      return null;
    }
    if (workspace.run.status === "cancelled") {
      setActionError("运行已取消，不能继续编辑或保存。");
      return null;
    }
    if (actionBusyRef.current) return null;
    actionBusyRef.current = true;
    const operationSequence = ++requestSequenceRef.current;
    setActionBusy(name);
    setActionError("");
    setRevisionNotice("");
    setPollError("");
    try {
      const result = await operation(workspace.run.revision);
      if (operationSequence !== requestSequenceRef.current || !mountedRef.current) return null;
      if (!commitWorkspace(result)) {
        setActionError("服务端返回的运行不属于当前项目，拒绝更新工作区。");
        return null;
      }
      return result;
    } catch (error) {
      handleOperationError(error);
      return null;
    } finally {
      actionBusyRef.current = false;
      if (mountedRef.current) setActionBusy("");
    }
  };

  const markDraftSaved = useCallback(
    (
      result: DynamicWorkflowAction,
      stageKeyToSave: DynamicStageKey,
      fallbackText: string,
      submittedText: string,
    ) => {
      if (!runId) return;
      const savedStage = result.stages.find((stage) => stage.stage_key === stageKeyToSave);
      const serverText = savedStage ? formatJson(savedStage.draft_output) : fallbackText;
      const store = (draftStatesRef.current[runId] ||= {});
      const currentDraft = store[stageKeyToSave];
      if (currentDraft && currentDraft.text !== submittedText) {
        const stillDirty = currentDraft.text !== serverText;
        store[stageKeyToSave] = {
          ...currentDraft,
          serverText,
          dirty: stillDirty,
          hasServerConflict: stillDirty,
        };
        setDraftStatusVersion((version) => version + 1);
        return;
      }
      store[stageKeyToSave] = {
        text: serverText,
        serverText,
        dirty: false,
        hasServerConflict: false,
      };
      if (
        activeDraftRef.current?.runId === runId &&
        activeDraftRef.current.stageKey === stageKeyToSave
      ) {
        updateDraftText(serverText);
        setDraftParseError("");
      }
    },
    [runId, updateDraftText],
  );

  const handleSaveDraft = async () => {
    if (
      !workspace ||
      !selectedStage ||
      !selectedGate ||
      isLocked ||
      ["cancelled", "confirmed"].includes(workspace.run.status) ||
      selectedStage.status === "cancelled"
    ) {
      return;
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(draftText);
    } catch {
      setDraftParseError("JSON 格式不正确，请修正后再保存。");
      return;
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      setDraftParseError("草稿必须是 JSON 对象，不能是数组、字符串或空值。");
      return;
    }
    setDraftParseError("");
    const submittedDraftText = draftText;
    const gatePayload =
      selectedGate.passed && selectedGate.can_confirm
        ? { gate_result: selectedGate }
        : {};
    const saved = await runOperation("save-draft", (revision) =>
      api.dynamicRuns.saveDraft(workspace.run.id, selectedStageKey, {
        expected_revision: revision,
        draft_output: parsed as DynamicJsonObject,
        ...gatePayload,
      }),
    );
    if (saved) {
      markDraftSaved(saved, selectedStageKey, formatJson(parsed), submittedDraftText);
    }
  };

  const handleConfirm = async () => {
    if (hasDirtyDraft) {
      setActionError("请先保存未保存的草稿，再确认当前阶段。");
      return;
    }
    if (!workspace || !selectedStage || !canConfirmCurrentStage) {
      if (workspace && selectedStage) {
        setActionError(`${confirmDisabledReason}，无法确认。`);
      }
      return;
    }
    await runOperation("confirm", (revision) =>
      api.dynamicRuns.confirmStage(workspace.run.id, selectedStageKey, revision),
    );
  };

  const handleRegenerate = async () => {
    if (
      !workspace ||
      !selectedStage ||
      isLocked ||
      !canRegenerate ||
      selectedStage.status === "cancelled"
    ) {
      return;
    }
    await runOperation("regenerate", (revision) =>
      api.dynamicRuns.regenerateStage(workspace.run.id, selectedStageKey, revision),
    );
  };

  const handlePreflight = async () => {
    if (!runId) return;
    setDeliveryLoading(true);
    setDeliveryError("");
    try {
      const result = await api.dynamicRuns.preflight(runId, {
        strict: true,
        allow_partial: partialExport,
      });
      setPreflight(result);
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        setDeliveryError(error.message || "严格预检被阻止，请查看交付清单中的原因。");
      } else {
        setDeliveryError(formatError(error));
      }
    } finally {
      setDeliveryLoading(false);
    }
  };

  const handleExport = async () => {
    if (!runId || (!partialExport && preflight?.status !== "ready")) return;
    if (
      partialExport &&
      !window.confirm("当前导出可能缺少或跳过部分交付内容。确认继续导出部分 ZIP 吗？")
    ) {
      return;
    }
    setExportBusy(true);
    setDeliveryError("");
    try {
      await api.dynamicRuns.exportZip(runId, {
        strict: !partialExport,
        allow_partial: partialExport,
      });
    } catch (error) {
      setDeliveryError(formatError(error));
    } finally {
      setExportBusy(false);
    }
  };

  if (loading) {
    return (
      <div className="min-h-screen bg-[#111715] px-5 py-8 text-white sm:px-8">
        <LoadingState label="正在恢复动态漫工作区…" />
      </div>
    );
  }

  if (hasInvalidStageKey) {
    return (
      <div className="min-h-screen bg-[#111715] px-5 py-8 sm:px-8">
        <ErrorState
          title="动态漫阶段参数无效"
          message={`未识别的阶段参数：${stageKey}`}
          onRetry={() => navigate(`/free-create/${projectId}/pipeline/${runId}`, { replace: true })}
        />
      </div>
    );
  }

  if (loadError || !workspace || !selectedStage || !selectedDefinition || !selectedGate) {
    return (
      <div className="min-h-screen bg-[#111715] px-5 py-8 sm:px-8">
        <ErrorState
          title="动态漫工作区读取失败"
          message={loadError || "服务端没有返回完整的阶段数据"}
          onRetry={() => void refresh().catch((error: unknown) => setLoadError(formatError(error)))}
        />
      </div>
    );
  }

  const run = workspace.run;
  const runStatus = run.status;
  const sourceStage = stageByKey.get("0-source");
  const canStart =
    ["pending", "ready", "stale"].includes(runStatus) &&
    Boolean(sourceStage && ["pending", "ready", "stale"].includes(sourceStage.status));
  const canPause = ["pending", "queued", "running"].includes(runStatus);
  const canResume = ["blocked", "failed", "interrupted", "stale", "partial_failed"].includes(runStatus);
  const canCancel = !["cancelled", "confirmed"].includes(runStatus);
  const canConfirm = canConfirmCurrentStage;
  const canEdit = !["cancelled", "confirmed"].includes(runStatus) && !isLocked && selectedStage.status !== "cancelled";
  const canRegenerate =
    !["cancelled", "confirmed"].includes(runStatus) &&
    !isLocked &&
    !["queued", "running", "cancelled"].includes(selectedStage.status);
  const confirmDisabledReason = hasDirtyDraft
    ? "请先保存未保存的草稿"
    : ["stale", "failed", "partial_failed", "interrupted"].includes(runStatus)
      ? `运行状态为${statusLabels[runStatus] || runStatus}，请先恢复运行`
      : "当前阶段尚未通过门禁";
  const artifactValue =
    artifactTab === "raw"
      ? selectedStage.raw_output
      : artifactTab === "normalized"
        ? selectedStage.normalized_output
        : draftText;

  return (
    <div className="min-h-screen bg-[#111715] text-ink">
      <header className="border-b border-white/10 bg-[#151d1a] text-white">
        <div className="mx-auto max-w-[1680px] px-5 py-5 sm:px-8 lg:px-10">
          <div className="flex flex-col justify-between gap-5 lg:flex-row lg:items-center">
            <div className="flex min-w-0 items-start gap-4">
              <button
                type="button"
                aria-label="返回动态漫运行列表"
                className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-white/10 text-white/65 transition hover:border-white/25 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#f29a70]"
                onClick={() => navigate(`/free-create/${projectId}`)}
              >
                <ArrowLeft className="h-4 w-4" />
              </button>
              <div className="min-w-0">
                <p className="text-[10px] font-bold uppercase tracking-[0.28em] text-[#f29a70]">Pipeline workspace</p>
                <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-2">
                  <h1 className="truncate text-2xl font-extrabold tracking-[-0.05em]">{getDynamicRunTitle(run)}</h1>
                  <StatusBadge status={run.status} />
                </div>
                <p className="mt-2 text-xs text-white/45">{run.chapter_title || "未命名章节"} · {run.id}</p>
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-2 text-xs text-white/55">
              <span className="rounded-md border border-white/10 bg-white/[0.04] px-3 py-2">修订 R{run.revision}</span>
              <span className="rounded-md border border-white/10 bg-white/[0.04] px-3 py-2">
                目标 {String(run.config_snapshot.target_duration_sec ?? "—")} 秒
              </span>
              <Button
                type="button"
                variant="dark"
                size="sm"
                busy={refreshing}
                disabled={Boolean(actionBusy) || refreshing}
                onClick={() => {
                  setActionError("");
                  void refresh().catch((error: unknown) => setActionError(formatError(error)));
                }}
              >
                <RefreshCw className="h-3.5 w-3.5" />
                刷新
              </Button>
            </div>
          </div>
          {revisionNotice ? (
            <div role="alert" className="mt-5 flex items-center gap-2 rounded-lg border border-[#dfb98f] bg-[#fff5e9] px-4 py-3 text-sm text-[#7b4b22]">
              <AlertTriangle className="h-4 w-4 shrink-0" aria-hidden="true" />
              {revisionNotice}
            </div>
          ) : null}
          {actionError ? (
            <div role="alert" className="mt-3 rounded-lg border border-[#e1b3ad] bg-[#fff5f2] px-4 py-3 text-sm text-[#9b342c]">{actionError}</div>
          ) : null}
          {pollError ? (
            <div role="status" aria-live="polite" className="mt-3 rounded-lg border border-[#dfb98f] bg-[#fff9f0] px-4 py-3 text-sm text-[#7b4b22]">{pollError}</div>
          ) : null}
        </div>
      </header>

      <main className="mx-auto max-w-[1680px] px-5 py-6 sm:px-8 lg:px-10 lg:py-8">
        <div className="grid gap-5 xl:grid-cols-[250px_minmax(0,1fr)_350px]">
          <aside aria-label="工作流阶段" className="self-start rounded-xl border border-white/10 bg-[#1a2420] p-3 text-white xl:sticky xl:top-5">
            <div className="flex items-center justify-between px-2 py-2">
              <div>
                <p className="text-[10px] font-bold uppercase tracking-[0.18em] text-white/35">Stages</p>
                <h2 className="mt-1 text-sm font-bold">阶段轨道</h2>
              </div>
              <Workflow className="h-4 w-4 text-[#f29a70]" aria-hidden="true" />
            </div>
            <nav className="mt-3 space-y-1" aria-label="动态漫阶段列表">
              {STAGE_DEFINITIONS.map((definition, index) => {
                const stage = stageByKey.get(definition.key);
                const status = stage?.status || "pending";
                const locked = stageIsLocked(definition, stageByKey);
                const current = run.current_stage === definition.key;
                const parallel = definition.key.startsWith("5") && definition.prerequisites.length === 1;
                return (
                  <button
                    type="button"
                    key={definition.key}
                    aria-label={`${definition.key}：${definition.label}，${statusLabels[status] || status}${locked ? "，前置阶段未确认" : ""}${runId && draftStatesRef.current[runId]?.[definition.key]?.dirty ? "，有未保存内容" : ""}${current ? "，当前阶段" : ""}`}
                    className={`group flex w-full items-center gap-2 rounded-lg border px-2.5 py-2.5 text-left transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#f29a70] ${
                      selectedStageKey === definition.key
                        ? "border-[#f29a70]/70 bg-[#f29a70]/15"
                        : "border-transparent hover:border-white/15 hover:bg-white/[0.05]"
                    } ${locked ? "opacity-55" : ""}`}
                    onClick={() => navigate(`/free-create/${projectId}/pipeline/${runId}/stage/${definition.key}`)}
                  >
                    <span className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-md border text-[10px] font-bold ${current ? "border-[#f29a70]/70 bg-[#f29a70] text-[#1b1714]" : "border-white/15 bg-white/[0.04] text-white/55"}`}>
                      {index === 5 || index === 6 ? definition.key.slice(1, 2) : index}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="flex items-center gap-1.5">
                        <span className="truncate text-xs font-bold">{definition.key}</span>
                        {parallel ? <span className="rounded border border-[#f29a70]/35 px-1 text-[9px] text-[#f29a70]">并行</span> : null}
                      </span>
                      <span className="mt-1 block truncate text-[11px] text-white/40">{definition.label}</span>
                    </span>
                    <span className="flex shrink-0 flex-col items-end gap-1">
                      {locked ? <LockKeyhole className="h-3.5 w-3.5 text-white/30" aria-label="前置阶段未确认" /> : null}
                      <span className="text-[10px] text-white/45">{statusLabels[status] || status}</span>
                    </span>
                    <ChevronRight className="h-3.5 w-3.5 shrink-0 text-white/20 transition group-hover:translate-x-0.5" aria-hidden="true" />
                  </button>
                );
              })}
            </nav>
            <div className="mt-4 border-t border-white/10 px-2 pt-4 text-[11px] leading-5 text-white/35">
              <span className="font-bold text-white/55">5a / 5b</span> 会在分镜确认后并行推进。
            </div>
          </aside>

          <section className="min-w-0 space-y-5">
            <Card className="overflow-hidden border-line bg-paper">
              <div className="border-b border-line px-5 py-4 sm:px-6">
                <div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-start">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-xs font-bold uppercase tracking-[0.18em] text-vermilion">{selectedStageKey}</span>
                      <StatusBadge status={selectedStage.status} />
                      <GateBadge status={selectedGate.status} />
                    </div>
                    <h2 className="mt-2 text-xl font-extrabold tracking-[-0.03em]">{selectedDefinition.label}</h2>
                    <p className="mt-1 text-sm leading-6 text-muted">{selectedDefinition.description}</p>
                  </div>
                  <div className="flex shrink-0 items-center gap-2 text-xs text-muted">
                    <FileJson className="h-4 w-4" />
                    第 {selectedStage.attempt} 次尝试
                  </div>
                </div>
              </div>

              <div className="p-5 sm:p-6">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <h3 className="text-sm font-bold">产物预览</h3>
                    <p className="mt-1 text-xs text-muted">原始和规范化内容只读；草稿可编辑为 JSON 对象。</p>
                  </div>
                  <div className="inline-flex max-w-full gap-1 overflow-x-auto rounded-md border border-line bg-[#e9e6de] p-1" role="tablist" aria-label="产物视图">
                    {(["raw", "normalized", "draft"] as const).map((tab) => (
                      <button
                        type="button"
                        role="tab"
                        aria-selected={artifactTab === tab}
                        key={tab}
                        className={`whitespace-nowrap rounded px-3 py-2 text-xs font-bold transition ${artifactTab === tab ? "bg-paper text-ink shadow-sm" : "text-muted hover:text-ink"}`}
                        onClick={() => setArtifactTab(tab)}
                      >
                        {tab === "raw" ? "raw 原始" : tab === "normalized" ? "normalized 规范" : "draft 草稿"}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="mt-4">
                  {artifactTab === "draft" ? (
                    <Textarea
                      aria-label={`${selectedStageKey} draft JSON`}
                      aria-describedby={hasDirtyDraft ? "pipeline-draft-status" : "pipeline-action-status"}
                      value={draftText}
                      spellCheck={false}
                      className="min-h-[360px] rounded-lg border-[#c9c4b9] bg-[#fffefa] font-mono text-xs leading-5"
                      onChange={(event) => {
                        const value = event.target.value;
                        updateDraftText(value);
                        if (runId) {
                          const store = (draftStatesRef.current[runId] ||= {});
                          const previous = store[selectedStageKey];
                          const serverText = previous?.serverText || formatJson(selectedStage.draft_output);
                          store[selectedStageKey] = {
                            text: value,
                            serverText,
                            dirty: value !== serverText,
                            hasServerConflict: value !== serverText && Boolean(previous?.hasServerConflict),
                          };
                        }
                        if (draftParseError) setDraftParseError("");
                      }}
                      disabled={!canEdit}
                    />
                  ) : (
                    <pre aria-label={`${selectedStageKey} ${artifactTab} JSON`} className="min-h-[360px] max-h-[560px] overflow-auto rounded-lg border border-line bg-[#f2f0ea] p-4 font-mono text-xs leading-5 text-[#3e413c]">
                      {displaySnapshot(artifactValue)}
                    </pre>
                  )}
                </div>
                {draftParseError ? (
                  <p role="alert" className="mt-2 text-xs font-semibold text-[#a4362d]">{draftParseError}</p>
                ) : null}
                {hasDirtyDraft ? (
                  <p
                    id="pipeline-draft-status"
                    role="status"
                    aria-live="polite"
                    className="mt-3 rounded-md border border-[#dfb98f] bg-[#fff9f0] px-3 py-2 text-xs text-[#7b4b22]"
                  >
                    未保存更改。轮询或切换阶段时会保留当前草稿。
                  </p>
                ) : null}
                {selectedDraftState?.hasServerConflict ? (
                  <p role="status" aria-live="polite" className="mt-2 text-xs text-[#7b4b22]">
                    服务端草稿已更新，已保留本地未保存内容；保存前请确认。
                  </p>
                ) : null}
                {isLocked ? (
                  <p className="mt-3 flex items-center gap-2 rounded-md border border-[#dfb98f] bg-[#fff9f0] px-3 py-2 text-xs text-[#7b4b22]">
                    <LockKeyhole className="h-3.5 w-3.5" aria-hidden="true" />
                    前置阶段尚未确认，此阶段暂时锁定。
                  </p>
                ) : null}
                <div className="mt-4 flex flex-wrap justify-end gap-2">
                  <Button
                    type="button"
                    size="sm"
                    disabled={!canEdit || Boolean(actionBusy) || artifactTab !== "draft"}
                    aria-describedby="pipeline-action-status"
                    busy={actionBusy === "save-draft"}
                    onClick={() => void handleSaveDraft()}
                  >
                    <Save className="h-3.5 w-3.5" />
                    保存草稿（R{run.revision}）
                  </Button>
                </div>
              </div>
            </Card>

            <Card className="border-line bg-paper p-5 sm:p-6">
              <div className="flex items-center gap-2">
                <FileJson className="h-4 w-4 text-vermilion" aria-hidden="true" />
                <h3 className="text-sm font-bold">输入快照</h3>
                <span className="ml-auto text-xs text-muted">{run.source_kind === "full_text" ? "完整原文" : "剧情梗概"}</span>
              </div>
              <pre aria-label="source snapshot" className="mt-4 max-h-52 overflow-auto rounded-lg border border-line bg-[#f2f0ea] p-4 font-mono text-xs leading-5 text-[#3e413c]">
                {displaySnapshot(run.source_snapshot)}
              </pre>
              <details className="mt-4 rounded-lg border border-line bg-white px-3 py-2">
                <summary className="cursor-pointer text-xs font-bold text-ink">查看运行配置快照</summary>
                <pre className="mt-3 max-h-48 overflow-auto border-t border-line pt-3 font-mono text-xs leading-5 text-muted">
                  {displaySnapshot(run.config_snapshot)}
                </pre>
              </details>
            </Card>
          </section>

          <aside className="min-w-0 space-y-5">
            <Card className="border-line bg-paper p-5">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <p className="text-[10px] font-bold uppercase tracking-[0.18em] text-vermilion">Gate diagnostics</p>
                  <h2 className="mt-1 text-base font-bold">门禁诊断</h2>
                </div>
                <GateBadge status={selectedGate.status} />
              </div>
              <div className="mt-4 space-y-3">
                <DiagnosticList level="blocking" diagnostics={selectedGate.diagnostics} />
                <DiagnosticList level="warning" diagnostics={selectedGate.diagnostics} />
              </div>
              <details className="mt-4 rounded-lg border border-line bg-white px-3 py-2">
                <summary className="cursor-pointer text-xs font-bold">门禁结果（只读）</summary>
                <pre className="mt-3 max-h-44 overflow-auto border-t border-line pt-3 font-mono text-[11px] leading-5 text-muted">
                  {formatJson(selectedGate)}
                </pre>
              </details>
            </Card>

            <Card className="border-line bg-paper p-5">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <p className="text-[10px] font-bold uppercase tracking-[0.18em] text-vermilion">Actions</p>
                  <h2 className="mt-1 text-base font-bold">操作栏</h2>
                </div>
                <span className="text-xs text-muted">R{run.revision}</span>
              </div>
              <FailureSummary run={run} stages={workspace.stages} />
              <p id="pipeline-action-status" className="sr-only">
                {canStart ? "开始运行可用。" : `开始运行不可用，当前运行状态为${statusLabels[runStatus] || runStatus}。`}
                {canResume ? "恢复 / 重试可用。" : "当前运行没有可用的恢复 / 重试动作。"}
                {canConfirm ? "确认当前阶段可用。" : `确认当前阶段不可用：${confirmDisabledReason}。`}
                {canEdit ? "草稿编辑可用。" : "草稿编辑不可用，当前阶段或运行状态不允许编辑。"}
              </p>
              <div className="mt-4 grid grid-cols-2 gap-2">
                <Button
                  type="button"
                  size="sm"
                  variant="primary"
                  disabled={!canStart || Boolean(actionBusy)}
                  aria-describedby="pipeline-action-status"
                  busy={actionBusy === "start"}
                  onClick={() => void runOperation("start", (revision) => api.dynamicRuns.start(run.id, revision))}
                >
                  <Play className="h-3.5 w-3.5" />
                  开始运行
                </Button>
                <Button
                  type="button"
                  size="sm"
                  disabled={!canPause || Boolean(actionBusy)}
                  aria-describedby="pipeline-action-status"
                  busy={actionBusy === "pause"}
                  onClick={() => void runOperation("pause", (revision) => api.dynamicRuns.pause(run.id, revision))}
                >
                  <Pause className="h-3.5 w-3.5" />
                  暂停
                </Button>
                <Button
                  type="button"
                  size="sm"
                  disabled={!canResume || Boolean(actionBusy)}
                  aria-describedby="pipeline-action-status"
                  busy={actionBusy === "resume"}
                  onClick={() => void runOperation("resume", (revision) => api.dynamicRuns.resume(run.id, revision))}
                >
                  <RotateCcw className="h-3.5 w-3.5" />
                  恢复
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="danger"
                  disabled={!canCancel || Boolean(actionBusy)}
                  aria-describedby="pipeline-action-status"
                  busy={actionBusy === "cancel"}
                  onClick={() => void runOperation("cancel", (revision) => api.dynamicRuns.cancel(run.id, revision))}
                >
                  <Square className="h-3.5 w-3.5" />
                  取消
                </Button>
              </div>
              <div className="mt-4 border-t border-line pt-4">
                <Button
                  type="button"
                  className="w-full"
                  disabled={!canConfirm || Boolean(actionBusy) || isLocked}
                  aria-describedby="pipeline-action-status"
                  busy={actionBusy === "confirm"}
                  onClick={() => void handleConfirm()}
                  aria-label={`确认 ${selectedStageKey}`}
                >
                  <Check className="h-4 w-4" />
                  确认当前阶段
                </Button>
                <Button
                  type="button"
                  variant="secondary"
                  className="mt-2 w-full"
                  disabled={!canRegenerate || Boolean(actionBusy)}
                  aria-describedby="pipeline-action-status"
                  busy={actionBusy === "regenerate"}
                  onClick={() => void handleRegenerate()}
                >
                  <RotateCcw className="h-4 w-4" />
                  重新生成当前阶段
                </Button>
              </div>
              <p className="mt-4 text-[11px] leading-5 text-muted">
                保存、确认和重新生成都会携带当前 revision。门禁只读，最终确认仍由服务端判断。
              </p>
            </Card>

            {deliveryError ? (
              <div role="alert" className="rounded-lg border border-[#e1b3ad] bg-[#fff5f2] px-4 py-3 text-xs leading-5 text-[#9b342c]">{deliveryError}</div>
            ) : null}
            {selectedStageKey === "6-delivery" ? (
              <Card className="border-line bg-paper p-5">
                <DeliveryPanel
                  run={run}
                  checklist={checklist}
                  preflight={preflight}
                  loading={deliveryLoading}
                  partialExport={partialExport}
                  exportBusy={exportBusy}
                  onPartialExportChange={setPartialExport}
                  onPreflight={() => void handlePreflight()}
                  onExport={() => void handleExport()}
                />
              </Card>
            ) : null}

            <div className="rounded-lg border border-white/10 bg-[#1a2420] px-4 py-3 text-xs leading-5 text-white/45">
              当前阶段：<span className="font-bold text-[#f29a70]">{selectedStageKey}</span>
              <br />最后更新：{formatDate(selectedStage.updated_at)}
            </div>
          </aside>
        </div>
      </main>
    </div>
  );
}
