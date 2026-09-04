"""合成与字幕时间轴修复回归测试

覆盖：
- C1 字幕时间轴按真实视频时长 + xfade 偏移计算，跳过无视频分镜
- C2 BGM 项目归属校验 + 路径穿越拒绝
- B-H2 合成 Job 的 provider_kind 标记为 compose
- B-M2 合成输入路径经 resolve_project_path 校验
- B-M7 合成失败 error 入库前脱敏
"""

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import insert, select

from app.db import Database
from app.models.tables import (
    chapter,
    chapter_composition,
    job,
    project,
    project_bgm,
    shot,
    shot_subtitle,
    video_version,
)
from app.services.composition_service import CompositionService
from app.services.errors import ServiceError
from app.services.file_service import FileServiceError, file_service
from app.services.subtitle_service import SubtitleService, shot_start_offsets
from app.services.utils import json_dumps, new_id, utc_now


def _create_project_chapter(
    database: Database,
    tmp_path: Path,
    *,
    db_name: str,
    shot_specs: list[dict],
) -> tuple[str, str, list[str]]:
    """创建项目、章节与分镜；返回 (project_id, chapter_id, shot_ids)。

    shot_specs 每项：duration_sec（计划时长）、video_file（None 表示无当前视频）。
    """
    project_id = new_id("project")
    chapter_id = new_id("chapter")
    now = utc_now()
    shot_ids: list[str] = []
    with database.engine.begin() as conn:
        conn.execute(
            insert(project).values(
                id=project_id,
                name=f"测试项目-{db_name}",
                storage_name=project_id,
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            insert(chapter).values(
                id=chapter_id,
                project_id=project_id,
                chapter_no=1,
                created_at=now,
                updated_at=now,
            )
        )
        for index, spec in enumerate(shot_specs, start=1):
            shot_id = new_id("shot")
            shot_ids.append(shot_id)
            conn.execute(
                insert(shot).values(
                    id=shot_id,
                    project_id=project_id,
                    chapter_id=chapter_id,
                    shot_no=index,
                    shot_order=index,
                    source_type="novel",
                    duration_sec=spec.get("duration_sec", 6.0),
                    created_at=now,
                    updated_at=now,
                )
            )
            if spec.get("video_file"):
                conn.execute(
                    insert(video_version).values(
                        id=new_id("video"),
                        shot_id=shot_id,
                        version_no=1,
                        file_path=spec["video_file"],
                        status="completed",
                        is_current=1,
                        created_at=now,
                    )
                )
    return project_id, chapter_id, shot_ids


def _save_subtitles(
    database: Database,
    shot_id: str,
    subtitles: list[dict],
) -> None:
    now = utc_now()
    with database.engine.begin() as conn:
        for item in subtitles:
            conn.execute(
                insert(shot_subtitle).values(
                    id=new_id("subtitle"),
                    shot_id=shot_id,
                    text=item["text"],
                    start_time=item["start_time"],
                    end_time=item["end_time"],
                    created_at=now,
                )
            )


def _parse_srt_blocks(srt_content: str) -> list[dict]:
    """把 SRT 文本解析为 [{index, time, text}] 便于断言。"""
    blocks: list[dict] = []
    parts = [part.strip() for part in srt_content.split("\n\n") if part.strip()]
    for part in parts:
        lines = part.split("\n")
        if len(lines) >= 3:
            blocks.append(
                {
                    "index": int(lines[0]),
                    "time": lines[1],
                    "text": "\n".join(lines[2:]),
                }
            )
    return blocks


# ---------------------------------------------------------------------------
# C1：字幕时间轴
# ---------------------------------------------------------------------------


def test_export_chapter_srt_skips_shots_without_video_and_uses_real_durations(
    tmp_path: Path,
) -> None:
    """无当前视频的分镜不参与时间轴累计，字幕与视频轨跳过行为一致。"""
    database = Database(f"sqlite:///{tmp_path / 'srt-skip.db'}")
    database.create_all()
    # 三个分镜：镜 1 / 镜 3 有视频，镜 2 没有视频（会被合成视频轨跳过）
    project_id, chapter_id, shot_ids = _create_project_chapter(
        database,
        tmp_path,
        db_name="srt-skip",
        shot_specs=[
            {"duration_sec": 6.0, "video_file": "shot-1.mp4"},
            {"duration_sec": 4.0, "video_file": None},
            {"duration_sec": 8.0, "video_file": "shot-3.mp4"},
        ],
    )
    _save_subtitles(
        database, shot_ids[0], [{"text": "镜一台词", "start_time": 1.0, "end_time": 2.0}]
    )
    _save_subtitles(
        database, shot_ids[1], [{"text": "镜二台词", "start_time": 0.5, "end_time": 1.5}]
    )
    _save_subtitles(
        database, shot_ids[2], [{"text": "镜三台词", "start_time": 1.0, "end_time": 2.0}]
    )

    service = SubtitleService(database)
    # 只提供有视频分镜的真实时长（与 handle_compose 过滤后的 shots_data 一致）
    srt = service.export_chapter_srt(
        chapter_id,
        shot_video_durations={shot_ids[0]: 6.0, shot_ids[2]: 8.0},
        crossfade_seconds=None,
    )

    blocks = _parse_srt_blocks(srt)
    # 镜 2 无视频：字幕不导出，其计划时长不参与累计
    assert [block["text"] for block in blocks] == ["镜一台词", "镜三台词"]
    # 镜 1 偏移 0：1.0 -> 2.0
    assert blocks[0]["time"] == "00:00:01,000 --> 00:00:02,000"
    # 镜 3 偏移 = 镜 1 真实时长 6.0（跳过镜 2 的计划时长 4.0）：7.0 -> 8.0
    assert blocks[1]["time"] == "00:00:07,000 --> 00:00:08,000"


def test_export_chapter_srt_applies_crossfade_offset_like_audio_track(
    tmp_path: Path,
) -> None:
    """开启叠化时，字幕偏移与配音音轨公式一致（含 xfade 扣减）。"""
    database = Database(f"sqlite:///{tmp_path / 'srt-xfade.db'}")
    database.create_all()
    project_id, chapter_id, shot_ids = _create_project_chapter(
        database,
        tmp_path,
        db_name="srt-xfade",
        shot_specs=[
            {"duration_sec": 6.0, "video_file": "shot-1.mp4"},
            {"duration_sec": 6.0, "video_file": "shot-2.mp4"},
        ],
    )
    _save_subtitles(database, shot_ids[0], [{"text": "镜一", "start_time": 1.0, "end_time": 2.0}])
    _save_subtitles(database, shot_ids[1], [{"text": "镜二", "start_time": 1.0, "end_time": 2.0}])

    service = SubtitleService(database)
    srt = service.export_chapter_srt(
        chapter_id,
        shot_video_durations={shot_ids[0]: 6.0, shot_ids[1]: 6.0},
        crossfade_seconds=0.5,
    )

    blocks = _parse_srt_blocks(srt)
    # 镜 2 的偏移 = 6.0 - 0.5 = 5.5（与 _audio_offsets 一致），字幕 1.0 -> 2.0
    # 映射到成片时间轴为 6.5 -> 7.5
    assert blocks[1]["time"] == "00:00:06,500 --> 00:00:07,500"
    # 与配音音轨公式保持一致：两条轨道对同一分镜使用相同起始偏移
    audio_starts = CompositionService._audio_offsets([6.0, 6.0], 0.5)
    assert audio_starts == [0.0, 5.5]


def test_export_chapter_srt_default_uses_planned_durations(tmp_path: Path) -> None:
    """不传新参数时保持旧行为：全部分镜按计划时长累计。"""
    database = Database(f"sqlite:///{tmp_path / 'srt-legacy.db'}")
    database.create_all()
    project_id, chapter_id, shot_ids = _create_project_chapter(
        database,
        tmp_path,
        db_name="srt-legacy",
        shot_specs=[
            {"duration_sec": 6.0, "video_file": "shot-1.mp4"},
            {"duration_sec": 4.0, "video_file": "shot-2.mp4"},
        ],
    )
    _save_subtitles(database, shot_ids[1], [{"text": "镜二", "start_time": 1.0, "end_time": 2.0}])

    service = SubtitleService(database)
    srt = service.export_chapter_srt(chapter_id)

    blocks = _parse_srt_blocks(srt)
    # 旧逻辑：镜 2 偏移 = 镜 1 计划时长 6.0
    assert blocks[0]["time"] == "00:00:07,000 --> 00:00:08,000"


def test_shot_start_offsets_matches_composition_audio_offsets() -> None:
    """字幕偏移纯函数与合成音轨偏移公式逐项一致。"""
    cases: list[tuple[list[float], float | None]] = [
        ([6.0, 6.0, 6.0], 0.5),
        ([6.0, 4.0, 8.0], None),
        ([6.0, 1.0], 2.0),
        ([5.0], 0.5),
        ([], 0.5),
    ]
    for durations, crossfade in cases:
        assert shot_start_offsets(durations, crossfade) == (
            CompositionService._audio_offsets(durations, crossfade)
        )


def test_burn_subtitles_passes_real_durations_and_crossfade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_burn_subtitles 把过滤后分镜的真实时长与叠化参数传给字幕导出。"""
    database = Database(f"sqlite:///{tmp_path / 'burn-wiring.db'}")
    database.create_all()
    service = CompositionService(database)

    monkeypatch.setattr(file_service, "project_path", lambda _project_id: tmp_path)
    monkeypatch.setattr(service, "_get_video_duration", lambda _path: 6.0)
    captured: dict = {}

    def fake_export(chapter_id: str, **kwargs: object) -> str:
        captured.update(kwargs)
        return ""  # 空 SRT：直接跳过烧录，避免真实 ffmpeg

    monkeypatch.setattr(service.subtitle_service, "export_chapter_srt", fake_export)

    shots_data = [
        {"shot_id": "shot_a", "video_path": "v1.mp4"},
        {"shot_id": "shot_b", "video_path": "v2.mp4"},
    ]
    result = service._burn_subtitles(
        tmp_path / "input.mp4",
        "chapter_x",
        tmp_path,
        shots_data=shots_data,
        project_id="project_x",
        crossfade_seconds=0.5,
    )

    assert result == tmp_path / "input.mp4"
    assert captured == {
        "shot_video_durations": {"shot_a": 6.0, "shot_b": 6.0},
        "crossfade_seconds": 0.5,
    }

    # 未提供合成上下文时退回旧行为（不传映射、无叠化）
    captured.clear()
    service._burn_subtitles(tmp_path / "input.mp4", "chapter_x", tmp_path)
    assert captured == {
        "shot_video_durations": None,
        "crossfade_seconds": None,
    }


# ---------------------------------------------------------------------------
# C2 / B-M2：BGM 归属与路径校验
# ---------------------------------------------------------------------------


def _insert_bgm(database: Database, project_id: str, file_path: str) -> str:
    bgm_id = new_id("bgm")
    with database.engine.begin() as conn:
        conn.execute(
            insert(project_bgm).values(
                id=bgm_id,
                project_id=project_id,
                name="测试 BGM",
                file_path=file_path,
                created_at=utc_now(),
            )
        )
    return bgm_id


def test_add_bgm_rejects_cross_project_bgm(tmp_path: Path) -> None:
    """BGM 属于其他项目时，合成任务显式失败而不是静默跳过。"""
    database = Database(f"sqlite:///{tmp_path / 'bgm-cross.db'}")
    database.create_all()
    project_a, chapter_a, _ = _create_project_chapter(
        database, tmp_path, db_name="bgm-cross-a", shot_specs=[{"video_file": "s.mp4"}]
    )
    project_b, _, _ = _create_project_chapter(
        database, tmp_path, db_name="bgm-cross-b", shot_specs=[]
    )
    bgm_id_b = _insert_bgm(database, project_b, "bgm/b.mp3")

    service = CompositionService(database)

    with pytest.raises(ServiceError, match="BGM 不存在或不属于当前项目"):
        service._add_bgm(
            tmp_path / "video.mp4",
            bgm_id_b,
            project_a,
            tmp_path,
            volume=0.3,
        )


def test_add_bgm_rejects_path_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BGM 数据库相对路径包含 .. 时在拼接前被拒绝。"""
    database = Database(f"sqlite:///{tmp_path / 'bgm-traversal.db'}")
    database.create_all()
    project_id, _, _ = _create_project_chapter(
        database, tmp_path, db_name="bgm-traversal", shot_specs=[]
    )
    bgm_id = _insert_bgm(database, project_id, "../outside/bgm.mp3")

    monkeypatch.setattr(file_service, "project_path", lambda _project_id: tmp_path)
    service = CompositionService(database)

    with pytest.raises(FileServiceError, match="项目相对路径"):
        service._add_bgm(
            tmp_path / "video.mp4",
            bgm_id,
            project_id,
            tmp_path,
            volume=0.3,
        )


def test_concat_videos_rejects_path_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """拼接视频时数据库相对路径必须经项目路径校验。"""
    database = Database(f"sqlite:///{tmp_path / 'concat-traversal.db'}")
    database.create_all()
    monkeypatch.setattr(file_service, "project_path", lambda _project_id: tmp_path)
    service = CompositionService(database)

    shots_data = [{"shot_id": "shot_a", "video_path": "../outside/evil.mp4"}]
    with pytest.raises(FileServiceError, match="项目相对路径"):
        service._concat_videos(shots_data, "project_x", tmp_path)


def test_add_audio_rejects_path_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配音文件路径包含 .. 时在混音前被拒绝。"""
    database = Database(f"sqlite:///{tmp_path / 'audio-traversal.db'}")
    database.create_all()
    monkeypatch.setattr(file_service, "project_path", lambda _project_id: tmp_path)
    service = CompositionService(database)

    shots_data = [
        {
            "shot_id": "shot_a",
            "video_path": None,
            "duration_sec": 6,
            "audio_path": "../outside/evil.mp3",
        }
    ]
    with pytest.raises(FileServiceError, match="项目相对路径"):
        service._add_audio(tmp_path / "video.mp4", shots_data, "project_x", tmp_path)


# ---------------------------------------------------------------------------
# B-H2 / B-M7：Job 标记与错误脱敏
# ---------------------------------------------------------------------------


def test_compose_job_uses_compose_provider_kind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """合成 Job 的 provider_kind 记录为 compose，不再占用 text 标识。"""
    database = Database(f"sqlite:///{tmp_path / 'compose-kind.db'}")
    database.create_all()
    project_id, chapter_id, _ = _create_project_chapter(
        database, tmp_path, db_name="compose-kind", shot_specs=[]
    )

    service = CompositionService(database)
    monkeypatch.setattr(
        service.jobs.runner,
        "enqueue",
        lambda job_id: True,
    )
    result = service.compose_chapter(chapter_id, {"resolution": "1080p"})

    with database.engine.connect() as conn:
        provider_kind = conn.scalar(
            select(job.c.provider_kind).where(job.c.id == result["job_id"])
        )
    assert provider_kind == "compose"


def test_compose_fails_when_bgm_from_other_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """合成任务使用其他项目的 BGM 时失败，error 写明原因且不含绝对路径。"""
    database = Database(f"sqlite:///{tmp_path / 'bgm-compose-fail.db'}")
    database.create_all()
    project_a, chapter_a, _ = _create_project_chapter(
        database,
        tmp_path,
        db_name="bgm-fail-a",
        shot_specs=[{"duration_sec": 6.0, "video_file": "shot-1.mp4"}],
    )
    project_b, _, _ = _create_project_chapter(
        database, tmp_path, db_name="bgm-fail-b", shot_specs=[]
    )
    bgm_id_b = _insert_bgm(database, project_b, "bgm/b.mp3")

    monkeypatch.setattr(file_service, "project_path", lambda _project_id: tmp_path)
    service = CompositionService(database)
    monkeypatch.setattr(service.jobs.runner, "enqueue", lambda job_id: True)

    config = {
        "resolution": "1080p",
        "include_audio": False,
        "burn_subtitles": False,
        "bgm_id": bgm_id_b,
    }
    record = service.compose_chapter(chapter_a, config, enqueue=False)
    monkeypatch.setattr(
        service,
        "_concat_videos",
        lambda *_args, **_kwargs: tmp_path / "concat.mp4",
    )
    monkeypatch.setattr(service, "_get_video_duration", lambda _path: 6.0)

    with pytest.raises(ServiceError, match="BGM 不存在或不属于当前项目"):
        asyncio.run(
            service.handle_compose(
                {
                    "id": record["job_id"],
                    "chapter_id": chapter_a,
                    "input_json": json_dumps(config),
                }
            )
        )

    with database.engine.connect() as conn:
        failed = conn.execute(
            select(chapter_composition.c.status, chapter_composition.c.error).where(
                chapter_composition.c.job_id == record["job_id"]
            )
        ).first()
    assert failed is not None
    assert failed.status == "failed"
    assert failed.error == "BGM 不存在或不属于当前项目"
    # 错误消息不得泄露测试环境的绝对路径
    assert str(tmp_path) not in failed.error


def test_compose_failure_error_is_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """合成失败时入库的 error 经过脱敏，不含 token/URL 等敏感信息。"""
    database = Database(f"sqlite:///{tmp_path / 'compose-sanitize.db'}")
    database.create_all()
    project_id, chapter_id, _ = _create_project_chapter(
        database,
        tmp_path,
        db_name="compose-sanitize",
        shot_specs=[{"duration_sec": 6.0, "video_file": "shot-1.mp4"}],
    )

    monkeypatch.setattr(file_service, "project_path", lambda _project_id: tmp_path)
    service = CompositionService(database)
    monkeypatch.setattr(service.jobs.runner, "enqueue", lambda job_id: True)

    config = {"resolution": "1080p", "include_audio": False, "burn_subtitles": False}
    record = service.compose_chapter(chapter_id, config, enqueue=False)
    monkeypatch.setattr(
        service,
        "_concat_videos",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("下载失败 token=secret123 参见 http://evil.example/x")
        ),
    )
    monkeypatch.setattr(service, "_get_video_duration", lambda _path: 6.0)

    with pytest.raises(RuntimeError):
        asyncio.run(
            service.handle_compose(
                {
                    "id": record["job_id"],
                    "chapter_id": chapter_id,
                    "input_json": json_dumps(config),
                }
            )
        )

    with database.engine.connect() as conn:
        error_text = conn.scalar(
            select(chapter_composition.c.error).where(
                chapter_composition.c.job_id == record["job_id"]
            )
        )
    assert error_text is not None
    assert "secret123" not in error_text
    assert "evil.example" not in error_text
    assert "[已隐藏]" in error_text
    # 对外读取接口同样脱敏
    public = service.get_chapter_composition(chapter_id)
    assert public is not None
    assert "secret123" not in public["error"]
