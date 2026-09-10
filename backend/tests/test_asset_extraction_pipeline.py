from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.db import Database
from app.schemas import AssetExtractionOutput
from app.services.asset_service import (
    _ASSET_FACT_ONLY_CONSTRAINT,
    _EXTRACT_EMPTY_RETRIES,
    AssetService,
)
from app.services.chapter_service import ChapterService
from app.services.file_service import FileService
from app.services.project_service import ProjectService
from app.services.utils import json_dumps
from app.services.writing_service import WritingService


@pytest.fixture
def asset_extraction_services(
    tmp_path: Path,
) -> tuple[Database, AssetService, dict[str, object], dict[str, object]]:
    database = Database(f"sqlite:///{(tmp_path / 'asset-extraction.db').as_posix()}")
    database.initialize(seed_mock_providers=True)
    project = ProjectService(database, FileService(tmp_path)).create({"name": "素材提取"})
    chapter = ChapterService(database, FileService(tmp_path)).create(
        project["id"], {"title": "第一章", "source_text": "小明换上灰色风衣走进旧剧院。"}
    )
    service = AssetService(database)
    yield database, service, project, chapter
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


def _extract_job(chapter_id: str, project_id: str = "") -> dict[str, object]:
    return {
        "project_id": project_id,
        "chapter_id": chapter_id,
        "provider_id": "mock-text",
        "input_json": json_dumps(
            {
                "asset_types": ["character", "scene", "prop"],
                "params": {},
            }
        ),
    }


def _analysis_content() -> dict[str, object]:
    return {
        "summary": "主角发现线索。",
        "genre": "悬疑",
        "time_period": "现代",
        "locations": ["办公室"],
        "worldview": "当代现实世界",
        "main_characters": ["小明", "李四"],
        "character_relations": [],
        "core_conflict": "寻找真相",
        "plot_structure": {},
        "emotion_curve": [],
        "foreshadowing": [],
        "visualizable_content": [],
        "hard_to_visualize": [],
        "production_risks": [],
    }


def test_reextract_ready_asset_keeps_existing_fields_and_fills_missing(
    asset_extraction_services: tuple[Database, AssetService, dict[str, object], dict[str, object]],
) -> None:
    _database, service, project, chapter = asset_extraction_services
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
    # 现有非空字段保留，新提取结果不覆盖
    assert updated["profile"]["appearance"] == "棕色长发"
    assert updated["profile"]["costume"] == "蓝色校服"
    assert updated["prompt"] == "棕色长发、蓝色校服"
    assert updated["negative_prompt"] == "旧负向提示词"
    # 现有空缺字段由新提取结果填充
    assert updated["consistency_keywords"] == ["黑色短发", "左眉浅疤", "灰色风衣"]


def test_reextract_asset_merges_fields_keeping_existing_non_empty(
    asset_extraction_services: tuple[Database, AssetService, dict[str, object], dict[str, object]],
) -> None:
    """同名素材重新提取：现有非空字段（含用户手动编辑）优先保留，新结果只填缺。"""
    _database, service, project, chapter = asset_extraction_services
    existing = service.create(
        str(project["id"]),
        {
            "asset_type": "character",
            "name": "小明",
            "profile": {
                "appearance": "用户确认的短发造型",
                "costume": "用户确认的旧风衣",
            },
            "prompt": "用户确认的角色提示词",
            "negative_prompt": "用户确认的负向提示词",
            "status": "confirmed",
        },
    )

    saved_id = service._upsert_extracted(project, "character", _fresh_character())
    updated = service.get(saved_id)

    assert saved_id == existing["id"]
    # 现有非空字段保留，不被新提取结果覆盖
    assert updated["profile"]["appearance"] == "用户确认的短发造型"
    assert updated["profile"]["costume"] == "用户确认的旧风衣"
    assert updated["prompt"] == "用户确认的角色提示词"
    assert updated["negative_prompt"] == "用户确认的负向提示词"
    # 状态保留（不因重新提取降级）
    assert updated["status"] == "confirmed"
    # 现有空缺字段由新提取结果填充
    assert updated["consistency_keywords"] == ["黑色短发", "左眉浅疤", "灰色风衣"]
    assert updated["revision"] == existing["revision"] + 1


