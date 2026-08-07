import { useEffect, useState } from "react";
import { useParams } from "react-router";
import { api } from "../api/client";
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
  const [selectedProvider, setSelectedProvider] = useState<string>("");
  const [voiceMapping, setVoiceMapping] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(false);
  const [generating, setGenerating] = useState<Set<number>>(new Set());
  const [audioStatus, setAudioStatus] = useState<Record<number, ShotAudio>>({});

  useEffect(() => {
    if (!projectId || !chapterId) return;

    Promise.all([
      api.chapters.get(chapterId),
      api.audio.extractDialogues(chapterId),
      api.providers.list("tts"),
      api.audio.getConfig(projectId),
    ]).then(([chapterData, dialoguesData, providersData, configData]) => {
      setChapter(chapterData);
      setDialogues(dialoguesData);
      setTtsProviders(providersData);
      if (configData.tts_provider_id) {
        setSelectedProvider(configData.tts_provider_id);
      } else if (providersData.length > 0) {
        setSelectedProvider(providersData[0].id);
      }

      // 为每个说话人设置默认音色
      const uniqueSpeakers = Array.from(new Set(dialoguesData.map(d => d.speaker)));
      const defaultMapping: Record<string, string> = {};
      uniqueSpeakers.forEach((speaker, idx) => {
        defaultMapping[speaker] = OPENAI_VOICES[idx % OPENAI_VOICES.length].id;
      });
      setVoiceMapping(defaultMapping);
    });
  }, [projectId, chapterId]);

  const handleGenerateSingle = async (dialogue: Dialogue) => {
    if (!selectedProvider) {
      alert("请先选择 TTS Provider");
      return;
    }

    const voiceId = voiceMapping[dialogue.speaker] || "alloy";
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
        tts_provider_id: selectedProvider,
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
    if (!selectedProvider) {
      alert("请先选择 TTS Provider");
      return;
    }

    setLoading(true);

    try {
      const shots = await api.shots.list(chapterId!);

      for (const dialogue of dialogues) {
        const shot = shots.find(s => s.shot_no === dialogue.shot_no);
        if (!shot) continue;

        const voiceId = voiceMapping[dialogue.speaker] || "alloy";

        try {
          const result = await api.audio.generateShotAudio(shot.id, {
            dialogue_text: dialogue.text,
            voice_id: voiceId,
            tts_provider_id: selectedProvider,
          });

          setAudioStatus(prev => ({
            ...prev,
            [dialogue.shot_no]: result,
          }));
        } catch (error) {
          console.error(`分镜 ${dialogue.shot_no} 生成失败:`, error);
        }
      }

      alert("批量生成完成");
    } catch (error) {
      console.error(error);
      alert(`批量生成失败：${error instanceof Error ? error.message : "未知错误"}`);
    } finally {
      setLoading(false);
    }
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

      {/* 配置面板 */}
      <div className="bg-white rounded-lg shadow p-4 mb-6">
        <h2 className="text-lg font-semibold mb-4">配置</h2>

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

        <div className="mb-4">
          <label className="block text-sm font-medium mb-2">角色音色分配</label>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {uniqueSpeakers.map(speaker => (
              <div key={speaker} className="flex items-center gap-2">
                <span className="text-sm w-24 truncate">{speaker}:</span>
                <select
                  value={voiceMapping[speaker] || "alloy"}
                  onChange={(e) => setVoiceMapping(prev => ({
                    ...prev,
                    [speaker]: e.target.value,
                  }))}
                  className="flex-1 border rounded px-2 py-1 text-sm"
                >
                  {OPENAI_VOICES.map(voice => (
                    <option key={voice.id} value={voice.id}>
                      {voice.name}
                    </option>
                  ))}
                </select>
              </div>
            ))}
          </div>
        </div>

        <button
          onClick={handleBatchGenerate}
          disabled={loading || !selectedProvider || dialogues.length === 0}
          className="bg-blue-500 text-white px-6 py-2 rounded hover:bg-blue-600 disabled:bg-gray-300 disabled:cursor-not-allowed"
        >
          {loading ? "批量生成中..." : "批量生成全部配音"}
        </button>
      </div>

      {/* 对话列表 */}
      <div className="bg-white rounded-lg shadow">
        <div className="p-4 border-b">
          <h2 className="text-lg font-semibold">对话列表 ({dialogues.length})</h2>
        </div>

        <div className="divide-y">
          {dialogues.map((dialogue) => (
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
                      → {OPENAI_VOICES.find(v => v.id === voiceMapping[dialogue.speaker])?.name || "alloy"}
                    </span>
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
                    </div>
                  )}
                </div>

                <button
                  onClick={() => handleGenerateSingle(dialogue)}
                  disabled={generating.has(dialogue.shot_no) || !selectedProvider}
                  className="px-4 py-2 bg-green-500 text-white rounded hover:bg-green-600 disabled:bg-gray-300 disabled:cursor-not-allowed text-sm"
                >
                  {generating.has(dialogue.shot_no) ? "生成中..." : "生成配音"}
                </button>
              </div>
            </div>
          ))}

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
