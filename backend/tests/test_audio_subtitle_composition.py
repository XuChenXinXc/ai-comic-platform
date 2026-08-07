"""测试音频、字幕和合成功能"""

from pathlib import Path

import pytest
from sqlalchemy import insert

from app.db import Database
from app.models.tables import audio_config, chapter, project, shot, shot_audio, shot_subtitle
from app.services.audio_service import AudioService
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
        conn.execute(shot_audio.update().where(shot_audio.c.shot_id == shot_id).values(is_current=0))
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
