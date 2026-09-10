"""测试音频、字幕和合成功能"""

import asyncio
import json
from pathlib import Path

from sqlalchemy import insert, select

from app.db import Database
from app.models.tables import chapter, project, screenplay, shot, shot_audio, video_version
from app.services.audio_service import AudioService
from app.services.composition_service import CompositionService
from app.services.file_service import file_service
from app.services.subtitle_service import SubtitleService
from app.services.utils import new_id, utc_now


def test_audio_config_crud(tmp_path: Path):
    """测试音频配置的增删改查"""
    test_database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    test_database.create_all()
    audio_service = AudioService(test_database)

    # 创建测试项目
    project_id = new_id("project")
    now = utc_now()
    with test_database.engine.begin() as conn:
        conn.execute(
            insert(project).values(
                id=project_id,
                name="测试项目",
                storage_name=project_id,
                created_at=now,
                updated_at=now,
            )
        )

    # 获取默认配置
    config = audio_service.get_project_audio_config(project_id)
    assert config["project_id"] == project_id
    assert config["bgm_volume"] == 0.3
    assert config["dialogue_volume"] == 1.0

    # 更新配置
    updated = audio_service.update_project_audio_config(
        project_id, {"bgm_volume": 0.5, "dialogue_volume": 0.8}
    )
    assert updated["bgm_volume"] == 0.5
    assert updated["dialogue_volume"] == 0.8

    # 再次获取验证
    config = audio_service.get_project_audio_config(project_id)
    assert config["bgm_volume"] == 0.5


