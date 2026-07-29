from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import insert, select

from app.api.schemas import ShotCreate
from app.db import Database
from app.jobs.repository import JobRepository
from app.models.tables import asset_image, keyframe_version, shot, video_version
from app.services.asset_service import AssetService
from app.services.errors import ServiceError
from app.services.media_service import MediaService
from app.services.project_service import ProjectService
from app.services.shot_service import ShotService
from app.services.utils import json_loads, utc_now


@pytest.fixture
def database(tmp_path: Path) -> Database:
    current = Database(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    current.initialize()
    yield current
    current.dispose()


@pytest.fixture
def project_record(database: Database) -> dict[str, Any]:
    return ProjectService(database).create(
        {
            "name": "引用完整性测试",
            "source_text": "主角走进房间。",
            "style_prompt": "二维国风漫剧",
        }
    )


def _create_asset(
    service: AssetService,
    project_id: str,
    asset_type: str,
    name: str,
) -> dict[str, Any]:
    return service.create(
        project_id,
        {
            "asset_type": asset_type,
            "name": name,
        },
    )


def _insert_asset_image(
    database: Database,
    asset_id: str,
    image_id: str,
) -> None:
    with database.engine.begin() as connection:
        connection.execute(
            insert(asset_image).values(
                id=image_id,
                asset_id=asset_id,
                version_no=1,
                file_path=None,
                prompt_snapshot="",
                negative_prompt_snapshot="",
                reference_files_json="[]",
                provider_id=None,
                model="",
                provider_task_id="",
                status="completed",
                is_current=False,
                input_signature="",
                source="uploaded",
                error="",
                created_at=utc_now(),
            )
        )


def _insert_keyframe(
    database: Database,
    shot_id: str,
    keyframe_id: str,
) -> None:
    with database.engine.begin() as connection:
        connection.execute(
            insert(keyframe_version).values(
                id=keyframe_id,
                shot_id=shot_id,
                version_no=1,
                file_path=None,
                prompt_snapshot="",
                negative_prompt_snapshot="",
                reference_bindings_json="[]",
                provider_id=None,
                model="",
                provider_task_id="",
                status="completed",
                is_current=False,
                input_signature="",
                source="uploaded",
                error="",
                created_at=utc_now(),
            )
        )


def _insert_video(
    database: Database,
    shot_id: str,
    video_id: str,
    *,
    keyframe_id: str | None = None,
    status: str = "completed",
) -> None:
    with database.engine.begin() as connection:
        connection.execute(
            insert(video_version).values(
                id=video_id,
                shot_id=shot_id,
                keyframe_version_id=keyframe_id,
                version_no=1,
                file_path=None,
                prompt_snapshot="",
                negative_prompt_snapshot="",
                requested_duration_sec=6,
                actual_duration_sec=None,
                provider_id=None,
                model="",
                provider_task_id="",
                status=status,
                is_current=False,
                input_signature="",
                source="uploaded",
                cost_estimate=0,
                error="",
                created_at=utc_now(),
            )
        )


def _create_active_job(
    database: Database,
    project_id: str,
    target_type: str,
    target_id: str,
) -> None:
    JobRepository(database).create_job(
        project_id=project_id,
        job_type=f"test_{target_type}",
        target_type=target_type,
        target_id=target_id,
    )


def _create_shot(
    service: ShotService,
    project_id: str,
    **overrides: Any,
) -> dict[str, Any]:
    values = ShotCreate(**overrides).model_dump()
    return service.create(project_id, values)


def _extracted_shot(action: str = "重新提取后的分镜") -> dict[str, Any]:
    return {
        "shot_no": 1,
        "scene_no": 1,
        "source_ref": "重新提取",
        "scene_name": "",
        "character_names": [],
        "prop_names": [],
        "positions": [],
        "start_state": "角色准备行动",
        "action": action,
        "expressions": [],
        "dialogue": [],
        "narration": "",
        "shot_type": "中景",
        "camera_angle": "平视",
        "camera_move": "固定",
        "duration_sec": 6,
        "end_state": "角色完成行动",
        "next_hook": "",
        "frame_description": "角色位于画面中央",
    }


@pytest.mark.parametrize(
    ("target_type", "use_image_id"),
    [
        ("asset", False),
        ("asset_image", True),
    ],
)
def test_delete_asset_with_related_active_job_returns_conflict(
    database: Database,
    project_record: dict[str, Any],
    target_type: str,
    use_image_id: bool,
) -> None:
    service = AssetService(database)
    current = _create_asset(service, project_record["id"], "character", "主角")
    image_id = "image-active"
    _insert_asset_image(database, current["id"], image_id)
    _create_active_job(
        database,
        project_record["id"],
        target_type,
        image_id if use_image_id else current["id"],
    )

    with pytest.raises(ServiceError) as exc_info:
        service.delete(current["id"])

    assert exc_info.value.status_code == 409


@pytest.mark.parametrize(
    ("asset_type", "role", "field"),
    [
        ("character", "character_identity", "character_ids"),
        ("scene", "scene_layout", "scene_asset_id"),
        ("prop", "prop_identity", "prop_ids"),
    ],
)
def test_delete_asset_cleans_shot_asset_and_image_references(
    database: Database,
    project_record: dict[str, Any],
    asset_type: str,
    role: str,
    field: str,
) -> None:
    asset_service = AssetService(database)
    shot_service = ShotService(database)
    current = _create_asset(asset_service, project_record["id"], asset_type, "待删除")
    kept = _create_asset(asset_service, project_record["id"], asset_type, "保留")
    image_id = f"image-{asset_type}"
    _insert_asset_image(database, current["id"], image_id)
    values: dict[str, Any] = {
        "action": "测试引用清理",
        "reference_bindings": [
            {
                "role": role,
                "asset_id": current["id"],
                "asset_image_id": image_id,
            }
        ],
    }
    if field == "scene_asset_id":
        values[field] = current["id"]
    else:
        values[field] = [current["id"], kept["id"]]
    current_shot = _create_shot(shot_service, project_record["id"], **values)

    asset_service.delete(current["id"])

    updated = shot_service.get(current_shot["id"])
    expected = None if field == "scene_asset_id" else [kept["id"]]
    assert updated[field] == expected
    assert updated["reference_bindings"] == []
    with database.engine.connect() as connection:
        raw = connection.execute(
            select(
                shot.c.character_ids_json,
                shot.c.prop_ids_json,
                shot.c.reference_bindings_json,
            ).where(shot.c.id == current_shot["id"])
        ).one()
    assert isinstance(json_loads(raw.character_ids_json, None), list)
    assert isinstance(json_loads(raw.prop_ids_json, None), list)
    assert isinstance(json_loads(raw.reference_bindings_json, None), list)


@pytest.mark.parametrize(
    ("asset_type", "role", "field"),
    [
        ("character", "character_identity", "character_ids"),
        ("scene", "scene_layout", "scene_asset_id"),
        ("prop", "prop_identity", "prop_ids"),
    ],
)
def test_merge_assets_remaps_shot_references_and_source_image_binding(
    database: Database,
    project_record: dict[str, Any],
    asset_type: str,
    role: str,
    field: str,
) -> None:
    asset_service = AssetService(database)
    shot_service = ShotService(database)
    target = _create_asset(asset_service, project_record["id"], asset_type, "目标")
    source = _create_asset(asset_service, project_record["id"], asset_type, "来源")
    kept = _create_asset(asset_service, project_record["id"], asset_type, "保留")
    source_image_id = f"source-image-{asset_type}"
    _insert_asset_image(database, source["id"], source_image_id)
    values: dict[str, Any] = {
        "action": "测试合并引用",
        "reference_bindings": [
            {
                "role": role,
                "asset_id": source["id"],
                "asset_image_id": source_image_id,
            }
        ],
    }
    if field == "scene_asset_id":
        values[field] = source["id"]
    else:
        values[field] = [source["id"], target["id"], source["id"], kept["id"]]
    current_shot = _create_shot(shot_service, project_record["id"], **values)

    asset_service.merge(target["id"], [source["id"]])

    updated = shot_service.get(current_shot["id"])
    expected = target["id"] if field == "scene_asset_id" else [target["id"], kept["id"]]
    assert updated[field] == expected
    assert updated["reference_bindings"] == [
        {
            "role": role,
            "asset_id": target["id"],
            "asset_image_id": source_image_id,
        }
    ]
    with database.engine.connect() as connection:
        moved_to = connection.execute(
            select(asset_image.c.asset_id).where(asset_image.c.id == source_image_id)
        ).scalar_one()
    assert moved_to == target["id"]


@pytest.mark.parametrize(
    ("target_type", "use_image_id"),
    [
        ("asset", False),
        ("asset_image", True),
    ],
)
def test_merge_assets_with_related_active_job_returns_conflict(
    database: Database,
    project_record: dict[str, Any],
    target_type: str,
    use_image_id: bool,
) -> None:
    service = AssetService(database)
    target = _create_asset(service, project_record["id"], "character", "目标")
    source = _create_asset(service, project_record["id"], "character", "来源")
    image_id = "source-image-active"
    _insert_asset_image(database, source["id"], image_id)
    _create_active_job(
        database,
        project_record["id"],
        target_type,
        image_id if use_image_id else source["id"],
    )

    with pytest.raises(ServiceError) as exc_info:
        service.merge(target["id"], [source["id"]])

    assert exc_info.value.status_code == 409


@pytest.mark.parametrize("operation", ["delete", "split", "merge"])
@pytest.mark.parametrize("target_type", ["shot", "keyframe", "video"])
def test_destructive_shot_operation_with_related_active_job_returns_conflict(
    database: Database,
    project_record: dict[str, Any],
    operation: str,
    target_type: str,
) -> None:
    service = ShotService(database)
    current = _create_shot(service, project_record["id"], action="目标分镜")
    source = _create_shot(service, project_record["id"], action="合并来源")
    keyframe_id = f"keyframe-{operation}-{target_type}"
    video_id = f"video-{operation}-{target_type}"
    _insert_keyframe(database, current["id"], keyframe_id)
    _insert_video(database, current["id"], video_id)
    target_id = {
        "shot": current["id"],
        "keyframe": keyframe_id,
        "video": video_id,
    }[target_type]
    _create_active_job(database, project_record["id"], target_type, target_id)

    with pytest.raises(ServiceError) as exc_info:
        if operation == "delete":
            service.delete(current["id"])
        elif operation == "split":
            service.split(current["id"], [])
        else:
            service.merge(current["id"], [source["id"]])

    assert exc_info.value.status_code == 409


def test_merge_shots_with_active_source_media_job_returns_conflict(
    database: Database,
    project_record: dict[str, Any],
) -> None:
    service = ShotService(database)
    target = _create_shot(service, project_record["id"], action="目标分镜")
    source = _create_shot(service, project_record["id"], action="来源分镜")
    source_video_id = "source-video-active"
    _insert_video(database, source["id"], source_video_id)
    _create_active_job(database, project_record["id"], "video", source_video_id)

    with pytest.raises(ServiceError) as exc_info:
        service.merge(target["id"], [source["id"]])

    assert exc_info.value.status_code == 409


def test_merge_shots_when_late_step_fails_rolls_back_all_changes(
    database: Database,
    project_record: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ShotService(database)
    target = _create_shot(service, project_record["id"], action="目标动作")
    source = _create_shot(service, project_record["id"], action="来源动作")

    def fail_renumber(
        _connection: Any,
        _project_id: str,
        _ids: list[str],
    ) -> None:
        raise ServiceError(409, "模拟事务末尾冲突")

    monkeypatch.setattr(service, "_renumber", fail_renumber)

    with pytest.raises(ServiceError) as exc_info:
        service.merge(target["id"], [source["id"]])

    assert exc_info.value.status_code == 409
    assert [item["action"] for item in service.list(project_record["id"])] == [
        target["action"],
        source["action"],
    ]


def test_delete_shot_cleans_previous_keyframe_binding(
    database: Database,
    project_record: dict[str, Any],
) -> None:
    service = ShotService(database)
    current = _create_shot(service, project_record["id"], action="待删除分镜")
    keyframe_id = "keyframe-to-delete"
    _insert_keyframe(database, current["id"], keyframe_id)
    referencing = _create_shot(
        service,
        project_record["id"],
        action="引用上一镜",
        reference_bindings=[
            {
                "role": "previous_keyframe",
                "keyframe_id": keyframe_id,
            }
        ],
    )

    service.delete(current["id"])

    updated = service.get(referencing["id"])
    assert updated["reference_bindings"] == []
    with database.engine.connect() as connection:
        raw = connection.execute(
            select(shot.c.reference_bindings_json).where(shot.c.id == referencing["id"])
        ).scalar_one()
    assert json_loads(raw, None) == []


def test_delete_keyframe_referenced_by_non_terminal_video_returns_conflict(
    database: Database,
    project_record: dict[str, Any],
) -> None:
    shot_service = ShotService(database)
    current = _create_shot(shot_service, project_record["id"], action="视频生成中")
    keyframe_id = "keyframe-for-waiting-video"
    video_id = "waiting-video"
    _insert_keyframe(database, current["id"], keyframe_id)
    _insert_video(
        database,
        current["id"],
        video_id,
        keyframe_id=keyframe_id,
        status="waiting_provider",
    )

    with pytest.raises(ServiceError) as exc_info:
        MediaService(database).delete_version("keyframe", keyframe_id)

    assert exc_info.value.status_code == 409


def test_delete_keyframe_referenced_by_active_video_job_returns_conflict(
    database: Database,
    project_record: dict[str, Any],
) -> None:
    shot_service = ShotService(database)
    current = _create_shot(shot_service, project_record["id"], action="视频准备重试")
    keyframe_id = "keyframe-for-active-video-job"
    video_id = "video-with-active-job"
    _insert_keyframe(database, current["id"], keyframe_id)
    _insert_video(
        database,
        current["id"],
        video_id,
        keyframe_id=keyframe_id,
        status="failed",
    )
    _create_active_job(database, project_record["id"], "video", video_id)

    with pytest.raises(ServiceError) as exc_info:
        MediaService(database).delete_version("keyframe", keyframe_id)

    assert exc_info.value.status_code == 409


def test_delete_keyframe_used_as_previous_keyframe_binding_returns_conflict(
    database: Database,
    project_record: dict[str, Any],
) -> None:
    shot_service = ShotService(database)
    source = _create_shot(shot_service, project_record["id"], action="上一镜")
    keyframe_id = "keyframe-used-by-next-shot"
    _insert_keyframe(database, source["id"], keyframe_id)
    _create_shot(
        shot_service,
        project_record["id"],
        action="下一镜",
        reference_bindings=[
            {
                "role": "previous_keyframe",
                "keyframe_id": keyframe_id,
            }
        ],
    )

    with pytest.raises(ServiceError) as exc_info:
        MediaService(database).delete_version("keyframe", keyframe_id)

    assert exc_info.value.status_code == 409


def test_reextract_shots_with_related_active_job_returns_conflict(
    database: Database,
    project_record: dict[str, Any],
) -> None:
    service = ShotService(database)
    first = _create_shot(service, project_record["id"], action="原第一镜")
    obsolete = _create_shot(service, project_record["id"], action="原第二镜")
    video_id = "reextract-active-video"
    _insert_video(database, obsolete["id"], video_id)
    _create_active_job(database, project_record["id"], "video", video_id)

    with pytest.raises(ServiceError) as exc_info:
        service._save_extracted(
            project_record,
            "novel",
            {"scope": "all"},
            [_extracted_shot()],
        )

    assert exc_info.value.status_code == 409
    assert [item["action"] for item in service.list(project_record["id"])] == [
        first["action"],
        obsolete["action"],
    ]


def test_reextract_scene_cleans_previous_keyframe_binding(
    database: Database,
    project_record: dict[str, Any],
) -> None:
    service = ShotService(database)
    obsolete = _create_shot(
        service,
        project_record["id"],
        source_ref="scene:1|待淘汰",
        action="待淘汰分镜",
    )
    keyframe_id = "reextract-obsolete-keyframe"
    _insert_keyframe(database, obsolete["id"], keyframe_id)
    referencing = _create_shot(
        service,
        project_record["id"],
        source_ref="scene:2|保留",
        action="保留分镜",
        reference_bindings=[
            {
                "role": "previous_keyframe",
                "keyframe_id": keyframe_id,
            }
        ],
    )

    service._save_extracted(
        project_record,
        "novel",
        {"scope": "scene", "scene_no": 1},
        [],
    )

    assert service.get(referencing["id"])["reference_bindings"] == []
