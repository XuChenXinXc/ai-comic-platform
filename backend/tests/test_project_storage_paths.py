from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest
from sqlalchemy import event, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.api.schemas import ShotCreate
from app.db import Database
from app.jobs.repository import JobRepository
from app.models.tables import (
    asset_image,
    keyframe_version,
    platform_setting,
    project,
    video_version,
)
from app.services.asset_service import AssetService
from app.services.errors import ServiceError
from app.services.file_service import FileService, FileServiceError
from app.services.project_service import ProjectService
from app.services.shot_service import ShotService
from app.services.utils import utc_now

_MEDIA_DIRECTORIES = {"角色", "物品", "场景", "分镜图", "分镜视频"}


def _create_database(tmp_path: Path, name: str = "platform.db") -> Database:
    database = Database(f"sqlite:///{(tmp_path / name).as_posix()}")
    database.create_all()
    return database


def _insert_project(
    database: Database,
    *,
    project_id: str,
    storage_root: Path,
    storage_name: str,
    storage_layout: str,
) -> None:
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(project).values(
                id=project_id,
                name=storage_name,
                storage_root=str(storage_root.resolve()),
                storage_name=storage_name,
                storage_layout=storage_layout,
                created_at=now,
                updated_at=now,
            )
        )


def _insert_asset_image_record(
    database: Database,
    *,
    image_id: str,
    asset_id: str,
    file_path: str,
    is_current: bool = True,
) -> None:
    with database.engine.begin() as connection:
        connection.execute(
            insert(asset_image).values(
                id=image_id,
                asset_id=asset_id,
                version_no=1,
                file_path=file_path,
                prompt_snapshot="",
                negative_prompt_snapshot="",
                reference_files_json="[]",
                provider_id=None,
                model="",
                provider_task_id="",
                status="completed",
                is_current=is_current,
                input_signature="",
                source="generated",
                error="",
                created_at=utc_now(),
            )
        )


def test_project_service_create_with_same_name_uses_unique_categorized_directories(
    tmp_path: Path,
) -> None:
    database = _create_database(tmp_path)
    storage = FileService(tmp_path / "workspace")
    service = ProjectService(database, storage)

    records = [service.create({"name": "项目一"}) for _ in range(3)]

    with database.engine.connect() as connection:
        stored = connection.execute(
            select(
                project.c.id,
                project.c.storage_root,
                project.c.storage_name,
                project.c.storage_layout,
            ).order_by(project.c.created_at)
        ).all()
    assert [row.storage_name for row in stored] == ["项目一", "项目一 (2)", "项目一 (3)"]
    assert {row.storage_root for row in stored} == {str(storage.projects_dir)}
    assert {row.storage_layout for row in stored} == {"categorized"}
    assert [record["storage_path"] for record in records] == [
        str(storage.projects_dir / "项目一"),
        str(storage.projects_dir / "项目一 (2)"),
        str(storage.projects_dir / "项目一 (3)"),
    ]
    for row in stored:
        project_dir = storage.projects_dir / row.storage_name
        assert {path.name for path in project_dir.iterdir() if path.is_dir()} == (
            _MEDIA_DIRECTORIES
        )
    database.dispose()


def test_project_service_create_after_storage_setting_change_uses_new_root_only_for_new_project(
    tmp_path: Path,
) -> None:
    database = _create_database(tmp_path)
    first_root = tmp_path / "first-root"
    second_root = tmp_path / "second-root"
    first_root.mkdir()
    second_root.mkdir()
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(platform_setting).values(
                key="project_storage_root",
                value=str(first_root),
                updated_at=now,
            )
        )
    service = ProjectService(
        database,
        FileService(tmp_path / "workspace", database=database),
    )
    first = service.create({"name": "第一项目"})

    with database.engine.begin() as connection:
        connection.execute(
            update(platform_setting)
            .where(platform_setting.c.key == "project_storage_root")
            .values(value=str(second_root), updated_at=utc_now())
        )
    second = service.create({"name": "第二项目"})

    assert first["storage_path"] == str(first_root / "第一项目")
    assert service.get(first["id"])["storage_path"] == str(first_root / "第一项目")
    assert second["storage_path"] == str(second_root / "第二项目")
    database.dispose()


