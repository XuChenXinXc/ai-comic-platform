"""合成服务

处理章节视频合成，包括视频拼接、配音混音、BGM添加、字幕烧录等。
"""

import json
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy import insert, select, update

from app.config import settings
from app.db import Database, db
from app.models.tables import (
    chapter,
    chapter_composition,
    job,
    project_bgm,
    shot,
    shot_audio,
    video_version,
)
from app.services.errors import bad_request, not_found
from app.services.file_service import file_service
from app.services.job_service import JobService
from app.services.subtitle_service import SubtitleService
from app.services.utils import json_dumps, json_loads, new_id, row_dict, utc_now

logger = logging.getLogger(__name__)


def _ffconcat_escape(value: str) -> str:
    """转义 ffconcat 单引号包裹的路径：路径内的单引号用 '\'' 表示。"""
    return value.replace("'", "'\\''")


def _ffmpeg_filter_quote(value: str) -> str:
    """转义并用单引号包裹 ffmpeg filter 参数值。"""
    escaped = value.replace("\\", "\\\\").replace("'", "'\\''")
    return f"'{escaped}'"


def _validate_file_path(file_path: Path, base_path: Path) -> Path:
    """验证文件路径，防止路径遍历攻击

    Args:
        file_path: 要验证的文件路径
        base_path: 允许的基础路径

    Returns:
        解析后的绝对路径

    Raises:
        bad_request: 如果路径不安全或不存在
    """
    try:
        # 解析为绝对路径
        resolved = file_path.resolve()
        base_resolved = base_path.resolve()

        # 检查是否在允许的基础路径内
        if not str(resolved).startswith(str(base_resolved)):
            raise bad_request(f"文件路径不在允许的目录内: {file_path}")

        # 检查文件是否存在
        if not resolved.exists():
            raise bad_request(f"文件不存在: {file_path}")

        return resolved
    except (OSError, ValueError) as exc:
        raise bad_request(f"无效的文件路径: {file_path}") from exc


