from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.api import settings as settings_api
from app.db import Database
from app.dependencies import get_setting_service
from app.services.errors import ServiceError
from app.services.file_service import FileService
from app.services.project_service import ProjectService
from app.services.setting_service import (
    SettingService,
    get_default_style_prompt,
    set_default_style_prompt,
)


def test_set_and_get_default_style_prompt_round_trip(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'style-setting.db').as_posix()}")
    database.create_all()

    assert get_default_style_prompt(database) == ""
    saved = set_default_style_prompt(database, "东方粗笔重彩·写意水墨二维国漫风")
    assert saved == "东方粗笔重彩·写意水墨二维国漫风"
    assert get_default_style_prompt(database) == "东方粗笔重彩·写意水墨二维国漫风"

    assert set_default_style_prompt(database, "") == ""
    assert get_default_style_prompt(database) == ""
    database.dispose()


def test_project_create_fills_platform_default_style_when_empty(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'style-fill.db').as_posix()}")
    database.create_all()
    set_default_style_prompt(database, "东方粗笔重彩·写意水墨二维国漫风")

    record = ProjectService(database, FileService(tmp_path / "files")).create(
        {"name": "默认画风项目"}
    )

    assert record["style_prompt"] == "东方粗笔重彩·写意水墨二维国漫风"
    database.dispose()


def test_project_create_keeps_explicit_style_over_platform_default(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'style-explicit.db').as_posix()}")
    database.create_all()
    set_default_style_prompt(database, "东方粗笔重彩·写意水墨二维国漫风")

    record = ProjectService(database, FileService(tmp_path / "files")).create(
        {"name": "自定义画风项目", "style_prompt": "赛璐璐平涂"}
    )

    assert record["style_prompt"] == "赛璐璐平涂"
    database.dispose()


def test_project_create_without_setting_leaves_style_empty(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'style-empty.db').as_posix()}")
    database.create_all()

    record = ProjectService(database, FileService(tmp_path / "files")).create(
        {"name": "无默认设置项目"}
    )

    assert record["style_prompt"] == ""
    database.dispose()


def test_default_style_prompt_settings_api_round_trip(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'style-api.db').as_posix()}")
    database.create_all()
    app = FastAPI()

    @app.exception_handler(ServiceError)
    async def handle_service_error(_: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    app.include_router(settings_api.router, prefix="/api")
    app.dependency_overrides[get_setting_service] = lambda: SettingService(database)

    with TestClient(app) as client:
        assert client.get("/api/settings/default-style-prompt").json() == {"style_prompt": ""}
        response = client.post(
            "/api/settings/default-style-prompt",
            json={"style_prompt": "东方粗笔重彩·写意水墨二维国漫风"},
        )
        assert response.status_code == 200
        assert response.json() == {"style_prompt": "东方粗笔重彩·写意水墨二维国漫风"}
        assert get_default_style_prompt(database) == "东方粗笔重彩·写意水墨二维国漫风"

        cleared = client.post(
            "/api/settings/default-style-prompt",
            json={"style_prompt": ""},
        )
        assert cleared.json() == {"style_prompt": ""}
    database.dispose()
