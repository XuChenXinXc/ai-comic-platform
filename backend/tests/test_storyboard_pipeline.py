from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.db import Database
from app.services.asset_service import AssetService
from app.services.errors import ServiceError
from app.services.file_service import FileService
from app.services.project_service import ProjectService
from app.services.shot_service import ShotService
from app.services.utils import json_dumps
from app.services.writing_service import WritingService


class RecordingJobService:
    def __init__(self) -> None:
        self.created: dict[str, Any] = {}

    def create(self, **values: Any) -> dict[str, Any]:
        self.created = values
        return {"id": "job-recorded", **values}


class ExtractionOutput:
    def __init__(self, shots: list[dict[str, Any]]) -> None:
        self.shots = shots

    def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
        return {"shots": self.shots}


def _screenplay_content() -> dict[str, Any]:
    return {
        "title": "下山",
        "logline": "少年决定离开师门。",
        "scenes": [
            {
                "scene_no": 1,
                "scene_name": "决心下山",
                "time_weather": "清晨，晴",
                "characters": ["少年"],
                "objective": "做出下山决定",
                "action": "少年在山巅远望后转身。",
                "dialogue": [],
                "narration": [],
                "emotion": "坚定",
                "transition_to_next": "切到山门。",
            }
        ],
    }


def _extracted_shot(scene_asset_id: str) -> dict[str, Any]:
    return {
        "shot_no": 1,
        "scene_no": 1,
        "source_ref": "决心下山",
        "scene_name": "青云山巅",
        "scene_asset_id": scene_asset_id,
        "character_names": [],
        "character_asset_ids": [],
        "prop_names": [],
        "prop_asset_ids": [],
        "positions": [],
        "start_state": "少年站在山巅边缘。",
        "action": "少年转身。",
        "expressions": [],
        "dialogue": [],
        "narration": "",
        "shot_type": "中景",
        "camera_angle": "平视",
        "camera_move": "固定",
        "duration_sec": 3,
        "end_state": "少年面向下山方向。",
        "next_hook": "准备迈步。",
        "frame_description": "少年站在山巅。",
        "image_prompt": "少年转身前的稳定首帧。",
        "video_prompt": "0-3秒：少年转身并停稳。",
        "negative_prompt": "身份漂移，场景变化。",
    }


@pytest.fixture
def storyboard_services(
    tmp_path: Path,
) -> tuple[Database, ShotService, RecordingJobService, dict[str, Any]]:
    database = Database(f"sqlite:///{(tmp_path / 'storyboard.db').as_posix()}")
    database.initialize(seed_mock_providers=True)
    project = ProjectService(database, FileService(tmp_path)).create(
        {"name": "分镜流水线", "source_text": "少年在青云山巅决定下山。"}
    )
    WritingService(database).update_screenplay(
        project["id"],
        {"content": _screenplay_content()},
    )
    jobs = RecordingJobService()
    service = ShotService(database, jobs=jobs)  # type: ignore[arg-type]
    yield database, service, jobs, project
    database.dispose()


def test_extract_queue_with_legacy_novel_source_uses_screenplay(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
) -> None:
    database, service, jobs, project = storyboard_services
    AssetService(database).create(
        project["id"],
        {"asset_type": "scene", "name": "青云山巅"},
    )

    service.queue_extract(
        project["id"],
        {"source_type": "novel", "scope": "all"},
    )

    assert jobs.created["input_data"]["source_type"] == "screenplay"


