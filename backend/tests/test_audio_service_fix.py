"""音频服务修复回归测试

覆盖：
- voice_mapping 音色映射服务端持久化（B：音色映射）
- generate_shot_audio / save_bgm 事务外文件 IO 与 ffprobe 探测（B-H1）
- ffprobe 失败抛明确错误且不残留文件（B-B8）
- 章节/分镜删除清理配音与成片文件，失败不 500（B-H3）
- delete_bgm 路径经 resolve_project_path 校验（B-M3）
- TTS Provider 通过公开接口解析（B-M4）
- download_url_atomic 协议白名单（B-L1）
- cleanup_old_jobs 事务与保留策略（B-L4）
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, insert, select, update

from app.db import Database
from app.models.tables import (
    api_provider,
    audio_config,
    chapter,
    chapter_composition,
    job,
    project,
    project_bgm,
    shot,
    shot_audio,
)
from app.services import audio_service as audio_service_module
from app.services.audio_service import AudioService
from app.services.chapter_service import ChapterService
from app.services.errors import ServiceError
from app.services.file_service import FileServiceError, file_service
from app.services.job_service import JobService
from app.services.provider_service import ProviderService
from app.services.shot_service import ShotService
from app.services.utils import new_id, utc_now

EDGE_VOICE = "edge-tts:zh-CN-XiaoxiaoNeural"


async def _always_audio(_path: Path) -> bool:
    """测试替身：视为包含音频流。"""
    return True


@pytest.fixture
def patched_file_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Database:
    """构造测试数据库并把全局 file_service 指向临时工作区。"""
    database = Database(f"sqlite:///{(tmp_path / 'audio-fix.db').as_posix()}")
    database.create_all()
    monkeypatch.setattr(file_service, "workspace_dir", tmp_path.resolve())
    monkeypatch.setattr(file_service, "projects_dir", (tmp_path / "projects").resolve())
    # 显式绑定测试数据库：服务层不再隐式改写全局单例的 database
    monkeypatch.setattr(file_service, "database", database)
    return database


def _insert_project(database: Database) -> str:
    project_id = new_id("project")
    now = utc_now()
    with database.engine.begin() as conn:
        conn.execute(
            insert(project).values(
                id=project_id,
                name="修复测试项目",
                storage_name=project_id,
                created_at=now,
                updated_at=now,
            )
        )
    return project_id


def _insert_chapter_shot(database: Database, project_id: str) -> tuple[str, str]:
    chapter_id = new_id("chapter")
    shot_id = new_id("shot")
    now = utc_now()
    with database.engine.begin() as conn:
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
    return chapter_id, shot_id


# ============ 音色映射持久化 ============


def test_update_config_validates_volume_range_and_blank_provider(
    patched_file_service: Database,
) -> None:
    """音量超范围/非法类型被 400 拒绝，tts_provider_id 空串归一为 NULL。

    回归：此前音量越界违反 CHECK 约束、空串违反 FK，均以未捕获
    IntegrityError 500 暴露。
    """
    database = patched_file_service
    project_id = _insert_project(database)
    service = AudioService(database)

    for bad_volume in (1.5, -0.1, "loud", True):
        with pytest.raises(ServiceError):
            service.update_project_audio_config(
                project_id, {"bgm_volume": bad_volume}
            )
    with pytest.raises(ServiceError):
        service.update_project_audio_config(project_id, {"dialogue_volume": 2})

    # 空串 Provider 归一化为 NULL，可正常保存（清除默认 TTS）。
    updated = service.update_project_audio_config(
        project_id, {"tts_provider_id": "", "bgm_volume": 0.8}
    )
    assert updated["bgm_volume"] == 0.8
    assert updated["tts_provider_id"] is None


def test_voice_mapping_default_is_empty(patched_file_service: Database) -> None:
    """无配置记录时返回空 voice_mapping。"""
    database = patched_file_service
    project_id = _insert_project(database)
    config = AudioService(database).get_project_audio_config(project_id)
    assert config["voice_mapping"] == {}


def test_voice_mapping_roundtrip(patched_file_service: Database) -> None:
    """合法音色映射保存到 voice_mapping_json 并回显。"""
    database = patched_file_service
    project_id = _insert_project(database)
    service = AudioService(database)

    mapping = {
        "林舟": EDGE_VOICE,
        "苏晚": "nova",
    }
    updated = service.update_project_audio_config(project_id, {"voice_mapping": mapping})
    assert updated["voice_mapping"] == mapping

    config = service.get_project_audio_config(project_id)
    assert config["voice_mapping"] == mapping

    # 原始列确以 JSON 存储（ensure_ascii=False 保留中文）
    with database.engine.connect() as conn:
        raw = conn.scalar(
            select(audio_config.c.voice_mapping_json).where(
                audio_config.c.project_id == project_id
            )
        )
    assert "林舟" in str(raw)


@pytest.mark.parametrize(
    "bad_mapping",
    [
        ["not", "a", "dict"],
        {"林舟": 123},
        {"": "nova"},
        {f"角色{i}": "nova" for i in range(201)},
    ],
    ids=["non-dict", "non-str-value", "empty-key", "over-limit"],
)
def test_voice_mapping_invalid_input_rejected(
    patched_file_service: Database, bad_mapping: Any
) -> None:
    """非法音色映射被拒绝且不落库。"""
    database = patched_file_service
    project_id = _insert_project(database)
    service = AudioService(database)

    with pytest.raises(ServiceError):
        service.update_project_audio_config(project_id, {"voice_mapping": bad_mapping})

    with database.engine.connect() as conn:
        count = conn.scalar(
            select(func.count()).select_from(audio_config).where(
                audio_config.c.project_id == project_id
            )
        )
    assert count == 0


def test_voice_mapping_corrupt_json_falls_back_to_empty(
    patched_file_service: Database,
) -> None:
    """voice_mapping_json 损坏时解析失败回退空映射，不阻断读取。"""
    database = patched_file_service
    project_id = _insert_project(database)
    now = utc_now()
    with database.engine.begin() as conn:
        conn.execute(
            insert(audio_config).values(
                project_id=project_id,
                voice_mapping_json='["不是", "对象"]',
                updated_at=now,
            )
        )
    config = AudioService(database).get_project_audio_config(project_id)
    assert config["voice_mapping"] == {}


# ============ 事务外文件 IO 与时长探测（B-H1 / B-B8） ============


def test_generate_shot_audio_success_and_version_rotation(
    patched_file_service: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生成配音：文件落盘、时长入库、当前版本切换；文件 IO 期间数据库不持有写事务。"""
    database = patched_file_service
    project_id = _insert_project(database)
    chapter_id, shot_id = _insert_chapter_shot(database, project_id)
    service = AudioService(database)

    async def fake_tts(_text: str, _voice: str) -> bytes:
        return b"ID3-fake-audio"

    async def fake_duration(_path: Path) -> float:
        return 1.5

    monkeypatch.setattr(service, "_generate_with_edge_tts", fake_tts)

    # 探针：atomic_write_bytes 执行期间，用独立连接执行短写事务；
    # 若主流程仍在写事务内做文件 IO，这里会因写锁冲突失败。
    real_atomic_write = audio_service_module.atomic_write_bytes
    probe_results: list[bool] = []

    def probing_write(path: Any, data: bytes, **kwargs: Any) -> Any:
        with database.engine.begin() as conn:
            conn.execute(
                update(project)
                .where(project.c.id == project_id)
                .values(updated_at=utc_now())
            )
        probe_results.append(True)
        return real_atomic_write(path, data, **kwargs)

    monkeypatch.setattr(audio_service_module, "atomic_write_bytes", probing_write)
    monkeypatch.setattr(
        audio_service_module, "get_video_duration", fake_duration
    )

    first = asyncio.run(
        service.generate_shot_audio(shot_id, "第一句台词", EDGE_VOICE)
    )
    assert first["status"] == "completed"
    assert first["is_current"] == 1
    assert first["version_no"] == 1
    assert first["duration_sec"] == 1.5
    assert probe_results == [True]

    # 文件真实存在（文件名以生成唯一 ID 结尾，按返回的 file_path 校验）
    project_dir = file_service.project_path(project_id)
    audio_path = project_dir / first["file_path"]
    assert audio_path.is_file()

    # 生成第二版后旧版取消 current
    second = asyncio.run(
        service.generate_shot_audio(shot_id, "第二句台词", EDGE_VOICE)
    )
    assert second["version_no"] == 2
    assert second["is_current"] == 1
    with database.engine.connect() as conn:
        currents = conn.execute(
            select(shot_audio.c.version_no).where(
                shot_audio.c.shot_id == shot_id, shot_audio.c.is_current == 1
            )
        ).scalars().all()
    assert currents == [2]


