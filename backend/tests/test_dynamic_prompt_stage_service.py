from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import insert, select, update

from app.db import Database, migrate_schema, utc_now
from app.dynamic_pipeline.media_queue_service import DynamicMediaQueueService
from app.dynamic_pipeline.prompt_stage_service import (
    PROMPT_STAGE_KEYS,
    DynamicPromptStageService,
    PromptStageExecutionError,
)
from app.dynamic_pipeline.reference_plan import build_reference_plan
from app.dynamic_pipeline.schemas import StageConfirmRequest, StageDraftUpdate
from app.jobs.repository import JobRepository
from app.models.tables import (
    api_provider,
    asset,
    asset_image,
    chapter,
    job,
    project,
    shot,
    workflow_run,
    workflow_stage,
)
from app.services.dynamic_workflow_service import DynamicWorkflowService


def _run(value: Any) -> Any:
    return asyncio.run(value)


def _meta(signature: str = "prompt-signature") -> dict[str, Any]:
    return {
        "schema_version": "dynamic-stage-test-v1",
        "normalizer_version": "dynamic-test-normalizer-v1",
        "input_signature": signature,
        "content_signature": "content-signature",
        "source_attempt": 1,
    }


def _character_bios(signature: str = "prompt-signature") -> dict[str, Any]:
    return {
        "artifact_meta": _meta(signature),
        "characters": [
            {
                "basic_info": {
                    "character_key": "hero",
                    "canonical_name": "林舟",
                    "aliases": [],
                    "importance": "core",
                    "identity": "线索追寻者",
                    "age": "成年",
                    "gender": "男",
                },
                "background": "在旧车站寻找线索。",
                "personality_behavior_logic": "先观察，再行动。",
                "dynamic_comic_setting": "动作时保持稳定轮廓。",
                "plot_relation": "当前镜头主体。",
                "visual_anchor": {
                    "visual_facts": ["黑色短发"],
                    "face": "窄脸",
                    "hair": "黑色短发",
                    "body": "瘦高",
                    "costume": "深蓝外套",
                    "accessories": [],
                    "immutable_features": ["黑色短发"],
                    "forbidden_changes": ["不得改变发型"],
                    "makeup_keyword_string": "窄脸|黑色短发|深蓝外套",
                    "variants": [],
                    "current_variant": None,
                    "approved_reference_ids": [],
                },
            }
        ],
        "ensemble_anchors": [],
        "unresolved_character_refs": [],
    }


def _reference_plan(label: str) -> dict[str, Any]:
    url = f"https://example.test/{label}.png"
    return {
        "slots": [
            {
                "index": 1,
                "role": "character",
                "asset_id": "asset-hero",
                "required": False,
                "purpose": "clean_identity",
                "provider_url": url,
            }
        ],
        "actual_local_files": [],
        "actual_urls": [url],
        "text_mapping": {
            "@Image1": {
                "index": 1,
                "label": label,
                "source": "url",
                "url": url,
                "value": url,
            }
        },
        "snapshot_hash": f"reference-{label}",
    }


def _storyboard(
    *,
    signature: str = "prompt-signature",
    continuous: bool = False,
    missing_asset: bool = False,
) -> dict[str, Any]:
    first = {
        "external_key": "shot-1",
        "shot_no": 1,
        "ordinal": 1,
        "scene_id": "scene-1",
        "scene_no": 1,
        "scene_name": "旧车站",
        "time_weather": "夜",
        "scene_asset_id": "asset-missing-scene" if missing_asset else "asset-station",
        "character_names": ["林舟"],
        "character_asset_ids": ["asset-hero"],
        "prop_names": [],
        "prop_asset_ids": [],
        "positions": [{"character": "林舟", "position": "站台中央"}],
        "expressions": [{"character": "林舟", "expression": "警觉"}],
        "start_state": "林舟站在站台中央",
        "action": "抬头观察远处",
        "end_state": "视线停在轨道尽头",
        "next_hook": "",
        "transition_from_prev": "scene_change",
        "duration_sec": 4,
        "provider_duration_sec": 4,
        "dialogue": [],
        "narration": "",
        "sound_cues": [],
        "dynamic_level": 0.2,
        "dynamic_event_id": None,
        "frame_description": "旧车站站台上的林舟",
        "source_refs": [],
        "reference_bindings": [],
        "prompt_intent": {},
    }
    shots = [first]
    if continuous:
        shots.append(
            {
                **first,
                "external_key": "shot-2",
                "shot_no": 2,
                "ordinal": 2,
                "start_state": "林舟保持上一镜视线",
                "action": "抬手指向轨道尽头",
                "end_state": "手臂停在指向位置",
                "transition_from_prev": "continuous",
                "frame_description": "林舟连续指向轨道尽头",
            }
        )
    return {
        "artifact_meta": _meta(signature),
        "target_duration_sec": 4 * len(shots),
        "shots": shots,
        "scenes": [],
        "assumptions": [],
        "open_questions": [],
    }


