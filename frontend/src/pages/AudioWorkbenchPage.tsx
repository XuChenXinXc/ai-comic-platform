import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router";
import { toast } from "sonner";
import { api, mediaUrl } from "../api/client";
import { PageHeader } from "../components/status";
import { Button, Modal } from "../components/ui";
import { formatError } from "../lib/utils";
import type { Chapter, ApiProvider, MediaVersion } from "../types/api";

interface Dialogue {
  shot_no: number;
  speaker: string;
  text: string;
}

interface ShotAudio {
  id: string;
  shot_id: string;
  dialogue_text: string;
  voice_id: string;
  file_path: string | null;
  duration_sec: number | null;
  status: string;
}

interface EdgeTTSVoice {
  name: string;
  display_name: string;
  gender: string;
  description: string;
}

const OPENAI_VOICES = [
  { id: "alloy", name: "Alloy（中性）" },
  { id: "echo", name: "Echo（男性）" },
  { id: "fable", name: "Fable（英式男性）" },
  { id: "onyx", name: "Onyx（低沉男性）" },
  { id: "nova", name: "Nova（女性）" },
  { id: "shimmer", name: "Shimmer（柔和女性）" },
];

// 过滤映射中的空键/空值/非字符串项，保证写入服务端的键值均为非空字符串。
function sanitizeVoiceMapping(mapping: Record<string, string>): Record<string, string> {
  const next: Record<string, string> = {};
  for (const [speaker, voice] of Object.entries(mapping)) {
    if (speaker && typeof voice === "string" && voice) {
      next[speaker] = voice;
    }
  }
  return next;
}