def test_compose_chapter_creates_job_and_record(
    tmp_path: Path,
    monkeypatch,
) -> None:
    test_database = Database(f"sqlite:///{tmp_path / 'compose.db'}")
    test_database.create_all()
    project_id = new_id("project")
    chapter_id = new_id("chapter")
    now = utc_now()
    with test_database.engine.begin() as conn:
        conn.execute(
            insert(project).values(
                id=project_id,
                name="测试项目",
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

    service = CompositionService(test_database)
    enqueued: list[str] = []
    monkeypatch.setattr(
        service.jobs.runner,
        "enqueue",
        lambda job_id: enqueued.append(job_id) or False,
    )

    result = service.compose_chapter(
        chapter_id,
        {
            "resolution": "1080p",
            "include_audio": False,
            "burn_subtitles": False,
        },
    )

    assert result["chapter_id"] == chapter_id
    assert result["job_id"]
    assert result["status"] == "pending"
    assert result["resolution"] == "1080p"
    assert enqueued == [result["job_id"]]


def test_compose_skips_shots_without_videos(
    tmp_path: Path,
    monkeypatch,
) -> None:
    test_database = Database(f"sqlite:///{tmp_path / 'compose-skip.db'}")
    test_database.create_all()
    project_id = new_id("project")
    chapter_id = new_id("chapter")
    now = utc_now()
    with test_database.engine.begin() as conn:
        conn.execute(
            insert(project).values(
                id=project_id,
                name="测试项目",
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
        for shot_no in (1, 2):
            conn.execute(
                insert(shot).values(
                    id=new_id("shot"),
                    project_id=project_id,
                    chapter_id=chapter_id,
                    shot_no=shot_no,
                    shot_order=shot_no,
                    source_type="screenplay",
                    source_ref="manual",
                    duration_sec=6,
                    created_at=now,
                    updated_at=now,
                )
            )
        shot_ids = [
            row[0]
            for row in conn.execute(
                select(shot.c.id)
                .where(shot.c.chapter_id == chapter_id)
                .order_by(shot.c.shot_no),
            )
        ]
        conn.execute(
            insert(video_version).values(
                id=new_id("video"),
                shot_id=shot_ids[0],
                version_no=1,
                file_path="shot-1.mp4",
                actual_duration_sec=6,
                status="completed",
                is_current=1,
                created_at=now,
            )
        )

    service = CompositionService(test_database)
    seen_shot_nos: list[list[int]] = []
    monkeypatch.setattr(
        file_service,
        "project_path",
        lambda _project_id: tmp_path,
    )
    monkeypatch.setattr(
        service,
        "_concat_videos",
        lambda shots_data, *_args, **_kwargs: (
            seen_shot_nos.append([item["shot_no"] for item in shots_data])
            or tmp_path / "concat.mp4"
        ),
    )
    monkeypatch.setattr(service, "_finalize_output", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "_get_video_duration", lambda _path: 6.0)

    asyncio.run(
        service.handle_compose(
            {
                "chapter_id": chapter_id,
                "input_json": "{}",
            }
        )
    )

    assert seen_shot_nos == [[1]]


def test_anime_cadence_ffmpeg_args_use_12fps_and_grain(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """动画节奏化：抽帧到 12fps + 轻微颗粒，音轨原样保留。"""
    test_database = Database(f"sqlite:///{tmp_path / 'cadence.db'}")
    service = CompositionService(test_database)

    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append([str(item) for item in cmd])
        return None

    monkeypatch.setattr("app.services.composition_service.subprocess.run", fake_run)

    output = service._apply_anime_cadence(Path("concat.mp4"), tmp_path)

    assert output == tmp_path / "anime_cadence.mp4"
    assert captured, "应调用 ffmpeg"
    cmd = captured[0]
    assert "fps=12,noise=alls=6:allf=u" in cmd
    assert "-r" in cmd and cmd[cmd.index("-r") + 1] == "12"
    assert "-c:a" in cmd and cmd[cmd.index("-c:a") + 1] == "copy"


def test_handle_compose_calls_anime_cadence_when_enabled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """合成配置开启 anime_cadence 时执行动画节奏化后处理。"""
    test_database = Database(f"sqlite:///{tmp_path / 'compose-cadence.db'}")
    test_database.create_all()
    project_id = new_id("project")
    chapter_id = new_id("chapter")
    now = utc_now()
    with test_database.engine.begin() as conn:
        conn.execute(
            insert(project).values(
                id=project_id,
                name="测试项目",
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
        conn.execute(
            insert(shot).values(
                id=new_id("shot"),
                project_id=project_id,
                chapter_id=chapter_id,
                shot_no=1,
                shot_order=1,
                source_type="screenplay",
                source_ref="manual",
                duration_sec=6,
                created_at=now,
                updated_at=now,
            )
        )
        shot_id = conn.execute(
            select(shot.c.id).where(shot.c.chapter_id == chapter_id)
        ).scalar_one()
        conn.execute(
            insert(video_version).values(
                id=new_id("video"),
                shot_id=shot_id,
                version_no=1,
                file_path="shot-1.mp4",
                actual_duration_sec=6,
                status="completed",
                is_current=1,
                created_at=now,
            )
        )

    service = CompositionService(test_database)
    monkeypatch.setattr(
        file_service,
        "project_path",
        lambda _project_id: tmp_path,
    )
    monkeypatch.setattr(
        service,
        "_concat_videos",
        lambda *_args, **_kwargs: tmp_path / "concat.mp4",
    )
    cadence_calls: list[Path] = []
    monkeypatch.setattr(
        service,
        "_apply_anime_cadence",
        lambda video_path, temp_path: (
            cadence_calls.append(video_path) or temp_path / "anime_cadence.mp4"
        ),
    )
    finalized: list[Path] = []
    monkeypatch.setattr(
        service,
        "_finalize_output",
        lambda video_path, output_path, resolution: finalized.append(video_path),
    )
    monkeypatch.setattr(service, "_get_video_duration", lambda _path: 6.0)

    asyncio.run(
        service.handle_compose(
            {
                "chapter_id": chapter_id,
                "input_json": json.dumps({"anime_cadence": True}),
            }
        )
    )

    assert cadence_calls == [tmp_path / "concat.mp4"]
    assert len(finalized) == 1
    assert finalized[0].name == "anime_cadence.mp4"


def test_crossfade_offsets_chain_correctly() -> None:
    offsets = CompositionService._crossfade_offsets([6.0, 6.0, 6.0], 0.5)

    assert offsets == [5.5, 11.0]


def test_audio_offsets_follow_crossfaded_video_timeline() -> None:
    starts = CompositionService._audio_offsets([6.0, 6.0, 6.0], 0.5)

    assert starts == [0.0, 5.5, 11.0]


def test_audio_offsets_without_crossfade_use_cumulative_duration() -> None:
    starts = CompositionService._audio_offsets([6.0, 4.0, 8.0], None)

    assert starts == [0.0, 6.0, 10.0]


def test_finalize_output_adapts_to_portrait_aspect_ratio(tmp_path: Path, monkeypatch) -> None:
    """分辨率终审：竖屏视频（9:16）缩放时自动匹配竖屏分辨率，避免被横屏拉伸加黑边。"""
    service = CompositionService()
    monkeypatch.setattr(
        service,
        "_get_video_stream_info",
        lambda _p: {"width": 1080, "height": 1920, "fps": 25, "has_audio": True},
    )
    captured_commands: list[list[str]] = []
    import subprocess

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **_kwargs: captured_commands.append(cmd),
    )

    service._finalize_output(tmp_path / "in.mp4", tmp_path / "out.mp4", "720p")
    assert len(captured_commands) == 1
    # 竖屏 720p 应为 720:1280 而非 1280:720
    vf_arg = captured_commands[0][captured_commands[0].index("-vf") + 1]
    assert "scale=720:1280" in vf_arg
    assert "pad=720:1280" in vf_arg


def test_effective_crossfade_seconds_is_capped_by_shortest_clip() -> None:
    fade = CompositionService._effective_crossfade_seconds([6.0, 1.0], 2.0)

    assert fade == 0.5


def test_shot_audio_version_management(tmp_path: Path):
    """测试分镜配音版本管理"""
    test_database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    test_database.create_all()

    # 创建测试数据
    project_id = new_id("project")
    chapter_id = new_id("chapter")
    shot_id = new_id("shot")
    now = utc_now()

    with test_database.engine.begin() as conn:
        conn.execute(
            insert(project).values(
                id=project_id,
                name="测试项目",
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
        conn.execute(
            insert(shot).values(
                id=shot_id,
                project_id=project_id,
                chapter_id=chapter_id,
                shot_no=1,
                shot_order=1,
                source_type="novel",
                created_at=now,
                updated_at=now,
            )
        )

    audio_service = AudioService(test_database)

    # 初始无配音
    assert audio_service.get_shot_audio(shot_id) is None

    # 手动插入第一个版本
    audio_id_1 = new_id("audio")
    with test_database.engine.begin() as conn:
        conn.execute(
            insert(shot_audio).values(
                id=audio_id_1,
                shot_id=shot_id,
                dialogue_text="测试台词1",
                version_no=1,
                is_current=1,
                status="completed",
                created_at=now,
            )
        )

    current = audio_service.get_shot_audio(shot_id)
    assert current is not None
    assert current["dialogue_text"] == "测试台词1"
    assert current["version_no"] == 1

    # 插入第二个版本
    audio_id_2 = new_id("audio")
    with test_database.engine.begin() as conn:
        # 取消旧版本
        conn.execute(
            shot_audio.update()
            .where(shot_audio.c.shot_id == shot_id)
            .values(is_current=0)
        )
        # 插入新版本
        conn.execute(
            insert(shot_audio).values(
                id=audio_id_2,
                shot_id=shot_id,
                dialogue_text="测试台词2",
                version_no=2,
                is_current=1,
                status="completed",
                created_at=now,
            )
        )

    current = audio_service.get_shot_audio(shot_id)
    assert current["dialogue_text"] == "测试台词2"
    assert current["version_no"] == 2

    # 列出所有版本
    versions = audio_service.list_shot_audios(shot_id)
    assert len(versions) == 2
    assert versions[0]["version_no"] == 2  # 降序排列
    assert versions[1]["version_no"] == 1


def test_list_chapter_audios_returns_current_audio_per_shot(tmp_path: Path):
    """章节配音列表：每个分镜返回当前配音，无配音的分镜返回 None。"""
    test_database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    test_database.create_all()
    project_id = new_id("project")
    chapter_id = new_id("chapter")
    shot_id_1 = new_id("shot")
    shot_id_2 = new_id("shot")
    now = utc_now()

    with test_database.engine.begin() as conn:
        conn.execute(
            insert(project).values(
                id=project_id,
                name="测试项目",
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
        for index, shot_id in enumerate((shot_id_1, shot_id_2), start=1):
            conn.execute(
                insert(shot).values(
                    id=shot_id,
                    project_id=project_id,
                    chapter_id=chapter_id,
                    shot_no=index,
                    shot_order=index,
                    source_type="novel",
                    created_at=now,
                    updated_at=now,
                )
            )
        conn.execute(
            insert(shot_audio).values(
                id=new_id("audio"),
                shot_id=shot_id_1,
                dialogue_text="第一镜台词",
                version_no=1,
                is_current=1,
                status="completed",
                created_at=now,
            )
        )

    audios = AudioService(test_database).list_chapter_audios(chapter_id)

    assert [item["shot_no"] for item in audios] == [1, 2]
    assert audios[0]["audio"]["dialogue_text"] == "第一镜台词"
    assert audios[1]["audio"] is None


def test_delete_shot_audio_promotes_latest_version(tmp_path: Path):
    """删除当前配音后，自动提升最新版本为当前。"""
    test_database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    test_database.create_all()
    project_id = new_id("project")
    chapter_id = new_id("chapter")
    shot_id = new_id("shot")
    now = utc_now()

    with test_database.engine.begin() as conn:
        conn.execute(
            insert(project).values(
                id=project_id,
                name="测试项目",
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
        conn.execute(
            insert(shot).values(
                id=shot_id,
                project_id=project_id,
                chapter_id=chapter_id,
                shot_no=1,
                shot_order=1,
                source_type="novel",
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            insert(shot_audio).values(
                id=new_id("audio"),
                shot_id=shot_id,
                dialogue_text="旧版本",
                version_no=1,
                is_current=1,
                status="completed",
                created_at=now,
            )
        )
        conn.execute(
            insert(shot_audio).values(
                id=new_id("audio"),
                shot_id=shot_id,
                dialogue_text="新版本",
                version_no=2,
                is_current=0,
                status="completed",
                created_at=now,
            )
        )

    audio_service = AudioService(test_database)
    current = audio_service.get_shot_audio(shot_id)
    audio_service.delete_shot_audio(current["id"])

    promoted = audio_service.get_shot_audio(shot_id)
    assert promoted is not None
    assert promoted["dialogue_text"] == "新版本"
    assert promoted["version_no"] == 2
    assert promoted["is_current"] == 1


def test_subtitle_time_validation(tmp_path: Path):
    """测试字幕时间轴验证"""
    test_database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    test_database.create_all()
    subtitle_service = SubtitleService(test_database)

    # 创建测试数据
    project_id = new_id("project")
    chapter_id = new_id("chapter")
    shot_id = new_id("shot")
    now = utc_now()

    with test_database.engine.begin() as conn:
        conn.execute(
            insert(project).values(
                id=project_id,
                name="测试项目",
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
        conn.execute(
            insert(shot).values(
                id=shot_id,
                project_id=project_id,
                chapter_id=chapter_id,
                shot_no=1,
                shot_order=1,
                source_type="novel",
                created_at=now,
                updated_at=now,
            )
        )

    # 保存字幕
    subtitles = [
        {"text": "第一句", "start_time": 0.0, "end_time": 2.0, "style_preset": "default"},
        {"text": "第二句", "start_time": 2.0, "end_time": 4.5, "style_preset": "default"},
    ]

    saved = subtitle_service.save_subtitles(shot_id, subtitles)
    assert len(saved) == 2
    assert saved[0]["text"] == "第一句"
    assert saved[0]["start_time"] == 0.0

    # 获取字幕
    retrieved = subtitle_service.get_shot_subtitles(shot_id)
    assert len(retrieved) == 2
    assert retrieved[0]["start_time"] < retrieved[1]["start_time"]  # 按时间排序


def test_srt_time_format():
    """测试 SRT 时间格式化"""
    subtitle_service = SubtitleService()

    # 测试各种时间格式
    assert subtitle_service._format_srt_time(0) == "00:00:00,000"
    assert subtitle_service._format_srt_time(1.5) == "00:00:01,500"
    assert subtitle_service._format_srt_time(65.123) == "00:01:05,123"
    assert subtitle_service._format_srt_time(3661.456) == "01:01:01,456"


def test_bgm_management(tmp_path: Path):
    """测试 BGM 管理"""
    test_database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    test_database.create_all()
    audio_service = AudioService(test_database)

    # 创建测试项目
    project_id = new_id("project")
    now = utc_now()
    with test_database.engine.begin() as conn:
        conn.execute(
            insert(project).values(
                id=project_id,
                name="测试项目",
                storage_name=project_id,
                created_at=now,
                updated_at=now,
            )
        )

    # 初始无 BGM
    bgms = audio_service.list_project_bgms(project_id)
    assert len(bgms) == 0

    # 注意：save_bgm 需要实际文件系统，这里只测试数据库逻辑
    # 实际保存 BGM 需要在集成测试中进行


def test_extract_dialogues_supports_ready_and_confirmed(tmp_path: Path):
    """从剧本提取对话：状态为 ready 和 confirmed 时均支持正常提取对话。"""
    test_database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    test_database.create_all()
    project_id = new_id("project")
    chapter_id = new_id("chapter")
    now = utc_now()

    with test_database.engine.begin() as conn:
        conn.execute(
            insert(project).values(
                id=project_id,
                name="测试项目",
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
        content = {
            "title": "测试剧本",
            "logline": "一句话介绍",
            "scenes": [
                {
                    "scene_no": 1,
                    "scene_name": "客栈",
                    "shots": [
                        {
                            "shot_no": 1,
                            "dialogue": [{"speaker": "张三", "text": "掌柜的，来两斤牛肉。"}],
                        },
                        {
                            "shot_no": 2,
                            "dialogue": [{"speaker": "掌柜", "text": "好嘞，客官请稍坐！"}],
                        },
                    ],
                }
            ],
        }
        conn.execute(
            insert(screenplay).values(
                chapter_id=chapter_id,
                content_json=json.dumps(content, ensure_ascii=False),
                status="ready",
                source_signature="sig-1",
                revision=1,
                updated_at=now,
            )
        )

    service = AudioService(test_database)
    # 1. 状态为 ready 时正常提取
    dialogues_ready = service.extract_dialogues_from_screenplay(chapter_id)
    assert len(dialogues_ready) == 2
    assert dialogues_ready[0]["speaker"] == "张三"
    assert dialogues_ready[0]["text"] == "掌柜的，来两斤牛肉。"

    # 2. 状态为 confirmed 时亦正常提取
    with test_database.engine.begin() as conn:
        conn.execute(
            screenplay.update()
            .where(screenplay.c.chapter_id == chapter_id)
            .values(status="confirmed")
        )
    dialogues_confirmed = service.extract_dialogues_from_screenplay(chapter_id)
    assert len(dialogues_confirmed) == 2
    assert dialogues_confirmed[1]["speaker"] == "掌柜"