def test_generate_shot_audio_duration_failure_raises_and_cleans_up(
    patched_file_service: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ffprobe 失败时抛「音频时长探测失败」且不写库、不残留文件（B-B8）。"""
    database = patched_file_service
    project_id = _insert_project(database)
    chapter_id, shot_id = _insert_chapter_shot(database, project_id)
    service = AudioService(database)

    async def fake_tts(_text: str, _voice: str) -> bytes:
        return b"ID3-fake-audio"

    async def broken_duration(_path: Path) -> float:
        raise ValueError("ffprobe 执行失败：exit 1")

    monkeypatch.setattr(service, "_generate_with_edge_tts", fake_tts)
    monkeypatch.setattr(audio_service_module, "get_video_duration", broken_duration)

    with pytest.raises(ServiceError, match="音频时长探测失败"):
        asyncio.run(service.generate_shot_audio(shot_id, "台词", EDGE_VOICE))

    # 不写库
    with database.engine.connect() as conn:
        count = conn.scalar(
            select(func.count()).select_from(shot_audio).where(
                shot_audio.c.shot_id == shot_id
            )
        )
    assert count == 0
    # 不残留文件
    audio_dir = file_service.project_path(project_id) / "分镜配音"
    assert not audio_dir.exists() or list(audio_dir.iterdir()) == []


def test_save_bgm_writes_file_and_duration_outside_transaction(
    patched_file_service: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """save_bgm：文件与时长落库正确（事务外完成文件 IO）。"""
    database = patched_file_service
    project_id = _insert_project(database)
    service = AudioService(database)

    async def fake_duration(_path: Path) -> float:
        return 12.5

    monkeypatch.setattr(audio_service_module, "get_video_duration", fake_duration)
    monkeypatch.setattr(audio_service_module, "has_audio_stream", _always_audio)

    record = asyncio.run(service.save_bgm(project_id, "片头曲", b"ID3-bgm", ["激昂"]))
    assert record["name"] == "片头曲"
    assert record["duration_sec"] == 12.5
    bgm_path = file_service.resolve_project_path(project_id, record["file_path"])
    assert bgm_path.is_file()


def test_save_bgm_rejects_file_without_audio_stream(
    patched_file_service: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无音频流的文件（如视频/图片冒充）被拒绝：不写库、不残留文件。"""
    database = patched_file_service
    project_id = _insert_project(database)
    service = AudioService(database)

    async def fake_duration(_path: Path) -> float:
        return 30.0

    async def no_audio(_path: Path) -> bool:
        return False

    monkeypatch.setattr(audio_service_module, "get_video_duration", fake_duration)
    monkeypatch.setattr(audio_service_module, "has_audio_stream", no_audio)

    with pytest.raises(ServiceError, match="不包含音频流"):
        asyncio.run(service.save_bgm(project_id, "伪音频", b"ID3-video", []))

    with database.engine.connect() as conn:
        count = conn.scalar(
            select(func.count())
            .select_from(project_bgm)
            .where(project_bgm.c.project_id == project_id)
        )
    assert count == 0
    bgm_dir = file_service.project_path(project_id) / "BGM"
    assert not bgm_dir.exists() or list(bgm_dir.iterdir()) == []


# ============ 章节/分镜删除清理配音与成片（B-H3） ============


def _insert_shot_audio_with_file(
    database: Database, shot_id: str, project_id: str, version_no: int
) -> Path:
    audio_id = new_id("audio")
    relative = f"分镜配音/shot-{shot_id}-v{version_no:03d}.mp3"
    path = file_service.resolve_project_path(project_id, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"ID3-audio")
    with database.engine.begin() as conn:
        conn.execute(
            insert(shot_audio).values(
                id=audio_id,
                shot_id=shot_id,
                dialogue_text="台词",
                version_no=version_no,
                file_path=relative,
                status="completed",
                is_current=1 if version_no == 1 else 0,
                created_at=utc_now(),
            )
        )
    return path


def test_chapter_delete_removes_audio_and_composition_files(
    patched_file_service: Database,
) -> None:
    """章节删除：配音文件与成片文件一并清理。"""
    database = patched_file_service
    project_id = _insert_project(database)
    chapter_id, shot_id = _insert_chapter_shot(database, project_id)

    audio_path = _insert_shot_audio_with_file(database, shot_id, project_id, 1)

    composition_relative = "export/chapter-compose.mp4"
    composition_path = file_service.resolve_project_path(
        project_id, composition_relative
    )
    composition_path.parent.mkdir(parents=True, exist_ok=True)
    composition_path.write_bytes(b"fake-mp4")
    now = utc_now()
    with database.engine.begin() as conn:
        conn.execute(
            insert(chapter_composition).values(
                id=new_id("composition"),
                chapter_id=chapter_id,
                file_path=composition_relative,
                status="completed",
                created_at=now,
                updated_at=now,
            )
        )

    assert audio_path.is_file()
    assert composition_path.is_file()

    ChapterService(database).delete(chapter_id)

    assert not audio_path.exists()
    assert not composition_path.exists()
    with database.engine.connect() as conn:
        remaining_audio = conn.scalar(
            select(func.count()).select_from(shot_audio).where(
                shot_audio.c.shot_id == shot_id
            )
        )
        remaining_composition = conn.scalar(
            select(func.count()).select_from(chapter_composition).where(
                chapter_composition.c.chapter_id == chapter_id
            )
        )
    assert remaining_audio == 0
    assert remaining_composition == 0


def test_chapter_delete_file_failure_does_not_raise(
    patched_file_service: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """章节删除：文件删除失败时记录日志继续，不向上抛 500。"""
    database = patched_file_service
    project_id = _insert_project(database)
    chapter_id, shot_id = _insert_chapter_shot(database, project_id)
    audio_path = _insert_shot_audio_with_file(database, shot_id, project_id, 1)

    service = ChapterService(database)

    def failing_delete(_project_id: str, _path: Any) -> bool:
        raise FileServiceError("文件被占用")

    monkeypatch.setattr(service.storage, "delete_file", failing_delete)

    # 不抛异常，章节行正常删除
    service.delete(chapter_id)
    with database.engine.connect() as conn:
        count = conn.scalar(
            select(func.count()).select_from(chapter).where(
                chapter.c.id == chapter_id
            )
        )
    assert count == 0
    assert audio_path.exists()  # 文件保留，等待人工处理


def test_shot_delete_removes_audio_files(
    patched_file_service: Database,
) -> None:
    """分镜删除：配音文件一并清理。"""
    database = patched_file_service
    project_id = _insert_project(database)
    chapter_id, shot_id = _insert_chapter_shot(database, project_id)
    audio_path = _insert_shot_audio_with_file(database, shot_id, project_id, 1)

    ShotService(database).delete(shot_id)

    assert not audio_path.exists()
    with database.engine.connect() as conn:
        count = conn.scalar(
            select(func.count()).select_from(shot_audio).where(
                shot_audio.c.shot_id == shot_id
            )
        )
    assert count == 0


def test_shot_delete_file_failure_does_not_raise(
    patched_file_service: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """分镜删除：文件删除失败时记录日志继续，不向上抛 500。"""
    database = patched_file_service
    project_id = _insert_project(database)
    chapter_id, shot_id = _insert_chapter_shot(database, project_id)
    _insert_shot_audio_with_file(database, shot_id, project_id, 1)

    def failing_delete(_project_id: str, _path: Any) -> bool:
        raise FileServiceError("文件被占用")

    monkeypatch.setattr(
        audio_service_module.file_service, "delete_file", failing_delete
    )
    # shot_service 使用模块级导入的同一 file_service 实例
    ShotService(database).delete(shot_id)
    with database.engine.connect() as conn:
        count = conn.scalar(
            select(func.count()).select_from(shot).where(shot.c.id == shot_id)
        )
    assert count == 0


# ============ delete_bgm 路径校验（B-M3） ============


def test_delete_bgm_escaping_path_rejected_by_resolve(
    patched_file_service: Database,
    tmp_path: Path,
) -> None:
    """BGM 记录带越界路径时：DB 行删除，但不越出项目目录删除文件。"""
    database = patched_file_service
    project_id = _insert_project(database)
    bgm_id = new_id("bgm")

    # 「../outside.mp3」相对项目目录解析出的项目外目标文件
    outside = (tmp_path / "projects" / "outside.mp3").resolve()
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"outside")

    with database.engine.begin() as conn:
        conn.execute(
            insert(project_bgm).values(
                id=bgm_id,
                project_id=project_id,
                name="越界 BGM",
                file_path="../outside.mp3",
                created_at=utc_now(),
            )
        )

    # delete_bgm 不抛错（尽力删除语义）；行已删除；项目外文件未被触碰
    AudioService(database).delete_bgm(bgm_id)
    with database.engine.connect() as conn:
        count = conn.scalar(
            select(func.count()).select_from(project_bgm).where(
                project_bgm.c.id == bgm_id
            )
        )
    assert count == 0
    assert outside.exists()


# ============ TTS Provider 公开解析接口（B-M4） ============


def test_resolve_tts_provider_validates_kind(patched_file_service: Database) -> None:
    """resolve_tts_provider 校验 kind 必须为 tts。"""
    database = patched_file_service
    providers = ProviderService(database)
    now = utc_now()
    text_provider_id = new_id("provider")
    tts_provider_id = new_id("provider")
    with database.engine.begin() as conn:
        conn.execute(
            insert(api_provider).values(
                id=text_provider_id,
                kind="text",
                name="文本 Provider",
                adapter="mock",
                created_at=now,
                updated_at=now,
                enabled=1,
            )
        )
        conn.execute(
            insert(api_provider).values(
                id=tts_provider_id,
                kind="tts",
                name="TTS Provider",
                adapter="mock",
                created_at=now,
                updated_at=now,
                enabled=1,
            )
        )

    with pytest.raises(ServiceError):
        providers.resolve_tts_provider(text_provider_id)

    provider, config = providers.resolve_tts_provider(tts_provider_id)
    assert config["kind"] == "tts"
    assert config["adapter"] == "mock"
    assert provider is not None


# ============ download_url_atomic 协议白名单（B-L1） ============


@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/Windows/system.ini",
        "ftp://example.com/file.mp3",
        "data:text/plain;base64,SGVsbG8=",
    ],
)
def test_download_url_atomic_rejects_non_http_scheme(
    tmp_path: Path, url: str
) -> None:
    """非 http/https 协议一律拒绝，防止 file:// 等读取本地文件。"""
    from app.services.file_service import download_url_atomic

    with pytest.raises(FileServiceError, match="不支持的下载协议"):
        download_url_atomic(url, tmp_path / "out.bin")


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://192.168.1.10/video.mp4",
        "http://10.0.0.8/video.mp4",
        "http://127.0.0.1:8012/api/health",
    ],
)
def test_download_url_atomic_rejects_private_hosts(
    tmp_path: Path, url: str
) -> None:
    """SSRF 防护：私网/链路本地/云元数据/回环地址一律拒绝。"""
    from app.services.file_service import download_url_atomic

    with pytest.raises(FileServiceError, match="内网或保留地址"):
        download_url_atomic(url, tmp_path / "out.bin")


