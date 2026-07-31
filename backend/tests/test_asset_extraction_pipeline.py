from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.db import Database
from app.schemas import AssetExtractionOutput
from app.services.asset_service import AssetService
from app.services.file_service import FileService
from app.services.project_service import ProjectService
from app.services.utils import json_dumps
from app.services.writing_service import WritingService


@pytest.fixture
def asset_extraction_services(tmp_path: Path) -> tuple[Database, AssetService, dict[str, object]]:
    database = Database(f"sqlite:///{(tmp_path / 'asset-extraction.db').as_posix()}")
    database.initialize(seed_mock_providers=True)
    project = ProjectService(database, FileService(tmp_path)).create(
        {"name": "素材提取", "source_text": "小明换上灰色风衣走进旧剧院。"}
    )
    service = AssetService(database)
    yield database, service, project
    database.dispose()


def _fresh_character() -> dict[str, object]:
    return {
        "name": "小明",
        "appearance": "黑色短发，左眉浅疤",
        "costume": "灰色风衣",
        "prompt": "黑色短发、左眉浅疤、灰色风衣",
        "negative_prompt": "身份漂移，服装变化，文字，水印",
        "consistency_keywords": ["黑色短发", "左眉浅疤", "灰色风衣"],
        "composition": "纯白背景横向角色设定面板",
        "importance": "core",
    }


def _character(name: str, *, aliases: list[str] | None = None) -> dict[str, object]:
    return {
        **_fresh_character(),
        "name": name,
        "aliases": aliases or [],
        "identity": name,
    }


def _screenplay(
    *,
    characters: list[str],
    speakers: list[str],
) -> dict[str, object]:
    return {
        "title": "庭审现场",
        "logline": "小明在庭审中寻找真相。",
        "scenes": [
            {
                "scene_no": 1,
                "scene_name": "法院大厅",
                "time_weather": "白天，晴",
                "characters": characters,
                "objective": "提交证据",
                "action": "众人进入大厅，小明把文件交给律师。",
                "dialogue": [
                    {"speaker": speaker, "text": "材料已经准备好了。"} for speaker in speakers
                ],
                "narration": [],
                "emotion": "紧张",
                "transition_to_next": "切到法庭内",
            }
        ],
    }


class SequencedAssetProvider:
    def __init__(self, outputs: list[dict[str, object]]) -> None:
        self.outputs = outputs
        self.calls: list[dict[str, Any]] = []

    async def generate_json(self, **values: Any) -> AssetExtractionOutput:
        self.calls.append(values)
        return AssetExtractionOutput.model_validate(self.outputs[len(self.calls) - 1])


def _extract_job(project_id: str) -> dict[str, object]:
    return {
        "project_id": project_id,
        "provider_id": "mock-text",
        "input_json": json_dumps(
            {
                "asset_types": ["character", "scene", "prop"],
                "params": {},
            }
        ),
    }


def test_reextract_ready_asset_replaces_stale_generated_profile(
    asset_extraction_services: tuple[Database, AssetService, dict[str, object]],
) -> None:
    _database, service, project = asset_extraction_services
    existing = service.create(
        str(project["id"]),
        {
            "asset_type": "character",
            "name": "小明",
            "profile": {"appearance": "棕色长发", "costume": "蓝色校服"},
            "prompt": "棕色长发、蓝色校服",
            "negative_prompt": "旧负向提示词",
            "status": "ready",
        },
    )

    saved_id = service._upsert_extracted(project, "character", _fresh_character())
    updated = service.get(saved_id)

    assert saved_id == existing["id"]
    assert updated["profile"]["appearance"] == "黑色短发，左眉浅疤"
    assert updated["profile"]["costume"] == "灰色风衣"
    assert updated["prompt"] == "黑色短发、左眉浅疤、灰色风衣"
    assert updated["negative_prompt"] == "身份漂移，服装变化，文字，水印"


def test_reextract_confirmed_asset_preserves_user_confirmed_content(
    asset_extraction_services: tuple[Database, AssetService, dict[str, object]],
) -> None:
    _database, service, project = asset_extraction_services
    existing = service.create(
        str(project["id"]),
        {
            "asset_type": "character",
            "name": "小明",
            "profile": {"appearance": "用户确认的短发造型"},
            "prompt": "用户确认的角色提示词",
            "negative_prompt": "用户确认的负向提示词",
            "status": "confirmed",
        },
    )

    saved_id = service._upsert_extracted(project, "character", _fresh_character())
    updated = service.get(saved_id)

    assert saved_id == existing["id"]
    assert updated["status"] == "confirmed"
    assert updated["revision"] == existing["revision"]
    assert updated["profile"] == {"appearance": "用户确认的短发造型"}
    assert updated["prompt"] == "用户确认的角色提示词"
    assert updated["negative_prompt"] == "用户确认的负向提示词"


def test_extract_assets_with_minor_screenplay_characters_saves_only_key_roles_once(
    asset_extraction_services: tuple[Database, AssetService, dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, service, project = asset_extraction_services
    WritingService(database).update_screenplay(
        str(project["id"]),
        {
            "content": _screenplay(
                characters=["小明", "律师", "前台", "张助理（电话音）", "众人"],
                speakers=["女助理"],
            )
        },
    )
    provider = SequencedAssetProvider(
        [
            {
                "characters": [_character("小明")],
                "scenes": [],
                "props": [],
            }
        ]
    )
    monkeypatch.setattr(
        service.providers,
        "resolve",
        lambda **_values: (provider, {"params": {}}),
    )

    result = asyncio.run(service.handle_extract(_extract_job(str(project["id"]))))

    characters = service.list(str(project["id"]), "character")
    assert len(provider.calls) == 1
    assert provider.calls[0]["user_prompt"] == f"小说正文：\n{project['source_text']}"
    assert {item["name"] for item in characters} == {"小明"}
    assert result["count"] == 1
