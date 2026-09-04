import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router";
import { toast } from "sonner";
import { api, mediaUrl } from "../api/client";
import { Modal } from "../components/ui";
import { formatError } from "../lib/utils";
import type { Chapter, MediaVersion } from "../types/api";

interface BGM {
  id: string;
  name: string;
  duration_sec: number | null;
  mood_tags: string;
}

interface CompositionConfig {
  resolution: "720p" | "1080p" | "4k";
  include_audio: boolean;
  burn_subtitles: boolean;
  bgm_id: string;
  bgm_volume: number;
}

export default function CompositionPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const [chapters, setChapters] = useState<Chapter[]>([]);
  const [bgms, setBgms] = useState<BGM[]>([]);
  const [selectedChapter, setSelectedChapter] = useState<string>("");
  const [config, setConfig] = useState<CompositionConfig>({
    resolution: "1080p",
    include_audio: true,
    burn_subtitles: true,
    bgm_id: "",
    bgm_volume: 0.3,
  });
  const [composing, setComposing] = useState(false);
  const [compositionStatus, setCompositionStatus] = useState<{
    status: string;
    file_path: string | null;
    error: string;
    job_id: string | null;
  } | null>(null);
  const [uploadingBGM, setUploadingBGM] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  // BGM 名称内联输入（上传时使用；留空则回退文件名）
  const [bgmName, setBgmName] = useState("");
  // 待确认删除的 BGM ID（删除确认弹窗）
  const [confirmDeleteBgmId, setConfirmDeleteBgmId] = useState<string | null>(null);
  const confirmDeleteBgm = bgms.find(bgm => bgm.id === confirmDeleteBgmId) || null;

  useEffect(() => {
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, []);

  useEffect(() => {
    if (!projectId) return;
    setLoadError(null);
    // 项目直达切换（前进后退/URL 跳转）时，旧请求晚到会覆盖新项目数据：
    // 响应落地前校验 projectId 未变。
    let cancelled = false;
    Promise.all([
      api.chapters.list(projectId),
      api.audio.listBGMs(projectId),
      api.audio.getConfig(projectId).catch(() => null),
    ]).then(([chaptersData, bgmsData, audioConfig]) => {
      if (cancelled) return;
      setChapters(chaptersData);
      setBgms(bgmsData);
      // 加载已保存的项目音频配置（BGM 音量），避免每次进入都重置为默认值。
      if (audioConfig && typeof audioConfig.bgm_volume === "number") {
        setConfig(prev => ({
          ...prev,
          bgm_volume: audioConfig.bgm_volume,
        }));
      }
      if (chaptersData.length > 0) {
        setSelectedChapter(chaptersData[0].id);
      } else {
        setSelectedChapter("");
      }
    }).catch((error) => {
      if (cancelled) return;
      console.error(error);
      setLoadError(formatError(error));
    });
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  useEffect(() => {
    if (!selectedChapter) return;
    setLoadError(null);
    // 切换章节后，上一个章节的轮询任务必须停止，避免状态互相覆盖。
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    let cancelled = false;
    // 查询当前章节的合成状态
    api.composition.getChapterComposition(selectedChapter).then(comp => {
      if (cancelled) return;
      if (comp) {
        setCompositionStatus(comp);
      } else {
        setCompositionStatus(null);
      }
    }).catch((error) => {
      if (cancelled) return;
      console.error(error);
      setLoadError(formatError(error));
    });
    return () => {
      cancelled = true;
    };
  }, [selectedChapter]);

  const handleCompose = async () => {
    if (!selectedChapter) {
      toast.error("请选择章节");
      return;
    }

    setComposing(true);

    try {
      const result = await api.composition.composeChapter(selectedChapter, {
        resolution: config.resolution,
        include_audio: config.include_audio,
        burn_subtitles: config.burn_subtitles,
        bgm_id: config.bgm_id || undefined,
        bgm_volume: config.bgm_volume,
      });

      setCompositionStatus(result);
      toast.success("合成任务已创建，请在任务列表查看进度");

      // 轮询任务状态
      if (result.job_id) {
        pollJobStatus(result.job_id);
      }
    } catch (error) {
      console.error(error);
      toast.error(`合成失败：${formatError(error)}`);
    } finally {
      setComposing(false);
    }
  };

  const pollJobStatus = (jobId: string) => {
    if (pollRef.current) clearInterval(pollRef.current);
    // 轮询上限：任务卡死时避免永久轮询；瞬时错误保留轮询不中断。
    let remainingTicks = 200; // 200 * 3s = 10 分钟
    pollRef.current = setInterval(async () => {
      if (remainingTicks <= 0) {
        if (pollRef.current) clearInterval(pollRef.current);
        pollRef.current = null;
        return;
      }
      remainingTicks -= 1;
      try {
        const job = await api.jobs.get(jobId);
        setCompositionStatus(prev => prev ? { ...prev, status: job.status, job_id: jobId } : prev);
        if (
          ["completed", "failed", "cancelled", "interrupted"].includes(
            job.status,
          )
        ) {
          if (pollRef.current) clearInterval(pollRef.current);
          pollRef.current = null;
          // 刷新合成状态
          const comp = await api.composition.getChapterComposition(selectedChapter);
          if (comp) {
            setCompositionStatus(comp);
          }
        }
      } catch {
        // 瞬时错误（网络抖动）不停止轮询，下一轮继续。
      }
    }, 3000);
  };

  const handleUploadBGM = async () => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "audio/*";
    input.onchange = async (e) => {
      const file = (e.target as HTMLInputElement).files?.[0];
      if (!file || !projectId) return;

      // 名称来自内联输入；留空回退为文件名（去掉扩展名）。
      const name = bgmName.trim() || file.name.replace(/\.[^.]+$/, "");
      if (!name) {
        toast.error("请输入 BGM 名称");
        return;
      }

      setUploadingBGM(true);

      try {
        const bgm = await api.audio.uploadBGM(projectId, file, name, []);
        setBgms(prev => [bgm, ...prev]);
        setBgmName("");
        toast.success("BGM 上传成功");
      } catch (error) {
        console.error(error);
        toast.error(`上传失败：${formatError(error)}`);
      } finally {
        setUploadingBGM(false);
      }
    };
    input.click();
  };

  const handleDeleteBGM = (bgmId: string) => {
    setConfirmDeleteBgmId(bgmId);
  };

  const handleConfirmDeleteBGM = async () => {
    const bgmId = confirmDeleteBgmId;
    if (!bgmId) return;
    setConfirmDeleteBgmId(null);

    try {
      await api.audio.deleteBGM(bgmId);
      setBgms(prev => prev.filter(b => b.id !== bgmId));
      if (config.bgm_id === bgmId) {
        setConfig(prev => ({ ...prev, bgm_id: "" }));
      }
      toast.success("BGM 已删除");
    } catch (error) {
      console.error(error);
      toast.error(`删除失败：${formatError(error)}`);
    }
  };

  return (
    <div className="p-6 max-w-5xl mx-auto">
      <div className="mb-6">
        <h1 className="text-2xl font-bold mb-2">最终合成</h1>
        <p className="text-gray-600">生成完整的章节视频（包含配音、BGM、字幕）</p>
      </div>

      {loadError ? (
        <div
          role="alert"
          className="mb-6 rounded border border-red-200 bg-red-50 p-4 text-sm text-red-700"
        >
          视频合成初始化失败：{loadError}
        </div>
      ) : null}
      {/* 章节选择 */}
      <div className="bg-white rounded-lg shadow p-4 mb-6">
        <h2 className="text-lg font-semibold mb-4">选择章节</h2>
        <select
          value={selectedChapter}
          onChange={(e) => setSelectedChapter(e.target.value)}
          className="w-full border rounded px-3 py-2"
        >
          <option value="">请选择章节</option>
          {chapters.map(chapter => (
            <option key={chapter.id} value={chapter.id}>
              第 {chapter.chapter_no} 章 - {chapter.title || "（无标题）"}
            </option>
          ))}
        </select>
      </div>

      {/* 合成配置 */}
      {selectedChapter && (
        <div className="bg-white rounded-lg shadow p-4 mb-6">
          <h2 className="text-lg font-semibold mb-4">合成配置</h2>

          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium mb-2">分辨率</label>
              <div className="flex gap-4">
                {["720p", "1080p", "4k"].map(res => (
                  <label key={res} className="flex items-center gap-2">
                    <input
                      type="radio"
                      checked={config.resolution === res}
                      onChange={() => setConfig(prev => ({ ...prev, resolution: res as CompositionConfig["resolution"] }))}
                    />
                    <span>{res}</span>
                  </label>
                ))}
              </div>
            </div>

            <div className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={config.include_audio}
                onChange={(e) => setConfig(prev => ({ ...prev, include_audio: e.target.checked }))}
              />
              <label className="text-sm font-medium">包含配音</label>
            </div>

            <div className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={config.burn_subtitles}
                onChange={(e) => setConfig(prev => ({ ...prev, burn_subtitles: e.target.checked }))}
              />
              <label className="text-sm font-medium">烧录字幕</label>
            </div>

            <div>
              <div className="flex items-center justify-between mb-2">
                <label className="block text-sm font-medium">背景音乐（可选）</label>
                <button
                  onClick={handleUploadBGM}
                  disabled={uploadingBGM}
                  className="text-sm text-blue-500 hover:text-blue-600 disabled:text-gray-400"
                >
                  {uploadingBGM ? "上传中..." : "+ 上传 BGM"}
                </button>
              </div>
              {/* BGM 名称内联输入：上传时使用，留空回退文件名 */}
              <input
                type="text"
                value={bgmName}
                onChange={(e) => setBgmName(e.target.value)}
                placeholder="上传时使用的 BGM 名称（留空使用文件名）"
                className="mb-2 w-full border rounded px-3 py-2 text-sm"
              />
              <select
                value={config.bgm_id}
                onChange={(e) => setConfig(prev => ({ ...prev, bgm_id: e.target.value }))}
                className="w-full border rounded px-3 py-2"
              >
                <option value="">无</option>
                {bgms.map(bgm => (
                  <option key={bgm.id} value={bgm.id}>
                    {bgm.name} {bgm.duration_sec && `(${bgm.duration_sec.toFixed(0)}s)`}
                  </option>
                ))}
              </select>

              {bgms.length > 0 && (
                <div className="mt-2 space-y-1">
                  {bgms.map(bgm => (
                    <div key={bgm.id} className="flex items-center justify-between text-sm py-1">
                      <span>{bgm.name}</span>
                      <button
                        onClick={() => handleDeleteBGM(bgm.id)}
                        className="text-red-500 hover:text-red-600"
                      >
                        删除
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {config.bgm_id && (
              <div>
                <label className="block text-sm font-medium mb-2">
                  BGM 音量: {(config.bgm_volume * 100).toFixed(0)}%
                </label>
                <input
                  type="range"
                  min="0"
                  max="1"
                  step="0.1"
                  value={config.bgm_volume}
                  onChange={(e) => setConfig(prev => ({ ...prev, bgm_volume: parseFloat(e.target.value) }))}
                  className="w-full"
                />
              </div>
            )}
          </div>

          <div className="mt-6">
            <button
              onClick={handleCompose}
              disabled={composing || !selectedChapter}
              className="bg-blue-500 text-white px-6 py-2 rounded hover:bg-blue-600 disabled:bg-gray-300 disabled:cursor-not-allowed"
            >
              {composing ? "创建中..." : "开始合成"}
            </button>
          </div>
        </div>
      )}

      {/* 合成状态 */}
      {compositionStatus && (
        <div className="bg-white rounded-lg shadow p-4">
          <h2 className="text-lg font-semibold mb-4">合成状态</h2>

          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <span className="text-sm font-medium">状态:</span>
              <span className={`px-2 py-0.5 rounded text-sm ${
                compositionStatus.status === "completed"
                  ? "bg-green-100 text-green-700"
                  : compositionStatus.status === "failed"
                  ? "bg-red-100 text-red-700"
                  : compositionStatus.status === "cancelled" || compositionStatus.status === "interrupted"
                  ? "bg-gray-100 text-gray-600"
                  : "bg-yellow-100 text-yellow-700"
              }`}>
                {compositionStatus.status === "completed" ? "已完成" :
                 compositionStatus.status === "failed" ? "失败" :
                 compositionStatus.status === "cancelled" ? "已取消" :
                 compositionStatus.status === "interrupted" ? "已中断" :
                 compositionStatus.status === "queued" ? "等待中" :
                 compositionStatus.status === "running" ||
                 compositionStatus.status === "waiting_provider" ||
                 compositionStatus.status === "downloading" ||
                 compositionStatus.status === "processing"
                   ? "合成中"
                   : "等待中"}
              </span>
            </div>

            {compositionStatus.error && (
              <div className="text-sm text-red-600">
                错误: {compositionStatus.error}
              </div>
            )}

            {compositionStatus.file_path && compositionStatus.status === "completed" && (
              <div className="mt-4">
                {/* 项目内相对路径 → 清洗后的媒体 URL（复用 mediaUrl 的路径清洗，避免手工拼接越界） */}
                {(() => {
                  const compositionMediaUrl = mediaUrl({
                    file_path: `${projectId}/${compositionStatus.file_path}`,
                  } as MediaVersion);
                  return (
                    <>
                      <video
                        controls
                        src={compositionMediaUrl}
                        className="w-full max-w-2xl rounded border"
                      />
                      <a
                        href={compositionMediaUrl}
                        download
                        className="inline-block bg-green-500 text-white px-6 py-2 rounded hover:bg-green-600"
                      >
                        下载成品视频
                      </a>
                    </>
                  );
                })()}
                <p className="text-sm text-gray-500 mt-2">
                  文件路径: {compositionStatus.file_path}
                </p>
              </div>
            )}

            {compositionStatus.job_id && compositionStatus.status === "processing" && (
              <p className="text-sm text-gray-500 mt-2">
                任务 ID: {compositionStatus.job_id}（可在任务列表查看详情）
              </p>
            )}
          </div>
        </div>
      )}

      {/* 删除 BGM 确认弹窗 */}
      <Modal
        open={confirmDeleteBgm !== null}
        onClose={() => setConfirmDeleteBgmId(null)}
        title="删除 BGM"
        description={confirmDeleteBgm ? `确定删除「${confirmDeleteBgm.name}」？` : undefined}
        footer={
          <>
            <button
              type="button"
              onClick={() => setConfirmDeleteBgmId(null)}
              className="px-4 py-2 rounded border border-gray-300 bg-white text-sm hover:bg-gray-50"
            >
              取消
            </button>
            <button
              type="button"
              onClick={() => void handleConfirmDeleteBGM()}
              className="px-4 py-2 rounded bg-red-500 text-white text-sm hover:bg-red-600"
            >
              删除
            </button>
          </>
        }
      >
        <p className="text-sm text-gray-600">删除后无法恢复。</p>
      </Modal>
    </div>
  );
}
