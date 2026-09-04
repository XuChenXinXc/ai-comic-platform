"""字幕服务

处理字幕生成、编辑、导出等功能。
"""

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import delete, insert, select

from app.db import Database, db
from app.models.tables import shot, shot_audio, shot_subtitle
from app.services.errors import bad_request, not_found
from app.services.utils import new_id, row_dict, utc_now

logger = logging.getLogger(__name__)


def effective_crossfade_seconds(
    durations: Sequence[float],
    crossfade_seconds: float,
) -> float:
    """实际使用的叠化时长：单段不叠化，且不能超过最短素材的一半。

    该公式与合成服务的 xfade 链保持一致，字幕与音轨时间轴共用，
    避免两边各自实现导致漂移。
    """
    if len(durations) < 2:
        return 0.0
    return max(0.1, min(float(crossfade_seconds), min(durations) / 2))


def shot_start_offsets(
    durations: Sequence[float],
    crossfade_seconds: float | None,
) -> list[float]:
    """按真实视频时长计算每段素材在成片时间轴上的起始时间。

    crossfade_seconds 为 None 表示无叠化（偏移即累计时长）；否则按
    xfade 重叠公式扣减叠化时长。与合成服务的配音音轨偏移公式一致。
    """
    fade = (
        effective_crossfade_seconds(durations, crossfade_seconds)
        if crossfade_seconds is not None
        else 0.0
    )
    offsets: list[float] = []
    cursor = 0.0
    for index, duration in enumerate(durations):
        offsets.append(cursor)
        cursor += duration - (fade if index < len(durations) - 1 else 0)
    return offsets


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

            # 简单策略:整句字幕,从 0 到 duration_sec
            # TODO(#1): 更精细的策略可以调用语音识别 API 获取时间戳
            # 当前使用均分策略足够满足 V1 需求,V2 优化为精确对齐
            # 改进方案:集成 Azure Speech / 阿里云 / 讯飞语音识别 API
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

    def export_chapter_srt(
        self,
        chapter_id: str,
        *,
        shot_video_durations: Mapping[str, float] | None = None,
        crossfade_seconds: float | None = None,
    ) -> str:
        """导出章节的 SRT 格式字幕文件

        Args:
            chapter_id: 章节 ID。
            shot_video_durations: 分镜 ID 到「当前完成视频实际时长」的映射。
                提供时，只有映射中的分镜参与时间轴累计（与合成视频轨跳过
                无视频分镜的行为一致），偏移按真实时长 + xfade 叠化计算，
                与配音音轨公式一致；映射外分镜的字幕不导出。
                未提供时保持旧行为：全部分镜按计划时长 duration_sec 累计。
            crossfade_seconds: 叠化时长；None 表示无叠化。
        """
        with self.database.engine.connect() as connection:
            # 获取章节所有分镜
            shot_records = connection.execute(
                select(shot.c.id, shot.c.shot_no, shot.c.duration_sec)
                .where(shot.c.chapter_id == chapter_id)
                .order_by(shot.c.shot_order)
            ).fetchall()

            if not shot_records:
                raise not_found(f"章节无分镜：{chapter_id}")

            # 确定参与时间轴的分镜及其时长来源
            if shot_video_durations is None:
                # 旧行为：全部分镜按计划时长累计
                participants = [
                    (shot_record, float(shot_record.duration_sec or 6.0))
                    for shot_record in shot_records
                ]
                fade_seconds = None
            else:
                # 新行为：只累计有当前视频的分镜的真实时长
                participants = [
                    (shot_record, float(shot_video_durations[shot_record.id]))
                    for shot_record in shot_records
                    if shot_record.id in shot_video_durations
                ]
                fade_seconds = crossfade_seconds
            offsets = shot_start_offsets(
                [duration for _, duration in participants],
                fade_seconds,
            )

            srt_content = []
            subtitle_index = 1

            for (shot_record, _shot_duration), shot_offset in zip(
                participants, offsets, strict=True
            ):
                # 获取该分镜的字幕
                subtitle_records = connection.execute(
                    select(shot_subtitle)
                    .where(shot_subtitle.c.shot_id == shot_record.id)
                    .order_by(shot_subtitle.c.start_time)
                ).fetchall()

                for subtitle_record in subtitle_records:
                    subtitle_dict = row_dict(subtitle_record)
                    start = shot_offset + subtitle_dict["start_time"]
                    end = shot_offset + subtitle_dict["end_time"]
                    text = subtitle_dict["text"]

                    # SRT 格式
                    srt_content.append(str(subtitle_index))
                    srt_content.append(
                        f"{self._format_srt_time(start)} --> {self._format_srt_time(end)}"
                    )
                    srt_content.append(text)
                    srt_content.append("")  # 空行

                    subtitle_index += 1

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
