"""Scene Pack（场景资产包）后端回归测试。

覆盖：场景默认视图自动建立、每个 View 独立当前图、关键帧场景参考图
解析优先级（分镜 View → MASTER → 旧图）、删除视图与分镜引用清理、
view_key 提交兼容、asset 展示图优先 MASTER。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from media_test_helpers import _PNG
from sqlalchemy import func, insert, select, update

from app.db import Database
from app.models.tables import asset_image, screenplay, shot
from app.services.asset_service import AssetService
from app.services.chapter_service import ChapterService
from app.services.errors import ServiceError
from app.services.file_service import file_service
from app.services.job_service import JobService
from app.services.media_service import MediaService
from app.services.project_service import ProjectService
from app.services.scene_view_service import DEFAULT_SCENE_VIEWS, SceneViewService
from app.services.shot_service import ShotService
from app.services.utils import new_id, utc_now


@pytest.fixture
def scene_pack_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[dict[str, Any]]:
    """构建 Scene Pack 测试环境：项目 + 场景 + 默认视图。"""
    database = Database(f"sqlite:///{(tmp_path / 'scene-pack.db').as_posix()}")
    database.initialize(seed_mock_providers=True)
    monkeypatch.setattr(file_service, "workspace_dir", tmp_path.resolve())
    monkeypatch.setattr(file_service, "projects_dir", (tmp_path / "projects").resolve())
    file_service.projects_dir.mkdir(parents=True, exist_ok=True)
    jobs = JobService(database)
    assets = AssetService(database, jobs)
    media = MediaService(database, jobs)
    views_service = SceneViewService(database, jobs)
    shots = ShotService(database, jobs)
    project = ProjectService(database).create({"name": "Scene Pack 测试"})
    chapter = ChapterService(database).create(project["id"], {"title": "第一章"})
    scene = assets.create(
        project["id"],
        {
            "asset_type": "scene",
            "name": "苏棠公寓",
            "prompt": "现代简约公寓客厅，浅木地板，深灰布艺沙发靠西墙",
            "negative_prompt": "人物，人群，文字",
            "consistency_keywords": ["浅木地板", "深灰布艺沙发"],
            "composition": "",
        },
    )
    views = views_service.ensure_defaults(scene["id"])
    yield {
        "database": database,
        "assets": assets,
        "media": media,
        "views_service": views_service,
        "shots": shots,
        "project": project,
        "chapter": chapter,
        "scene": scene,
        "views": views,
    }
    database.dispose()


def _attach_image_to_view(
    services: dict[str, Any],
    *,
    filename: str,
    view_key: str,
) -> dict[str, Any]:
    """生成一张图片并归属指定 Scene View，等价于「生成完成 + 激活」后的状态。

    与真实生成链路一致：asset_image 行带 scene_view_id，文件落盘到
    views/{view_key}/ 分区目录，is_current=1 / status=completed。
    """
    database = services["database"]
    scene = services["scene"]
    views = services["views"]
    view = next(item for item in views if item["view_key"] == view_key)
    project_id = scene["project_id"]
    with database.engine.connect() as connection:
        max_version = connection.scalar(
            select(func.max(asset_image.c.version_no)).where(
                asset_image.c.asset_id == scene["id"]
            )
        ) or 0
        version_no = int(max_version) + 1
    version_id = new_id("asset_image")
    relative = f"assets/scenes/{scene['id']}/views/{view_key}/v{version_no:03d}.png"
    version = {"id": version_id, "version_no": version_no, "file_path": relative}
    # 写真实文件（模拟生成落盘）
    target = file_service.resolve_project_path(project_id, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_PNG)
    del filename  # 文件名已并入相对路径，无额外用途
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(asset_image).values(
                id=version_id,
                asset_id=scene["id"],
                scene_view_id=view["id"],
                version_no=version_no,
                file_path=relative,
                provider_url=None,
                prompt_snapshot="场景视图图片",
                negative_prompt_snapshot="",
                reference_files_json="[]",
                provider_id=None,
                model="",
                provider_task_id="",
                status="completed",
                is_current=1,
                input_signature="test-scene-view",
                source="uploaded",
                error="",
                created_at=now,
            )
        )
    return version


def test_ensure_defaults_creates_master_and_four_views(
    scene_pack_services: dict[str, Any],
) -> None:
    """新场景 ensure 后自动建立 MASTER + 4 个标准视角，且幂等。"""
    views_service = scene_pack_services["views_service"]
    scene = scene_pack_services["scene"]
    views = scene_pack_services["views"]

    keys = [view["view_key"] for view in views]
    assert keys == ["master", "front", "reverse", "left45", "right45"]
    assert all(view["asset_id"] == scene["id"] for view in views)
    assert views[0]["view_key"] == "master"
    # 视角提示词缺省已填充（MASTER 除外）
    assert views[1]["prompt"] and not views[0]["prompt"]

    again = views_service.ensure_defaults(scene["id"])
    assert len(again) == len(views)


def test_view_current_images_isolated_per_view(
    scene_pack_services: dict[str, Any],
) -> None:
    """每个 Scene View 有自己的当前图分区，互相不干扰。"""
    views_service = scene_pack_services["views_service"]
    _attach_image_to_view(scene_pack_services, filename="master.png", view_key="master")
    _attach_image_to_view(scene_pack_services, filename="front.png", view_key="front")

    refreshed = views_service.list(scene_pack_services["scene"]["id"])
    master_now = next(view for view in refreshed if view["view_key"] == "master")
    front_now = next(view for view in refreshed if view["view_key"] == "front")
    reverse_now = next(view for view in refreshed if view["view_key"] == "reverse")

    assert master_now["current_image"] is not None
    assert master_now["current_image"]["scene_view_id"] == master_now["id"]
    assert front_now["current_image"] is not None
    assert front_now["current_image"]["scene_view_id"] == front_now["id"]
    # 未上传的视图没有图
    assert reverse_now["current_image"] is None


def test_asset_current_image_prefers_master(
    scene_pack_services: dict[str, Any],
) -> None:
    """场景素材的展示图（current_image）优先 MASTER，其次旧版场景图。"""
    media = scene_pack_services["media"]
    scene = scene_pack_services["scene"]
    assets = scene_pack_services["assets"]

    # 先上传旧版场景图（无视图）
    legacy = media.upload_asset_image(scene["id"], "legacy.png", "image/png", _PNG)
    fetched = assets.get(scene["id"])
    assert fetched["current_image"]["id"] == legacy["id"]
    assert fetched["current_image"]["scene_view_id"] is None

    # 再上传 MASTER 图：展示图切到 MASTER
    master_version = _attach_image_to_view(
        scene_pack_services, filename="master.png", view_key="master"
    )
    fetched = assets.get(scene["id"])
    assert fetched["current_image"]["id"] == master_version["id"]
    assert fetched["scene_views"] is not None
    assert len(fetched["scene_views"]) == 5


def test_delete_view_clears_shot_reference_and_current_image(
    scene_pack_services: dict[str, Any],
) -> None:
    """删除非 MASTER 视图：分镜引用自动置空，不影响其他视图与场景绑定。"""
    scene = scene_pack_services["scene"]
    views_service = scene_pack_services["views_service"]
    shots = scene_pack_services["shots"]
    chapter = scene_pack_services["chapter"]
    views = scene_pack_services["views"]

    front = next(view for view in views if view["view_key"] == "front")
    _attach_image_to_view(scene_pack_services, filename="front.png", view_key="front")

    shot = shots.create(
        chapter["id"],
        {
            "duration_sec": 6,
            "scene_asset_id": scene["id"],
            "scene_view_key": front["view_key"],
            "image_prompt": "客厅空镜",
            "video_prompt": "缓慢推近",
            "shot_no": 1,
        },
    )
    # 已移除多视角场景：分镜不再绑定视角，scene_view_id 恒为 NULL
    assert shot["scene_view_id"] is None

    views_service.delete(front["id"])

    refreshed = views_service.list(scene["id"])
    assert all(view["view_key"] != "front" for view in refreshed)
    shot_after = shots.get(shot["id"])
    assert shot_after["scene_view_id"] is None
    assert shot_after["scene_view_key"] is None
    assert shot_after["scene_asset_id"] == scene["id"]  # 场景绑定不丢失


def test_delete_master_is_rejected(
    scene_pack_services: dict[str, Any],
) -> None:
    """MASTER 视图不可删除。"""
    views_service = scene_pack_services["views_service"]
    views = scene_pack_services["views"]
    master = next(view for view in views if view["view_key"] == "master")
    with pytest.raises(ServiceError):
        views_service.delete(master["id"])


def test_generate_view_requires_master_image(
    scene_pack_services: dict[str, Any],
) -> None:
    """生成非 MASTER 视图前必须已有完成的 MASTER 图（空间一致性前提）。"""
    views_service = scene_pack_services["views_service"]
    scene = scene_pack_services["scene"]
    views = scene_pack_services["views"]
    front = next(view for view in views if view["view_key"] == "front")

    with pytest.raises(Exception) as excinfo:
        views_service.generate(scene["id"], front["view_key"], {})
    assert "MASTER" in str(excinfo.value)


def test_shot_update_scene_view_key_is_ignored(
    scene_pack_services: dict[str, Any],
) -> None:
    """已移除多视角场景：分镜提交 scene_view_key（key 或 id）不再绑定视角。"""
    shots = scene_pack_services["shots"]
    chapter = scene_pack_services["chapter"]
    scene = scene_pack_services["scene"]
    views = scene_pack_services["views"]
    front = next(view for view in views if view["view_key"] == "front")
    left45 = next(view for view in views if view["view_key"] == "left45")

    shot = shots.create(
        chapter["id"],
        {"duration_sec": 6, "scene_asset_id": scene["id"], "image_prompt": "客厅", "shot_no": 1},
    )
    # key 写法：不再落 scene_view_id
    updated = shots.update(shot["id"], {"scene_view_key": front["view_key"]})
    assert updated["scene_view_id"] is None
    assert updated["scene_view_key"] is None
    # id 写法：同样不生效
    updated = shots.update(shot["id"], {"scene_view_key": left45["id"]})
    assert updated["scene_view_id"] is None
    assert updated["scene_view_key"] is None
    # 空串同样保持 NULL
    updated = shots.update(shot["id"], {"scene_view_key": ""})
    assert updated["scene_view_id"] is None

    # 提取路径的 scene_view_key 解析
    chapter_record = scene_pack_services["chapter"]
    with scene_pack_services["database"].engine.begin() as connection:
        connection.execute(
            insert(screenplay).values(
                chapter_id=chapter_record["id"],
                content_json="{}",
                status="ready",
                own_signature="test-screenplay-signature",
                updated_at=utc_now(),
            )
        )
    extracted = {
        "shot_no": 2,
        "duration_sec": 6,
        "scene_asset_id": scene["id"],
        "scene_view_key": "reverse",
        "scene_name": "苏棠公寓",
        "character_names": [],
        "character_asset_ids": [],
        "prop_names": [],
        "prop_asset_ids": [],
        "positions": [],
        "start_state": "空镜",
        "action": "空镜",
        "expressions": [],
        "dialogue": [],
        "narration": "",
        "end_state": "空镜",
        "next_hook": "",
        "shot_type": "全景",
        "camera_angle": "平视",
        "camera_move": "固定",
        "frame_description": "客厅全景空镜",
        "image_prompt": "客厅全景空镜",
        "video_prompt": "固定镜头保持 6 秒",
        "negative_prompt": "人物",
        "scene_no": 1,
        "source_ref": "",
    }
    saved = shots._save_extracted(
        scene_pack_services["chapter"],
        "screenplay",
        {"scope": "all"},
        [extracted],
    )
    assert saved["count"] == 1
    stored = shots.list(chapter["id"])
    # scope=all 覆盖章节内已有分镜并重编号（从 1 开始），提取结果成为 shot_no=1
    assert len(stored) == 1
    second = stored[0]
    assert second["shot_no"] == 1
    # 提取路径同样不绑定视角：scene_view_key 被忽略，视角信息不落库
    assert second["scene_view_id"] is None
    assert second["scene_view_name"] is None


def test_keyframe_reference_resolution_priority(
    scene_pack_services: dict[str, Any],
) -> None:
    """关键帧场景参考图解析优先级：MASTER → 旧版场景图（已移除分镜视图选择）。"""
    media = scene_pack_services["media"]
    scene = scene_pack_services["scene"]
    shots = scene_pack_services["shots"]
    chapter = scene_pack_services["chapter"]

    # 上传旧版场景图（NULL 分区）
    legacy = media.upload_asset_image(scene["id"], "legacy.png", "image/png", _PNG)

    # 无 MASTER 图时：分镜场景参考图回退旧版场景图（单图链路）
    shot = shots.create(
        chapter["id"],
        {
            "duration_sec": 6,
            "scene_asset_id": scene["id"],
            "image_prompt": "客厅",
            "shot_no": 1,
        },
    )
    refreshed = media._resolve_shot_reference_bindings(media._shot(shot["id"]))
    scene_binding = next(item for item in refreshed if item["role"] == "scene_layout")
    assert scene_binding["asset_image_id"] == legacy["id"]

    # 生成 MASTER 图后：MASTER 优先
    master_version = _attach_image_to_view(
        scene_pack_services, filename="master.png", view_key="master"
    )
    assert legacy["id"] != master_version["id"]
    refreshed = media._resolve_shot_reference_bindings(media._shot(shot["id"]))
    scene_binding = next(item for item in refreshed if item["role"] == "scene_layout")
    assert scene_binding["asset_image_id"] == master_version["id"]


def test_default_views_metadata_complete() -> None:
    """默认 Scene View 清单完整：MASTER + 4 个标准视角，提示词齐全。"""
    keys = [item[0] for item in DEFAULT_SCENE_VIEWS]
    assert keys == ["master", "front", "reverse", "left45", "right45"]
    for view_key, name, sort_order, prompt in DEFAULT_SCENE_VIEWS:
        assert name
        assert isinstance(sort_order, int)
        if view_key != "master":
            assert prompt and "摄影机" in prompt


def test_delete_view_recomputes_shot_signature(
    scene_pack_services: dict[str, Any],
) -> None:
    """删除视图置空分镜 scene_view_id 后，own_signature 必须重算并推进 revision。

    回归：此前删除视图只清列不重算签名，落库签名与实际列不一致，
    后续签名比对会把分镜误判为过期，触发不必要的再生成。
    """
    database = scene_pack_services["database"]
    shots = scene_pack_services["shots"]
    views_service = scene_pack_services["views_service"]
    chapter = scene_pack_services["chapter"]
    scene = scene_pack_services["scene"]
    views = scene_pack_services["views"]
    front = next(view for view in views if view["view_key"] == "front")

    shot_record = shots.create(
        chapter["id"],
        {"duration_sec": 6, "scene_asset_id": scene["id"], "image_prompt": "客厅", "shot_no": 1},
    )
    # 模拟历史遗留行：直接把 scene_view_id 指向 front 并写入旧签名。
    with database.engine.begin() as connection:
        connection.execute(
            update(shot)
            .where(shot.c.id == shot_record["id"])
            .values(scene_view_id=front["id"], own_signature="legacy-signature")
        )

    views_service.delete(front["id"])

    after = shots.get(shot_record["id"])
    assert after["scene_view_id"] is None
    assert after["own_signature"] != "legacy-signature"
    assert after["revision"] == shot_record["revision"] + 1


def test_view_prompt_edit_marks_view_image_stale(
    scene_pack_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """编辑视角提示词后，该 View 的当前图应标记为 stale。

    回归：视角提示词参与 View 图片的输入签名，否则编辑提示词后旧图
    既不 stale 也无法重新生成（前端对已生成视图禁用生成按钮）。
    """
    media = scene_pack_services["media"]
    views_service = scene_pack_services["views_service"]
    scene = scene_pack_services["scene"]
    views = scene_pack_services["views"]
    front = next(view for view in views if view["view_key"] == "front")
    monkeypatch.setattr(
        media.providers,
        "resolve",
        lambda **_kwargs: (
            object(),
            {"id": "mock-image", "model": "gpt-image-2", "params": {}},
        ),
    )

    media.queue_asset_image(
        scene["id"],
        {"scene_view_id": front["id"], "scene_view_key": front["view_key"]},
    )

    def front_stale() -> bool:
        rows = media.list_asset_images(scene["id"], scene_view_id=front["id"])
        return bool(rows[0]["is_stale"])

    # 刚入队的版本：签名与预期一致，不 stale。
    assert not front_stale()

    views_service.update(front["id"], {"prompt": "机位再低一些，强调天花板高度"})
    # 视角提示词变化后，旧图必须标记为 stale。
    assert front_stale()