def test_file_service_with_project_layout_resolves_new_and_legacy_paths(
    tmp_path: Path,
) -> None:
    database = _create_database(tmp_path)
    categorized_root = tmp_path / "categorized"
    prefixed_root = tmp_path / "prefixed"
    _insert_project(
        database,
        project_id="project-new",
        storage_root=categorized_root,
        storage_name="新项目",
        storage_layout="categorized",
    )
    _insert_project(
        database,
        project_id="project-old",
        storage_root=prefixed_root,
        storage_name="旧项目",
        storage_layout="prefixed",
    )
    storage = FileService(tmp_path / "fallback", database=database)

    new_asset = storage.asset_image_path(
        "project-new",
        "character",
        "asset-1",
        1,
        asset_name="林舟",
    )
    new_current_asset = storage.asset_image_path(
        "project-new",
        "character",
        "asset-1",
        1,
        asset_name="林舟",
        current=True,
    )
    new_keyframe = storage.keyframe_path("project-new", "shot-1", 1, shot_no=1)
    new_video = storage.video_path("project-new", "shot-1", 1, shot_no=1)
    old_asset = storage.asset_image_path("project-old", "character", "asset-1", 1)
    legacy_relative = storage.resolve_project_path(
        "project-old", "assets/characters/asset-1/v001.png"
    )

    assert storage.project_path("project-new") == categorized_root / "新项目"
    assert storage.project_path("project-old") == prefixed_root / "project-old"
    assert storage.relative_project_path("project-new", new_asset) == ("角色/角色-林舟-v001.png")
    assert storage.relative_project_path("project-new", new_current_asset) == ("角色/角色-林舟.png")
    assert storage.relative_project_path("project-new", new_keyframe) == (
        "分镜图/分镜图-001-v001.png"
    )
    assert storage.relative_project_path("project-new", new_video) == (
        "分镜视频/分镜视频-001-v001.mp4"
    )
    assert storage.relative_project_path("project-old", old_asset) == (
        "旧项目-角色/asset-1/v001.png"
    )
    assert storage.relative_project_path("project-old", legacy_relative) == (
        "assets/characters/asset-1/v001.png"
    )
    database.dispose()


