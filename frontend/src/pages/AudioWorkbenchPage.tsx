import { useEffect, useState } from "react";
import { useParams } from "react-router";
import { api } from "../api/client";
import { formatError } from "../lib/utils";
import type { Chapter, ApiProvider } from "../types/api";

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

export default function AudioWorkbenchPage() {
  const { projectId, chapterId } = useParams<{ projectId: string; chapterId: string }>();
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

  useEffect(() => {
    if (!projectId || !chapterId) return;
    setLoadError(null);
    Promise.all([
      api.chapters.get(chapterId),
      api.audio.extractDialogues(chapterId),
      api.providers.list("tts"),
      api.audio.getConfig(projectId),
      api.audio.getEdgeTTSVoices(),
      api.audio.getChapterAudios(chapterId),
    ]).then(([chapterData, dialoguesData, providersData, configData, edgeVoicesData, chapterAudiosData]) => {
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

      // 恢复本机保存的音色映射（覆盖默认值）
      const savedMapping = localStorage.getItem(`audio-voice-mapping-${projectId}`);
      if (savedMapping) {
        try {
          const parsed = JSON.parse(savedMapping) as unknown;
          if (parsed && typeof parsed === "object") {
            for (const [speaker, voice] of Object.entries(
              parsed as Record<string, unknown>,
            )) {
              if (typeof voice === "string") {
                defaultMapping[speaker] = voice;
              }
            }
          }
        } catch {
          // 忽略损坏的本地缓存
        }
      }
      setVoiceMapping(defaultMapping);
    }).catch((error) => {
      console.error(error);
      setLoadError(formatError(error));
    });
  }, [projectId, chapterId]);

  const persistVoiceMapping = (next: Record<string, string>) => {
    setVoiceMapping(next);
    localStorage.setItem(`audio-voice-mapping-${projectId}`, JSON.stringify(next));
  };

  const handleDeleteAudio = async (dialogue: Dialogue) => {
    const audio = audioStatus[dialogue.shot_no];
    if (!audio?.id) return;
    if (!confirm(`确定删除分镜 ${dialogue.shot_no} 的配音？`)) return;

    try {
      await api.audio.deleteShotAudio(audio.id);
      setAudioStatus(prev => {
        const next = { ...prev };
        delete next[dialogue.shot_no];
        return next;
      });
    } catch (error) {
      console.error(error);
      alert(`删除失败：${error instanceof Error ? error.message : "未知错误"}`);
    }
  };

  const handleGenerateSingle = async (dialogue: Dialogue) => {
    const voiceId = voiceMapping[dialogue.speaker];

    if (!voiceId) {
      alert("请为该角色选择音色");
      return;
    }

    // 检查是否需要 Provider
    const isEdgeTTS = voiceId.startsWith("edge-tts:");
    if (!isEdgeTTS && !selectedProvider) {
      alert("请先选择 TTS Provider");
      return;
    }

    setGenerating(prev => new Set(prev).add(dialogue.shot_no));

    try {
      // 查询 shot_id
      const shots = await api.shots.list(chapterId!);
      const shot = shots.find(s => s.shot_no === dialogue.shot_no);
      if (!shot) {
        alert(`未找到分镜 ${dialogue.shot_no}`);
        return;
      }

      const result = await api.audio.generateShotAudio(shot.id, {
        dialogue_text: dialogue.text,
        voice_id: voiceId,
        tts_provider_id: isEdgeTTS ? undefined : selectedProvider,
      });

      setAudioStatus(prev => ({
        ...prev,
        [dialogue.shot_no]: result,
      }));

      alert(`分镜 ${dialogue.shot_no} 配音生成成功`);
    } catch (error) {
      console.error(error);
      alert(`生成失败：${error instanceof Error ? error.message : "未知错误"}`);
    } finally {
      setGenerating(prev => {
        const newSet = new Set(prev);
        newSet.delete(dialogue.shot_no);
        return newSet;
      });
    }
  };

  const handleBatchGenerate = async () => {
    setLoading(true);
    setBatchProgress({ done: 0, total: dialogues.length });

    try {
      const shots = await api.shots.list(chapterId!);
      const failures: string[] = [];
      const skipped: string[] = [];
      let done = 0;

      for (const dialogue of dialogues) {
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

      const summary =
        `批量生成完成：成功 ${done - failures.length} 条，失败 ${failures.length} 条` +
        (skipped.length ? `，跳过 ${skipped.length} 条` : "");
      const details = [
        ...failures.map(item => `失败：${item}`),
        ...skipped.map(item => `跳过：${item}`),
      ];
      alert(
        details.length === 0
          ? `批量生成完成：成功 ${done} 条`
          : `${summary}\n\n${details.join("\n")}`,
      );
    } catch (error) {
      console.error(error);
      alert(`批量生成失败：${error instanceof Error ? error.message : "未知错误"}`);
    } finally {
      setLoading(false);
      setBatchProgress(null);
    }
  };

  const switchTtsMode = (mode: "edge" | "provider") => {
    setTtsMode(mode);
    setVoiceMapping(prev => {
      const next: Record<string, string> = {};
      const fallbackEdgeVoice = edgeTTSVoices[0]?.name;
      for (const [speaker, current] of Object.entries(prev)) {
        const isEdge = current.startsWith("edge-tts:");
        if (mode === "edge") {
          next[speaker] = isEdge ? current : fallbackEdgeVoice ? `edge-tts:${fallbackEdgeVoice}` : current;
        } else {
          next[speaker] = isEdge ? OPENAI_VOICES[0].id : current;
        }
      }
      localStorage.setItem(`audio-voice-mapping-${projectId}`, JSON.stringify(next));
      return next;
    });
  };

  const uniqueSpeakers = Array.from(new Set(dialogues.map(d => d.speaker)));

  return (
    <div className="p-6 max-w-7xl mx-auto">
      <div className="mb-6">
        <h1 className="text-2xl font-bold mb-2">配音工作台</h1>
        <p className="text-gray-600">
          章节：{chapter?.title || `第 ${chapter?.chapter_no} 章`}
        </p>
      </div>

      {loadError ? (
        <div
          role="alert"
          className="mb-6 rounded border border-red-200 bg-red-50 p-4 text-sm text-red-700"
        >
          配音工作台加载失败：{loadError}
        </div>
      ) : null}
      {/* 配置面板 */}
      <div className="bg-white rounded-lg shadow p-4 mb-6">
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
                <span className="text-xs text-gray-500 ml-1">微软提供，音质优秀</span>
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
                <span className="text-xs text-gray-500 ml-1">需要配置 API Key</span>
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
                className="w-full border rounded px-3 py-2"
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
                  className="flex-1 border rounded px-2 py-1 text-sm"
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
          className="bg-blue-500 text-white px-6 py-2 rounded hover:bg-blue-600 disabled:bg-gray-300 disabled:cursor-not-allowed"
        >
          {loading
            ? batchProgress
              ? `批量生成中 ${batchProgress.done}/${batchProgress.total}...`
              : "批量生成中..."
            : "批量生成全部配音"}
        </button>
      </div>

      {/* 对话列表 */}
      <div className="bg-white rounded-lg shadow">
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
              <div key={dialogue.shot_no} className="p-4 hover:bg-gray-50">
                <div className="flex items-start justify-between gap-4">
                  <div className="flex-1">
                    <div className="flex items-center gap-2 mb-2">
                      <span className="text-sm font-medium text-gray-500">
                        分镜 {dialogue.shot_no}
                      </span>
                      <span className="text-sm px-2 py-0.5 bg-blue-100 text-blue-700 rounded">
                        {dialogue.speaker}
                      </span>
                      <span className="text-xs text-gray-400">
                        → {voiceName}
                      </span>
                      {isEdgeTTS && (
                        <span className="text-xs px-1.5 py-0.5 bg-green-100 text-green-700 rounded">
                          免费
                        </span>
                      )}
                    </div>
                    <p className="text-gray-800">{dialogue.text}</p>

                    {audioStatus[dialogue.shot_no] && (
                      <div className="mt-2 text-sm">
                        <span className={`px-2 py-0.5 rounded ${
                          audioStatus[dialogue.shot_no].status === "completed"
                            ? "bg-green-100 text-green-700"
                            : "bg-yellow-100 text-yellow-700"
                        }`}>
                          {audioStatus[dialogue.shot_no].status === "completed" ? "已生成" : "生成中"}
                        </span>
                        {audioStatus[dialogue.shot_no].duration_sec && (
                          <span className="ml-2 text-gray-500">
                            时长: {audioStatus[dialogue.shot_no].duration_sec?.toFixed(1)}s
                          </span>
                        )}
                        {audioStatus[dialogue.shot_no].file_path && (
                          <audio
                            controls
                            preload="none"
                            src={`/media/${projectId}/${audioStatus[dialogue.shot_no].file_path}`}
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
                      className="px-4 py-2 bg-green-500 text-white rounded hover:bg-green-600 disabled:bg-gray-300 disabled:cursor-not-allowed text-sm"
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
            <div className="p-8 text-center text-gray-500">
              <p>该章节暂无对话</p>
              <p className="text-sm mt-1">请先在"文字工作台"确认剧本</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