export default function AudioWorkbenchPage() {
  const navigate = useNavigate();
  const { projectId, chapterId } = useParams<{ projectId: string; chapterId: string }>();
  // 组件挂载状态：卸载后停止批量生成循环与状态更新，避免对已卸载组件 setState。
  const mountedRef = useRef(true);
  // 章节实时镜像：在途生成/删除响应落地前校验章节未变，
  // 防止切换到新章节后把旧章节结果写入同名分镜。
  const chapterIdRef = useRef(chapterId);
  chapterIdRef.current = chapterId;
  // 响应落地守卫：组件仍挂载且章节未切换时才允许写入状态。
  const canCommit = () => mountedRef.current && chapterIdRef.current === chapterId;
  const [chapter, setChapter] = useState<Chapter | null>(null);
  const [dialogues, setDialogues] = useState<Dialogue[]>([]);
  const [ttsProviders, setTtsProviders] = useState<ApiProvider[]>([]);
  const [edgeTTSVoices, setEdgeTTSVoices] = useState<EdgeTTSVoice[]>([]);
  const [ttsMode, setTtsMode] = useState<"edge" | "provider">("edge"); // 默认使用 Edge TTS
  const [selectedProvider, setSelectedProvider] = useState<string>("");
  const [voiceMapping, setVoiceMapping] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(false);
  const [generating, setGenerating] = useState<Set<number>>(new Set());
  const [audioStatus, setAudioStatus] = useState<Record<number, ShotAudio>>({});
  const [batchProgress, setBatchProgress] = useState<{ done: number; total: number } | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  // 待确认删除的对话（配音删除确认弹窗）
  const [confirmDeleteDialogue, setConfirmDeleteDialogue] = useState<Dialogue | null>(null);
  // 批量生成结果面板（失败/跳过明细较长时用 Modal 展示）
  const [batchResult, setBatchResult] = useState<{ summary: string; details: string[] } | null>(null);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // 存在非终态配音（排队/生成中）时每 2s 轮询章节配音状态，
  // 避免异步产出后状态徽标停留在「生成中」。
  const hasPendingAudio = Object.values(audioStatus).some(
    audio => !!audio && !["completed", "failed"].includes(audio.status),
  );
  useEffect(() => {
    if (!hasPendingAudio || !chapterId) return;
    let cancelled = false;
    const timer = window.setInterval(() => {
      api.audio
        .getChapterAudios(chapterId)
        .then(items => {
          if (cancelled || !mountedRef.current) return;
          setAudioStatus(prev => {
            const updates: Record<number, ShotAudio> = {};
            for (const item of items) {
              if (!item.audio) continue;
              const prevAudio = prev[item.shot_no];
              const unchanged =
                prevAudio &&
                prevAudio.status === item.audio.status &&
                prevAudio.file_path === item.audio.file_path &&
                prevAudio.duration_sec === item.audio.duration_sec;
              if (!unchanged) {
                updates[item.shot_no] = item.audio as ShotAudio;
              }
            }
            if (Object.keys(updates).length === 0) return prev;
            return { ...prev, ...updates };
          });
        })
        .catch(() => {
          // 轮询失败静默忽略，下一轮重试
        });
    }, 2000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [chapterId, hasPendingAudio]);

  useEffect(() => {
    if (!projectId || !chapterId) return;
    // 竞态防护：切换章节/卸载后丢弃旧响应，避免旧章节数据覆盖新章节。
    let cancelled = false;
    setLoadError(null);
    setGenerating(new Set());
    setLoading(false);
    setBatchProgress(null);
    Promise.all([
      api.chapters.get(chapterId),
      api.audio.extractDialogues(chapterId),
      api.providers.list("tts"),
      api.audio.getConfig(projectId),
      api.audio.getEdgeTTSVoices(),
      api.audio.getChapterAudios(chapterId),
    ]).then(([chapterData, dialoguesData, providersData, configData, edgeVoicesData, chapterAudiosData]) => {
      if (cancelled) return;
      setChapter(chapterData);
      setDialogues(dialoguesData);
      setTtsProviders(providersData);
      setEdgeTTSVoices(edgeVoicesData);
      setAudioStatus(chapterAudiosData.reduce<Record<number, ShotAudio>>((acc, item) => {
        if (item.audio) {
          acc[item.shot_no] = item.audio as ShotAudio;
        }
        return acc;
      }, {}));

      if (configData.tts_provider_id) {
        setSelectedProvider(configData.tts_provider_id);
        setTtsMode("provider");
      } else if (providersData.length > 0) {
        setSelectedProvider(providersData[0].id);
      }

      // 为每个说话人设置默认音色
      const uniqueSpeakers = Array.from(new Set(dialoguesData.map(d => d.speaker)));
      const defaultMapping: Record<string, string> = {};

      // 根据模式设置默认音色
      if (edgeVoicesData.length > 0 && !configData.tts_provider_id) {
        // 优先使用 Edge TTS
        uniqueSpeakers.forEach((speaker, idx) => {
          defaultMapping[speaker] = `edge-tts:${edgeVoicesData[idx % edgeVoicesData.length].name}`;
        });
      } else {
        // 回退到 OpenAI 音色
        uniqueSpeakers.forEach((speaker, idx) => {
          defaultMapping[speaker] = OPENAI_VOICES[idx % OPENAI_VOICES.length].id;
        });
      }

      // 恢复服务端持久化的音色映射（覆盖默认值）
      for (const [speaker, voice] of Object.entries(configData.voice_mapping || {})) {
        if (typeof voice === "string" && voice) {
          defaultMapping[speaker] = voice;
        }
      }

      // 一次性迁移旧版 localStorage 音色映射：服务端映射为空且本地有值时写入服务端，成功后删除本地键。
      const legacyMappingRaw = localStorage.getItem(`audio-voice-mapping-${projectId}`);
      if (legacyMappingRaw && Object.keys(configData.voice_mapping || {}).length === 0) {
        let hasLegacyValues = false;
        try {
          const parsed = JSON.parse(legacyMappingRaw) as unknown;
          if (parsed && typeof parsed === "object") {
            for (const [speaker, voice] of Object.entries(
              parsed as Record<string, unknown>,
            )) {
              if (typeof voice === "string" && voice) {
                defaultMapping[speaker] = voice;
                hasLegacyValues = true;
              }
            }
          }
        } catch {
          // 忽略损坏的本地缓存
        }
        if (hasLegacyValues) {
          void api.audio
            .updateConfig(projectId, { voice_mapping: sanitizeVoiceMapping(defaultMapping) })
            .then(() => {
              // 迁移成功后移除旧本地键；失败时保留，下次进入页面重试。
              localStorage.removeItem(`audio-voice-mapping-${projectId}`);
            })
            .catch(() => {
              // 迁移失败静默保留本地键，避免提示打断正常使用
            });
        }
      }
      setVoiceMapping(defaultMapping);
    }).catch((error) => {
      if (cancelled) return;
      console.error(error);
      setLoadError(formatError(error));
    });
    return () => {
      cancelled = true;
    };
  }, [projectId, chapterId]);

  // 更新本地映射并持久化到服务端；保存失败仅提示，不回滚本地即时状态。
  const persistVoiceMapping = (next: Record<string, string>) => {
    setVoiceMapping(next);
    if (!projectId) return;
    api.audio
      .updateConfig(projectId, { voice_mapping: sanitizeVoiceMapping(next) })
      .catch((error) => {
        if (!canCommit()) return;
        console.error(error);
        toast.error(`音色映射保存失败：${formatError(error)}`);
      });
  };

  const handleDeleteAudio = (dialogue: Dialogue) => {
    const audio = audioStatus[dialogue.shot_no];
    if (!audio?.id) return;
    setConfirmDeleteDialogue(dialogue);
  };

  const handleConfirmDeleteAudio = async () => {
    const dialogue = confirmDeleteDialogue;
    if (!dialogue) return;
    const audio = audioStatus[dialogue.shot_no];
    setConfirmDeleteDialogue(null);
    if (!audio?.id) return;

    try {
      await api.audio.deleteShotAudio(audio.id);
      if (!canCommit()) return;
      setAudioStatus(prev => {
        const next = { ...prev };
        delete next[dialogue.shot_no];
        return next;
      });
      toast.success(`分镜 ${dialogue.shot_no} 配音已删除`);
    } catch (error) {
      if (!canCommit()) return;
      console.error(error);
      toast.error(`删除失败：${formatError(error)}`);
    }
  };

  const handleGenerateSingle = async (dialogue: Dialogue) => {
    const voiceId = voiceMapping[dialogue.speaker];

    if (!voiceId) {
      toast.error("请为该角色选择音色");
      return;
    }

    // 检查是否需要 Provider
    const isEdgeTTS = voiceId.startsWith("edge-tts:");
    if (!isEdgeTTS && !selectedProvider) {
      toast.error("请先选择 TTS Provider");
      return;
    }

    setGenerating(prev => new Set(prev).add(dialogue.shot_no));

    try {
      // 查询 shot_id
      const shots = await api.shots.list(chapterId!);
      if (!canCommit()) return;
      const shot = shots.find(s => s.shot_no === dialogue.shot_no);
      if (!shot) {
        toast.error(`未找到分镜 ${dialogue.shot_no}`);
        return;
      }

      const result = await api.audio.generateShotAudio(shot.id, {
        dialogue_text: dialogue.text,
        voice_id: voiceId,
        tts_provider_id: isEdgeTTS ? undefined : selectedProvider,
      });

      if (!canCommit()) return;
      setAudioStatus(prev => ({
        ...prev,
        [dialogue.shot_no]: result,
      }));

      toast.success(`分镜 ${dialogue.shot_no} 配音生成成功`);
    } catch (error) {
      if (!canCommit()) return;
      console.error(error);
      toast.error(`生成失败：${formatError(error)}`);
    } finally {
      if (canCommit()) {
        setGenerating(prev => {
          const newSet = new Set(prev);
          newSet.delete(dialogue.shot_no);
          return newSet;
        });
      }
    }
  };

  const handleBatchGenerate = async () => {
    setLoading(true);
    setBatchProgress({ done: 0, total: dialogues.length });

    try {
      const shots = await api.shots.list(chapterId!);
      if (!canCommit()) return;
      const failures: string[] = [];
      const skipped: string[] = [];
      let done = 0;

      for (const dialogue of dialogues) {
        if (!canCommit()) return; // 用户已离开页面：停止批量生成
        const shot = shots.find(s => s.shot_no === dialogue.shot_no);
        if (!shot) {
          skipped.push(`分镜 ${dialogue.shot_no}：找不到分镜`);
          done += 1;
          setBatchProgress({ done, total: dialogues.length });
          continue;
        }

        const voiceId = voiceMapping[dialogue.speaker];
        if (!voiceId) {
          skipped.push(`分镜 ${dialogue.shot_no}（${dialogue.speaker}）：未选择音色`);
          done += 1;
          setBatchProgress({ done, total: dialogues.length });
          continue;
        }

        const isEdgeTTS = voiceId.startsWith("edge-tts:");
        if (!isEdgeTTS && !selectedProvider) {
          skipped.push(
            `分镜 ${dialogue.shot_no}（${dialogue.speaker}）：需要先选择 TTS Provider`,
          );
          done += 1;
          setBatchProgress({ done, total: dialogues.length });
          continue;
        }

        try {
          const result = await api.audio.generateShotAudio(shot.id, {
            dialogue_text: dialogue.text,
            voice_id: voiceId,
            tts_provider_id: isEdgeTTS ? undefined : selectedProvider,
          });

          if (!canCommit()) return;
          setAudioStatus(prev => ({
            ...prev,
            [dialogue.shot_no]: result,
          }));
        } catch (error) {
          console.error(`分镜 ${dialogue.shot_no} 生成失败:`, error);
          failures.push(`分镜 ${dialogue.shot_no}：${error instanceof Error ? error.message : "未知错误"}`);
        }
        done += 1;
        setBatchProgress({ done, total: dialogues.length });
      }

      if (!canCommit()) return;
      const summary =
        `批量生成完成：成功 ${done - failures.length} 条，失败 ${failures.length} 条` +
        (skipped.length ? `，跳过 ${skipped.length} 条` : "");
      const details = [
        ...failures.map(item => `失败：${item}`),
        ...skipped.map(item => `跳过：${item}`),
      ];
      if (details.length === 0) {
        toast.success(`批量生成完成：成功 ${done} 条`);
      } else {
        // 失败/跳过明细较长，用结果面板展示而不是 alert。
        setBatchResult({ summary, details });
      }
    } catch (error) {
      if (!canCommit()) return;
      console.error(error);
      toast.error(`批量生成失败：${formatError(error)}`);
    } finally {
      if (canCommit()) {
        setLoading(false);
        setBatchProgress(null);
      }
    }
  };

  const switchTtsMode = (mode: "edge" | "provider") => {
    setTtsMode(mode);
    // 切换模式时换算音色体系（Edge ↔ OpenAI），结果同步保存到服务端。
    const next: Record<string, string> = {};
    const fallbackEdgeVoice = edgeTTSVoices[0]?.name;
    for (const [speaker, current] of Object.entries(voiceMapping)) {
      const isEdge = current.startsWith("edge-tts:");
      if (mode === "edge") {
        next[speaker] = isEdge ? current : fallbackEdgeVoice ? `edge-tts:${fallbackEdgeVoice}` : current;
      } else {
        next[speaker] = isEdge ? OPENAI_VOICES[0].id : current;
      }
    }
    persistVoiceMapping(next);
  };

  const uniqueSpeakers = Array.from(new Set(dialogues.map(d => d.speaker)));

  return (
    <div className="mx-auto max-w-7xl">
      <PageHeader
        eyebrow="Step 06 / Audio"
        title="配音工作台"
        description={`章节：${chapter?.title || `第 ${chapter?.chapter_no} 章`}`}
      />

      {loadError ? (
        <div
          role="alert"
          className="mb-6 flex flex-col gap-3 rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900 sm:flex-row sm:items-center sm:justify-between dark:border-amber-500/25 dark:bg-amber-500/10 dark:text-amber-200"
        >
          <div>
            <p className="font-semibold">配音工作台加载提示：{loadError}</p>
            <p className="mt-1 text-xs text-amber-800/80 dark:text-amber-300/80">
              角色配音依赖章节剧本提取的对话台词。请先在【剧本生成】页面生成剧本；已生成的剧本可点击【确认剧本】冻结。
            </p>
          </div>
          <Button
            size="sm"
            variant="primary"
            className="shrink-0"
            onClick={() => navigate("/writing")}
          >
            前往剧本生成
          </Button>
        </div>
      ) : null}
      {/* 配置面板 */}
      <div className="bg-paper rounded-card border border-black/[0.06] shadow-card p-5 mb-6 dark:border-white/10 dark:bg-[#101010]">
        <h2 className="text-lg font-semibold mb-4">配置</h2>

        {/* TTS 模式选择 */}
        <div className="mb-4">
          <label className="block text-sm font-medium mb-2">TTS 模式</label>
          <div className="flex gap-4">
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="radio"
                value="edge"
                checked={ttsMode === "edge"}
                onChange={(e) => switchTtsMode(e.target.value as "edge" | "provider")}
                className="w-4 h-4"
              />
              <span className="text-sm">
                Edge TTS（免费，推荐）
                <span className="text-xs text-muted ml-1">微软提供，音质优秀</span>
              </span>
            </label>
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="radio"
                value="provider"
                checked={ttsMode === "provider"}
                onChange={(e) => switchTtsMode(e.target.value as "edge" | "provider")}
                className="w-4 h-4"
              />
              <span className="text-sm">
                自定义 Provider
                <span className="text-xs text-muted ml-1">需要配置 API Key</span>
              </span>
            </label>
          </div>
        </div>

        {/* Provider 选择（仅在 provider 模式下显示） */}
        {ttsMode === "provider" && (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-4">
            <div>
              <label className="block text-sm font-medium mb-2">TTS Provider</label>
              <select
                value={selectedProvider}
                onChange={(e) => setSelectedProvider(e.target.value)}
                className="h-11 w-full rounded-2xl border border-black/10 bg-black/[0.02] px-4 text-sm text-ink outline-none transition focus:border-vermilion focus:ring-2 focus:ring-vermilion/15 dark:border-white/10 dark:bg-white/5"
              >
                <option value="">请选择</option>
                {ttsProviders.map(provider => (
                  <option key={provider.id} value={provider.id}>
                    {provider.name} ({provider.adapter})
                  </option>
                ))}
              </select>
              {ttsProviders.length === 0 && (
                <p className="text-sm text-red-500 mt-1">
                  未配置 TTS Provider，请前往"接口配置"页面添加
                </p>
              )}
            </div>
          </div>
        )}

        <div className="mb-4">
          <label className="block text-sm font-medium mb-2">角色音色分配</label>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {uniqueSpeakers.map(speaker => (
              <div key={speaker} className="flex items-center gap-2">
                <span className="text-sm w-24 truncate font-medium">{speaker}:</span>
                <select
                  value={voiceMapping[speaker] || (ttsMode === "edge" ? `edge-tts:${edgeTTSVoices[0]?.name}` : "alloy")}
                  onChange={(e) => persistVoiceMapping({
                    ...voiceMapping,
                    [speaker]: e.target.value,
                  })}
                  className="flex-1 h-11 rounded-2xl border border-black/10 bg-black/[0.02] px-4 text-sm text-ink outline-none transition focus:border-vermilion focus:ring-2 focus:ring-vermilion/15 dark:border-white/10 dark:bg-white/5"
                >
                  {ttsMode === "edge" ? (
                    // Edge TTS 声音列表
                    edgeTTSVoices.map(voice => (
                      <option key={voice.name} value={`edge-tts:${voice.name}`}>
                        {voice.display_name}
                      </option>
                    ))
                  ) : (
                    // OpenAI 声音列表
                    OPENAI_VOICES.map(voice => (
                      <option key={voice.id} value={voice.id}>
                        {voice.name}
                      </option>
                    ))
                  )}
                </select>
              </div>
            ))}
          </div>
          {ttsMode === "edge" && edgeTTSVoices.length === 0 && (
            <p className="text-sm text-yellow-600 mt-2">
              正在加载 Edge TTS 声音列表...
            </p>
          )}
        </div>

        <button
          onClick={handleBatchGenerate}
          disabled={loading || generating.size > 0 || (ttsMode === "provider" && !selectedProvider) || dialogues.length === 0}
          className="bg-vermilion text-white px-6 py-2 rounded-full hover:bg-brand-hover disabled:bg-black/10 dark:disabled:bg-white/10 disabled:cursor-not-allowed"
        >
          {loading
            ? batchProgress
              ? `批量生成中 ${batchProgress.done}/${batchProgress.total}...`
              : "批量生成中..."
            : "批量生成全部配音"}
        </button>
      </div>

      {/* 对话列表 */}
      <div className="bg-paper rounded-card border border-black/[0.06] shadow-card dark:border-white/10 dark:bg-[#101010]">
        <div className="p-4 border-b">
          <h2 className="text-lg font-semibold">对话列表 ({dialogues.length})</h2>
        </div>

        <div className="divide-y">
          {dialogues.map((dialogue) => {
            const voiceId = voiceMapping[dialogue.speaker];
            const isEdgeTTS = voiceId?.startsWith("edge-tts:");
            const voiceName = isEdgeTTS
              ? edgeTTSVoices.find(v => voiceId === `edge-tts:${v.name}`)?.display_name || "未知"
              : OPENAI_VOICES.find(v => v.id === voiceId)?.name || "未知";

            return (
              <div key={dialogue.shot_no} className="p-4 hover:bg-black/5 dark:hover:bg-white/10">
                <div className="flex items-start justify-between gap-4">
                  <div className="flex-1">
                    <div className="flex items-center gap-2 mb-2">
                      <span className="text-sm font-medium text-muted">
                        分镜 {dialogue.shot_no}
                      </span>
                      <span className="text-xs font-semibold px-2.5 py-1 bg-sky-50 text-sky-700 rounded-full dark:bg-sky-500/10 dark:text-sky-300">
                        {dialogue.speaker}
                      </span>
                      <span className="text-xs text-muted">
                        → {voiceName}
                      </span>
                      {isEdgeTTS && (
                        <span className="text-xs px-1.5 py-0.5 bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-300 rounded">
                          免费
                        </span>
                      )}
                    </div>
                    <p className="text-ink">{dialogue.text}</p>

                    {audioStatus[dialogue.shot_no] && (
                      <div className="mt-2 text-sm">
                        <span className={`px-2.5 py-1 rounded-full text-xs font-semibold ${
                          audioStatus[dialogue.shot_no].status === "completed"
                            ? "bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-300"
                            : "bg-amber-50 text-amber-700 dark:bg-amber-500/10 dark:text-amber-300"
                        }`}>
                          {audioStatus[dialogue.shot_no].status === "completed" ? "已生成" : "生成中"}
                        </span>
                        {audioStatus[dialogue.shot_no].duration_sec && (
                          <span className="ml-2 text-muted">
                            时长: {audioStatus[dialogue.shot_no].duration_sec?.toFixed(1)}s
                          </span>
                        )}
                        {audioStatus[dialogue.shot_no].file_path && (
                          <audio
                            controls
                            preload="none"
                            src={mediaUrl({
                              file_path: `${projectId}/${audioStatus[dialogue.shot_no].file_path}`,
                            } as MediaVersion)}
                            className="mt-2 h-8 w-full max-w-md"
                          />
                        )}
                      </div>
                    )}
                  </div>

                  <div className="flex flex-col items-end gap-2">
                    <button
                      onClick={() => handleGenerateSingle(dialogue)}
                      disabled={generating.has(dialogue.shot_no) || (ttsMode === "provider" && !selectedProvider)}
                      className="px-4 py-2 bg-emerald-500 text-white rounded-full hover:bg-emerald-600 disabled:bg-black/10 dark:disabled:bg-white/10 disabled:cursor-not-allowed text-sm"
                    >
                      {generating.has(dialogue.shot_no)
                        ? "生成中..."
                        : audioStatus[dialogue.shot_no]?.file_path
                          ? "重新生成"
                          : "生成配音"}
                    </button>
                    {audioStatus[dialogue.shot_no]?.id && (
                      <button
                        onClick={() => handleDeleteAudio(dialogue)}
                        className="text-sm text-red-500 hover:text-red-600"
                      >
                        删除配音
                      </button>
                    )}
                  </div>
                </div>
              </div>
            );
          })}

          {dialogues.length === 0 && (
            <div className="p-8 text-center text-muted">
              <p>该章节暂无对话</p>
              <p className="text-sm mt-1">请先在"文字工作台"确认剧本</p>
            </div>
          )}
        </div>
      </div>

      {/* 删除配音确认弹窗 */}
      <Modal
        open={confirmDeleteDialogue !== null}
        onClose={() => setConfirmDeleteDialogue(null)}
        title="删除配音"
        description={
          confirmDeleteDialogue
            ? `确定删除分镜 ${confirmDeleteDialogue.shot_no} 的配音？`
            : undefined
        }
        footer={
          <>
            <button
              type="button"
              onClick={() => setConfirmDeleteDialogue(null)}
              className="px-4 py-2 rounded-full border border-line bg-paper text-sm hover:bg-black/5 dark:hover:bg-white/10"
            >
              取消
            </button>
            <button
              type="button"
              onClick={() => void handleConfirmDeleteAudio()}
              className="px-4 py-2 rounded-full bg-red-500 text-white text-sm hover:bg-red-600"
            >
              删除
            </button>
          </>
        }
      >
        <p className="text-sm text-muted">删除后需要重新生成配音。</p>
      </Modal>

      {/* 批量生成结果面板 */}
      <Modal
        open={batchResult !== null}
        onClose={() => setBatchResult(null)}
        title="批量生成结果"
      >
        {batchResult ? (
          <div>
            <p className="text-sm font-medium mb-3">{batchResult.summary}</p>
            <ul className="text-sm text-muted space-y-1 max-h-80 overflow-y-auto">
              {batchResult.details.map((item, idx) => (
                <li key={idx}>{item}</li>
              ))}
            </ul>
          </div>
        ) : null}
      </Modal>
    </div>
  );
}