def test_extract_assets_with_minor_screenplay_characters_saves_only_key_roles_once(
    asset_extraction_services: tuple[Database, AssetService, dict[str, object], dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, service, project, chapter = asset_extraction_services
    WritingService(database).update_screenplay(
        str(chapter["id"]),
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

    result = asyncio.run(
        service.handle_extract(_extract_job(str(chapter["id"]), str(project["id"])))
    )

    characters = service.list(str(project["id"]), "character")
    assert len(provider.calls) == 1
    # 视觉语义重解释治理（Phase 2）：提取调用头部注入项目统一画风与只记事实硬约束
    assert provider.calls[0]["user_prompt"] == (
        f"项目统一画风：{project['style_prompt']}\n"
        f"{_ASSET_FACT_ONLY_CONSTRAINT}\n"
        f"本章正文：\n{chapter['source_text']}\n\n"
        "已有素材目录（已存在，本章若出现同一对象请勿重复提取）：\n[]"
        "\n\n本集已生成剧本的场次名（提取场景素材时：正文地点若与本集剧本"
        "场次一致，`name` 必须逐字采用剧本场次名，或至少把剧本场次名收进"
        " `aliases`，保证后续分镜按名称绑定场景素材）：\n"
        f"{json_dumps(['法院大厅'])}"
    )
    assert {item["name"] for item in characters} == {"小明"}
    assert result["count"] == 1


def test_upsert_matches_existing_asset_by_alias(
    asset_extraction_services: tuple[Database, AssetService, dict[str, object], dict[str, object]],
) -> None:
    _database, service, project, _chapter = asset_extraction_services
    existing = service.create(
        str(project["id"]),
        {
            "asset_type": "character",
            "name": "小明",
            "profile": {"aliases": ["阿明"]},
            "prompt": "已有提示词",
            "negative_prompt": "已有负向提示词",
        },
    )

    saved_id = service._upsert_extracted(
        project,
        "character",
        {**_fresh_character(), "name": "阿明"},
    )

    assert saved_id == existing["id"]
    assert [item["name"] for item in service.list(str(project["id"]), "character")] == ["小明"]


def test_upsert_matches_existing_asset_by_state_variant_name(
    asset_extraction_services: tuple[Database, AssetService, dict[str, object], dict[str, object]],
) -> None:
    _database, service, project, _chapter = asset_extraction_services
    existing = service.create(
        str(project["id"]),
        {
            "asset_type": "prop",
            "name": "万灯阁命灯",
            "profile": {"concept": "命灯"},
            "prompt": "铜制命灯",
            "negative_prompt": "人物",
        },
    )
    extracted = {
        "name": "万灯阁黑色命灯",
        "description": "熄灭后的命灯",
        "prompt": "黑色命灯",
        "negative_prompt": "人物",
        "consistency_keywords": ["铜灯"],
        "composition": "居中",
        "importance": "core",
    }

    saved_id = service._upsert_extracted(project, "prop", extracted)

    assert saved_id == existing["id"]
    assert [item["name"] for item in service.list(str(project["id"]), "prop")] == ["万灯阁命灯"]


def test_upsert_merges_list_profile_fields_instead_of_discarding_new_values(
    asset_extraction_services: tuple[Database, AssetService, dict[str, object], dict[str, object]],
) -> None:
    _database, service, project, _chapter = asset_extraction_services
    existing = service.create(
        str(project["id"]),
        {
            "asset_type": "character",
            "name": "小明",
            "profile": {"aliases": ["阿明"], "costume_variants": ["旧袍"]},
            "prompt": "已有提示词",
            "negative_prompt": "已有负向提示词",
        },
    )

    service._upsert_extracted(
        project,
        "character",
        {
            **_fresh_character(),
            "aliases": ["小名"],
            "costume_variants": ["战损袍"],
        },
    )

    updated = service.get(existing["id"])
    assert updated["profile"]["aliases"] == ["阿明", "小名"]
    assert updated["profile"]["costume_variants"] == ["旧袍", "战损袍"]


def test_extract_warns_when_analysis_main_character_has_no_asset(
    asset_extraction_services: tuple[Database, AssetService, dict[str, object], dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, service, project, chapter = asset_extraction_services
    WritingService(database).update_analysis(
        str(chapter["id"]),
        {"content": _analysis_content()},
    )
    service.create(
        str(project["id"]),
        {
            "asset_type": "character",
            "name": "小明",
            "profile": {"appearance": "黑色短发"},
            "prompt": "黑色短发",
            "negative_prompt": "身份漂移",
        },
    )

    class EmptyAssetProvider:
        async def generate_json(self, **_values: Any) -> AssetExtractionOutput:
            return AssetExtractionOutput.model_validate(
                {"characters": [], "scenes": [], "props": []}
            )

    monkeypatch.setattr(
        service.providers,
        "resolve",
        lambda **_values: (EmptyAssetProvider(), {"params": {}}),
    )

    result = asyncio.run(
        service.handle_extract(_extract_job(str(chapter["id"]), str(project["id"])))
    )

    assert "核心角色「李四」尚未在素材库中" in result["warnings"]


def test_complete_missing_creates_assets_from_missing_list(
    asset_extraction_services: tuple[Database, AssetService, dict[str, object], dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺失素材补全：只按缺失清单提取入库，清单为空时不调用模型。

    分镜阶段报告 missing_assets 后，由 complete_missing 用专用提示词
    asset_complete 补建素材，供重新提取分镜时按 ID 绑定。
    """
    _database, service, project, chapter = asset_extraction_services
    missing = [
        {
            "asset_type": "scene",
            "reason": "剧情进入办公楼大堂，素材库没有对应场景",
            "source_ref": "第二场",
            "suggested_name": "中关村科技公司一层大堂",
            "suggested_aliases": ["公司大堂", "一楼大厅"],
        }
    ]
    provider = SequencedAssetProvider(
        [
            {
                "characters": [],
                "scenes": [
                    {
                        "name": "中关村科技公司一层大堂",
                        "aliases": ["公司大堂", "一楼大厅"],
                        "layout": "旋转门正对前台，左右各一组闸机",
                        "prompt": "办公楼一层大堂，旋转门与前台位置固定",
                        "negative_prompt": "人物，人群，人影，文字，水印",
                        "consistency_keywords": ["旋转门", "前台"],
                        "composition": "主透视图、双向全景、俯视平面图",
                        "importance": "core",
                    }
                ],
                "props": [],
            }
        ]
    )
    monkeypatch.setattr(
        service.providers,
        "resolve",
        lambda **_values: (provider, {"params": {}}),
    )

    result = asyncio.run(
        service.complete_missing(str(chapter["id"]), missing, provider_id="mock-text")
    )

    assert result["count"] == 1
    saved = service.get(result["asset_ids"][0])
    assert saved["name"] == "中关村科技公司一层大堂"
    assert saved["asset_type"] == "scene"
    assert saved["profile"]["aliases"] == ["公司大堂", "一楼大厅"]
    # 补素材提示词携带缺失清单与已有目录
    assert "缺失素材清单" in provider.calls[0]["user_prompt"]
    assert "已有素材目录" in provider.calls[0]["user_prompt"]


def test_complete_missing_with_empty_list_does_not_call_model(
    asset_extraction_services: tuple[Database, AssetService, dict[str, object], dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺失清单为空时直接返回空结果，不调用模型。"""
    _database, service, project, chapter = asset_extraction_services
    called: list[str] = []

    class UnexpectedProvider:
        async def generate_json(self, **_values: Any) -> AssetExtractionOutput:
            called.append("called")
            return AssetExtractionOutput(
                characters=[], scenes=[], props=[]
            )

    monkeypatch.setattr(
        service.providers,
        "resolve",
        lambda **_values: (UnexpectedProvider(), {"params": {}}),
    )

    result = asyncio.run(service.complete_missing(str(chapter["id"]), []))

    assert result == {"asset_ids": [], "count": 0, "warnings": []}
    assert called == []


def test_extract_first_empty_retry_with_content_saves_asset(
    asset_extraction_services: tuple[Database, AssetService, dict[str, object], dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首次提取、第一次返回空、重试返回内容：provider 被调 2 次，且二次调用带强化措辞。"""
    _database, service, project, chapter = asset_extraction_services
    provider = SequencedAssetProvider(
        [
            {"characters": [], "scenes": [], "props": []},
            {"characters": [_character("小明")], "scenes": [], "props": []},
        ]
    )
    monkeypatch.setattr(
        service.providers,
        "resolve",
        lambda **_values: (provider, {"params": {}}),
    )

    result = asyncio.run(
        service.handle_extract(_extract_job(str(chapter["id"]), str(project["id"])))
    )

    assert len(provider.calls) == 2
    # 首次调用 user_prompt 保持原拼装方式不变（含画风与只记事实硬约束头部）
    assert provider.calls[0]["user_prompt"] == (
        f"项目统一画风：{project['style_prompt']}\n"
        f"{_ASSET_FACT_ONLY_CONSTRAINT}\n"
        f"本章正文：\n{chapter['source_text']}\n\n"
        "已有素材目录（已存在，本章若出现同一对象请勿重复提取）：\n[]"
    )
    # 第二次调用在原 user_prompt 基础上追加了首次提取的强化措辞
    assert provider.calls[1]["user_prompt"].startswith(provider.calls[0]["user_prompt"])
    assert "首次素材提取" in provider.calls[1]["user_prompt"]
    assert "不得返回空数组" in provider.calls[1]["user_prompt"]
    characters = service.list(str(project["id"]), "character")
    assert {item["name"] for item in characters} == {"小明"}
    assert result["count"] == 1


def test_extract_first_always_empty_raises_and_saves_nothing(
    asset_extraction_services: tuple[Database, AssetService, dict[str, object], dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首次提取、始终返回空：抛出异常，且 asset 表无新记录。"""
    _database, service, project, chapter = asset_extraction_services
    calls: list[dict[str, Any]] = []

    class EmptyAssetProvider:
        async def generate_json(self, **values: Any) -> AssetExtractionOutput:
            calls.append(values)
            return AssetExtractionOutput.model_validate(
                {"characters": [], "scenes": [], "props": []}
            )

    monkeypatch.setattr(
        service.providers,
        "resolve",
        lambda **_values: (EmptyAssetProvider(), {"params": {}}),
    )

    with pytest.raises(RuntimeError, match="首次素材提取未返回任何素材"):
        asyncio.run(
            service.handle_extract(_extract_job(str(chapter["id"]), str(project["id"])))
        )

    # 首调 + 重试各一次
    assert len(calls) == 1 + _EXTRACT_EMPTY_RETRIES
    assert service.list(str(project["id"]), "character") == []
    assert service.list(str(project["id"]), "scene") == []
    assert service.list(str(project["id"]), "prop") == []


def test_extract_incremental_empty_is_valid_no_retry(
    asset_extraction_services: tuple[Database, AssetService, dict[str, object], dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """增量提取（素材库已有内容）返回空：合法正常结果，不重试不抛异常。"""
    _database, service, project, chapter = asset_extraction_services
    service.create(
        str(project["id"]),
        {
            "asset_type": "character",
            "name": "小明",
            "profile": {"appearance": "黑色短发"},
            "prompt": "黑色短发",
            "negative_prompt": "身份漂移",
        },
    )
    provider = SequencedAssetProvider(
        [
            {
                "characters": [],
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

    result = asyncio.run(
        service.handle_extract(_extract_job(str(chapter["id"]), str(project["id"])))
    )

    assert len(provider.calls) == 1
    assert result["count"] == 0


def test_merge_assets_without_target_image_promotes_source_current_image(
    asset_extraction_services: tuple[Database, AssetService, dict[str, object], dict[str, object]],
) -> None:
    """素材合并保护：若目标素材原本无图，源素材的当前图应晋升为目标当前图。"""
    from sqlalchemy import insert, select

    from app.models.tables import asset_image

    database, service, project, _chapter = asset_extraction_services
    target = service.create(
        str(project["id"]),
        {"asset_type": "scene", "name": "目标场景", "prompt": "空旷大厅"},
    )
    source = service.create(
        str(project["id"]),
        {"asset_type": "scene", "name": "源场景", "prompt": "大厅角落"},
    )

    now = "2026-09-07T00:00:00Z"
    with database.engine.begin() as conn:
        conn.execute(
            insert(asset_image).values(
                id="img_src_1",
                asset_id=str(source["id"]),
                version_no=1,
                file_path="uploads/src1.png",
                prompt_snapshot="大厅角落",
                negative_prompt_snapshot="",
                reference_files_json="[]",
                provider_id=None,
                model="",
                provider_task_id="",
                status="completed",
                is_current=True,
                input_signature="sig_1",
                source="generated",
                error="",
                created_at=now,
            )
        )

    merged = service.merge(str(target["id"]), [str(source["id"])])
    assert merged["id"] == target["id"]

    with database.engine.connect() as conn:
        images = conn.execute(
            select(asset_image).where(asset_image.c.asset_id == str(target["id"]))
        ).mappings().all()

    assert len(images) == 1
    assert images[0]["id"] == "img_src_1"
    assert bool(images[0]["is_current"]) is True
