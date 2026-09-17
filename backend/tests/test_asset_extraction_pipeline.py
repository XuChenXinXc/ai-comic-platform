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


def _screenplay_with_props(props: list[str]) -> dict[str, object]:
    """构造一个仅用于审计测试的确认剧本，props 可控且场景/角色可预先建好。"""
    return {
        "title": "雨夜修表铺",
        "logline": "小明在修表铺寻找父亲留下的线索。",
        "scenes": [
            {
                "scene_no": 1,
                "scene_name": "法院大厅",
                "time_weather": "白天，晴",
                "characters": ["小明"],
                "props": props,
                "objective": "确认关键线索",
                "action": "众人进入大厅，小明把文件交给律师。",
                "dialogue": [{"speaker": "小明", "text": "东西在这里。"}],
                "narration": [],
                "emotion": "紧张",
                "transition_to_next": "切到法庭内",
            }
        ],
    }


def _screenplay_with_photo_mention() -> dict[str, object]:
    """场次 action 里提到另一个场景素材名，但实际是在描述照片内容。

    模拟真实误报：action 原文「苏晴指了指他手中的照片——照片上是一家门头
    写着「林记钟表」的旧店」，本场并未发生空间切换。
    """
    return {
        "title": "雨夜修表铺",
        "logline": "苏晴在老照片里认出父亲的钟表店。",
        "scenes": [
            {
                "scene_no": 1,
                "scene_name": "便利店屋檐下",
                "time_weather": "雨夜",
                "characters": [],
                "props": [],
                "objective": "确认照片线索",
                "action": "苏晴指了指他手中的照片——照片上是一家门头写着「林记钟表」的旧店。",
                "dialogue": [],
                "narration": [],
                "emotion": "怀念",
                "transition_to_next": "切到青石巷。",
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
    screenplay_payload = _screenplay(
        characters=["小明", "律师", "前台", "张助理（电话音）", "众人"],
        speakers=["女助理"],
    )
    saved_screenplay = WritingService(database).update_screenplay(
        str(chapter["id"]),
        {
            "content": screenplay_payload,
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
    # 视觉语义重解释治理：提取以剧本为第一权威事实，注入画风与事实硬约束
    prompt = provider.calls[0]["user_prompt"]
    assert f"项目统一画风：{project['style_prompt']}" in prompt
    assert _ASSET_FACT_ONLY_CONSTRAINT in prompt
    assert "本章正文：" in prompt
    assert "已有素材目录（已存在，本章若出现同一对象请勿重复提取）：" in prompt
    assert "【本集剧本事实（权威依据，提取的角色与场景必须覆盖剧本实际出场对象）】" in prompt
    assert json_dumps(saved_screenplay["content"]) in prompt
    assert "1. 剧本中登场的所有角色必须全部提取" in prompt
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


def test_upsert_does_not_merge_when_multiple_existing_assets_match_alias(
    asset_extraction_services: tuple[Database, AssetService, dict[str, object], dict[str, object]],
) -> None:
    """多候选别名歧义时不合并：两个已有素材别名均包含同一词时，新素材仍独立创建。"""
    _database, service, project, _chapter = asset_extraction_services
    c1 = service.create(
        str(project["id"]),
        {
            "asset_type": "character",
            "name": "李大明",
            "profile": {"aliases": ["大明", "阿明"]},
            "prompt": "已有提示词1",
            "negative_prompt": "负向",
        },
    )
    c2 = service.create(
        str(project["id"]),
        {
            "asset_type": "character",
            "name": "李小明",
            "profile": {"aliases": ["小明", "阿明"]},
            "prompt": "已有提示词2",
            "negative_prompt": "负向",
        },
    )

    saved_id = service._upsert_extracted(
        project,
        "character",
        {**_fresh_character(), "name": "阿明"},
    )

    assert saved_id not in (c1["id"], c2["id"])
    names = {item["name"] for item in service.list(str(project["id"]), "character")}
    assert names == {"李大明", "李小明", "阿明"}


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


def test_extract_parenthetical_analysis_name_matches_existing_asset_without_warning(
    asset_extraction_services: tuple[Database, AssetService, dict[str, object], dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """小说分析把主角名存成「顾珩（注释）」时，已提取角色不应被误报为缺失。"""
    database, service, project, chapter = asset_extraction_services
    content = _analysis_content()
    content["main_characters"] = [
        "小明",
        "顾珩（群里的甲方顾工，也是隔壁邻居；原文后段写作陆珩，属同一角色）",
    ]
    WritingService(database).update_analysis(
        str(chapter["id"]),
        {"content": content},
    )
    service.create(
        str(project["id"]),
        {
            "asset_type": "character",
            "name": "顾珩",
            "profile": {"appearance": "黑色短发", "aliases": ["顾工", "陆珩"]},
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

    assert not any("顾珩" in warning for warning in result["warnings"])
    assert "核心角色「小明」尚未在素材库中" in result["warnings"]


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


def test_complete_missing_creates_independent_asset_when_alias_matches_existing_sub_scene(
    asset_extraction_services: tuple[Database, AssetService, dict[str, object], dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """父子场景别名冲突时补全：已有苏棠公寓客厅/厨房（别名均含苏棠公寓），补全苏棠公寓应独立创建并清理子场景歧义别名。"""
    _database, service, project, chapter = asset_extraction_services
    living_room = service.create(
        str(project["id"]),
        {
            "asset_type": "scene",
            "name": "苏棠公寓客厅",
            "profile": {"aliases": ["苏棠公寓", "客厅"]},
            "prompt": "小户型客厅",
            "negative_prompt": "人物",
        },
    )
    kitchen = service.create(
        str(project["id"]),
        {
            "asset_type": "scene",
            "name": "苏棠公寓厨房",
            "profile": {"aliases": ["苏棠公寓", "厨房"]},
            "prompt": "小户型厨房",
            "negative_prompt": "人物",
        },
    )
    missing = [
        {
            "asset_type": "scene",
            "reason": "剧本场次为苏棠公寓，素材库只有子场景且别名冲突无法唯一解析",
            "source_ref": "第一场",
            "suggested_name": "苏棠公寓",
            "suggested_aliases": ["女主公寓"],
        }
    ]
    provider = SequencedAssetProvider(
        [
            {
                "characters": [],
                "scenes": [
                    {
                        "name": "苏棠公寓",
                        "aliases": ["女主公寓"],
                        "layout": "开间全貌，客厅与厨房贯通",
                        "prompt": "高层单身公寓整体室内",
                        "negative_prompt": "人物",
                        "consistency_keywords": ["单身公寓"],
                        "composition": "平视全景",
                        "importance": "normal",
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
    new_asset_id = result["asset_ids"][0]
    assert new_asset_id not in (living_room["id"], kitchen["id"])
    saved = service.get(new_asset_id)
    assert saved["name"] == "苏棠公寓"

    # 原有子场景别名中的「苏棠公寓」已被清理，不再有别名歧义
    updated_living = service.get(living_room["id"])
    updated_kitchen = service.get(kitchen["id"])
    assert "苏棠公寓" not in updated_living["profile"].get("aliases", [])
    assert "苏棠公寓" not in updated_kitchen["profile"].get("aliases", [])


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


def test_audit_treats_all_confirmed_screenplay_props_as_core_requirements(
    asset_extraction_services: tuple[Database, AssetService, dict[str, object], dict[str, object]],
) -> None:
    """剧本中的关键道具作为核心需求参与审计，缺失时如实报告。"""
    database, service, project, chapter = asset_extraction_services
    service.create(str(project["id"]), {"asset_type": "scene", "name": "法院大厅"})
    service.create(str(project["id"]), {"asset_type": "character", "name": "小明"})
    WritingService(database).update_screenplay(
        str(chapter["id"]),
        {
            "content": _screenplay_with_props(["传家宝剑", "密信", "银色怀表"]),
            "status": "confirmed",
        },
    )

    audit = service.audit_screenplay_assets(str(chapter["id"]))

    missing_props = {
        item["suggested_name"]
        for item in audit["missing_assets"]
        if item["asset_type"] == "prop"
    }
    assert missing_props == {"传家宝剑", "密信", "银色怀表"}
    assert audit["complete"] is False


def test_audit_keeps_key_prop_and_scene_missing(
    asset_extraction_services: tuple[Database, AssetService, dict[str, object], dict[str, object]],
) -> None:
    """非关键物品（如钥匙、纸袋、手机、灶台等）自动过滤不阻断，关键物品与场景缺失保持硬阻断。"""
    database, service, project, chapter = asset_extraction_services
    service.create(str(project["id"]), {"asset_type": "character", "name": "小明"})
    WritingService(database).update_screenplay(
        str(chapter["id"]),
        {
            "content": _screenplay_with_props(["银色怀表", "铜钥匙", "手机", "灶台"]),
            "status": "confirmed",
        },
    )

    audit = service.audit_screenplay_assets(str(chapter["id"]))

    missing = {
        (item["asset_type"], item["suggested_name"])
        for item in audit["missing_assets"]
    }
    # 关键物品未放宽，仍然报缺失
    assert ("prop", "银色怀表") in missing
    # 非关键物品自动过滤，不报缺失阻断
    assert ("prop", "铜钥匙") not in missing
    assert ("prop", "手机") not in missing
    assert ("prop", "灶台") not in missing
    # 场景缺失继续硬阻断
    assert ("scene", "法院大厅") in missing
    assert audit["complete"] is False


def test_audit_cross_scene_mention_is_warning_not_block(
    asset_extraction_services: tuple[Database, AssetService, dict[str, object], dict[str, object]],
) -> None:
    """action 里提到其它场景名只产生提示，不再硬阻断（照片/回忆式描述常见误报）。"""
    database, service, project, chapter = asset_extraction_services
    service.create(str(project["id"]), {"asset_type": "scene", "name": "便利店屋檐下"})
    service.create(
        str(project["id"]),
        {
            "asset_type": "scene",
            "name": "林记钟表店门口",
            "profile": {"aliases": ["林记钟表"]},
        },
    )
    WritingService(database).update_screenplay(
        str(chapter["id"]),
        {"content": _screenplay_with_photo_mention(), "status": "confirmed"},
    )

    audit = service.audit_screenplay_assets(str(chapter["id"]))

    # 旧字段保留：仍然如实列出跨场提及，供 UI 展示
    assert audit["visual_space_conflicts"]
    # 但不再参与 complete，角色/场景齐备即通过
    assert audit["complete"] is True
    assert any("林记钟表店门口" in message for message in audit["warnings"])
    # 不再使用命令式「请先在剧本中从空间变化处拆场」文案
    assert not any("请先在剧本中从空间变化处拆场" in message for message in audit["warnings"])


def test_audit_missing_prop_blocks_storyboard_extraction(
    asset_extraction_services: tuple[Database, AssetService, dict[str, object], dict[str, object]],
) -> None:
    """确认剧本中的关键物品缺失必须阻断，防止分镜以纯文字静默降级。"""
    database, service, project, chapter = asset_extraction_services
    service.create(str(project["id"]), {"asset_type": "scene", "name": "法院大厅"})
    service.create(str(project["id"]), {"asset_type": "character", "name": "小明"})
    WritingService(database).update_screenplay(
        str(chapter["id"]),
        {
            "content": _screenplay_with_props(["父亲笔记"]),
            "status": "confirmed",
        },
    )

    audit = service.audit_screenplay_assets(str(chapter["id"]))

    missing = {
        (item["asset_type"], item["suggested_name"])
        for item in audit["missing_assets"]
    }
    assert ("prop", "父亲笔记") in missing
    assert audit["complete"] is False


def test_audit_missing_character_still_blocks(
    asset_extraction_services: tuple[Database, AssetService, dict[str, object], dict[str, object]],
) -> None:
    """角色缺失仍硬阻断：身份一致性必需，不能只靠文字降级。"""
    database, service, project, chapter = asset_extraction_services
    service.create(str(project["id"]), {"asset_type": "scene", "name": "法院大厅"})
    WritingService(database).update_screenplay(
        str(chapter["id"]),
        {"content": _screenplay_with_props([]), "status": "confirmed"},
    )

    audit = service.audit_screenplay_assets(str(chapter["id"]))

    missing = {
        (item["asset_type"], item["suggested_name"])
        for item in audit["missing_assets"]
    }
    assert ("character", "小明") in missing
    assert audit["complete"] is False


def test_audit_missing_scene_still_blocks(
    asset_extraction_services: tuple[Database, AssetService, dict[str, object], dict[str, object]],
) -> None:
    """场景缺失仍硬阻断：构图与视频生成必需。"""
    database, service, project, chapter = asset_extraction_services
    service.create(str(project["id"]), {"asset_type": "character", "name": "小明"})
    WritingService(database).update_screenplay(
        str(chapter["id"]),
        {"content": _screenplay_with_props([]), "status": "confirmed"},
    )

    audit = service.audit_screenplay_assets(str(chapter["id"]))

    missing = {
        (item["asset_type"], item["suggested_name"])
        for item in audit["missing_assets"]
    }
    assert ("scene", "法院大厅") in missing
    assert audit["complete"] is False
