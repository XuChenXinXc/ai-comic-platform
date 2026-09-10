"""场景族（Scene Family）一致性回归测试。

覆盖：族成员生成以锚点场景图为参考、锚点图缺失阻止生成、锚点场景独立生成、
锚点图重生成后成员图 stale、提取 schema 接受场景族字段并入库。
每个场景素材只有一张当前图（无 Scene Views）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from media_test_helpers import _PNG
from sqlalchemy import func, insert, select, update

from app.db import Database
from app.models.tables import asset_image
from app.schemas import AssetExtractionOutput
from app.services.asset_service import AssetService
from app.services.chapter_service import ChapterService
from app.services.errors import ServiceError
from app.services.file_service import file_service
from app.services.job_service import JobService
from app.services.media_service import MediaService
from app.services.project_service import ProjectService
from app.services.utils import json_dumps, new_id, utc_now


@pytest.fixture
def scene_family_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[dict[str, Any]]:
    """场景族测试环境：项目 + 锚点场景「城墙」+ 族成员「城门洞」。"""
    database = Database(f"sqlite:///{(tmp_path / 'scene-family.db').as_posix()}")
    database.initialize(seed_mock_providers=True)
    monkeypatch.setattr(file_service, "workspace_dir", tmp_path.resolve())
    monkeypatch.setattr(file_service, "projects_dir", (tmp_path / "projects").resolve())
    file_service.projects_dir.mkdir(parents=True, exist_ok=True)
    jobs = JobService(database)
    assets = AssetService(database, jobs)
    media = MediaService(database, jobs)
    project = ProjectService(database).create({"name": "场景族测试"})
    ChapterService(database).create(project["id"], {"title": "第一章"})
    anchor = assets.create(
        project["id"],
        {
            "asset_type": "scene",
            "name": "城墙",
            "prompt": "古城墙顶，青砖城楼，朱漆大门",
            "negative_prompt": "人物，人群",
            "consistency_keywords": ["青砖", "朱漆门"],
            "composition": "",
            "profile": {
                "scene_family": "古城墙",
                "family_anchor": True,
                "layout": "城墙顶俯瞰城楼与垛口",
            },
        },
    )
    member = assets.create(
        project["id"],
        {
            "asset_type": "scene",
            "name": "城门洞",
            "prompt": "城门洞内部拱形通道，青砖券顶",
            "negative_prompt": "人物，人群",
            "consistency_keywords": ["青砖", "拱形甬道"],
            "composition": "",
            "profile": {
                "scene_family": "古城墙",
                "family_anchor": False,
                "layout": "拱形甬道，石板地面",
            },
        },
    )
    yield {
        "database": database,
        "assets": assets,
        "media": media,
        "project": project,
        "anchor": anchor,
        "member": member,
    }
    database.dispose()


def _attach_image(
    services: dict[str, Any],
    scene: dict[str, Any],
    *,
    version_no: int | None = None,
    content: bytes | None = None,
) -> str:
    """给场景插入一张当前图（带真实文件），返回相对路径。"""
    database = services["database"]
    with database.engine.connect() as connection:
        max_version = connection.scalar(
            select(func.max(asset_image.c.version_no)).where(
                asset_image.c.asset_id == scene["id"]
            )
        ) or 0
    version = version_no if version_no is not None else int(max_version) + 1
    relative = f"assets/scenes/{scene['id']}/v{version:03d}.png"
    target = file_service.resolve_project_path(scene["project_id"], relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content if content is not None else _PNG)
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(asset_image).values(
                id=new_id("asset_image"),
                asset_id=scene["id"],
                version_no=version,
                file_path=relative,
                provider_url=None,
                prompt_snapshot="场景图",
                negative_prompt_snapshot="",
                reference_files_json="[]",
                provider_id=None,
                model="",
                provider_task_id="",
                status="completed",
                is_current=1,
                input_signature="attached",
                source="generated",
                error="",
                created_at=now,
            )
        )
    return relative


def _mock_image_provider(media: MediaService, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        media.providers,
        "resolve",
        lambda **_kwargs: (
            object(),
            {"id": "mock-image", "model": "gpt-image-2", "params": {}},
        ),
    )


def test_family_member_generation_uses_anchor_reference(
    scene_family_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """族成员生成场景图：必须以锚点场景当前图为参考，提示词追加同族一致性约束。"""
    services = scene_family_services
    media = services["media"]
    anchor_path = _attach_image(services, services["anchor"])
    _mock_image_provider(media, monkeypatch)

    media.queue_asset_image(services["member"]["id"], {})

    rows = media.list_asset_images(services["member"]["id"])
    assert len(rows) == 1
    assert anchor_path in rows[0]["reference_files_json"]
    assert "【场景族一致性】" in rows[0]["prompt_snapshot"]
    assert "古城墙" in rows[0]["prompt_snapshot"]
    assert "城墙" in rows[0]["prompt_snapshot"]
    assert "城门洞" in rows[0]["prompt_snapshot"]


def test_family_member_without_anchor_image_conflicts(
    scene_family_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """锚点场景未生成场景图时，族成员生成必须被拒绝，不允许独立文生图。"""
    services = scene_family_services
    media = services["media"]
    _mock_image_provider(media, monkeypatch)

    with pytest.raises(ServiceError) as exc_info:
        media.queue_asset_image(services["member"]["id"], {})

    assert "先生成锚点场景" in exc_info.value.detail
    assert "城墙" in exc_info.value.detail


def test_family_member_without_anchor_scene_conflicts(
    scene_family_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """场景族缺少锚点场景时，族成员生成必须被拒绝并提示指定锚点。"""
    services = scene_family_services
    media = services["media"]
    orphan = services["assets"].create(
        services["project"]["id"],
        {
            "asset_type": "scene",
            "name": "孤城东门",
            "prompt": "孤城东门，荒草石阶",
            "negative_prompt": "人物",
            "consistency_keywords": ["石阶"],
            "composition": "",
            "profile": {"scene_family": "孤城", "layout": "城门石阶"},
        },
    )
    _mock_image_provider(media, monkeypatch)

    with pytest.raises(ServiceError) as exc_info:
        media.queue_asset_image(orphan["id"], {})

    assert "缺少锚点场景" in exc_info.value.detail


def test_anchor_scene_generates_independently(
    scene_family_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """锚点场景自身正常生成：不带参考图、不带族一致性文本。"""
    services = scene_family_services
    media = services["media"]
    _mock_image_provider(media, monkeypatch)

    media.queue_asset_image(services["anchor"]["id"], {})

    rows = media.list_asset_images(services["anchor"]["id"])
    assert len(rows) == 1
    assert rows[0]["reference_files_json"] == []
    assert "【场景族一致性】" not in rows[0]["prompt_snapshot"]


def test_anchor_regeneration_marks_member_image_stale(
    scene_family_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """锚点场景图重生成后，族成员已生成的图必须标记为 stale。"""
    services = scene_family_services
    database = services["database"]
    media = services["media"]
    anchor_path = _attach_image(services, services["anchor"])
    _mock_image_provider(media, monkeypatch)
    media.queue_asset_image(services["member"]["id"], {})

    def member_stale() -> bool:
        rows = media.list_asset_images(services["member"]["id"])
        assert len(rows) == 1
        return bool(rows[0]["is_stale"])

    assert not member_stale()
    assert anchor_path in media.list_asset_images(services["member"]["id"])[0][
        "reference_files_json"
    ]

    # 锚点重生成：先把旧版 current 置 0（current 唯一索引按素材分区），
    # 再插入新版本 current 图（路径随版本号变化）
    with database.engine.begin() as connection:
        connection.execute(
            update(asset_image)
            .where(
                asset_image.c.asset_id == services["anchor"]["id"],
                asset_image.c.file_path == anchor_path,
            )
            .values(is_current=0)
        )
    new_path = _attach_image(
        services, services["anchor"], content=_PNG + b"v2"
    )
    assert new_path != anchor_path
    assert member_stale()


def test_scene_schema_accepts_family_fields() -> None:
    """提取输出 schema 接受 scene_family / family_anchor 字段。"""
    output = AssetExtractionOutput.model_validate(
        {
            "characters": [],
            "props": [],
            "scenes": [
                {
                    "name": "城墙",
                    "prompt": "古城墙顶",
                    "negative_prompt": "人物",
                    "consistency_keywords": ["青砖"],
                    "composition": "无人空镜",
                    "layout": "城墙顶俯瞰",
                    "scene_family": "古城墙",
                    "family_anchor": True,
                }
            ],
        }
    )
    assert output.scenes[0].scene_family == "古城墙"
    assert output.scenes[0].family_anchor is True


class _SequencedSceneProvider:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    async def generate_json(self, **values: Any) -> AssetExtractionOutput:
        del values
        return AssetExtractionOutput.model_validate(self.payload)


def test_extraction_stores_family_fields_in_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """提取保存后场景族字段应落入素材档案 profile。"""
    database = Database(f"sqlite:///{(tmp_path / 'extract-family.db').as_posix()}")
    database.initialize(seed_mock_providers=True)
    monkeypatch.setattr(file_service, "workspace_dir", tmp_path.resolve())
    monkeypatch.setattr(file_service, "projects_dir", (tmp_path / "projects").resolve())
    file_service.projects_dir.mkdir(parents=True, exist_ok=True)
    project = ProjectService(database).create({"name": "素材提取"})
    chapter = ChapterService(database).create(
        project["id"], {"title": "第一章", "source_text": "众人登上城墙，又下到城门洞。"}
    )
    service = AssetService(database)
    provider = _SequencedSceneProvider(
        {
            "characters": [],
            "props": [],
            "scenes": [
                {
                    "name": "城墙",
                    "prompt": "古城墙顶",
                    "negative_prompt": "人物",
                    "consistency_keywords": ["青砖"],
                    "composition": "无人空镜",
                    "layout": "城墙顶俯瞰",
                    "scene_family": "古城墙",
                    "family_anchor": True,
                }
            ],
        }
    )
    monkeypatch.setattr(
        service.providers,
        "resolve",
        lambda **_values: (provider, {"params": {}}),
    )

    result = asyncio.run(
        service.handle_extract(
            {
                "project_id": project["id"],
                "chapter_id": chapter["id"],
                "provider_id": "mock-text",
                "input_json": json_dumps(
                    {"asset_types": ["scene"], "params": {}}
                ),
            }
        )
    )

    assert result["count"] == 1
    scenes = service.list(project["id"], "scene")
    assert scenes[0]["name"] == "城墙"
    assert scenes[0]["profile"]["scene_family"] == "古城墙"
    assert scenes[0]["profile"]["family_anchor"] is True
    database.dispose()
