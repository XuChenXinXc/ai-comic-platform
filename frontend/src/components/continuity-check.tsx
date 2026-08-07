import {
  ChevronLeft,
  ChevronRight,
  Clapperboard,
  Play,
  Volume2,
  VolumeX,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { mediaUrl } from "../api/client";
import type { Shot } from "../types/api";
import { Badge, Button } from "./ui";

/** 只保留已有已完成视频的分镜，按 shot_no 排序（供连播使用）。 */
export function playableShots(shots: Shot[]): Shot[] {
  return shots
    .filter(
      (shot) =>
        shot.current_video?.status === "completed" && Boolean(mediaUrl(shot.current_video)),
    )
    .sort((a, b) => a.shot_no - b.shot_no);
}

/** 完整分镜序列按 shot_no 排序（供双帧对比使用，缺失视频的镜头也保留并提示）。 */
export function orderedShots(shots: Shot[]): Shot[] {
  return [...shots].sort((a, b) => a.shot_no - b.shot_no);
}

/** 视频加载后自动停在末帧（供与下一镜首帧对比）。 */
function LastFrameVideo({ shot }: { shot: Shot }) {
  const autoSeekRef = useRef(false);
  const video = shot.current_video;
  const url = mediaUrl(video);
  if (!url) {
    return (
      <div className="flex aspect-video w-full items-center justify-center rounded-md bg-black/60 text-xs text-white/60">
        当前分镜没有可用的视频版本
      </div>
    );
  }
  return (
    <video
      key={video?.id}
      src={url}
      muted
      controls
      playsInline
      preload="metadata"
      className="aspect-video w-full rounded-md bg-black object-contain"
      onLoadedMetadata={(event) => {
        const element = event.currentTarget;
        try {
          autoSeekRef.current = true;
          element.currentTime = Math.max(0, element.duration - 0.1);
        } catch {
          autoSeekRef.current = false;
          // 部分编码格式 seek 失败时保留首帧，用户可手动拖动
        }
      }}
      onSeeked={(event) => {
        // 只拦截自动定位末帧的 pause，不干扰用户手动拖动/播放
        if (autoSeekRef.current) {
          autoSeekRef.current = false;
          event.currentTarget.pause();
        }
      }}
    />
  );
}

/** 相邻两镜的边界帧对比：上一镜视频末帧 vs 下一镜关键帧（首帧）。 */
function EdgeCompare({ previous, next }: { previous: Shot; next: Shot }) {
  const nextKeyframeUrl = mediaUrl(next.current_keyframe);
  return (
    <div className="rounded-xl border border-line bg-white/60 p-4">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <Badge>分镜 {previous.shot_no}</Badge>
        <ChevronRight className="h-4 w-4 text-muted" />
        <Badge>分镜 {next.shot_no}</Badge>
        <span className="ml-auto text-[11px] text-muted">
          {previous.scene_name || "未绑定场景"} → {next.scene_name || "未绑定场景"}
        </span>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <p className="mb-1.5 text-[10px] font-bold uppercase tracking-[0.14em] text-muted">
            上一镜末帧（可拖动视频查看）
          </p>
          <LastFrameVideo shot={previous} />
        </div>
        <div>
          <p className="mb-1.5 text-[10px] font-bold uppercase tracking-[0.14em] text-muted">
            下一镜首帧（关键帧）
          </p>
          {nextKeyframeUrl ? (
            <img
              src={nextKeyframeUrl}
              alt={`分镜 ${next.shot_no} 关键帧`}
              loading="lazy"
              className="aspect-video w-full rounded-md border border-line bg-white object-contain"
            />
          ) : (
            <div className="flex aspect-video w-full items-center justify-center rounded-md bg-black/60 text-xs text-white/60">
              下一镜还没有关键帧
            </div>
          )}
        </div>
      </div>
      <div className="mt-3 grid gap-2 text-xs leading-5 text-[#454842] sm:grid-cols-2">
        <div className="rounded-md bg-white/70 p-2.5">
          <span className="mr-1 font-bold text-ink">上一镜结束：</span>
          {previous.end_state || previous.next_hook || "（无描述）"}
        </div>
        <div className="rounded-md bg-white/70 p-2.5">
          <span className="mr-1 font-bold text-ink">下一镜开始：</span>
          {next.start_state || "（无描述）"}
        </div>
      </div>
      {previous.current_video?.status !== "completed" ? (
        <p className="mt-2 text-[11px] text-vermilion">
          上一镜视频未完成，末帧仅供参考
        </p>
      ) : null}
      {next.current_video?.status !== "completed" ? (
        <p className="mt-2 text-[11px] text-vermilion">下一镜视频尚未生成</p>
      ) : null}
    </div>
  );
}

/** 顺序连播预览：按 shot_no 自动接续播放所有已完成的视频。 */
function PlaybackPreview({ shots }: { shots: Shot[] }) {
  const [index, setIndex] = useState(0);
  const [muted, setMuted] = useState(true);
  const videoRef = useRef<HTMLVideoElement>(null);
  // shots 可能在 Modal 打开期间变短（后台刷新），渲染时钳制避免越界白屏
  const safeIndex = Math.min(index, shots.length - 1);
  const current = shots[safeIndex];
  const hasNext = safeIndex < shots.length - 1;

  const advance = () => {
    if (hasNext) setIndex((value) => Math.min(value + 1, shots.length - 1));
  };
  const goTo = (value: number) => {
    setIndex(Math.max(0, Math.min(shots.length - 1, value)));
  };
  const togglePlay = () => {
    const element = videoRef.current;
    if (!element) return;
    if (element.paused) void element.play();
    else element.pause();
  };

  useEffect(() => {
    const element = videoRef.current;
    if (!element) return;
    element.muted = muted;
  }, [muted]);

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1.5fr)_minmax(240px,1fr)]">
      <div>
        <video
          key={current.id}
          ref={videoRef}
          src={mediaUrl(current.current_video)}
          autoPlay
          muted
          controls
          playsInline
          className="aspect-video w-full rounded-xl bg-black object-contain shadow"
          onEnded={advance}
        />
        <div className="mt-3 flex items-center justify-between gap-2">
          <Button size="sm" variant="secondary" onClick={() => goTo(index - 1)} disabled={index === 0}>
            <ChevronLeft className="h-4 w-4" />
            上一镜
          </Button>
          <Button size="sm" onClick={togglePlay}>
            <Play className="h-4 w-4" />
            播放 / 暂停
          </Button>
          <Button size="sm" variant="secondary" onClick={() => setMuted((value) => !value)}>
            {muted ? <VolumeX className="h-4 w-4" /> : <Volume2 className="h-4 w-4" />}
            {muted ? "取消静音" : "静音"}
          </Button>
          <Button size="sm" variant="secondary" onClick={() => goTo(index + 1)} disabled={!hasNext}>
            下一镜
            <ChevronRight className="h-4 w-4" />
          </Button>
        </div>
      </div>

      <div className="min-w-0">
        <div className="flex flex-wrap gap-1.5">
          {shots.map((shot, shotIndex) => (
            <button
              key={shot.id}
              type="button"
              onClick={() => goTo(shotIndex)}
              className={`h-8 min-w-8 rounded-md px-2 text-xs font-bold transition ${
                shotIndex === safeIndex
                  ? "bg-vermilion text-white"
                  : "bg-white/70 text-muted hover:bg-white"
              }`}
            >
              {shot.shot_no}
            </button>
          ))}
        </div>
        <div className="mt-3 rounded-xl border border-line bg-white/70 p-4">
          <div className="flex items-center gap-2">
            <h3 className="font-bold">分镜 {current.shot_no}</h3>
            <Badge>{current.scene_name || "未绑定场景"}</Badge>
            <Badge>{current.duration_sec}s</Badge>
          </div>
          <p className="mt-2 line-clamp-3 text-xs leading-5 text-[#454842]">
            {current.action || current.image_prompt}
          </p>
          <div className="mt-3 space-y-1.5 text-xs leading-5 text-[#454842]">
            <p>
              <span className="font-bold text-ink">开始：</span>
              {current.start_state || "（无描述）"}
            </p>
            <p>
              <span className="font-bold text-ink">结束：</span>
              {current.end_state || "（无描述）"}
            </p>
            {current.next_hook ? (
              <p>
                <span className="font-bold text-ink">下一镜钩子：</span>
                {current.next_hook}
              </p>
            ) : null}
          </div>
        </div>
        <p className="mt-3 text-[11px] leading-5 text-muted">
          提示：按 shot_no 顺序自动连播；到最后一镜停止。建议留意相邻镜头之间角色服装、场景、道具与动作状态的衔接。
        </p>
      </div>
    </div>
  );
}

