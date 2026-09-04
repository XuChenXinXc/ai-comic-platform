from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import insert, inspect, select

from app.api import settings as settings_api
from app.config import PROJECT_ROOT, Settings, get_settings
from app.db import Database, migrate_schema
from app.dependencies import get_setting_service
from app.models.tables import platform_setting, project
from app.services.errors import ServiceError
from app.services.setting_service import SettingService
from app.services.utils import utc_now


def _create_database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{(tmp_path / 'settings.db').as_posix()}")
    database.create_all()
    return database


def _create_settings(
    tmp_path: Path,
    *,
    project_storage_dir: Path | None = None,
) -> Settings:
    return Settings(
        _env_file=None,
        workspace_dir=tmp_path / "workspace",
        project_storage_dir=project_storage_dir,
    )


def _create_client(service: SettingService) -> TestClient:
    app = FastAPI()

    @app.exception_handler(ServiceError)
    async def handle_service_error(_: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    app.include_router(settings_api.router, prefix="/api")
    app.dependency_overrides[get_setting_service] = lambda: service
    return TestClient(app)


def test_database_create_all_with_legacy_project_insert_uses_storage_defaults(
    tmp_path: Path,
) -> None:
    database = _create_database(tmp_path)
    now = utc_now()

    with database.engine.begin() as connection:
        connection.execute(
            insert(project).values(
                id="project-1",
                name="兼容项目",
                created_at=now,
                updated_at=now,
            )
        )
        record = connection.execute(
            select(project.c.storage_root, project.c.storage_layout).where(
                project.c.id == "project-1"
            )
        ).one()

    assert platform_setting.name in inspect(database.engine).get_table_names()
    assert record._mapping == {"storage_root": "", "storage_layout": "prefixed"}
    database.dispose()


def test_get_project_storage_without_override_returns_default_path(tmp_path: Path) -> None:
    database = _create_database(tmp_path)
    settings = _create_settings(tmp_path)

    result = SettingService(database, settings=settings).get_project_storage()

    assert result == {
        "path": str((settings.workspace_dir / "projects").resolve()),
        "source": "default",
    }
    database.dispose()


def test_get_project_storage_with_environment_path_returns_environment_source(
    tmp_path: Path,
) -> None:
    environment_root = tmp_path / "environment-projects"
    database = _create_database(tmp_path)
    settings = _create_settings(tmp_path, project_storage_dir=environment_root)

    result = SettingService(database, settings=settings).get_project_storage()

    assert result == {
        "path": str(environment_root.resolve()),
        "source": "environment",
    }
    database.dispose()


def test_select_project_storage_with_directory_persists_database_override(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "selected-projects"
    selected_root.mkdir()
    database = _create_database(tmp_path)
    service = SettingService(
        database,
        settings=_create_settings(tmp_path),
        directory_selector=lambda: selected_root,
    )

    result = service.select_project_storage()

    assert result == {
        "selected": True,
        "path": str(selected_root.resolve()),
        "source": "database",
    }
    with database.engine.connect() as connection:
        value = connection.scalar(
            select(platform_setting.c.value).where(platform_setting.c.key == "project_storage_root")
        )
    assert value == str(selected_root.resolve())
    assert list(selected_root.iterdir()) == []
    database.dispose()


def test_select_project_storage_when_cancelled_preserves_current_setting(
    tmp_path: Path,
) -> None:
    current_root = tmp_path / "current-projects"
    current_root.mkdir()
    database = _create_database(tmp_path)
    original_service = SettingService(
        database,
        settings=_create_settings(tmp_path),
        directory_selector=lambda: current_root,
    )
    original_service.select_project_storage()
    cancelled_service = SettingService(
        database,
        settings=_create_settings(tmp_path),
        directory_selector=lambda: None,
    )

    result = cancelled_service.select_project_storage()

    assert result == {
        "selected": False,
        "path": str(current_root.resolve()),
        "source": "database",
    }
    database.dispose()


@pytest.mark.parametrize(
    "candidate",
    [
        Path("relative-projects"),
        Path(PROJECT_ROOT.anchor),
        PROJECT_ROOT,
        PROJECT_ROOT.parent,
        PROJECT_ROOT / "backend",
    ],
)
def test_select_project_storage_with_unsafe_path_returns_400(
    tmp_path: Path,
    candidate: Path,
) -> None:
    database = _create_database(tmp_path)
    service = SettingService(
        database,
        settings=_create_settings(tmp_path),
        directory_selector=lambda: candidate,
    )

    with pytest.raises(ServiceError) as exc_info:
        service.select_project_storage()

    assert exc_info.value.status_code == 400
    database.dispose()


def test_select_project_storage_with_workspace_itself_returns_400(tmp_path: Path) -> None:
    database = _create_database(tmp_path)
    settings = _create_settings(tmp_path)
    service = SettingService(
        database,
        settings=settings,
        directory_selector=lambda: settings.workspace_dir,
    )

    with pytest.raises(ServiceError) as exc_info:
        service.select_project_storage()

    assert exc_info.value.status_code == 400
    database.dispose()


def test_select_project_storage_with_deep_windows_path_returns_400(
    tmp_path: Path,
) -> None:
    database = _create_database(tmp_path)
    deep_path = tmp_path / ("深" * 140)
    service = SettingService(
        database,
        settings=_create_settings(tmp_path),
        directory_selector=lambda: deep_path,
    )

    with pytest.raises(ServiceError) as exc_info:
        service.select_project_storage()

    assert exc_info.value.status_code == 400
    database.dispose()


def test_select_project_storage_with_file_path_returns_400(tmp_path: Path) -> None:
    selected_file = tmp_path / "not-a-directory"
    selected_file.write_text("占位", encoding="utf-8")
    database = _create_database(tmp_path)
    service = SettingService(
        database,
        settings=_create_settings(tmp_path),
        directory_selector=lambda: selected_file,
    )

    with pytest.raises(ServiceError) as exc_info:
        service.select_project_storage()

    assert exc_info.value.status_code == 400
    database.dispose()


def test_project_storage_api_get_returns_path_and_source(tmp_path: Path) -> None:
    database = _create_database(tmp_path)
    settings = _create_settings(tmp_path)
    service = SettingService(database, settings=settings)

    with _create_client(service) as client:
        response = client.get("/api/settings/project-storage")

    assert response.status_code == 200
    assert response.json() == {
        "path": str((settings.workspace_dir / "projects").resolve()),
        "source": "default",
    }
    database.dispose()


def test_project_storage_api_select_ignores_request_path_and_uses_selector(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "selected-by-dialog"
    selected_root.mkdir()
    database = _create_database(tmp_path)
    service = SettingService(
        database,
        settings=_create_settings(tmp_path),
        directory_selector=lambda: selected_root,
    )

    with _create_client(service) as client:
        response = client.post(
            "/api/settings/project-storage/select",
            json={"path": str(tmp_path / "path-from-request")},
        )

    assert response.status_code == 200
    assert response.json() == {
        "selected": True,
        "path": str(selected_root.resolve()),
        "source": "database",
    }
    database.dispose()


def test_migrate_schema_from_0003_backfills_existing_project_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "legacy-0003.db"
    configured_root = tmp_path / "configured-projects"
    monkeypatch.setenv("APP_PROJECTS_DIR", str(configured_root))
    monkeypatch.setenv("APP_WORKSPACE", str(tmp_path / "workspace"))
    get_settings.cache_clear()
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE alembic_version (
                version_num VARCHAR(32) NOT NULL PRIMARY KEY
            );
            INSERT INTO alembic_version (version_num) VALUES ('20260730_0003');
            CREATE TABLE project (
                id TEXT NOT NULL PRIMARY KEY,
                name TEXT NOT NULL,
                storage_name TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO project (id, name, storage_name)
            VALUES ('project-1', '旧项目', '旧项目');
            """
        )
    database = Database(f"sqlite:///{database_path.as_posix()}")

    migrate_schema(database)

    with sqlite3.connect(database_path) as connection:
        record = connection.execute(
            "SELECT storage_root, storage_layout FROM project WHERE id = 'project-1'"
        ).fetchone()
        setting_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(platform_setting)")
        }
    assert record == (str(configured_root.resolve()), "prefixed")
    assert setting_columns == {"key", "value", "updated_at"}
    database.dispose()
    get_settings.cache_clear()


def test_camera_policy_default_conservative_and_roundtrip(tmp_path: Path) -> None:
    database = _create_database(tmp_path)
    service = SettingService(database, settings=_create_settings(tmp_path))
    client = _create_client(service)

    response = client.get("/api/settings/camera-policy")
    assert response.status_code == 200
    body = response.json()
    assert body["policy"] == "克制"
    assert body["options"] == ["克制", "标准", "丰富"]

    response = client.post("/api/settings/camera-policy", json={"policy": "标准"})
    assert response.status_code == 200
    assert response.json()["policy"] == "标准"

    assert client.get("/api/settings/camera-policy").json()["policy"] == "标准"
    database.dispose()


def test_camera_policy_rejects_unknown_value(tmp_path: Path) -> None:
    database = _create_database(tmp_path)
    service = SettingService(database, settings=_create_settings(tmp_path))
    client = _create_client(service)

    response = client.post("/api/settings/camera-policy", json={"policy": "随便"})
    assert response.status_code == 400

    # 拒绝后仍保持默认克制
    assert client.get("/api/settings/camera-policy").json()["policy"] == "克制"
    database.dispose()