def test_file_service_migrates_existing_categorized_media_to_flat_readable_paths(
    tmp_path: Path,
) -> None:
    database = _create_database(tmp_path)
    storage = FileService(tmp_path / "workspace", database=database)
    current_project = ProjectService(database, storage).create({"name": "测试文章"})
    current_asset = AssetService(database).create(
        current_project["id"],
        {"asset_type": "character", "name": "林舟"},
    )
    current_shot = ShotService(database).create(
        current_project["id"],
        ShotCreate(duration_sec=6).model_dump(),
    )
    old_asset_v1 = f"角色/{current_asset['id']}/v001.png"
    old_asset_v2 = f"角色/{current_asset['id']}/v002.png"
    old_keyframe = f"分镜关键帧/{current_shot['id']}/v001.png"
    old_video = f"分镜视频/{current_shot['id']}/v001.mp4"
    for relative, content in (
        (old_asset_v1, b"asset-v1"),
        (old_asset_v2, b"asset-v2"),
        (old_keyframe, b"keyframe"),
        (old_video, b"video"),
    ):
        storage.write_bytes_atomic(current_project["id"], relative, content)

    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(asset_image),
            [
                {
                    "id": "asset-image-1",
                    "asset_id": current_asset["id"],
                    "version_no": 1,
                    "file_path": old_asset_v1,
                    "prompt_snapshot": "",
                    "negative_prompt_snapshot": "",
                    "reference_files_json": "[]",
                    "provider_id": None,
                    "model": "",
                    "provider_task_id": "",
                    "status": "completed",
                    "is_current": False,
                    "input_signature": "",
                    "source": "generated",
                    "error": "",
                    "created_at": now,
                },
                {
                    "id": "asset-image-2",
                    "asset_id": current_asset["id"],
                    "version_no": 2,
                    "file_path": old_asset_v2,
                    "prompt_snapshot": "",
                    "negative_prompt_snapshot": "",
                    "reference_files_json": "[]",
                    "provider_id": None,
                    "model": "",
                    "provider_task_id": "",
                    "status": "completed",
                    "is_current": True,
                    "input_signature": "",
                    "source": "generated",
                    "error": "",
                    "created_at": now,
                },
            ],
        )
        connection.execute(
            insert(keyframe_version).values(
                id="keyframe-1",
                shot_id=current_shot["id"],
                version_no=1,
                file_path=old_keyframe,
                prompt_snapshot="",
                negative_prompt_snapshot="",
                reference_bindings_json="[]",
                provider_id=None,
                model="",
                provider_task_id="",
                status="completed",
                is_current=True,
                input_signature="",
                source="generated",
                error="",
                created_at=now,
            )
        )
        connection.execute(
            insert(video_version).values(
                id="video-1",
                shot_id=current_shot["id"],
                keyframe_version_id="keyframe-1",
                version_no=1,
                file_path=old_video,
                prompt_snapshot="",
                negative_prompt_snapshot="",
                requested_duration_sec=6,
                actual_duration_sec=6,
                provider_id=None,
                model="",
                provider_task_id="",
                status="completed",
                is_current=True,
                input_signature="",
                source="generated",
                cost_estimate=0,
                error="",
                created_at=now,
            )
        )

    assert storage.migrate_categorized_media_layouts() == 4

    with database.engine.connect() as connection:
        asset_paths = connection.execute(
            select(asset_image.c.version_no, asset_image.c.file_path).order_by(
                asset_image.c.version_no
            )
        ).all()
        keyframe_path = connection.scalar(select(keyframe_version.c.file_path))
        video_path = connection.scalar(select(video_version.c.file_path))
    assert asset_paths == [
        (1, "角色/角色-林舟-v001.png"),
        (2, "角色/角色-林舟.png"),
    ]
    assert keyframe_path == "分镜图/分镜图-001.png"
    assert video_path == "分镜视频/分镜视频-001.mp4"
    assert (
        storage.resolve_project_path(current_project["id"], "角色/角色-林舟-v001.png").read_bytes()
        == b"asset-v1"
    )
    assert (
        storage.resolve_project_path(current_project["id"], "角色/角色-林舟.png").read_bytes()
        == b"asset-v2"
    )
    assert (
        storage.resolve_project_path(current_project["id"], "分镜图/分镜图-001.png").read_bytes()
        == b"keyframe"
    )
    assert (
        storage.resolve_project_path(
            current_project["id"], "分镜视频/分镜视频-001.mp4"
        ).read_bytes()
        == b"video"
    )
    assert (
        storage.resolve_project_path(current_project["id"], f"角色/{current_asset['id']}").exists()
        is False
    )
    assert storage.resolve_project_path(current_project["id"], "分镜关键帧").exists() is False
    database.dispose()


