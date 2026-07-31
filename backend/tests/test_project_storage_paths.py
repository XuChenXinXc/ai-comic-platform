from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest
from sqlalchemy import event, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.db import Database
from app.jobs.repository import JobRepository
from app.models.tables import platform_setting, project
from app.services.errors import ServiceError
from app.services.file_service import FileService, FileServiceError
from app.services.project_service import ProjectService
from app.services.utils import utc_now

_MEDIA_DIRECTORIES = {"角色", "物品", "场景", "分镜关键帧", "分镜视频"}


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

    new_asset = storage.asset_image_path("project-new", "character", "asset-1", 1)
    new_keyframe = storage.keyframe_path("project-new", "shot-1", 1)
    new_video = storage.video_path("project-new", "shot-1", 1)
    old_asset = storage.asset_image_path("project-old", "character", "asset-1", 1)
    legacy_relative = storage.resolve_project_path(
        "project-old", "assets/characters/asset-1/v001.png"
    )

    assert storage.project_path("project-new") == categorized_root / "新项目"
    assert storage.project_path("project-old") == prefixed_root / "project-old"
    assert storage.relative_project_path("project-new", new_asset) == ("角色/asset-1/v001.png")
    assert storage.relative_project_path("project-new", new_keyframe) == (
        "分镜关键帧/shot-1/v001.png"
    )
    assert storage.relative_project_path("project-new", new_video) == ("分镜视频/shot-1/v001.mp4")
    assert storage.relative_project_path("project-old", old_asset) == (
        "旧项目-角色/asset-1/v001.png"
    )
    assert storage.relative_project_path("project-old", legacy_relative) == (
        "assets/characters/asset-1/v001.png"
    )
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
