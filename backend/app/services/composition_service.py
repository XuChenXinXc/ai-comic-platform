"""合成服务

处理章节视频合成，包括视频拼接、配音混音、BGM添加、字幕烧录等。
"""

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
    keyframe_version,
    project,
    project_bgm,
    shot,
    shot_audio,
    shot_subtitle,
    video_version,
)
from app.services.errors import bad_request, not_found
from app.services.file_service import file_service
from app.services.job_service import JobService
from app.services.subtitle_service import SubtitleService
from app.services.utils import json_dumps, json_loads, new_id, row_dict, utc_now

logger = logging.getLogger(__name__)


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
            job_id = new_id()
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
            composition_id = new_id()
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

            # 加入任务队列
            self.jobs.runner.enqueue(job_id)

            record = connection.execute(
                select(chapter_composition).where(chapter_composition.c.id == composition_id)
            ).first()

        return row_dict(record)

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

            # 2. 检查所有视频是否已生成
            missing_videos = [s["shot_no"] for s in shots_data if not s.get("video_path")]
            if missing_videos:
                raise RuntimeError(f"以下分镜未生成视频：{missing_videos}")

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

                # 5.1 拼接视频
                concat_video = self._concat_videos(shots_data, project_path, temp_path)

                # 5.2 添加配音（如果启用）
                if config.get("include_audio"):
                    concat_video = self._add_audio(concat_video, shots_data, project_path, temp_path)

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
        self, shots_data: list[dict[str, Any]], project_path: Path, temp_path: Path
    ) -> Path:
        """拼接视频"""
        # 创建 concat 文件
        concat_file = temp_path / "concat.txt"
        with concat_file.open("w", encoding="utf-8") as f:
            for shot in shots_data:
                video_full_path = project_path / shot["video_path"]
                # FFmpeg concat 需要绝对路径或用单引号包裹
                f.write(f"file '{video_full_path.as_posix()}'\n")

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

    def _add_audio(
        self, video_path: Path, shots_data: list[dict[str, Any]], project_path: Path, temp_path: Path
    ) -> Path:
        """添加配音音轨"""
        # 为每个分镜的配音创建静音填充，使其与视频对齐
        audio_files = []
        current_time = 0.0

        for shot in shots_data:
            if shot["audio_path"]:
                audio_full_path = project_path / shot["audio_path"]
                audio_files.append((current_time, audio_full_path))
            current_time += shot["duration_sec"]

        if not audio_files:
            return video_path

        # 合并所有配音（带时间偏移）
        concat_audio = temp_path / "concat_audio.mp3"
        filter_complex = []
        for idx, (offset, audio_path) in enumerate(audio_files):
            filter_complex.append(f"[{idx}]adelay={int(offset * 1000)}|{int(offset * 1000)}[a{idx}]")

        filter_complex.append(f"{''.join([f'[a{i}]' for i in range(len(audio_files))])}amix=inputs={len(audio_files)}[aout]")

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
            subprocess.run(cmd, check=True, timeout=300, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
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
                f"[0:a]afade=t=in:st=0:d=2,afade=t=out:st={video_duration - 2}:d=2,volume={volume}[bgm];"
                f"[1:a][bgm]amix=inputs=2:duration=first[aout]",
                "-map",
                "1:v:0",
                "-map",
                "[aout]",
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
        style = "FontName=Microsoft YaHei,FontSize=24,PrimaryColour=&HFFFFFF,OutlineColour=&H000000,Outline=2"

        subprocess.run(
            [
                settings.ffmpeg_path,
                "-i",
                str(video_path),
                "-vf",
                f"subtitles={srt_file.as_posix()}:force_style='{style}'",
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