def test_file_service_media_migration_with_existing_target_keeps_original_files(
    tmp_path: Path,
) -> None:
    database = _create_database(tmp_path)
    storage = FileService(tmp_path / "workspace", database=database)
    current_project = ProjectService(database, storage).create({"name": "冲突测试"})
    current_asset = AssetService(database).create(
        current_project["id"],
        {"asset_type": "character", "name": "林舟"},
    )
    old_relative = f"角色/{current_asset['id']}/v001.png"
    target_relative = "角色/角色-林舟.png"
    storage.write_bytes_atomic(current_project["id"], old_relative, b"original")
    storage.write_bytes_atomic(current_project["id"], target_relative, b"existing")
    with database.engine.begin() as connection:
        connection.execute(
            insert(asset_image).values(
                id="asset-image-1",
                asset_id=current_asset["id"],
                version_no=1,
                file_path=old_relative,
                prompt_snapshot="",
                negative_prompt_snapshot="",
                reference_files_json="[]",
                provider_id=None,
                model="",
                provider_task_id="",
                status="completed",
                is_current=True,
                input_signature="",
                source="generated",
                error="",
                created_at=utc_now(),
            )
        )

    with pytest.raises(FileServiceError, match="迁移目标已存在"):
        storage.migrate_categorized_media_layouts()

    assert (
        storage.resolve_project_path(current_project["id"], old_relative).read_bytes()
        == b"original"
    )
    assert (
        storage.resolve_project_path(current_project["id"], target_relative).read_bytes()
        == b"existing"
    )
    with database.engine.connect() as connection:
        assert connection.scalar(select(asset_image.c.file_path)) == old_relative
    database.dispose()


def test_file_service_media_migration_recovers_interrupted_staged_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _create_database(tmp_path)
    storage = FileService(tmp_path / "workspace", database=database)
    current_project = ProjectService(database, storage).create({"name": "中断恢复"})
    current_asset = AssetService(database).create(
        current_project["id"],
        {"asset_type": "character", "name": "林舟"},
    )
    old_relative = f"角色/{current_asset['id']}/v001.png"
    target_relative = "角色/角色-林舟.png"
    storage.write_bytes_atomic(current_project["id"], old_relative, b"original")
    _insert_asset_image_record(
        database,
        image_id="asset-image-interrupted-stage",
        asset_id=current_asset["id"],
        file_path=old_relative,
    )
    original_move_file = storage.move_file
    move_count = 0

    def interrupt_after_first_move(*args: object, **kwargs: object) -> Path:
        nonlocal move_count
        result = original_move_file(*args, **kwargs)
        move_count += 1
        if move_count == 1:
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(storage, "move_file", interrupt_after_first_move)
    with pytest.raises(KeyboardInterrupt):
        storage.migrate_categorized_media_layouts()
    monkeypatch.setattr(storage, "move_file", original_move_file)

    assert storage.resolve_project_path(current_project["id"], old_relative).exists() is False
    assert storage.resolve_project_path(current_project["id"], target_relative).exists() is False

    assert storage.migrate_categorized_media_layouts() == 1

    with database.engine.connect() as connection:
        assert connection.scalar(select(asset_image.c.file_path)) == target_relative
    assert (
        storage.resolve_project_path(current_project["id"], target_relative).read_bytes()
        == b"original"
    )
    database.dispose()


def test_file_service_media_migration_recovers_file_moved_before_database_update(
    tmp_path: Path,
) -> None:
    database = _create_database(tmp_path)
    storage = FileService(tmp_path / "workspace", database=database)
    current_project = ProjectService(database, storage).create({"name": "提交前中断"})
    current_asset = AssetService(database).create(
        current_project["id"],
        {"asset_type": "character", "name": "林舟"},
    )
    old_relative = f"角色/{current_asset['id']}/v001.png"
    target_relative = "角色/角色-林舟.png"
    source = storage.write_bytes_atomic(current_project["id"], old_relative, b"original")
    target = storage.resolve_project_path(current_project["id"], target_relative)
    _insert_asset_image_record(
        database,
        image_id="asset-image-interrupted-target",
        asset_id=current_asset["id"],
        file_path=old_relative,
    )
    storage.move_file(current_project["id"], source, target)

    assert storage.migrate_categorized_media_layouts() == 1

    with database.engine.connect() as connection:
        assert connection.scalar(select(asset_image.c.file_path)) == target_relative
    assert target.read_bytes() == b"original"
    database.dispose()