# ============ cleanup_old_jobs 事务与保留策略（B-L4） ============


def _insert_job(
    database: Database,
    project_id: str,
    job_id: str,
    *,
    status: str,
    finished_at: str | None,
) -> None:
    now = utc_now()
    with database.engine.begin() as conn:
        conn.execute(
            insert(job).values(
                id=job_id,
                project_id=project_id,
                job_type="generate_keyframe",
                status=status,
                finished_at=finished_at,
                created_at=now,
                updated_at=now,
            )
        )


def test_cleanup_old_jobs_keeps_recent_and_failed(patched_file_service: Database) -> None:
    """清理旧任务：默认保留失败任务与新任务。"""
    database = patched_file_service
    project_id = _insert_project(database)

    old = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    recent = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

    _insert_job(database, project_id, "job-old-done", status="completed", finished_at=old)
    _insert_job(
        database, project_id, "job-old-failed", status="failed", finished_at=old
    )
    _insert_job(
        database, project_id, "job-recent-done", status="completed", finished_at=recent
    )

    service = JobService(database)
    assert service.cleanup_old_jobs(project_id) == 1
    with database.engine.connect() as conn:
        remaining = sorted(
            conn.execute(select(job.c.id).where(job.c.project_id == project_id)).scalars()
        )
    assert remaining == ["job-old-failed", "job-recent-done"]

    # keep_failed=False 时连旧失败任务一起清理
    assert service.cleanup_old_jobs(project_id, keep_failed=False) == 1
    with database.engine.connect() as conn:
        remaining = sorted(
            conn.execute(select(job.c.id).where(job.c.project_id == project_id)).scalars()
        )
    assert remaining == ["job-recent-done"]
