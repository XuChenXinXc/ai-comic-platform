import os
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from sqlalchemy import insert

from app.db import Database, utc_now
from app.main import ProjectMediaStaticFiles, SPAStaticFiles
from app.main import app as main_app
from app.models.tables import project


def test_spa_static_files_returns_index_only_for_client_routes(tmp_path: Path) -> None:
    (tmp_path / "assets").mkdir()
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    (tmp_path / "index.html").write_text("<main>导演台</main>", encoding="utf-8")
    (tmp_path / "assets" / "app.js").write_text("window.app = true", encoding="utf-8")
    (media_dir / "frame.txt").write_text("media", encoding="utf-8")
    test_app = FastAPI()

    @test_app.get("/api/ping")
    def ping() -> dict[str, str]:
        return {"status": "ok"}

    test_app.mount("/media", StaticFiles(directory=media_dir))
    test_app.mount("/", SPAStaticFiles(directory=tmp_path, html=True))

    with TestClient(test_app) as client:
        api_response = client.get("/api/ping")
        unknown_api = client.get("/api/unknown")
        media_response = client.get("/media/frame.txt")
        client_route = client.get("/writing")
        assets_client_route = client.get("/assets")
        existing_asset = client.get("/assets/app.js")
        missing_asset = client.get("/assets/missing.js")

    assert api_response.json() == {"status": "ok"}
    assert unknown_api.status_code == 404
    assert unknown_api.text != "<main>导演台</main>"
    assert media_response.text == "media"
    assert client_route.status_code == 200
    assert client_route.text == "<main>导演台</main>"
    assert assets_client_route.status_code == 200
    assert assets_client_route.text == "<main>导演台</main>"
    assert existing_asset.status_code == 200
    assert existing_asset.text == "window.app = true"
    assert missing_asset.status_code == 404


def test_spa_static_files_does_not_fallback_media_paths(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<main>导演台</main>", encoding="utf-8")
    test_app = FastAPI()
    test_app.mount("/", SPAStaticFiles(directory=tmp_path, html=True))

    with TestClient(test_app) as client:
        response = client.get("/media/missing.mp4")

    assert response.status_code == 404
    assert response.text != "<main>导演台</main>"


def test_main_app_registers_project_storage_settings_routes() -> None:
    route_paths = set(main_app.openapi()["paths"])

    assert "/api/settings/project-storage" in route_paths
    assert "/api/settings/project-storage/select" in route_paths


def test_project_media_static_files_serves_existing_project_file(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'platform.db').as_posix()}")
    database.create_all()
    media_dir = tmp_path / "media"
    project_dir = media_dir / "project-1" / "项目一-角色"
    project_dir.mkdir(parents=True)
    (project_dir / "frame#1%.txt").write_text("media", encoding="utf-8")
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(project).values(
                id="project-1",
                name="测试项目",
                created_at=now,
                updated_at=now,
            )
        )
    test_app = FastAPI()
    test_app.mount(
        "/media",
        ProjectMediaStaticFiles(directory=media_dir, database=database),
    )

    with TestClient(test_app) as client:
        relative_path = quote("项目一-角色/frame#1%.txt", safe="/")
        response = client.get(f"/media/project-1/{relative_path}")

    database.dispose()
    assert response.status_code == 200
    assert response.text == "media"


def test_project_media_static_files_serves_categorized_file_from_persisted_root(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'platform.db').as_posix()}")
    database.create_all()
    fallback_dir = tmp_path / "fallback"
    fallback_dir.mkdir()
    storage_root = tmp_path / "selected-root"
    project_dir = storage_root / "项目一" / "角色"
    project_dir.mkdir(parents=True)
    (project_dir / "frame.txt").write_text("categorized", encoding="utf-8")
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(project).values(
                id="project-1",
                name="测试项目",
                storage_root=str(storage_root),
                storage_name="项目一",
                storage_layout="categorized",
                created_at=now,
                updated_at=now,
            )
        )
    test_app = FastAPI()
    test_app.mount(
        "/media",
        ProjectMediaStaticFiles(directory=fallback_dir, database=database),
    )

    with TestClient(test_app) as client:
        response = client.get("/media/project-1/%E8%A7%92%E8%89%B2/frame.txt")

    database.dispose()
    assert response.status_code == 200
    assert response.text == "categorized"


def test_project_media_static_files_rejects_project_directory_symlink(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'platform.db').as_posix()}")
    database.create_all()
    storage_root = tmp_path / "selected-root"
    real_project = storage_root / "real-project"
    real_project.mkdir(parents=True)
    (real_project / "frame.txt").write_text("must stay private", encoding="utf-8")
    linked_project = storage_root / "linked-project"
    try:
        os.symlink(real_project, linked_project, target_is_directory=True)
    except OSError as exc:
        database.dispose()
        pytest.skip(f"当前环境不允许创建目录符号链接：{exc}")
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(project).values(
                id="project-linked",
                name="链接项目",
                storage_root=str(storage_root),
                storage_name="linked-project",
                storage_layout="categorized",
                created_at=now,
                updated_at=now,
            )
        )
    fallback_dir = tmp_path / "fallback"
    fallback_dir.mkdir()
    test_app = FastAPI()
    test_app.mount(
        "/media",
        ProjectMediaStaticFiles(directory=fallback_dir, database=database),
    )

    with TestClient(test_app) as client:
        response = client.get("/media/project-linked/frame.txt")

    database.dispose()
    assert response.status_code == 404


@pytest.mark.parametrize(
    "request_path",
    [
        "/media/config.py",
        "/media/project-missing/frame.txt",
        "/media/project-1/%2e%2e/config.py",
    ],
)
def test_project_media_static_files_with_non_project_path_returns_404(
    tmp_path: Path,
    request_path: str,
) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'platform.db').as_posix()}")
    database.create_all()
    media_dir = tmp_path / "media"
    (media_dir / "project-1").mkdir(parents=True)
    (media_dir / "config.py").write_text("secret", encoding="utf-8")
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(project).values(
                id="project-1",
                name="测试项目",
                created_at=now,
                updated_at=now,
            )
        )
    test_app = FastAPI()
    test_app.mount(
        "/media",
        ProjectMediaStaticFiles(directory=media_dir, database=database),
    )

    with TestClient(test_app) as client:
        response = client.get(request_path)

    database.dispose()
    assert response.status_code == 404