def test_extract_queue_without_scene_assets_returns_conflict(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
) -> None:
    _database, service, _jobs, project = storyboard_services

    with pytest.raises(ServiceError) as exc_info:
        service.queue_extract(
            project["id"],
            {"source_type": "screenplay", "scope": "all"},
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "请先提取场景素材，再提取分镜"


def test_extract_shots_with_catalog_asset_id_binds_scene(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, service, _jobs, project = storyboard_services
    scene = AssetService(database).create(
        project["id"],
        {
            "asset_type": "scene",
            "name": "青云山巅",
            "profile": {"concept": "云海环绕的山巅平台"},
        },
    )
    captured: dict[str, Any] = {}

    class CatalogProvider:
        async def generate_json(self, **values: Any) -> ExtractionOutput:
            captured.update(values)
            return ExtractionOutput([_extracted_shot(scene["id"])])

    monkeypatch.setattr(
        service.providers,
        "resolve",
        lambda **_kwargs: (
            CatalogProvider(),
            {"id": "mock-text", "params": {}},
        ),
    )
    job = {
        "project_id": project["id"],
        "provider_id": "mock-text",
        "input_json": json_dumps(
            {
                "source_type": "screenplay",
                "scope": "all",
                "scene_no": None,
                "params": {},
            }
        ),
    }

    asyncio.run(service.handle_extract(job))

    shots = service.list(project["id"])
    assert scene["id"] in captured["user_prompt"]
    assert "青云山巅" in captured["user_prompt"]
    assert shots[0]["scene_asset_id"] == scene["id"]
    assert shots[0]["source_type"] == "screenplay"
    assert shots[0]["duration_sec"] == 3


def test_extract_shots_with_names_and_aliases_bind_assets(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
) -> None:
    database, service, _jobs, project = storyboard_services
    scene = AssetService(database).create(
        project["id"],
        {
            "asset_type": "scene",
            "name": "青云山巅",
            "profile": {"aliases": ["山巅平台"]},
        },
    )
    character = AssetService(database).create(
        project["id"],
        {
            "asset_type": "character",
            "name": "少年",
        },
    )
    prop = AssetService(database).create(
        project["id"],
        {
            "asset_type": "prop",
            "name": "下山令",
            "profile": {"aliases": ["青铜令牌"]},
        },
    )
    extracted = _extracted_shot("")
    extracted["scene_name"] = "山巅平台"
    extracted["character_names"] = ["少年"]
    extracted["prop_names"] = ["青铜令牌"]

    service._save_extracted(
        project,
        "screenplay",
        {"scope": "all", "scene_no": None},
        [extracted],
    )

    saved = service.list(project["id"])[0]
    assert saved["scene_asset_id"] == scene["id"]
    assert saved["character_ids"] == [character["id"]]
    assert saved["prop_ids"] == [prop["id"]]


def test_extract_shot_with_primary_name_colliding_with_alias_binds_primary_asset(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
) -> None:
    database, service, _jobs, project = storyboard_services
    scene = AssetService(database).create(
        project["id"],
        {"asset_type": "scene", "name": "办公室"},
    )
    key_character = AssetService(database).create(
        project["id"],
        {"asset_type": "character", "name": "小明"},
    )
    AssetService(database).create(
        project["id"],
        {
            "asset_type": "character",
            "name": "李总",
            "profile": {"aliases": ["小明"]},
        },
    )
    extracted = _extracted_shot(scene["id"])
    extracted["scene_name"] = "办公室"
    extracted["character_names"] = ["小明"]
    extracted["character_asset_ids"] = [key_character["id"]]

    service._save_extracted(
        project,
        "screenplay",
        {"scope": "all", "scene_no": None},
        [extracted],
    )

    saved = service.list(project["id"])[0]
    assert saved["character_ids"] == [key_character["id"]]


def test_extract_shots_with_key_and_minor_characters_binds_only_key_character(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
) -> None:
    database, service, _jobs, project = storyboard_services
    scene = AssetService(database).create(
        project["id"],
        {"asset_type": "scene", "name": "办公室"},
    )
    key_character = AssetService(database).create(
        project["id"],
        {"asset_type": "character", "name": "小明"},
    )
    extracted = _extracted_shot(scene["id"])
    extracted["scene_name"] = "办公室"
    extracted["character_names"] = ["小明", "律师", "前台", "张助理", "女助理"]
    extracted["character_asset_ids"] = [key_character["id"]]

    result = service._save_extracted(
        project,
        "screenplay",
        {"scope": "all", "scene_no": None},
        [extracted],
    )

    saved = service.list(project["id"])[0]
    assert result["count"] == 1
    assert saved["scene_asset_id"] == scene["id"]
    assert saved["character_ids"] == [key_character["id"]]


def test_extract_shots_with_unmatched_scene_and_prop_raises_and_saves_nothing(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
) -> None:
    database, service, _jobs, project = storyboard_services
    AssetService(database).create(
        project["id"],
        {"asset_type": "scene", "name": "办公室"},
    )
    AssetService(database).create(
        project["id"],
        {"asset_type": "character", "name": "小明"},
    )
    AssetService(database).create(
        project["id"],
        {"asset_type": "prop", "name": "下山令"},
    )
    extracted = _extracted_shot("")
    extracted["scene_name"] = "办公室门外"
    extracted["prop_names"] = ["未知令牌"]

    with pytest.raises(ServiceError) as exc_info:
        service._save_extracted(
            project,
            "screenplay",
            {"scope": "all", "scene_no": None},
            [extracted],
        )

    assert "分镜 1" in exc_info.value.detail
    assert "办公室门外" in exc_info.value.detail
    assert "未知令牌" in exc_info.value.detail
    assert service.list(project["id"]) == []


def test_extract_shot_with_mismatched_scene_id_and_name_raises_and_saves_nothing(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
) -> None:
    database, service, _jobs, project = storyboard_services
    AssetService(database).create(
        project["id"],
        {"asset_type": "scene", "name": "青云山巅"},
    )
    wrong_scene = AssetService(database).create(
        project["id"],
        {"asset_type": "scene", "name": "竹林"},
    )
    extracted = _extracted_shot(wrong_scene["id"])

    with pytest.raises(ServiceError) as exc_info:
        service._save_extracted(
            project,
            "screenplay",
            {"scope": "all", "scene_no": None},
            [extracted],
        )

    assert exc_info.value.status_code == 400
    assert "青云山巅" in exc_info.value.detail
    assert service.list(project["id"]) == []


def test_extract_shot_with_mismatched_character_id_and_name_raises_and_saves_nothing(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
) -> None:
    database, service, _jobs, project = storyboard_services
    scene = AssetService(database).create(
        project["id"],
        {"asset_type": "scene", "name": "青云山巅"},
    )
    AssetService(database).create(
        project["id"],
        {"asset_type": "character", "name": "少年"},
    )
    wrong_character = AssetService(database).create(
        project["id"],
        {"asset_type": "character", "name": "师姐"},
    )
    extracted = _extracted_shot(scene["id"])
    extracted["character_names"] = ["少年"]
    extracted["character_asset_ids"] = [wrong_character["id"]]

    with pytest.raises(ServiceError) as exc_info:
        service._save_extracted(
            project,
            "screenplay",
            {"scope": "all", "scene_no": None},
            [extracted],
        )

    assert exc_info.value.status_code == 400
    assert "少年" in exc_info.value.detail
    assert service.list(project["id"]) == []


def test_resolve_asset_ids_with_duplicate_ids_returns_unique_ids(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
) -> None:
    database, service, _jobs, project = storyboard_services
    character = AssetService(database).create(
        project["id"],
        {"asset_type": "character", "name": "少年"},
    )
    lookup = service._asset_lookup(project["id"])["character"]

    resolved, unmatched = service._resolve_asset_ids(
        lookup,
        provided_ids=[character["id"], character["id"]],
        names=["少年"],
    )

    assert resolved == [character["id"]]
    assert unmatched == []
