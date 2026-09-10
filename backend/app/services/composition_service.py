"""合成服务

处理章节视频合成，包括视频拼接、配音混音、BGM添加、字幕烧录等。
"""

import json
import logging
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

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
from app.services.errors import bad_request, conflict, not_found
from app.services.file_service import chapter_folder_name, file_service
from app.services.job_service import JobService
from app.services.subtitle_service import (
    SubtitleService,
    effective_crossfade_seconds,
    shot_start_offsets,
)
from app.services.utils import (
    json_dumps,
    json_loads,
    new_id,
    row_dict,
    sanitize_error_message,
    utc_now,
)

logger = logging.getLogger(__name__)


def _ffconcat_escape(value: str) -> str:
    """转义 ffconcat 单引号包裹的路径：路径内的单引号用 '\'' 表示。"""
    return value.replace("'", "'\\''")


def _ffmpeg_filter_quote(value: str) -> str:
    """转义并用单引号包裹 ffmpeg filter 参数值。"""
    escaped = value.replace("\\", "\\\\").replace("'", "'\\''")
    return f"'{escaped}'"


def _ffmpeg_filter_path(value: str) -> str:
    """转义并用单引号包裹 ffmpeg filter 中的文件路径。

    filtergraph 会把冒号当作参数分隔符，Windows 盘符（``C:/...``）即使包在单引号里
    也会被拆开，导致后半段路径被当成下一个位置参数。必须额外转义冒号。
    """
    escaped = value.replace("'", "'\\''").replace(":", "\\:")
    return f"'{escaped}'"


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
        *,
        enqueue: bool = True,
        depends_on: Sequence[str] = (),
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
        try:
            with self.database.engine.begin() as connection:
                # 整个 select + 失效旧记录 + insert 必须跑在同一事务里；
                # 并发冲突由 ux_composition_chapter 部分唯一索引 + 下方
                # IntegrityError 捕获兜底（SQLITE_BUSY 由 busy_timeout 兜底）。
                assert connection.in_transaction()

                # 检查章节是否存在
                chapter_record = connection.execute(
                    select(chapter.c.id, chapter.c.project_id, chapter.c.title).where(
                        chapter.c.id == chapter_id
                    )
                ).first()
                if chapter_record is None:
                    raise not_found(f"章节不存在：{chapter_id}")
                assert chapter_record is not None

                project_id = chapter_record.project_id
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
                input_data = dict(config)
                dependency_ids = [str(value) for value in depends_on if str(value).strip()]
                connection.execute(
                    insert(job).values(
                        id=job_id,
                        project_id=project_id,
                        chapter_id=chapter_id,
                        job_type="compose_chapter",
                        target_type="chapter",
                        target_id=chapter_id,
                        # 合成是本地 FFmpeg 任务，不是文字 Provider 调用；
                        # 标记为 compose 以免占用 text worker 池（runner 侧
                        # 的 compose 池调度由独立改动提供，未识别前回退
                        # job_type 推断）。
                        provider_kind="compose",
                        status="queued",
                        input_json=json_dumps(input_data),
                        depends_on_json=json_dumps(dependency_ids),
                        result_json="{}",
                        error="",
                        retry_count=0,
                        progress_current=0,
                        progress_total=1,
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
                    select(chapter_composition).where(
                        chapter_composition.c.id == composition_id
                    )
                ).first()
        except IntegrityError as exc:
            # 并发 compose 撞 ux_composition_chapter 部分唯一索引时，
            # 转成 409 语义，而不是裸 500。
            raise conflict("章节合成任务已存在或正在创建，请稍后重试") from exc

        result = row_dict(record)
        if result is None:
            raise RuntimeError("合成记录创建后无法读取")

        # 必须先提交任务，再入队：runner 用独立连接读取任务，
        # 在未提交事务内入队会读不到刚插入的行，导致任务永远停在排队中。
        if enqueue:
            self.jobs.runner.enqueue(job_id)

        return result

    async def handle_compose(self, job_record: dict) -> dict[str, Any]:
        """任务处理器：执行合成"""
        chapter_id = job_record["chapter_id"]
        config = job_record.get("input_data")
        if not isinstance(config, Mapping):
            config = json_loads(job_record["input_json"], {})
        config = dict(config) if isinstance(config, Mapping) else {}

        logger.info(f"开始合成章节 {chapter_id}，配置：{config}")

        with self.database.engine.begin() as connection:
            # 只更新本次任务关联的合成记录（按 job_id），
            # 避免把该章节历史已完成记录的状态/路径一并污染。
            # 测试/直接调用场景下 job_record 可能缺 id，此时退化为
            # 「该章节最新的 pending/processing 记录」而非全量覆盖。
            job_id = job_record.get("id")
            if job_id:
                legacy_update = connection.execute(
                    update(chapter_composition)
                    .where(
                        chapter_composition.c.job_id == job_id,
                        chapter_composition.c.chapter_id == chapter_id,
                    )
                    .values(status="processing", updated_at=utc_now())
                )
            else:
                legacy_update = connection.execute(
                    update(chapter_composition)
                    .where(
                        chapter_composition.c.chapter_id == chapter_id,
                        chapter_composition.c.status.in_(("pending", "processing")),
                    )
                    .values(status="processing", updated_at=utc_now())
                )
            if legacy_update.rowcount < 1 and job_id:
                raise RuntimeError("Composition 置为 processing 条件更新未命中")

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
            if chapter_record is None:
                raise RuntimeError(f"章节不存在：{chapter_id}")

            project_id = chapter_record.project_id
            project_path = file_service.project_path(project_id)

            # 4. 创建输出目录
            output_dir = project_path / "成片"
            output_dir.mkdir(parents=True, exist_ok=True)

            chapter_name = chapter_folder_name(
                chapter_record.chapter_no,
                str(chapter_record.title or ""),
            )

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
                    project_id,
                    temp_path,
                    crossfade=crossfade_enabled,
                    crossfade_seconds=crossfade_seconds,
                )

                # 5.2 添加配音（如果启用）
                if config.get("include_audio"):
                    concat_video = self._add_audio(
                        concat_video,
                        shots_data,
                        project_id,
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
                        project_id,
                        temp_path,
                        volume=config.get("bgm_volume", 0.3),
                    )

                # 5.4 烧录字幕（如果启用）
                if config.get("burn_subtitles"):
                    concat_video = self._burn_subtitles(
                        concat_video,
                        chapter_id,
                        temp_path,
                        shots_data=shots_data,
                        project_id=project_id,
                        crossfade_seconds=(
                            crossfade_seconds if crossfade_enabled else None
                        ),
                    )

                # 5.5 动画节奏化（可选）：抽帧到 12fps（一拍二张数感）+ 轻微颗粒，
                # 用于降低视频 AI 味；在最终输出前处理，分辨率调整仍由 finalize 承担。
                if config.get("anime_cadence"):
                    concat_video = self._apply_anime_cadence(concat_video, temp_path)

                # 5.6 最终输出
                self._finalize_output(concat_video, output_path, config.get("resolution", "1080p"))

            # 6. 获取视频时长
            duration_sec = self._get_video_duration(output_path)

            # 7. 更新数据库
            with self.database.engine.begin() as connection:
                values = {
                    "file_path": str(output_path.relative_to(project_path)),
                    "duration_sec": duration_sec,
                    "status": "completed",
                    "updated_at": utc_now(),
                }
                job_id = job_record.get("id")
                legacy_statement = update(chapter_composition)
                if job_id:
                    legacy_statement = legacy_statement.where(
                        chapter_composition.c.job_id == job_id,
                        chapter_composition.c.chapter_id == chapter_id,
                        chapter_composition.c.status == "processing",
                    )
                else:
                    # 直接调用场景（无 job id）：只更新该章节最新 processing 记录。
                    legacy_statement = legacy_statement.where(
                        chapter_composition.c.chapter_id == chapter_id,
                        chapter_composition.c.status == "processing",
                    )
                legacy_update = connection.execute(legacy_statement.values(**values))
                if legacy_update.rowcount < 1 and job_id:
                    raise RuntimeError("Composition 完成条件更新未命中")

            logger.info(f"章节 {chapter_id} 合成完成：{output_path}")

            return {
                "file_path": str(output_path.relative_to(project_path)),
                "duration_sec": duration_sec,
            }

        except Exception as exc:
            # 入库前统一脱敏：避免 ffmpeg stderr、绝对路径或敏感凭据
            # 直接写入 composition.error 并经 API 返回给前端
            error_text = sanitize_error_message(str(exc))
            logger.error(f"章节 {chapter_id} 合成失败：{error_text}")
            with self.database.engine.begin() as connection:
                job_id = job_record.get("id")
                legacy_failed_statement = update(chapter_composition)
                if job_id:
                    legacy_failed_statement = legacy_failed_statement.where(
                        chapter_composition.c.job_id == job_id,
                        chapter_composition.c.chapter_id == chapter_id,
                        chapter_composition.c.status.in_(("pending", "processing", "failed")),
                    )
                else:
                    legacy_failed_statement = legacy_failed_statement.where(
                        chapter_composition.c.chapter_id == chapter_id,
                        chapter_composition.c.status.in_(("pending", "processing", "failed")),
                    )
                legacy_failed_update = connection.execute(
                    legacy_failed_statement.values(
                        status="failed", error=error_text, updated_at=utc_now()
                    )
                )
                if legacy_failed_update.rowcount < 1 and job_id:
                    raise RuntimeError("Composition 失败条件更新未命中") from exc
            raise

    def _get_chapter_shots(
        self,
        chapter_id: str,
    ) -> list[dict[str, Any]]:
        """获取章节分镜媒体：读取章节当前版本。"""
        with self.database.engine.connect() as connection:
            # 获取分镜列表
            shot_records = connection.execute(
                select(shot.c.id, shot.c.shot_no, shot.c.duration_sec, shot.c.project_id)
                .where(shot.c.chapter_id == chapter_id)
                .order_by(shot.c.shot_order)
            ).fetchall()

            shots_data = []
            for shot_record in shot_records:
                shot_id = shot_record.id
                video_record = connection.execute(
                    select(
                        video_version.c.id,
                        video_version.c.file_path,
                        video_version.c.actual_duration_sec,
                    ).where(
                        video_version.c.shot_id == shot_id,
                        video_version.c.is_current == 1,
                    )
                ).first()
                audio_record = connection.execute(
                    select(
                        shot_audio.c.id,
                        shot_audio.c.file_path,
                        shot_audio.c.duration_sec,
                    ).where(
                        shot_audio.c.shot_id == shot_id, shot_audio.c.is_current == 1
                    )
                ).first()

                shots_data.append(
                    {
                        "shot_id": shot_id,
                        "shot_no": shot_record.shot_no,
                        "duration_sec": shot_record.duration_sec,
                        "video_path": video_record.file_path if video_record else None,
                        "video_version_id": video_record.id if video_record else None,
                        "audio_path": audio_record.file_path if audio_record else None,
                        "audio_version_id": audio_record.id if audio_record else None,
                        "audio_duration": audio_record.duration_sec if audio_record else None,
                    }
                )

            return shots_data

    def _concat_videos(
        self,
        shots_data: list[dict[str, Any]],
        project_id: str,
        temp_path: Path,
        *,
        crossfade: bool = False,
        crossfade_seconds: float = 0.5,
    ) -> Path:
        """拼接视频"""
        if crossfade:
            return self._concat_videos_with_crossfade(
                shots_data,
                project_id,
                temp_path,
                crossfade_seconds,
            )
        # 创建 concat 文件
        concat_file = temp_path / "concat.txt"
        with concat_file.open("w", encoding="utf-8") as f:
            for shot in shots_data:
                # 数据库相对路径必须经项目路径校验，防止路径穿越
                video_full_path = file_service.resolve_project_path(
                    project_id, shot["video_path"]
                )
                # FFmpeg concat 需要绝对路径或用单引号包裹
                f.write(f"file '{_ffconcat_escape(video_full_path.as_posix())}'\n")

        output = temp_path / "concat.mp4"

        try:
            subprocess.run(
                [
                    settings.ffmpeg_path,
                    "-y",
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
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            return output
        except subprocess.CalledProcessError as exc:
            err_msg = exc.stderr.decode("utf-8", errors="ignore")[:200] if exc.stderr else ""
            logger.warning("直接流复制拼接失败，尝试回退到重编码拼接：%s", err_msg)
            return self._concat_videos_with_reencode(shots_data, project_id, temp_path)

    def _concat_videos_with_reencode(
        self,
        shots_data: list[dict[str, Any]],
        project_id: str,
        temp_path: Path,
    ) -> Path:
        """回退方案：重编码拼接（当输入视频参数不一致导致 stream copy 失败时使用）。"""
        video_paths = [
            file_service.resolve_project_path(project_id, shot["video_path"])
            for shot in shots_data
            if shot.get("video_path")
        ]
        if not video_paths:
            raise bad_request("没有可拼接的视频")
        if len(video_paths) == 1:
            return video_paths[0]

        first = self._get_video_stream_info(video_paths[0])
        width, height, fps = first["width"], first["height"], first["fps"]
        output = temp_path / "concat_reencode.mp4"

        cmd = [settings.ffmpeg_path, "-y"]
        for path in video_paths:
            cmd.extend(["-i", str(path)])

        filter_parts: list[str] = []
        concat_inputs: list[str] = []
        for i in range(len(video_paths)):
            filter_parts.append(
                f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}[v{i}]"
            )
            concat_inputs.append(f"[v{i}]")

        filter_parts.append(
            f"{''.join(concat_inputs)}concat=n={len(video_paths)}:v=1:a=0[outv]"
        )
        cmd.extend([
            "-filter_complex",
            ";".join(filter_parts),
            "-map",
            "[outv]",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "20",
            str(output),
        ])
        try:
            subprocess.run(
                cmd,
                check=True,
                timeout=600,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except subprocess.CalledProcessError as exc:
            error_msg = exc.stderr.decode("utf-8", errors="ignore") if exc.stderr else "未知错误"
            raise bad_request(f"重编码视频拼接失败: {error_msg[:200]}") from exc
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
        """实际使用的叠化时长（委托共用纯函数，见 subtitle_service）。"""
        return effective_crossfade_seconds(durations, crossfade_seconds)

    @staticmethod
    def _audio_offsets(
        durations: list[float],
        crossfade_seconds: float | None,
    ) -> list[float]:
        """按真实视频时长计算每段素材在成片时间轴上的起始时间。

        委托共用纯函数（见 subtitle_service），字幕与音轨共用同一公式，
        避免 xfade 偏移在两个服务里各自实现产生漂移。
        """
        return shot_start_offsets(durations, crossfade_seconds)

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
        project_id: str,
        temp_path: Path,
        crossfade_seconds: float,
    ) -> Path:
        """用 xfade 叠化拼接分镜视频（每个转场 0.5s）。"""
        # 数据库相对路径必须经项目路径校验，防止路径穿越
        video_paths = [
            file_service.resolve_project_path(project_id, shot["video_path"])
            for shot in shots_data
            if shot.get("video_path")
        ]
        if len(video_paths) < 2:
            return self._concat_videos(shots_data, project_id, temp_path)

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

        cmd = [settings.ffmpeg_path, "-y"]
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
                stdin=subprocess.DEVNULL,
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
        project_id: str,
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
                    self._get_video_duration(
                        file_service.resolve_project_path(
                            project_id, item["video_path"]
                        )
                    )
                )
            else:
                video_durations.append(float(item.get("duration_sec") or 0))
        starts = self._audio_offsets(video_durations, crossfade_seconds)
        audio_files = [
            (
                offset,
                file_service.resolve_project_path(project_id, item["audio_path"]),
            )
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

        cmd = [settings.ffmpeg_path, "-y"]
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
                stdin=subprocess.DEVNULL,
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
                "-y",
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
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        return output

    def _add_bgm(
        self,
        video_path: Path,
        bgm_id: str,
        project_id: str,
        temp_path: Path,
        volume: float,
    ) -> Path:
        """添加 BGM 音轨"""
        with self.database.engine.connect() as connection:
            # BGM 必须属于当前项目，防止跨项目引用其他项目的音频文件
            bgm_record = connection.execute(
                select(project_bgm.c.file_path).where(
                    project_bgm.c.id == bgm_id,
                    project_bgm.c.project_id == project_id,
                )
            ).first()

        if bgm_record is None:
            # 不再静默跳过：BGM 缺失或跨项目引用都让合成任务显式失败，
            # 错误消息不包含任何路径信息
            raise bad_request("BGM 不存在或不属于当前项目")

        # 数据库相对路径必须经项目路径校验，防止路径穿越
        bgm_full_path = file_service.resolve_project_path(
            project_id, bgm_record.file_path
        )
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
                "-y",
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
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        return output

    def _burn_subtitles(
        self,
        video_path: Path,
        chapter_id: str,
        temp_path: Path,
        *,
        shots_data: list[dict[str, Any]] | None = None,
        project_id: str = "",
        crossfade_seconds: float | None = None,
    ) -> Path:
        """烧录字幕

        Args:
            shots_data: 参与本次合成的分镜列表（已过滤出有当前视频的分镜）。
                提供且 project_id 非空时，字幕时间轴与视频轨/音轨保持一致：
                只累计这些分镜的真实视频时长并应用 xfade 偏移，
                无视频分镜的字幕不烧录。未提供时保持旧行为。
            project_id: 项目 ID，用于把数据库相对路径解析为安全的绝对路径。
            crossfade_seconds: 叠化时长；None 表示无叠化。
        """
        # 导出 SRT 文件
        shot_video_durations: Mapping[str, float] | None = None
        if shots_data is not None and project_id:
            shot_video_durations = {
                item["shot_id"]: self._get_video_duration(
                    file_service.resolve_project_path(project_id, item["video_path"])
                )
                for item in shots_data
                if item.get("video_path")
            }
        srt_content = self.subtitle_service.export_chapter_srt(
            chapter_id,
            shot_video_durations=shot_video_durations,
            crossfade_seconds=crossfade_seconds,
        )
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
                "-y",
                "-i",
                str(video_path),
                "-vf",
                (
                    f"subtitles={_ffmpeg_filter_path(srt_file.as_posix())}"
                    f":force_style={_ffmpeg_filter_quote(style)}"
                ),
                "-c:a",
                "copy",
                str(output),
            ],
            check=True,
            timeout=600,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        return output

    def _apply_anime_cadence(self, video_path: Path, temp_path: Path) -> Path:
        """动画节奏化：抽帧到 12fps（一拍二手绘张数感）并叠加轻微均匀颗粒。

        视频 AI 味的核心是全程均匀插值：把 25fps 合成结果抽到 12fps 后，
        观感更接近手绘动画的张数节奏；颗粒进一步压掉塑料感。音轨原样保留。
        """
        output = temp_path / "anime_cadence.mp4"
        subprocess.run(
            [
                settings.ffmpeg_path,
                "-y",
                "-i",
                str(video_path),
                "-vf",
                "fps=12,noise=alls=6:allf=u",
                "-r",
                "12",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "18",
                "-c:a",
                "copy",
                str(output),
            ],
            check=True,
            timeout=600,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        return output

    def _finalize_output(self, video_path: Path, output_path: Path, resolution: str) -> None:
        """最终输出（可选：调整分辨率）

        使用等比缩放 + pad，保持竖屏/异形比内容不被强行拉伸变形。
        """
        stream_info = self._get_video_stream_info(video_path)
        is_portrait = stream_info["height"] > stream_info["width"]

        if resolution == "720p":
            width, height = (720, 1280) if is_portrait else (1280, 720)
        elif resolution == "4k":
            width, height = (2160, 3840) if is_portrait else (3840, 2160)
        else:  # 1080p 或其他
            width, height = (1080, 1920) if is_portrait else (1920, 1080)

        if resolution in {"720p", "4k"}:
            scale = (
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
            )
            subprocess.run(
                [
                    settings.ffmpeg_path,
                    "-y",
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
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        else:
            # 直接复制
            subprocess.run(
                [
                    settings.ffmpeg_path,
                    "-y",
                    "-i",
                    str(video_path),
                    "-c",
                    "copy",
                    str(output_path),
                ],
                check=True,
                timeout=600,
                stdin=subprocess.DEVNULL,
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
            # 时长解析失败不能静默当 0 秒：0 时长会污染成片记录与时间轴，
            # 与音频侧「探测失败抛明确错误」的口径一致。
            raise bad_request(f"获取视频时长失败：{exc}") from exc

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

        result = row_dict(record)
        if result is None:
            return None
        # 对外返回前统一脱敏，避免历史 error 中的绝对路径或敏感信息
        # 经 /composition/chapters/{id}/composition 接口泄露
        result["error"] = sanitize_error_message(result.get("error", ""))
        return result