export function ContinuityCheck({ shots }: { shots: Shot[] }) {
  const [mode, setMode] = useState<"playback" | "edges">("playback");
  const playable = useMemo(() => playableShots(shots), [shots]);
  const ordered = useMemo(() => orderedShots(shots), [shots]);

  return (
    <div>
      <div className="mb-4 flex gap-2">
        <Button
          size="sm"
          variant={mode === "playback" ? "primary" : "secondary"}
          onClick={() => setMode("playback")}
        >
          连播预览
        </Button>
        <Button
          size="sm"
          variant={mode === "edges" ? "primary" : "secondary"}
          onClick={() => setMode("edges")}
        >
          相邻双帧对比
        </Button>
      </div>
      {mode === "playback" ? (
        playable.length === 0 ? (
          <div className="flex flex-col items-center gap-3 py-10 text-center">
            <Clapperboard className="h-8 w-8 text-muted" />
            <p className="text-sm text-muted">
              还没有已完成的视频。请先在分镜中生成关键帧与视频，再回来做连播预览。
            </p>
          </div>
        ) : (
          <PlaybackPreview shots={playable} />
        )
      ) : ordered.length < 2 ? (
        <div className="flex flex-col items-center gap-3 py-10 text-center">
          <Clapperboard className="h-8 w-8 text-muted" />
          <p className="text-sm text-muted">至少需要两个分镜才能做相邻双帧对比。</p>
        </div>
      ) : (
        <div className="space-y-4">
          {ordered.slice(0, -1).map((shot, index) => (
            <EdgeCompare
              key={`${shot.id}-${ordered[index + 1].id}`}
              previous={shot}
              next={ordered[index + 1]}
            />
          ))}
        </div>
      )}
    </div>
  );
}