def _capabilities() -> dict[str, Any]:
    return {
        "provider_id": "snapshot-image-provider",
        "model": "snapshot-model",
        "capability_version": "test-1",
        "supports_multiple_images": True,
        "max_reference_images": 3,
        "supported_durations": [4, 6],
        "min_duration_sec": 4,
        "max_duration_sec": 8,
        "duration_step_sec": 2,
        "supports_end_frame": True,
        "supports_audio": True,
        "supports_dialogue": True,
    }


@pytest.fixture
def prompt_context(tmp_path: Path):
    database = Database(f"sqlite:///{(tmp_path / 'dynamic-prompt-stage.db').as_posix()}")
    migrate_schema(database)
    now = utc_now()
    run_id = "run-prompt-service"
    chapter_id = "chapter-prompt-service"
    project_id = "project-prompt-service"
    with database.engine.begin() as connection:
        connection.execute(
            insert(project).values(
                id=project_id,
                name="Prompt 阶段测试项目",
                style_prompt="电影感二维动画风格",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(chapter).values(
                id=chapter_id,
                project_id=project_id,
                chapter_no=1,
                title="第一章",
                source_text="CHAPTER正文不得进入Prompt输入",
                source_signature="chapter-source",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(workflow_run).values(
                id=run_id,
                project_id=project_id,
                chapter_id=chapter_id,
                kind="dynamic_comic_0_6",
                schema_version="1",
                status="queued",
                current_stage="5a-image-prompts",
                source_kind="full_text",
                source_snapshot_json=json.dumps(
                    {"text": "RUN快照文本", "title": "测试故事"}, ensure_ascii=False
                ),
                config_snapshot_json=json.dumps(
                    {
                        "style_prompt": "测试风格补充",
                        "image_provider_id": "config-image-provider",
                        "video_provider_id": "config-video-provider",
                        "image_provider_model": "config-image-model",
                        "video_provider_model": "config-video-model",
                    },
                    ensure_ascii=False,
                ),
                asset_mapping_json=json.dumps(
                    [
                        {
                            "workflow_asset_id": f"dynamic-asset:{run_id}:hero",
                            "asset_id": "asset-hero",
                            "usage": "character",
                            "locked_revision": None,
                            "reuse_mode": "new",
                        }
                    ],
                    ensure_ascii=False,
                ),
                input_signature="prompt-signature",
                revision=1,
                error="",
                created_at=now,
                updated_at=now,
            )
        )
        stage_values = [
            {
                "id": "stage-1-character",
                "workflow_run_id": run_id,
                "stage_key": "1-character-bios",
                "ordinal": 1,
                "status": "confirmed",
                "input_signature": "prompt-signature",
                "normalized_output_json": json.dumps(_character_bios(), ensure_ascii=False),
                "draft_output_json": json.dumps(_character_bios(), ensure_ascii=False),
                "created_at": now,
                "updated_at": now,
            },
            {
                "id": "stage-4-storyboard",
                "workflow_run_id": run_id,
                "stage_key": "4-storyboard",
                "ordinal": 4,
                "status": "confirmed",
                "input_signature": "prompt-signature",
                "normalized_output_json": json.dumps(_storyboard(), ensure_ascii=False),
                "draft_output_json": json.dumps(_storyboard(), ensure_ascii=False),
                "created_at": now,
                "updated_at": now,
            },
        ]
        connection.execute(insert(workflow_stage), stage_values)
        connection.execute(
            insert(asset),
            [
                {
                    "id": "asset-hero",
                    "project_id": project_id,
                    "asset_type": "character",
                    "name": "林舟",
                    "profile_json": json.dumps(
                        {
                            "canonical_name": "林舟",
                            "face": "窄脸",
                            "hair": "黑色短发",
                            "body": "瘦高",
                            "costume": "深蓝外套",
                            "visual_facts": ["黑色短发"],
                        },
                        ensure_ascii=False,
                    ),
                    "visual_anchor_json": json.dumps(
                        {"immutable_features": ["轮廓稳定"]}, ensure_ascii=False
                    ),
                    "status": "confirmed",
                    "revision": 1,
                    "created_at": now,
                    "updated_at": now,
                },
                {
                    "id": "asset-station",
                    "project_id": project_id,
                    "asset_type": "scene",
                    "name": "旧车站",
                    "profile_json": json.dumps(
                        {
                            "canonical_name": "旧车站",
                            "layout": "站台纵深",
                            "lighting_direction": "月光从左侧进入",
                            "visual_facts": ["铁轨和旧站牌"],
                        },
                        ensure_ascii=False,
                    ),
                    "visual_anchor_json": "{}",
                    "status": "confirmed",
                    "revision": 1,
                    "created_at": now,
                    "updated_at": now,
                },
            ],
        )
        connection.execute(
            insert(shot).values(
                id="shot-1-row",
                project_id=project_id,
                chapter_id=chapter_id,
                shot_no=1,
                shot_order=1,
                source_type="screenplay",
                external_key="shot-1",
                scene_asset_id="asset-station",
                transition_from_prev="scene_change",
                reference_bindings_json=json.dumps(_reference_plan("shot-1"), ensure_ascii=False),
                created_at=now,
                updated_at=now,
            )
        )
    try:
        yield database
    finally:
        database.dispose()


def _add_prompt_stage(
    database: Database,
    stage_key: str,
    *,
    storyboard: dict[str, Any] | None = None,
    stage_status: str = "queued",
    run_status: str = "queued",
    signature: str = "prompt-signature",
    run_signature: str | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    now = utc_now()
    stage_id = f"stage-{stage_key}"
    other_stage_key = (
        PROMPT_STAGE_KEYS[1] if stage_key == PROMPT_STAGE_KEYS[0] else PROMPT_STAGE_KEYS[0]
    )
    with database.engine.begin() as connection:
        connection.execute(
            update(workflow_run)
            .where(workflow_run.c.id == "run-prompt-service")
            .values(
                status=run_status,
                current_stage=stage_key,
                input_signature=run_signature or signature,
                updated_at=now,
            )
        )
        connection.execute(
            update(workflow_stage)
            .where(workflow_stage.c.id == "stage-4-storyboard")
            .values(
                normalized_output_json=json.dumps(
                    storyboard or _storyboard(signature=signature), ensure_ascii=False
                ),
                draft_output_json=json.dumps(
                    storyboard or _storyboard(signature=signature), ensure_ascii=False
                ),
                status="confirmed",
                input_signature=signature,
                updated_at=now,
            )
        )
        connection.execute(
            insert(workflow_stage),
            [
                {
                    "id": stage_id,
                    "workflow_run_id": "run-prompt-service",
                    "stage_key": stage_key,
                    "ordinal": 5,
                    "status": stage_status,
                    "input_signature": signature,
                    "created_at": now,
                    "updated_at": now,
                },
                {
                    "id": f"stage-{other_stage_key}",
                    "workflow_run_id": "run-prompt-service",
                    "stage_key": other_stage_key,
                    "ordinal": 5,
                    "status": "queued",
                    "input_signature": f"{other_stage_key}-signature",
                    "created_at": now,
                    "updated_at": now,
                },
            ],
        )
    job_id = f"job-{stage_key}"
    input_data: dict[str, Any] = {
        "stage_key": stage_key,
        "provider_capability_snapshot": _capabilities(),
        "provider_id": "job-provider",
        "provider_model": "job-model",
    }
    if overrides is not None:
        input_data["overrides_by_external_key"] = overrides
    with database.engine.begin() as connection:
        connection.execute(
            insert(job).values(
                id=job_id,
                project_id="project-prompt-service",
                chapter_id="chapter-prompt-service",
                workflow_run_id="run-prompt-service",
                workflow_stage_id=stage_id,
                job_type="dynamic_stage_generate",
                target_type="workflow_stage",
                target_id=stage_id,
                provider_kind="image" if stage_key.startswith("5a") else "video",
                provider_id=None,
                status="queued",
                progress_current=0,
                progress_total=1,
                input_json=json.dumps(input_data, ensure_ascii=False),
                input_signature=signature,
                depends_on_json="[]",
                result_json="{}",
                created_at=now,
                updated_at=now,
            )
        )
        row = connection.execute(select(job).where(job.c.id == job_id)).first()
    assert row is not None
    record = dict(row._mapping)
    record["input_data"] = input_data
    return record


def _latest_stage(database: Database, stage_id: str) -> Any:
    with database.engine.connect() as connection:
        return connection.execute(
            select(workflow_stage).where(workflow_stage.c.id == stage_id)
        ).one()


def test_5a_happy_path_persists_static_bundle_and_finishes_job(prompt_context) -> None:
    database = prompt_context
    job_record = _add_prompt_stage(
        database,
        PROMPT_STAGE_KEYS[0],
        signature="branch-signature",
        run_signature="root-signature",
    )

    class NoProviderCall:
        def resolve(self, **_: Any) -> Any:
            raise AssertionError("纯编译 Prompt 阶段不应解析或调用 Provider")

    service = DynamicPromptStageService(database, provider_service=NoProviderCall())

    result = _run(service.execute_job(job_record))

    assert result["stage_key"] == "5a-image-prompts"
    assert result["status"] == "ready"
    assert result["target_count"] == 1
    stage_row = _latest_stage(database, "stage-5a-image-prompts")
    with database.engine.connect() as connection:
        job_row = connection.execute(select(job).where(job.c.id == job_record["id"])).one()
        run_row = connection.execute(
            select(workflow_run).where(workflow_run.c.id == "run-prompt-service")
        ).one()
    raw = json.loads(stage_row.raw_output_json)
    normalized = json.loads(stage_row.normalized_output_json)
    assert raw["capture_mode"] == "compiled_prompt_stage"
    assert normalized["stage_key"] == "5a-image-prompts"
    assert normalized["artifact_meta"]["input_signature"] == "branch-signature"
    assert normalized["bundles"]["shot-1"]["video"] is None
    assert "黑色短发" in normalized["bundles"]["shot-1"]["image"]["prompt"]
    assert "CHAPTER正文不得进入Prompt输入" not in json.dumps(normalized, ensure_ascii=False)
    assert json.loads(stage_row.draft_output_json) == normalized
    assert json.loads(stage_row.gate_result_json)["status"] == "ready"
    assert json.loads(stage_row.output_refs_json)[0]["capture_mode"] == "compiled_prompt_stage"
    assert stage_row.status == "ready"
    assert run_row.status == "running"
    assert run_row.input_signature == "root-signature"
    assert job_row.status == "completed"
    assert json.loads(job_row.result_json)["input_signature"] == "branch-signature"
    assert json.loads(job_row.result_json) == result
    with database.engine.connect() as connection:
        assert connection.scalar(select(job.c.id).where(job.c.id != job_record["id"])) is None


def test_prompt_barrier_finishes_run_only_after_other_branch_is_terminal(prompt_context) -> None:
    database = prompt_context
    job_record = _add_prompt_stage(database, PROMPT_STAGE_KEYS[0])
    with database.engine.begin() as connection:
        connection.execute(
            update(workflow_stage)
            .where(workflow_stage.c.id == "stage-5b-video-prompts")
            .values(status="confirmed")
        )

    result = _run(DynamicPromptStageService(database).execute_job(job_record))

    assert result["status"] == "ready"
    with database.engine.connect() as connection:
        statuses = connection.execute(
            select(workflow_stage.c.status, workflow_run.c.status)
            .select_from(
                workflow_stage.join(
                    workflow_run,
                    workflow_stage.c.workflow_run_id == workflow_run.c.id,
                )
            )
            .where(workflow_stage.c.id == "stage-5a-image-prompts")
        ).one()
    assert tuple(statuses) == ("ready", "ready")


def test_5b_continuous_without_previous_end_frame_is_blocked_but_job_completes(
    prompt_context,
) -> None:
    database = prompt_context
    job_record = _add_prompt_stage(
        database,
        PROMPT_STAGE_KEYS[1],
        storyboard=_storyboard(continuous=True),
    )
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(shot).values(
                id="shot-2-row",
                project_id="project-prompt-service",
                chapter_id="chapter-prompt-service",
                shot_no=2,
                shot_order=2,
                source_type="screenplay",
                external_key="shot-2",
                scene_asset_id="asset-station",
                transition_from_prev="continuous",
                reference_bindings_json=json.dumps(_reference_plan("shot-2"), ensure_ascii=False),
                created_at=now,
                updated_at=now,
            )
        )
    result = _run(DynamicPromptStageService(database).execute_job(job_record))

    assert result["status"] == "blocked"
    assert result["target_count"] == 2
    stage_row = _latest_stage(database, "stage-5b-video-prompts")
    with database.engine.connect() as connection:
        job_status = connection.scalar(select(job.c.status).where(job.c.id == job_record["id"]))
        run_status = connection.scalar(
            select(workflow_run.c.status).where(workflow_run.c.id == "run-prompt-service")
        )
    assert stage_row.status == "blocked"
    assert run_status == "running"
    assert job_status == "completed"
    diagnostics = json.loads(stage_row.gate_result_json)["diagnostics"]
    assert any(item["code"] == "continuous_previous_end_frame_missing" for item in diagnostics)


def test_missing_anchor_is_compiler_blocking_and_does_not_use_chapter_text(prompt_context) -> None:
    database = prompt_context
    job_record = _add_prompt_stage(
        database,
        PROMPT_STAGE_KEYS[0],
        storyboard=_storyboard(missing_asset=True),
    )

    result = _run(DynamicPromptStageService(database).execute_job(job_record))

    assert result["status"] == "blocked"
    stage_row = _latest_stage(database, "stage-5a-image-prompts")
    stage_payload = json.loads(stage_row.normalized_output_json)
    assert "CHAPTER正文不得进入Prompt输入" not in json.dumps(stage_payload, ensure_ascii=False)
    codes = {item["code"] for item in json.loads(stage_row.gate_result_json)["diagnostics"]}
    assert "asset_unresolved" in codes


def test_reference_slot_override_index_change_is_blocking(prompt_context) -> None:
    database = prompt_context
    job_record = _add_prompt_stage(
        database,
        PROMPT_STAGE_KEYS[0],
        overrides={
            "shot-1": {
                "reference_slots": [
                    {
                        "index": 2,
                        "role": "character",
                        "purpose": "clean_identity",
                    }
                ]
            }
        },
    )

    result = _run(DynamicPromptStageService(database).execute_job(job_record))

    assert result["status"] == "blocked"
    stage_row = _latest_stage(database, "stage-5a-image-prompts")
    codes = {item["code"] for item in json.loads(stage_row.gate_result_json)["diagnostics"]}
    assert "reference_slot_override_index_mismatch" in codes


def test_asset_image_reference_binding_resolves_to_one_canonical_plan(prompt_context) -> None:
    database = prompt_context
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(asset_image).values(
                id="asset-image-hero-1",
                asset_id="asset-hero",
                version_no=1,
                file_path=None,
                provider_url="https://cdn.example.test/hero-approved.png",
                reference_purpose="clean_identity",
                approved=1,
                status="completed",
                is_current=1,
                input_signature="image-input",
                source="uploaded",
                created_at=now,
            )
        )
        connection.execute(
            update(shot)
            .where(shot.c.id == "shot-1-row")
            .values(
                reference_bindings_json=json.dumps(
                    [
                        {
                            "index": 1,
                            "role": "character_identity",
                            "purpose": "clean_identity",
                            "asset_image_id": "asset-image-hero-1",
                        }
                    ]
                )
            )
        )
    job_record = _add_prompt_stage(database, PROMPT_STAGE_KEYS[0])

    result = _run(DynamicPromptStageService(database).execute_job(job_record))

    assert result["status"] == "ready"
    stage_row = _latest_stage(database, "stage-5a-image-prompts")
    payload = json.loads(stage_row.normalized_output_json)
    plan = payload["bundles"]["shot-1"]["provenance"]["reference_plan"]
    assert plan["slots"][0]["image_version_id"] == "asset-image-hero-1"
    assert plan["actual_urls"] == ["https://cdn.example.test/hero-approved.png"]
    assert list(plan["text_mapping"]) == ["@Image1"]


def test_prompt_stage_signature_rebuilds_current_reference_plan_and_ignores_raw_snapshot_hash(
    prompt_context,
) -> None:
    database = prompt_context
    workflow_service = DynamicWorkflowService(database)
    with database.engine.connect() as connection:
        run_row = connection.execute(
            select(workflow_run).where(workflow_run.c.id == "run-prompt-service")
        ).one()
        stage4 = workflow_service._stage_from_connection(
            connection,
            "run-prompt-service",
            "4-storyboard",
        )
        assert stage4 is not None
        first, _ = workflow_service._prompt_stage_input_signature(
            connection,
            PROMPT_STAGE_KEYS[0],
            dict(run_row._mapping),
            stage4,
        )

    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(asset_image).values(
                id="asset-image-signature",
                asset_id="asset-hero",
                version_no=1,
                file_path=None,
                provider_url="https://cdn.example.test/signature-1.png",
                reference_purpose="clean_identity",
                approved=1,
                status="completed",
                is_current=1,
                input_signature="image-signature",
                source="uploaded",
                created_at=now,
            )
        )
        connection.execute(
            update(shot)
            .where(shot.c.id == "shot-1-row")
            .values(
                reference_bindings_json=json.dumps(
                    {
                        "snapshot_hash": "caller-supplied-old-hash",
                        "slots": [
                            {
                                "index": 1,
                                "role": "character_identity",
                                "purpose": "clean_identity",
                                "asset_image_id": "asset-image-signature",
                            }
                        ],
                    }
                )
            )
        )

    with database.engine.connect() as connection:
        run_row = connection.execute(
            select(workflow_run).where(workflow_run.c.id == "run-prompt-service")
        ).one()
        stage4 = workflow_service._stage_from_connection(
            connection,
            "run-prompt-service",
            "4-storyboard",
        )
        assert stage4 is not None
        approved, _ = workflow_service._prompt_stage_input_signature(
            connection,
            PROMPT_STAGE_KEYS[0],
            dict(run_row._mapping),
            stage4,
        )

    with database.engine.begin() as connection:
        connection.execute(
            update(asset_image)
            .where(asset_image.c.id == "asset-image-signature")
            .values(approved=0)
        )
    with database.engine.connect() as connection:
        run_row = connection.execute(
            select(workflow_run).where(workflow_run.c.id == "run-prompt-service")
        ).one()
        stage4 = workflow_service._stage_from_connection(
            connection,
            "run-prompt-service",
            "4-storyboard",
        )
        assert stage4 is not None
        unapproved, _ = workflow_service._prompt_stage_input_signature(
            connection,
            PROMPT_STAGE_KEYS[0],
            dict(run_row._mapping),
            stage4,
        )

    with database.engine.begin() as connection:
        connection.execute(
            update(asset_image)
            .where(asset_image.c.id == "asset-image-signature")
            .values(approved=1, provider_url="https://cdn.example.test/signature-2.png")
        )
    with database.engine.connect() as connection:
        run_row = connection.execute(
            select(workflow_run).where(workflow_run.c.id == "run-prompt-service")
        ).one()
        stage4 = workflow_service._stage_from_connection(
            connection,
            "run-prompt-service",
            "4-storyboard",
        )
        assert stage4 is not None
        changed_url, _ = workflow_service._prompt_stage_input_signature(
            connection,
            PROMPT_STAGE_KEYS[0],
            dict(run_row._mapping),
            stage4,
        )

    assert first != approved
    assert approved != unapproved
    assert unapproved != changed_url