def test_file_service_media_migration_lenient_mode_does_not_block_on_missing_file(
    tmp_path: Path,
) -> None:
    database = _create_database(tmp_path)
    storage = FileService(tmp_path / "workspace", database=database)
    current_project = ProjectService(database, storage).create({"name": "缺失文件"})
    current_asset = AssetService(database).create(
        current_project["id"],
        {"asset_type": "character", "name": "林舟"},
    )
    old_relative = f"角色/{current_asset['id']}/v001.png"
    _insert_asset_image_record(
        database,
        image_id="asset-image-missing",
        asset_id=current_asset["id"],
        file_path=old_relative,
    )

    assert storage.migrate_categorized_media_layouts(strict=False) == 0

    with database.engine.connect() as connection:
        assert connection.scalar(select(asset_image.c.file_path)) == old_relative
    database.dispose()


def test_file_service_resolve_missing_project_file_does_not_create_directory(
    tmp_path: Path,
) -> None:
    database = _create_database(tmp_path)
    storage_root = tmp_path / "media"
    _insert_project(
        database,
        project_id="project-1",
        storage_root=storage_root,
        storage_name="尚未创建",
        storage_layout="categorized",
    )
    storage = FileService(tmp_path / "fallback", database=database)

    result = storage.resolve_project_path("project-1", "角色/asset-1/v001.png")

    assert result == storage_root / "尚未创建" / "角色" / "asset-1" / "v001.png"
    assert (storage_root / "尚未创建").exists() is False
    database.dispose()


def test_project_service_create_when_database_insert_fails_cleans_only_reserved_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _create_database(tmp_path)
    storage = FileService(tmp_path / "workspace")
    storage.projects_dir.mkdir(parents=True)
    existing_dir = storage.projects_dir / "项目一"
    existing_dir.mkdir()
    marker = existing_dir / "existing.txt"
    marker.write_text("保留", encoding="utf-8")
    _insert_project(
        database,
        project_id="project-duplicate",
        storage_root=storage.projects_dir,
        storage_name="项目一",
        storage_layout="categorized",
    )
    monkeypatch.setattr("app.services.project_service.new_id", lambda _prefix: "project-duplicate")

    with pytest.raises(IntegrityError):
        ProjectService(database, storage).create({"name": "项目一"})

    assert marker.read_text(encoding="utf-8") == "保留"
    assert (storage.projects_dir / "项目一 (2)").exists() is False
    database.dispose()


def test_project_service_delete_removes_only_persisted_direct_project_directory(
    tmp_path: Path,
) -> None:
    database = _create_database(tmp_path)
    storage = FileService(tmp_path / "workspace")
    service = ProjectService(database, storage)
    record = service.create({"name": "待删除项目"})
    project_dir = Path(record["storage_path"])
    sibling = storage.projects_dir / "保留目录"
    sibling.mkdir()
    marker = sibling / "marker.txt"
    marker.write_text("保留", encoding="utf-8")

    service.delete(record["id"])

    with database.engine.connect() as connection:
        exists = connection.scalar(select(project.c.id).where(project.c.id == record["id"]))
    assert exists is None
    assert project_dir.exists() is False
    assert marker.read_text(encoding="utf-8") == "保留"
    database.dispose()


def test_project_service_delete_with_non_direct_storage_path_returns_400(
    tmp_path: Path,
) -> None:
    database = _create_database(tmp_path)
    storage_root = tmp_path / "media"
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "marker.txt"
    marker.write_text("保留", encoding="utf-8")
    _insert_project(
        database,
        project_id="project-unsafe",
        storage_root=storage_root,
        storage_name="../outside",
        storage_layout="categorized",
    )

    with pytest.raises(ServiceError) as exc_info:
        ProjectService(database, FileService(tmp_path / "fallback")).delete("project-unsafe")

    with database.engine.connect() as connection:
        exists = connection.scalar(select(project.c.id).where(project.c.id == "project-unsafe"))
    assert exc_info.value.status_code == 400
    assert exists == "project-unsafe"
    assert marker.read_text(encoding="utf-8") == "保留"
    database.dispose()