class CompositionService:
    def __init__(
        self,
        database: Database | None = None,
        jobs: JobService | None = None,
    ) -> None:
        self.database = database or db
        self.jobs = jobs or JobService(self.database)
        self.subtitle_service = SubtitleService(self.database)

    def compose_chapter(
        self,
        chapter_id: str,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        """
        创建章节合成任务

        Args:
            chapter_id: 章节 ID
            config: 合成配置
                - resolution: 分辨率 (720p/1080p/4k)
                - include_audio: 是否包含配音
                - burn_subtitles: 是否烧录字幕
                - bgm_id: BGM ID（可选）
                - bgm_volume: BGM 音量（默认 0.3）

        Returns:
            合成任务信息
        """
        with self.database.engine.begin() as connection:
            # 检查章节是否存在
            chapter_record = connection.execute(
                select(chapter.c.id, chapter.c.project_id, chapter.c.title).where(
                    chapter.c.id == chapter_id
                )
            ).first()
            if chapter_record is None:
                raise not_found(f"章节不存在：{chapter_id}")

            project_id = chapter_record.project_id

            # 检查是否已有进行中的合成任务
            existing = connection.execute(
                select(chapter_composition).where(
                    chapter_composition.c.chapter_id == chapter_id,
                    chapter_composition.c.status.in_(["pending", "processing"]),
                )
            ).first()
            if existing:
                raise bad_request("章节已有合成任务在进行中")

            # 创建 Job
            job_id = new_id("job")
            now = utc_now()
            connection.execute(
                insert(job).values(
                    id=job_id,
                    project_id=project_id,
                    chapter_id=chapter_id,
                    job_type="compose_chapter",
                    target_type="chapter",
                    target_id=chapter_id,
                    status="queued",
                    input_json=json_dumps(config),
                    created_at=now,
                    updated_at=now,
                )
            )

            # 创建合成记录
            composition_id = new_id("compose")
            connection.execute(
                insert(chapter_composition).values(
                    id=composition_id,
                    chapter_id=chapter_id,
                    job_id=job_id,
                    resolution=config.get("resolution", "1080p"),
                    include_audio=int(config.get("include_audio", True)),
                    burn_subtitles=int(config.get("burn_subtitles", True)),
                    bgm_id=config.get("bgm_id"),
                    status="pending",
                    created_at=now,
                    updated_at=now,
                )
            )

            record = connection.execute(
                select(chapter_composition).where(chapter_composition.c.id == composition_id)
            ).first()

        result = row_dict(record)

        # 必须先提交任务，再入队：runner 用独立连接读取任务，
        # 在未提交事务内入队会读不到刚插入的行，导致任务永远停在排队中。
        self.jobs.runner.enqueue(job_id)

        return result

    async def handle_compose(self, job_record: dict) -> dict[str, Any]:
        """任务处理器：执行合成"""
        chapter_id = job_record["chapter_id"]
        config = json_loads(job_record["input_json"], {})

        logger.info(f"开始合成章节 {chapter_id}，配置：{config}")

        with self.database.engine.begin() as connection:
            # 更新状态为 processing
            connection.execute(
                update(chapter_composition)
                .where(chapter_composition.c.chapter_id == chapter_id)
                .values(status="processing", updated_at=utc_now())
            )

        try:
            # 1. 获取所有分镜视频
            shots_data = self._get_chapter_shots(chapter_id)
            if not shots_data:
                raise RuntimeError("章节无分镜视频")

            # 2. 只合成已生成视频的分镜；自由制作预置的空分镜没有视频，直接跳过。
            skipped_videos = [s["shot_no"] for s in shots_data if not s.get("video_path")]
            shots_data = [s for s in shots_data if s.get("video_path")]
            if not shots_data:
                raise RuntimeError("没有已生成的分镜视频，请先生成至少一个分镜视频")
            if skipped_videos:
                logger.info("合成跳过未生成视频的分镜：%s", skipped_videos)

            # 3. 获取项目路径
            with self.database.engine.connect() as connection:
                chapter_record = connection.execute(
                    select(chapter.c.project_id, chapter.c.chapter_no, chapter.c.title).where(
                        chapter.c.id == chapter_id
                    )
                ).first()

            project_id = chapter_record.project_id
            project_path = file_service.project_path(project_id)

            # 4. 创建输出目录
            output_dir = project_path / "成片"
            output_dir.mkdir(parents=True, exist_ok=True)

            chapter_name = f"第{chapter_record.chapter_no:02d}章"
            if chapter_record.title:
                chapter_name += f"-{chapter_record.title}"

            output_filename = f"{chapter_name}.mp4"
            output_path = output_dir / output_filename

            # 5. 合成视频
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)

                crossfade_enabled = bool(config.get("crossfade"))
                crossfade_seconds = float(config.get("crossfade_seconds") or 0.5)

                # 5.1 拼接视频
                concat_video = self._concat_videos(
                    shots_data,
                    project_path,
                    temp_path,
                    crossfade=crossfade_enabled,
                    crossfade_seconds=crossfade_seconds,
                )

                # 5.2 添加配音（如果启用）
                if config.get("include_audio"):
                    concat_video = self._add_audio(
                        concat_video,
                        shots_data,
                        project_path,
                        temp_path,
                        crossfade_seconds=(
                            crossfade_seconds if crossfade_enabled else None
                        ),
                    )

                # 5.3 添加 BGM（如果指定）
                if config.get("bgm_id"):
                    concat_video = self._add_bgm(
                        concat_video,
                        config["bgm_id"],
                        project_path,
                        temp_path,
                        volume=config.get("bgm_volume", 0.3),
                    )

                # 5.4 烧录字幕（如果启用）
                if config.get("burn_subtitles"):
                    concat_video = self._burn_subtitles(concat_video, chapter_id, temp_path)

                # 5.5 最终输出
                self._finalize_output(concat_video, output_path, config.get("resolution", "1080p"))

            # 6. 获取视频时长
            duration_sec = self._get_video_duration(output_path)

            # 7. 更新数据库
            with self.database.engine.begin() as connection:
                connection.execute(
                    update(chapter_composition)
                    .where(chapter_composition.c.chapter_id == chapter_id)
                    .values(
                        file_path=str(output_path.relative_to(project_path)),
                        duration_sec=duration_sec,
                        status="completed",
                        updated_at=utc_now(),
                    )
                )

            logger.info(f"章节 {chapter_id} 合成完成：{output_path}")

            return {
                "file_path": str(output_path.relative_to(project_path)),
                "duration_sec": duration_sec,
            }

        except Exception as exc:
            logger.error(f"章节 {chapter_id} 合成失败：{exc}")
            with self.database.engine.begin() as connection:
                connection.execute(
                    update(chapter_composition)
                    .where(chapter_composition.c.chapter_id == chapter_id)
                    .values(status="failed", error=str(exc), updated_at=utc_now())
                )
            raise

    def _get_chapter_shots(self, chapter_id: str) -> list[dict[str, Any]]:
        """获取章节所有分镜的视频和配音信息"""
        with self.database.engine.connect() as connection:
            # 获取分镜列表
            shot_records = connection.execute(
                select(shot.c.id, shot.c.shot_no, shot.c.duration_sec)
                .where(shot.c.chapter_id == chapter_id)
                .order_by(shot.c.shot_order)
            ).fetchall()

            shots_data = []
            for shot_record in shot_records:
                shot_id = shot_record.id

                # 获取当前视频
                video_record = connection.execute(
                    select(video_version.c.file_path, video_version.c.actual_duration_sec).where(
                        video_version.c.shot_id == shot_id, video_version.c.is_current == 1
                    )
                ).first()

                # 获取当前配音
                audio_record = connection.execute(
                    select(shot_audio.c.file_path, shot_audio.c.duration_sec).where(
                        shot_audio.c.shot_id == shot_id, shot_audio.c.is_current == 1
                    )
                ).first()

                shots_data.append(
                    {
                        "shot_id": shot_id,
                        "shot_no": shot_record.shot_no,
                        "duration_sec": shot_record.duration_sec,
                        "video_path": video_record.file_path if video_record else None,
                        "audio_path": audio_record.file_path if audio_record else None,
                        "audio_duration": audio_record.duration_sec if audio_record else None,
                    }
                )

            return shots_data

    def _concat_videos(
        self,
        shots_data: list[dict[str, Any]],
        project_path: Path,
        temp_path: Path,
        *,
        crossfade: bool = False,
        crossfade_seconds: float = 0.5,
    ) -> Path:
        """拼接视频"""
        if crossfade:
            return self._concat_videos_with_crossfade(
                shots_data,
                project_path,
                temp_path,
                crossfade_seconds,
            )
        # 创建 concat 文件
        concat_file = temp_path / "concat.txt"
        with concat_file.open("w", encoding="utf-8") as f:
            for shot in shots_data:
                video_full_path = project_path / shot["video_path"]
                # FFmpeg concat 需要绝对路径或用单引号包裹
                f.write(f"file '{_ffconcat_escape(video_full_path.as_posix())}'\n")

        output = temp_path / "concat.mp4"

        try:
            subprocess.run(
                [
                    settings.ffmpeg_path,
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(concat_file),
                    "-c",
                    "copy",
                    str(output),
                ],
                check=True,
                timeout=600,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except subprocess.CalledProcessError as exc:
            error_msg = exc.stderr.decode("utf-8", errors="ignore") if exc.stderr else "未知错误"
            raise bad_request(f"视频拼接失败: {error_msg[:200]}") from exc

        return output

    @staticmethod
    def _crossfade_offsets(durations: list[float], crossfade_seconds: float) -> list[float]:
        """计算 xfade 链中每个转场在组合时间轴上的起始偏移。"""
        offsets: list[float] = []
        combined = durations[0]
        for index in range(1, len(durations)):
            offset = combined - crossfade_seconds
            offsets.append(max(0.0, offset))
            combined = combined + durations[index] - crossfade_seconds
        return offsets

    @staticmethod
    def _effective_crossfade_seconds(
        durations: list[float],
        crossfade_seconds: float,
    ) -> float:
        """实际使用的叠化时长：单段不叠化，且不能超过最短素材的一半。"""
        if len(durations) < 2:
            return 0.0
        return max(0.1, min(float(crossfade_seconds), min(durations) / 2))

    @staticmethod
    def _audio_offsets(
        durations: list[float],
        crossfade_seconds: float | None,
    ) -> list[float]:
        """按真实视频时长计算每段素材在成片时间轴上的起始时间。"""
        fade = (
            CompositionService._effective_crossfade_seconds(
                durations,
                crossfade_seconds,
            )
            if crossfade_seconds is not None
            else 0.0
        )
        offsets: list[float] = []
        cursor = 0.0
        for index, duration in enumerate(durations):
            offsets.append(cursor)
            cursor += duration - (fade if index < len(durations) - 1 else 0)
        return offsets

    @staticmethod
    def _get_video_stream_info(path: Path) -> dict[str, Any]:
        probe = subprocess.run(
            [
                settings.ffprobe_path,
                "-v",
                "error",
                "-show_streams",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        streams = json.loads(probe.stdout).get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), {})
        try:
            num, den = str(video.get("r_frame_rate", "25/1")).split("/")
            fps = max(1, round(int(num) / int(den)))
        except (ValueError, ZeroDivisionError):
            fps = 25
        return {
            "width": int(video.get("width", 1280)),
            "height": int(video.get("height", 720)),
            "fps": fps,
            "has_audio": any(
                stream.get("codec_type") == "audio" for stream in streams
            ),
        }

    def _concat_videos_with_crossfade(
        self,
        shots_data: list[dict[str, Any]],
        project_path: Path,
        temp_path: Path,
        crossfade_seconds: float,
    ) -> Path:
        """用 xfade 叠化拼接分镜视频（每个转场 0.5s）。"""
        video_paths = [
            project_path / shot["video_path"]
            for shot in shots_data
            if shot.get("video_path")
        ]
        if len(video_paths) < 2:
            return self._concat_videos(shots_data, project_path, temp_path)

        durations = [self._get_video_duration(path) for path in video_paths]
        first = self._get_video_stream_info(video_paths[0])
        width, height, fps = first["width"], first["height"], first["fps"]
        has_audio = all(
            self._get_video_stream_info(path)["has_audio"] for path in video_paths
        )
        crossfade_seconds = self._effective_crossfade_seconds(
            durations,
            crossfade_seconds,
        )
        offsets = self._crossfade_offsets(durations, crossfade_seconds)

        cmd = [settings.ffmpeg_path]
        for path in video_paths:
            cmd.extend(["-i", str(path)])

        filters: list[str] = []
        for index in range(len(video_paths)):
            filters.append(
                f"[{index}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
                f"fps={fps},format=yuv420p[v{index}]"
            )
        previous = "v0"
        for index in range(1, len(video_paths)):
            label = f"vx{index}" if index < len(video_paths) - 1 else "vout"
            filters.append(
                f"[{previous}][v{index}]xfade=transition=fade:"
                f"duration={crossfade_seconds}:offset={offsets[index - 1]}[{label}]"
            )
            previous = label

        if has_audio:
            for index in range(len(video_paths)):
                filters.append(
                    f"[{index}:a]aformat=sample_rates=44100:"
                    f"channel_layouts=stereo[a{index}]"
                )
            previous = "a0"
            for index in range(1, len(video_paths)):
                label = f"ax{index}" if index < len(video_paths) - 1 else "aout"
                filters.append(
                    f"[{previous}][a{index}]acrossfade=d={crossfade_seconds}[{label}]"
                )
                previous = label

        cmd.extend(
            [
                "-filter_complex",
                ";".join(filters),
                "-map",
                "[vout]",
            ]
        )
        if has_audio:
            cmd.extend(["-map", "[aout]"])
        cmd.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "18",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                str(temp_path / "concat.mp4"),
            ]
        )

        try:
            subprocess.run(
                cmd,
                check=True,
                timeout=1200,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except subprocess.CalledProcessError as exc:
            error_msg = (
                exc.stderr.decode("utf-8", errors="ignore") if exc.stderr else "未知错误"
            )
            raise bad_request(f"叠化拼接失败: {error_msg[:300]}") from exc

        return temp_path / "concat.mp4"

    def _add_audio(
        self,
        video_path: Path,
        shots_data: list[dict[str, Any]],
        project_path: Path,
        temp_path: Path,
        *,
        crossfade_seconds: float | None = None,
    ) -> Path:
        """添加配音音轨"""
        # 时间轴必须按真实视频时长计算：xfade 会重叠缩短成片，
        # 使用拍摄计划里的 duration_sec 会让后续配音整体前移。
        video_durations: list[float] = []
        for item in shots_data:
            if item.get("video_path"):
                video_durations.append(
                    self._get_video_duration(project_path / item["video_path"])
                )
            else:
                video_durations.append(float(item.get("duration_sec") or 0))
        starts = self._audio_offsets(video_durations, crossfade_seconds)
        audio_files = [
            (offset, project_path / item["audio_path"])
            for offset, item in zip(starts, shots_data, strict=True)
            if item.get("audio_path")
        ]

        if not audio_files:
            return video_path

        # 合并所有配音（带时间偏移）
        concat_audio = temp_path / "concat_audio.mp3"
        filter_complex = []
        for idx, (offset, _audio_path) in enumerate(audio_files):
            delay_ms = int(offset * 1000)
            filter_complex.append(f"[{idx}]adelay={delay_ms}|{delay_ms}[a{idx}]")

        mix_inputs = "".join(f"[a{i}]" for i in range(len(audio_files)))
        filter_complex.append(
            f"{mix_inputs}amix=inputs={len(audio_files)}[aout]"
        )

        cmd = [settings.ffmpeg_path]
        for _, audio_path in audio_files:
            cmd.extend(["-i", str(audio_path)])
        cmd.extend(
            [
                "-filter_complex",
                ";".join(filter_complex),
                "-map",
                "[aout]",
                str(concat_audio),
            ]
        )

        try:
            subprocess.run(
                cmd,
                check=True,
                timeout=300,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except subprocess.CalledProcessError as exc:
            error_msg = exc.stderr.decode("utf-8", errors="ignore") if exc.stderr else "未知错误"
            raise bad_request(f"音频混音失败: {error_msg[:200]}") from exc

        # 混合到视频
        output = temp_path / "with_audio.mp4"
        subprocess.run(
            [
                settings.ffmpeg_path,
                "-i",
                str(video_path),
                "-i",
                str(concat_audio),
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                str(output),
            ],
            check=True,
            timeout=600,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        return output

    def _add_bgm(
        self, video_path: Path, bgm_id: str, project_path: Path, temp_path: Path, volume: float
    ) -> Path:
        """添加 BGM 音轨"""
        with self.database.engine.connect() as connection:
            bgm_record = connection.execute(
                select(project_bgm.c.file_path).where(project_bgm.c.id == bgm_id)
            ).first()

        if bgm_record is None:
            logger.warning(f"BGM 不存在：{bgm_id}，跳过")
            return video_path

        bgm_full_path = project_path / bgm_record.file_path
        output = temp_path / "with_bgm.mp4"

        # 获取视频时长
        video_duration = self._get_video_duration(video_path)
        has_video_audio = self._get_video_stream_info(video_path)["has_audio"]
        fade_out_start = max(0.0, video_duration - 2)
        bgm_chain = (
            f"[0:a]afade=t=in:st=0:d=2,"
            f"afade=t=out:st={fade_out_start}:d=2,"
            f"volume={volume}[bgm]"
        )
        if has_video_audio:
            filter_complex = (
                f"{bgm_chain};"
                "[1:a][bgm]amix=inputs=2:duration=first[aout]"
            )
            audio_map = "[aout]"
        else:
            # 视频没有音轨时不能引用 [1:a]，直接使用 BGM 音轨。
            filter_complex = bgm_chain
            audio_map = "[bgm]"

        # 混音：视频音轨 + BGM（循环并淡入淡出）
        subprocess.run(
            [
                settings.ffmpeg_path,
                "-stream_loop",
                "-1",  # 无限循环 BGM
                "-i",
                str(bgm_full_path),
                "-i",
                str(video_path),
                "-t",
                str(video_duration),
                "-filter_complex",
                filter_complex,
                "-map",
                "1:v:0",
                "-map",
                audio_map,
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                str(output),
            ],
            check=True,
            timeout=600,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        return output

    def _burn_subtitles(self, video_path: Path, chapter_id: str, temp_path: Path) -> Path:
        """烧录字幕"""
        # 导出 SRT 文件
        srt_content = self.subtitle_service.export_chapter_srt(chapter_id)
        if not srt_content.strip():
            logger.info(f"章节 {chapter_id} 无字幕，跳过烧录")
            return video_path

        srt_file = temp_path / "subtitles.srt"
        srt_file.write_text(srt_content, encoding="utf-8")

        output = temp_path / "with_subtitles.mp4"

        # 字幕样式
        style = (
            "FontName=Microsoft YaHei,FontSize=24,PrimaryColour=&HFFFFFF,"
            "OutlineColour=&H000000,Outline=2"
        )

        subprocess.run(
            [
                settings.ffmpeg_path,
                "-i",
                str(video_path),
                "-vf",
                (
                    f"subtitles={_ffmpeg_filter_quote(srt_file.as_posix())}"
                    f":force_style={_ffmpeg_filter_quote(style)}"
                ),
                "-c:a",
                "copy",
                str(output),
            ],
            check=True,
            timeout=600,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        return output

    def _finalize_output(self, video_path: Path, output_path: Path, resolution: str) -> None:
        """最终输出（可选：调整分辨率）"""
        if resolution == "720p":
            scale = "scale=1280:720"
        elif resolution == "4k":
            scale = "scale=3840:2160"
        else:  # 1080p 或其他
            scale = None

        if scale:
            subprocess.run(
                [
                    settings.ffmpeg_path,
                    "-i",
                    str(video_path),
                    "-vf",
                    scale,
                    "-c:a",
                    "copy",
                    str(output_path),
                ],
                check=True,
                timeout=600,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        else:
            # 直接复制
            subprocess.run(
                [
                    settings.ffmpeg_path,
                    "-i",
                    str(video_path),
                    "-c",
                    "copy",
                    str(output_path),
                ],
                check=True,
                timeout=600,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )

    @staticmethod
    def _get_video_duration(video_path: Path) -> float:
        """获取视频时长"""
        try:
            result = subprocess.run(
                [
                    settings.ffprobe_path,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(video_path),
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            return float(result.stdout.strip())
        except subprocess.CalledProcessError as exc:
            error_msg = exc.stderr if exc.stderr else "未知错误"
            raise bad_request(f"获取视频时长失败: {error_msg[:200]}") from exc
        except ValueError as exc:
            logger.warning(f"无法获取视频时长：{exc}")
            return 0.0

    def get_chapter_composition(self, chapter_id: str) -> dict[str, Any] | None:
        """获取章节合成记录"""
        with self.database.engine.connect() as connection:
            record = connection.execute(
                select(chapter_composition)
                .where(chapter_composition.c.chapter_id == chapter_id)
                .order_by(chapter_composition.c.created_at.desc())
            ).first()

        if record is None:
            return None

        return row_dict(record)