def test_unapproved_asset_image_reference_blocks_before_compiler(prompt_context) -> None:
    database = prompt_context
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(asset_image).values(
                id="asset-image-hero-unapproved",
                asset_id="asset-hero",
                version_no=1,
                file_path=None,
                provider_url="https://cdn.example.test/hero-unapproved.png",
                reference_purpose="clean_identity",
                approved=0,
                status="completed",
                is_current=1,
                input_signature="image-input",
                source="uploaded",
                created_at=now,
            )
        )
        connection.execute(
            update(shot)
            .where(shot.c.id == "shot-1-row")
            .values(
                reference_bindings_json=json.dumps(
                    [
                        {
                            "index": 1,
                            "role": "character_identity",
                            "purpose": "clean_identity",
                            "asset_image_id": "asset-image-hero-unapproved",
                        }
                    ]
                )
            )
        )
    job_record = _add_prompt_stage(database, PROMPT_STAGE_KEYS[0])

    with pytest.raises(PromptStageExecutionError, match="参考图计划"):
        _run(DynamicPromptStageService(database).execute_job(job_record))

    with database.engine.connect() as connection:
        assert (
            connection.scalar(select(job.c.status).where(job.c.id == job_record["id"]))
            == "failed"
        )


def test_stage4_must_be_confirmed_without_compiler_call(prompt_context, monkeypatch) -> None:
    database = prompt_context
    job_record = _add_prompt_stage(database, PROMPT_STAGE_KEYS[0])
    with database.engine.begin() as connection:
        connection.execute(
            update(workflow_stage)
            .where(workflow_stage.c.id == "stage-4-storyboard")
            .values(status="queued")
        )
    calls: list[object] = []

    def fail_compile(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        raise AssertionError("stage4 未确认时不应调用编译器")

    monkeypatch.setattr(
        "app.dynamic_pipeline.prompt_stage_service.compile_image_prompt_stage",
        fail_compile,
    )
    with pytest.raises(PromptStageExecutionError, match="4-storyboard"):
        _run(DynamicPromptStageService(database).execute_job(job_record))

    assert calls == []
    with database.engine.connect() as connection:
        job_status = connection.scalar(select(job.c.status).where(job.c.id == job_record["id"]))
        stage_status = connection.scalar(
            select(workflow_stage.c.status).where(workflow_stage.c.id == "stage-5a-image-prompts")
        )
    assert job_status == "failed"
    assert stage_status == "queued"


def test_missing_capability_snapshot_uses_conservative_default_and_warning(prompt_context) -> None:
    database = prompt_context
    job_record = _add_prompt_stage(database, PROMPT_STAGE_KEYS[0])
    with database.engine.begin() as connection:
        connection.execute(
            update(job)
            .where(job.c.id == job_record["id"])
            .values(input_json=json.dumps({"stage_key": "5a-image-prompts"}))
        )
    result = _run(DynamicPromptStageService(database).execute_job(job_record))

    assert result["status"] == "warning"
    stage_row = _latest_stage(database, "stage-5a-image-prompts")
    diagnostics = json.loads(stage_row.gate_result_json)["diagnostics"]
    assert any(item["code"] == "provider_capability_snapshot_missing" for item in diagnostics)
    assert json.loads(stage_row.normalized_output_json)["provenance"]["provider_id"]


def test_input_signature_mismatch_interrupts_without_compilation(
    prompt_context, monkeypatch
) -> None:
    database = prompt_context
    job_record = _add_prompt_stage(database, PROMPT_STAGE_KEYS[0])
    with database.engine.begin() as connection:
        connection.execute(
            update(workflow_stage)
            .where(workflow_stage.c.id == "stage-5a-image-prompts")
            .values(input_signature="stage-signature")
        )
    calls: list[object] = []

    def fail_compile(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        raise AssertionError("签名冲突时不应调用编译器")

    monkeypatch.setattr(
        "app.dynamic_pipeline.prompt_stage_service.compile_image_prompt_stage",
        fail_compile,
    )
    with pytest.raises(PromptStageExecutionError, match="signature"):
        _run(DynamicPromptStageService(database).execute_job(job_record))

    assert calls == []
    with database.engine.connect() as connection:
        statuses = connection.execute(
            select(job.c.status, workflow_stage.c.status, workflow_run.c.status)
            .select_from(
                job.join(workflow_stage, job.c.workflow_stage_id == workflow_stage.c.id).join(
                    workflow_run, job.c.workflow_run_id == workflow_run.c.id
                )
            )
            .where(job.c.id == job_record["id"])
        ).one()
    assert tuple(statuses) == ("interrupted", "stale", "queued")


def test_prompt_snapshot_mismatch_marks_delivery_stale_without_compiler_call(
    prompt_context,
    monkeypatch,
) -> None:
    database = prompt_context
    job_record = _add_prompt_stage(database, PROMPT_STAGE_KEYS[0])
    workflow_service = DynamicWorkflowService(database)
    with database.engine.begin() as connection:
        now = utc_now()
        connection.execute(
            insert(workflow_stage).values(
                id="stage-6-delivery",
                workflow_run_id="run-prompt-service",
                stage_key="6-delivery",
                ordinal=6,
                status="queued",
                created_at=now,
                updated_at=now,
            )
        )
        run_row = connection.execute(
            select(workflow_run).where(workflow_run.c.id == "run-prompt-service")
        ).one()
        stage_row = connection.execute(
            select(workflow_stage).where(workflow_stage.c.id == "stage-4-storyboard")
        ).one()
        signature, _ = workflow_service._prompt_stage_input_signature(
            connection,
            PROMPT_STAGE_KEYS[0],
            dict(run_row._mapping),
            dict(stage_row._mapping),
        )
        connection.execute(
            update(workflow_stage)
            .where(workflow_stage.c.id == "stage-5a-image-prompts")
            .values(
                input_signature=signature,
                output_refs_json=json.dumps(
                    [
                        {
                            "capture_mode": "prompt_input_snapshot",
                            "snapshot_version": "dynamic-prompt-stage-input-v2",
                        }
                    ]
                ),
            )
        )
        connection.execute(
            update(job)
            .where(job.c.id == job_record["id"])
            .values(
                input_signature=signature,
                input_json=json.dumps({"stage_key": PROMPT_STAGE_KEYS[0]}),
            )
        )
        connection.execute(update(asset).where(asset.c.id == "asset-hero").values(revision=2))
    calls: list[Any] = []

    def fail_compile(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        raise AssertionError("输入快照变化时不应调用 compiler")

    monkeypatch.setattr(
        "app.dynamic_pipeline.prompt_stage_service.compile_image_prompt_stage",
        fail_compile,
    )
    with pytest.raises(PromptStageExecutionError, match="signature"):
        _run(DynamicPromptStageService(database).execute_job(job_record))

    with database.engine.connect() as connection:
        statuses = connection.execute(
            select(job.c.status, workflow_stage.c.status)
            .select_from(job.join(workflow_stage, job.c.workflow_stage_id == workflow_stage.c.id))
            .where(job.c.id == job_record["id"])
        ).one()
        delivery_status = connection.scalar(
            select(workflow_stage.c.status).where(
                workflow_stage.c.workflow_run_id == "run-prompt-service",
                workflow_stage.c.stage_key == "6-delivery",
            )
        )
    assert tuple(statuses) == ("interrupted", "stale")
    assert delivery_status == "stale"
    assert calls == []


@pytest.mark.parametrize("protected_field", ["job", "stage", "run"])
def test_protected_state_is_not_overwritten_or_compiled(
    prompt_context, protected_field: str
) -> None:
    database = prompt_context
    job_record = _add_prompt_stage(database, PROMPT_STAGE_KEYS[0])
    with database.engine.begin() as connection:
        if protected_field == "job":
            connection.execute(
                update(job).where(job.c.id == job_record["id"]).values(status="cancelled")
            )
        elif protected_field == "stage":
            connection.execute(
                update(workflow_stage)
                .where(workflow_stage.c.id == "stage-5a-image-prompts")
                .values(status="confirmed")
            )
        else:
            connection.execute(
                update(workflow_run)
                .where(workflow_run.c.id == "run-prompt-service")
                .values(status="blocked")
            )
    with pytest.raises(PromptStageExecutionError):
        _run(DynamicPromptStageService(database).execute_job(job_record))

    with database.engine.connect() as connection:
        current = connection.execute(
            select(job.c.status, workflow_stage.c.status, workflow_run.c.status)
            .select_from(
                job.join(workflow_stage, job.c.workflow_stage_id == workflow_stage.c.id).join(
                    workflow_run, job.c.workflow_run_id == workflow_run.c.id
                )
            )
            .where(job.c.id == job_record["id"])
        ).one()
    if protected_field == "job":
        assert tuple(current) == ("cancelled", "queued", "queued")
    elif protected_field == "stage":
        assert tuple(current) == ("interrupted", "confirmed", "queued")
    else:
        assert tuple(current) == ("interrupted", "queued", "blocked")


def test_result_conflict_does_not_overwrite_confirmed_stage(prompt_context) -> None:
    database = prompt_context
    job_record = _add_prompt_stage(database, PROMPT_STAGE_KEYS[0])

    class RaceService(DynamicPromptStageService):
        def _compile(self, context):  # type: ignore[no-untyped-def]
            output = super()._compile(context)
            with self.database.engine.begin() as connection:
                connection.execute(
                    update(workflow_stage)
                    .where(workflow_stage.c.id == context.stage_id)
                    .values(status="confirmed")
                )
            return output

    with pytest.raises(PromptStageExecutionError, match="冲突"):
        _run(RaceService(database).execute_job(job_record))

    with database.engine.connect() as connection:
        current = connection.execute(
            select(job.c.status, workflow_stage.c.status)
            .select_from(job.join(workflow_stage, job.c.workflow_stage_id == workflow_stage.c.id))
            .where(job.c.id == job_record["id"])
        ).one()
    assert tuple(current) == ("interrupted", "confirmed")


def test_real_prompt_service_bundle_hashes_confirm_and_queue_media_with_reference_and_override(
    prompt_context,
) -> None:
    database = prompt_context
    now = utc_now()
    canonical_plan = build_reference_plan(
        [
            {
                "index": 1,
                "role": "character_identity",
                "asset_id": "asset-hero",
                "purpose": "clean_identity",
                "provider_url": "https://cdn.example.test/real-hero.png",
            }
        ],
        provider_capabilities=_capabilities(),
    )
    with database.engine.begin() as connection:
        connection.execute(
            insert(api_provider),
            [
                {
                    "id": "config-image-provider",
                    "kind": "image",
                    "name": "测试图片 Provider",
                    "adapter": "mock",
                    "model": "config-image-model",
                    "created_at": now,
                    "updated_at": now,
                },
                {
                    "id": "config-video-provider",
                    "kind": "video",
                    "name": "测试视频 Provider",
                    "adapter": "mock",
                    "model": "config-video-model",
                    "created_at": now,
                    "updated_at": now,
                },
            ],
        )
        connection.execute(
            update(workflow_run)
            .where(workflow_run.c.id == "run-prompt-service")
            .values(
                config_snapshot_json=json.dumps(
                    {
                        "style_prompt": "测试风格补充",
                        "target_duration_sec": 4,
                        "image_provider_id": "config-image-provider",
                        "image_provider_model": "config-image-model",
                        "video_provider_id": "config-video-provider",
                        "video_provider_model": "config-video-model",
                    },
                    ensure_ascii=False,
                )
            )
        )
        connection.execute(
            update(shot)
            .where(shot.c.id == "shot-1-row")
            .values(reference_bindings_json=json.dumps(canonical_plan.model_dump(mode="json")))
        )

    image_job = _add_prompt_stage(
        database,
        PROMPT_STAGE_KEYS[0],
        signature="real-5a-signature",
        run_signature="prompt-signature",
        overrides={"shot-1": {"image_prompt": "人工图片覆盖"}},
    )
    video_signature = "real-5b-signature"
    video_input = {
        "stage_key": PROMPT_STAGE_KEYS[1],
        "provider_capability_snapshot": _capabilities(),
        "provider_id": "job-video-provider",
        "provider_model": "job-video-model",
    }
    video_stage_id = "stage-5b-video-prompts"
    with database.engine.begin() as connection:
        connection.execute(
            update(workflow_stage)
            .where(workflow_stage.c.id == video_stage_id)
            .values(status="queued", input_signature=video_signature)
        )
        connection.execute(
            insert(job).values(
                id="job-real-5b",
                project_id="project-prompt-service",
                chapter_id="chapter-prompt-service",
                workflow_run_id="run-prompt-service",
                workflow_stage_id=video_stage_id,
                job_type="dynamic_stage_generate",
                target_type="workflow_stage",
                target_id=video_stage_id,
                provider_kind="video",
                provider_id=None,
                status="queued",
                progress_current=0,
                progress_total=1,
                input_json=json.dumps(video_input, ensure_ascii=False),
                input_signature=video_signature,
                depends_on_json="[]",
                result_json="{}",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(workflow_stage).values(
                id="stage-6-delivery",
                workflow_run_id="run-prompt-service",
                stage_key="6-delivery",
                ordinal=6,
                status="pending",
                input_signature="",
                raw_output_json="{}",
                normalized_output_json="{}",
                draft_output_json="{}",
                output_refs_json="[]",
                gate_result_json="{}",
                attempt=1,
                created_at=now,
                updated_at=now,
            )
        )

    prompt_service = DynamicPromptStageService(database)
    image_result = _run(prompt_service.execute_job(image_job))
    video_job = {
        "id": "job-real-5b",
        "input_data": video_input,
    }
    video_result = _run(prompt_service.execute_job(video_job))
    assert image_result["status"] == "ready"
    assert video_result["status"] == "ready"

    media_queue = DynamicMediaQueueService(
        database,
        job_repository=JobRepository(database),
        enqueue=lambda _job_id: True,
    )
    workflow = DynamicWorkflowService(database, media_queue_service=media_queue)

    run = workflow.get_run("run-prompt-service")
    image_stage = workflow.get_stage("run-prompt-service", PROMPT_STAGE_KEYS[0])
    image_saved = workflow.save_stage_draft(
        "run-prompt-service",
        PROMPT_STAGE_KEYS[0],
        StageDraftUpdate(
            expected_revision=run["revision"],
            draft_output=image_stage["draft_output"],
        ),
    )
    image_confirmed = workflow.confirm_stage(
        "run-prompt-service",
        PROMPT_STAGE_KEYS[0],
        StageConfirmRequest(expected_revision=image_saved["run"]["revision"]),
    )
    video_stage = workflow.get_stage("run-prompt-service", PROMPT_STAGE_KEYS[1])
    video_saved = workflow.save_stage_draft(
        "run-prompt-service",
        PROMPT_STAGE_KEYS[1],
        StageDraftUpdate(
            expected_revision=image_confirmed["run"]["revision"],
            draft_output=video_stage["draft_output"],
        ),
    )
    video_confirmed = workflow.confirm_stage(
        "run-prompt-service",
        PROMPT_STAGE_KEYS[1],
        StageConfirmRequest(expected_revision=video_saved["run"]["revision"]),
    )

    for action, stage_key in (
        (image_confirmed, PROMPT_STAGE_KEYS[0]),
        (video_confirmed, PROMPT_STAGE_KEYS[1]),
    ):
        stage = next(item for item in action["stages"] if item["stage_key"] == stage_key)
        assert stage["status"] == "confirmed"
        assert not {
            item["code"]
            for item in stage["gate_result"]["diagnostics"]
        } & {"prompt_bundle_base_hash_mismatch", "prompt_bundle_input_hash_mismatch"}

    with database.engine.connect() as connection:
        assert connection.scalar(
            select(job.c.id).where(
                job.c.workflow_run_id == "run-prompt-service",
                job.c.job_type == "generate_keyframe",
            )
        ) is not None
        assert connection.scalar(
            select(job.c.id).where(
                job.c.workflow_run_id == "run-prompt-service",
                job.c.job_type == "generate_video",
            )
        ) is not None
