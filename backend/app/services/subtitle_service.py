"""字幕服务

处理字幕生成、编辑、导出等功能。
"""

import logging
from pathlib import Path
from typing import Any

from sqlalchemy import delete, insert, select, update

from app.db import Database, db
from app.models.tables import shot, shot_audio, shot_subtitle
from app.services.errors import bad_request, not_found
from app.services.utils import new_id, row_dict, utc_now

logger = logging.getLogger(__name__)


class SubtitleService:
    def __init__(self, database: Database | None = None) -> None:
        self.database = database or db

    def generate_subtitle_from_audio(self, shot_id: str) -> list[dict[str, Any]]:
        """根据配音自动生成字幕时间轴"""
        with self.database.engine.connect() as connection:
            # 获取分镜配音
            audio_record = connection.execute(
                select(shot_audio).where(
                    shot_audio.c.shot_id == shot_id, shot_audio.c.is_current == 1
                )
            ).first()

            if audio_record is None:
                raise not_found(f"分镜未生成配音：{shot_id}")

            audio_dict = row_dict(audio_record)
            dialogue_text = audio_dict["dialogue_text"]
            duration_sec = audio_dict.get("duration_sec", 0)

            if not duration_sec or duration_sec <= 0:
                raise bad_request("配音时长无效，无法生成字幕")

            # 简单策略：整句字幕，从 0 到 duration_sec
            # TODO(#1): 更精细的策略可以调用语音识别 API 获取时间戳
            # 详见 FEATURE_TRACKING.md - Issue #1
            subtitle_data = [
                {
                    "text": dialogue_text,
                    "start_time": 0.0,
                    "end_time": duration_sec,
                    "style_preset": "default",
                }
            ]

        return subtitle_data

    def save_subtitles(self, shot_id: str, subtitles: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """保存分镜字幕"""
        with self.database.engine.begin() as connection:
            # 检查分镜是否存在
            shot_record = connection.execute(
                select(shot.c.id).where(shot.c.id == shot_id)
            ).first()
            if shot_record is None:
                raise not_found(f"分镜不存在：{shot_id}")

            # 删除旧字幕
            connection.execute(delete(shot_subtitle).where(shot_subtitle.c.shot_id == shot_id))

            # 插入新字幕
            now = utc_now()
            for subtitle_item in subtitles:
                subtitle_id = new_id("subtitle")
                connection.execute(
                    insert(shot_subtitle).values(
                        id=subtitle_id,
                        shot_id=shot_id,
                        text=subtitle_item["text"],
                        start_time=subtitle_item["start_time"],
                        end_time=subtitle_item["end_time"],
                        style_preset=subtitle_item.get("style_preset", "default"),
                        created_at=now,
                    )
                )

            # 返回保存后的字幕
            records = connection.execute(
                select(shot_subtitle)
                .where(shot_subtitle.c.shot_id == shot_id)
                .order_by(shot_subtitle.c.start_time)
            ).fetchall()

        return [row_dict(r) for r in records]

    def get_shot_subtitles(self, shot_id: str) -> list[dict[str, Any]]:
        """获取分镜字幕"""
        with self.database.engine.connect() as connection:
            records = connection.execute(
                select(shot_subtitle)
                .where(shot_subtitle.c.shot_id == shot_id)
                .order_by(shot_subtitle.c.start_time)
            ).fetchall()

        return [row_dict(r) for r in records]

    def export_chapter_srt(self, chapter_id: str) -> str:
        """导出章节的 SRT 格式字幕文件"""
        with self.database.engine.connect() as connection:
            # 获取章节所有分镜
            shot_records = connection.execute(
                select(shot.c.id, shot.c.shot_no, shot.c.duration_sec)
                .where(shot.c.chapter_id == chapter_id)
                .order_by(shot.c.shot_order)
            ).fetchall()

            if not shot_records:
                raise not_found(f"章节无分镜：{chapter_id}")

            srt_content = []
            subtitle_index = 1
            current_time = 0.0

            for shot_record in shot_records:
                shot_id = shot_record.id
                shot_duration = shot_record.duration_sec or 6.0

                # 获取该分镜的字幕
                subtitle_records = connection.execute(
                    select(shot_subtitle)
                    .where(shot_subtitle.c.shot_id == shot_id)
                    .order_by(shot_subtitle.c.start_time)
                ).fetchall()

                for subtitle_record in subtitle_records:
                    subtitle_dict = row_dict(subtitle_record)
                    start = current_time + subtitle_dict["start_time"]
                    end = current_time + subtitle_dict["end_time"]
                    text = subtitle_dict["text"]

                    # SRT 格式
                    srt_content.append(str(subtitle_index))
                    srt_content.append(f"{self._format_srt_time(start)} --> {self._format_srt_time(end)}")
                    srt_content.append(text)
                    srt_content.append("")  # 空行

                    subtitle_index += 1

                current_time += shot_duration

        return "\n".join(srt_content)

    @staticmethod
    def _format_srt_time(seconds: float) -> str:
        """格式化 SRT 时间戳：00:00:00,000"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        millis = int((seconds % 1) * 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

    def get_subtitle_styles(self) -> dict[str, dict[str, str]]:
        """获取预设字幕样式"""
        return {
            "default": {
                "name": "默认",
                "position": "bottom",
                "font": "Microsoft YaHei",
                "font_size": "24",
                "color": "white",
                "outline": "2",
                "outline_color": "black",
            },
            "top": {
                "name": "顶部",
                "position": "top",
                "font": "Microsoft YaHei",
                "font_size": "24",
                "color": "white",
                "outline": "2",
                "outline_color": "black",
            },
            "floating": {
                "name": "悬浮",
                "position": "middle",
                "font": "Microsoft YaHei",
                "font_size": "28",
                "color": "yellow",
                "outline": "3",
                "outline_color": "black",
            },
        }
