"""Phase 6B 纯字幕时间轴与人工校正测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.db import Database
from app.dynamic_pipeline.subtitle_delivery import (
    SubtitleCorrection,
    SubtitleLine,
    SubtitleTimelineError,
    apply_subtitle_correction,
    build_absolute_timeline,
    build_subtitle_lines,
    render_srt,
)
from app.models.tables import chapter, project, shot, shot_audio
from app.services.subtitle_service import SubtitleService
from app.services.utils import new_id, utc_now


def test_subtitle_models_are_strict_and_validate_time_range() -> None:
    with pytest.raises(ValidationError):
        SubtitleLine.model_validate(
            {
                "dialogue_line_id": "line-1",
                "text": "台词",
                "start_time": 0,
                "end_time": 1,
                "unknown": "不允许",
            }
        )

    with pytest.raises(ValidationError):
        SubtitleCorrection(dialogue_line_id="line-1")


def test_build_subtitle_lines_binds_aliases_and_keeps_multiple_dialogues() -> None:
    timeline = build_subtitle_lines(
        [
            {
                "line_id": "line-1",
                "speaker": "甲",
                "line": "先说",
                "audio": {"id": "audio-1"},
            },
            {
                "dialogue_line_id": "line-2",
                "character_name": "乙",
                "content": "后说",
                "audio_id": "audio-2",
            },
        ],
        audio_durations={"audio-1": 1.25, "audio-2": 0.75},
        shot_duration_sec=5,
        source_signature="script-signature",
    )

    assert [line.dialogue_line_id for line in timeline.lines] == ["line-1", "line-2"]
    assert [line.audio_id for line in timeline.lines] == ["audio-1", "audio-2"]
    assert [line.speaker for line in timeline.lines] == ["甲", "乙"]
    assert [(line.start_time, line.end_time) for line in timeline.lines] == [
        (0.0, 1.25),
        (1.25, 2.0),
    ]
    assert timeline.video_duration_snapshot == 5
    assert timeline.timeline_signature


def test_build_subtitle_lines_prefers_explicit_times_and_actual_video_duration() -> None:
    timeline = build_subtitle_lines(
        [
            {
                "dialogue_line_id": "line-1",
                "text": "有明确时间",
                "start_sec": 3.2,
                "end_sec": 4.8,
                "audio_id": "audio-1",
            }
        ],
        audio_durations={"audio-1": 1},
        shot_duration_sec=3,
        video_duration_sec=5,
        source_signature="source-1",
    )

    assert timeline.lines[0].start_time == 3.2
    assert timeline.lines[0].end_time == 4.8
    assert timeline.video_duration_snapshot == 5


def test_build_subtitle_lines_blocks_out_of_bounds_without_truncating() -> None:
    with pytest.raises(SubtitleTimelineError) as error:
        build_subtitle_lines(
            [{"dialogue_line_id": "line-1", "text": "太长"}],
            audio_durations={"line-1": 4},
            shot_duration_sec=3,
            source_signature="source-1",
        )

    assert any(item["code"] == "subtitle_out_of_bounds" for item in error.value.diagnostics)
    assert "4" in str(error.value)


def test_build_subtitle_lines_preserves_explicit_overlap_with_warning() -> None:
    timeline = build_subtitle_lines(
        [
            {
                "dialogue_line_id": "line-1",
                "text": "重叠一",
                "start_time": 0,
                "end_time": 2,
            },
            {
                "dialogue_line_id": "line-2",
                "text": "重叠二",
                "start_time": 1,
                "end_time": 3,
            },
        ],
        shot_duration_sec=3,
        source_signature="source-1",
    )

    assert timeline.status == "warning"
    assert len(timeline.lines) == 2
    assert "line-1" in timeline.warnings[0]
    assert "line-2" in timeline.warnings[0]


def test_build_absolute_timeline_uses_actual_duration_and_xfade_offsets() -> None:
    first = build_subtitle_lines(
        [{"dialogue_line_id": "line-1", "audio_id": "audio-1", "text": "一"}],
        audio_durations={"audio-1": 1},
        shot_duration_sec=6,
        source_signature="source-1",
    )
    second = build_subtitle_lines(
        [{"dialogue_line_id": "line-2", "audio_id": "audio-2", "text": "二"}],
        audio_durations={"audio-2": 1},
        shot_duration_sec=7,
        source_signature="source-2",
    )

    absolute = build_absolute_timeline(
        [
            {"shot_id": "shot-1", "shot_order": 1, "duration_sec": 6, "timeline": first},
            {"shot_id": "shot-2", "shot_order": 2, "duration_sec": 7, "timeline": second},
        ],
        actual_video_durations={"shot-1": 5.5, "shot-2": 4.25},
        crossfade_sec=0.5,
    )

    assert absolute.lines[0].start_time == 0
    assert absolute.lines[0].end_time == 1
    assert absolute.lines[1].start_time == 5.0
    assert absolute.lines[1].end_time == 6.0
    assert absolute.provenance["shot_offsets_sec"] == {"shot-1": 0.0, "shot-2": 5.0}
    assert absolute.video_duration_snapshot == 9.25


def test_build_absolute_timeline_warns_when_actual_duration_is_missing() -> None:
    relative = build_subtitle_lines(
        [{"dialogue_line_id": "line-1", "text": "字幕"}],
        audio_durations={"line-1": 1},
        shot_duration_sec=3,
        source_signature="source-1",
    )

    absolute = build_absolute_timeline(
        [{"shot_id": "shot-1", "shot_order": 1, "duration_sec": 3, "timeline": relative}],
        actual_video_durations={},
    )

    assert absolute.status == "warning"
    assert absolute.video_duration_snapshot == 3
    assert "shot duration" in absolute.warnings[0]


def test_apply_subtitle_correction_changes_revision_signature_and_preserves_input() -> None:
    original = build_subtitle_lines(
        [{"dialogue_line_id": "line-1", "audio_id": "audio-1", "text": "原文"}],
        audio_durations={"audio-1": 2},
        shot_duration_sec=4,
        source_signature="source-1",
    )
    original_dump = original.model_dump(mode="python")

    corrected = apply_subtitle_correction(
        original,
        [
            {
                "dialogue_line_id": "line-1",
                "text": "人工改文",
                "start": 0.25,
                "end": 1.5,
                "style": "top",
            }
        ],
    )

    assert original.model_dump(mode="python") == original_dump
    assert corrected.revision == 2
    assert corrected.timeline_signature != original.timeline_signature
    assert corrected.lines[0].text == "人工改文"
    assert corrected.lines[0].style_preset == "top"
    assert (corrected.lines[0].start_time, corrected.lines[0].end_time) == (0.25, 1.5)


def test_apply_subtitle_correction_blocks_missing_line_and_out_of_bounds() -> None:
    timeline = build_subtitle_lines(
        [{"dialogue_line_id": "line-1", "text": "原文"}],
        audio_durations={"line-1": 1},
        shot_duration_sec=2,
        source_signature="source-1",
    )

    with pytest.raises(SubtitleTimelineError) as missing_error:
        apply_subtitle_correction(
            timeline,
            [{"dialogue_line_id": "missing", "text": "不存在"}],
        )
    assert any(
        item["code"] == "dialogue_line_not_found" for item in missing_error.value.diagnostics
    )

    with pytest.raises(SubtitleTimelineError) as range_error:
        apply_subtitle_correction(
            timeline,
            [{"dialogue_line_id": "line-1", "end_time": 2.1}],
        )
    assert any(item["code"] == "subtitle_out_of_bounds" for item in range_error.value.diagnostics)


def test_render_srt_rounds_milliseconds_and_sorts_stably() -> None:
    lines = [
        SubtitleLine(
            dialogue_line_id="line-2",
            text="后一条",
            start_time=1.0005,
            end_time=2.0005,
        ),
        SubtitleLine(
            dialogue_line_id="line-1",
            text="前一条",
            start_time=0.9995,
            end_time=1.9995,
        ),
    ]

    srt = render_srt(lines)

    assert srt == (
        "1\n00:00:01,000 --> 00:00:02,000\n前一条\n\n2\n00:00:01,001 --> 00:00:02,001\n后一条"
    )


def test_legacy_subtitle_generation_remains_unchanged(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'legacy-subtitle.db'}")
    database.create_all()
    project_id = new_id("project")
    chapter_id = new_id("chapter")
    shot_id = new_id("shot")
    now = utc_now()

    with database.engine.begin() as connection:
        connection.execute(
            project.insert().values(
                id=project_id,
                name="legacy",
                storage_name=project_id,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            chapter.insert().values(
                id=chapter_id,
                project_id=project_id,
                chapter_no=1,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            shot.insert().values(
                id=shot_id,
                project_id=project_id,
                chapter_id=chapter_id,
                shot_no=1,
                shot_order=1,
                source_type="screenplay",
                source_ref="legacy",
                duration_sec=3,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            shot_audio.insert().values(
                id=new_id("audio"),
                shot_id=shot_id,
                dialogue_text="旧字幕台词",
                duration_sec=1.75,
                version_no=1,
                is_current=1,
                status="completed",
                created_at=now,
            )
        )

    result = SubtitleService(database).generate_subtitle_from_audio(shot_id)

    assert result == [
        {
            "text": "旧字幕台词",
            "start_time": 0.0,
            "end_time": 1.75,
            "style_preset": "default",
        }
    ]