def test_project_service_delete_database_failure_restores_reserved_directory(
    tmp_path: Path,
) -> None:
    database = _create_database(tmp_path)
    storage = FileService(tmp_path / "workspace")
    service = ProjectService(database, storage)
    record = service.create({"name": "数据库失败项目"})
    project_dir = Path(record["storage_path"])

    def fail_project_delete(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if "delete from project" in statement.lower():
            raise RuntimeError("模拟数据库删除失败")

    event.listen(database.engine, "before_cursor_execute", fail_project_delete)
    try:
        with pytest.raises(RuntimeError, match="模拟数据库删除失败"):
            service.delete(record["id"])
    finally:
        event.remove(database.engine, "before_cursor_execute", fail_project_delete)

    assert project_dir.exists() is True
    with database.engine.connect() as connection:
        project_id = connection.scalar(select(project.c.id).where(project.c.id == record["id"]))
    assert project_id == record["id"]
    database.dispose()


def test_project_service_delete_restore_failure_returns_500(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _create_database(tmp_path)
    storage = FileService(tmp_path / "workspace")
    service = ProjectService(database, storage)
    record = service.create({"name": "恢复失败项目"})
    project_dir = Path(record["storage_path"])

    def fail_project_delete(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if "delete from project" in statement.lower():
            raise RuntimeError("模拟数据库删除失败")

    def fail_restore(_target: Path, _tombstone: Path | None) -> None:
        raise FileServiceError("模拟目录恢复失败")

    monkeypatch.setattr(storage, "restore_reserved_project", fail_restore)
    event.listen(database.engine, "before_cursor_execute", fail_project_delete)
    try:
        with pytest.raises(ServiceError) as exc_info:
            service.delete(record["id"])
    finally:
        event.remove(database.engine, "before_cursor_execute", fail_project_delete)

    assert exc_info.value.status_code == 500
    assert "项目目录恢复失败" in exc_info.value.detail
    with database.engine.connect() as connection:
        project_id = connection.scalar(select(project.c.id).where(project.c.id == record["id"]))
    assert project_id == record["id"]
    assert project_dir.exists() is False
    assert len(list(project_dir.parent.glob(f".{project_dir.name}.deleting-*"))) == 1
    database.dispose()


def test_project_service_delete_finalize_failure_returns_500(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _create_database(tmp_path)
    storage = FileService(tmp_path / "workspace")
    service = ProjectService(database, storage)
    record = service.create({"name": "清理失败项目"})
    project_dir = Path(record["storage_path"])

    def fail_finalize(_tombstone: Path | None) -> None:
        raise FileServiceError("模拟目录清理失败")

    monkeypatch.setattr(storage, "finalize_reserved_project", fail_finalize)

    with pytest.raises(ServiceError) as exc_info:
        service.delete(record["id"])

    assert exc_info.value.status_code == 500
    assert "磁盘目录清理失败" in exc_info.value.detail
    with database.engine.connect() as connection:
        project_id = connection.scalar(select(project.c.id).where(project.c.id == record["id"]))
    assert project_id is None
    assert project_dir.exists() is False
    assert len(list(project_dir.parent.glob(f".{project_dir.name}.deleting-*"))) == 1
    database.dispose()


def test_project_service_delete_with_active_job_keeps_original_directory(
    tmp_path: Path,
) -> None:
    database = _create_database(tmp_path)
    storage = FileService(tmp_path / "workspace")
    service = ProjectService(database, storage)
    record = service.create({"name": "活动任务项目"})
    project_dir = Path(record["storage_path"])
    role_dir = project_dir / "角色"
    (role_dir / "original.txt").write_text("原素材", encoding="utf-8")
    JobRepository(database).create_job({"project_id": record["id"], "job_type": "active-test"})
    wrote_during_check = False

    def write_when_checking_active_job(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal wrote_during_check
        if wrote_during_check or "from job" not in statement.lower():
            return
        wrote_during_check = True
        role_dir.mkdir(parents=True, exist_ok=True)
        (role_dir / "new.txt").write_text("并发新素材", encoding="utf-8")

    event.listen(database.engine, "before_cursor_execute", write_when_checking_active_job)
    try:
        with pytest.raises(ServiceError) as exc_info:
            service.delete(record["id"])
    finally:
        event.remove(database.engine, "before_cursor_execute", write_when_checking_active_job)

    assert exc_info.value.status_code == 409
    assert sorted(path.name for path in role_dir.iterdir()) == ["new.txt", "original.txt"]
    assert list(project_dir.parent.glob(f".{project_dir.name}.deleting-*")) == []
    database.dispose()


def test_project_service_delete_blocks_new_job_until_project_is_deleted(
    tmp_path: Path,
) -> None:
    database = _create_database(tmp_path)
    storage = FileService(tmp_path / "workspace")
    service = ProjectService(database, storage)
    record = service.create({"name": "删除锁项目"})
    worker_started = threading.Event()
    worker_done = threading.Event()
    worker_errors: list[Exception] = []
    worker_thread: threading.Thread | None = None

    def create_job() -> None:
        worker_started.set()
        try:
            JobRepository(database).create_job(
                {"project_id": record["id"], "job_type": "concurrent-test"}
            )
        except Exception as exc:
            worker_errors.append(exc)
        finally:
            worker_done.set()

    def start_job_before_project_delete(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal worker_thread
        if worker_thread is not None or "delete from project" not in statement.lower():
            return
        worker_thread = threading.Thread(target=create_job)
        worker_thread.start()
        assert worker_started.wait(timeout=1) is True
        assert worker_done.wait(timeout=0.1) is False

    event.listen(database.engine, "before_cursor_execute", start_job_before_project_delete)
    try:
        service.delete(record["id"])
    finally:
        event.remove(database.engine, "before_cursor_execute", start_job_before_project_delete)

    assert worker_done.wait(timeout=5) is True
    assert worker_thread is not None
    worker_thread.join(timeout=1)
    assert len(worker_errors) == 1
    assert isinstance(worker_errors[0], IntegrityError)
    database.dispose()


def test_file_service_rejects_project_directory_symlink(
    tmp_path: Path,
) -> None:
    database = _create_database(tmp_path)
    storage_root = tmp_path / "media"
    target = storage_root / "real-project"
    target.mkdir(parents=True)
    link = storage_root / "linked-project"
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError as exc:
        database.dispose()
        pytest.skip(f"当前环境不允许创建目录符号链接：{exc}")
    _insert_project(
        database,
        project_id="project-linked",
        storage_root=storage_root,
        storage_name="linked-project",
        storage_layout="categorized",
    )
    storage = FileService(tmp_path / "fallback", database=database)

    with pytest.raises(FileServiceError, match="符号链接或目录联接"):
        storage.project_path("project-linked")
    database.dispose()


def test_file_service_instances_with_same_project_id_use_bound_database(
    tmp_path: Path,
) -> None:
    first_database = _create_database(tmp_path, "first.db")
    second_database = _create_database(tmp_path, "second.db")
    first_root = tmp_path / "first-root"
    second_root = tmp_path / "second-root"
    for database, root, storage_name in (
        (first_database, first_root, "第一项目"),
        (second_database, second_root, "第二项目"),
    ):
        _insert_project(
            database,
            project_id="shared-id",
            storage_root=root,
            storage_name=storage_name,
            storage_layout="categorized",
        )

    first_storage = FileService(tmp_path / "fallback", database=first_database)
    second_storage = FileService(tmp_path / "fallback", database=second_database)

    assert first_storage.project_path("shared-id") == first_root / "第一项目"
    assert second_storage.project_path("shared-id") == second_root / "第二项目"
    first_database.dispose()
    second_database.dispose()
