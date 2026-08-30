from __future__ import annotations

import json
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, insert, select, update

from app.db import Database
from app.dynamic_pipeline.materializer import MaterializationError
from app.dynamic_pipeline.schemas import (
    ArtifactMeta,
    Diagnostic,
    ScriptDialogue,
    StageConfirmRequest,
    StageDraftUpdate,
    StageGateResult,
    WorkflowRunCreate,
)
from app.dynamic_pipeline.storyboard_stage import (
    StoryboardOutput,
    StoryboardShot,
    validate_storyboard_output,
)
from app.jobs.dynamic_bridge import DynamicJobBridge, build_dynamic_idempotency_key
from app.jobs.repository import JobRepository
from app.models.tables import asset, job, shot, workflow_run, workflow_stage
from app.services.dynamic_workflow_service import DynamicWorkflowService
from app.services.errors import ServiceError
from app.services.file_service import FileService
from app.services.utils import json_dumps, utc_now


@dataclass
class Stage5Context:
    database: Database
    service: DynamicWorkflowService
    enqueued: list[str]
    enqueue_seen: list[tuple[str, bool]]


@dataclass
class Stage4Setup:
    run_id: str
    storyboard: dict[str, Any]
    stage4_saved: dict[str, Any]


@pytest.fixture
def stage5_context(tmp_path: Path) -> Iterator[Stage5Context]:
    database = Database(f"sqlite:///{(tmp_path / 'dynamic-stage5-barrier.db').as_posix()}")
    database.initialize(seed_mock_providers=True)
    enqueued: list[str] = []
    enqueue_seen: list[tuple[str, bool]] = []

    def enqueue(job_id: str) -> bool:
        with database.engine.connect() as connection:
            exists = connection.scalar(select(job.c.id).where(job.c.id == job_id)) == job_id
        enqueue_seen.append((job_id, exists))
        if exists:
            enqueued.append(job_id)
        return exists

    storage = FileService(tmp_path / "workspace", database=database)
    service = DynamicWorkflowService(
        database,
        storage=storage,
        job_bridge=DynamicJobBridge(JobRepository(database), enqueue=enqueue),
    )
    try:
        yield Stage5Context(
            database=database,
            service=service,
            enqueued=enqueued,
            enqueue_seen=enqueue_seen,
        )
    finally:
        database.dispose()


def _meta(signature: str, schema_version: str) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "normalizer_version": "dynamic-stage5-test-normalizer-v1",
        "input_signature": signature,
        "content_signature": f"{signature}-content",
        "source_attempt": 1,
    }


def _source_payload() -> dict[str, Any]:
    return {
        "artifact_meta": _meta("source-input", "dynamic-source-v1"),
        "metadata": {
            "source_kind": "synopsis",
            "title": "阶段 5 屏障测试故事",
            "chapter_title": "第一章",
            "source_signature": "stage5-source-signature",
            "target_duration_sec": 30,
            "aspect_ratio": "9:16",
        },
        "episode_logline": "主角在旧屋中发现发光的戒指。",
        "scenes": [
            {
                "scene_id": "scene-1",
                "ordinal": 1,
                "location": "旧屋",
                "time_weather": "夜，阴",
                "characters": [],
                "narrative_task": "建立线索",
                "plot_event": "主角发现戒指",
                "conflict_or_turn": "戒指引出新的危险",
                "visual_focus": "主角拿起发光的戒指",
                "estimated_duration_sec": 30,
                "shootability": {
                    "status": "shootable",
                    "visible_action": "主角拿起发光的戒指",
                    "required_asset_keys": [],
                    "production_risks": [],
                    "adaptation_note": None,
                },
                "source_refs": ["source:1"],
            }
        ],
        "character_roster": [],
        "conflict_turns": [],
        "visualizable_content": ["旧屋中的发光戒指"],
        "hard_to_visualize": [],
        "rewrite_requirements": [],
        "assumptions": [],
        "open_questions": [],
        "continuation_outline": [],
    }


def _bios_payload() -> dict[str, Any]:
    return {
        "artifact_meta": _meta("bios-input", "dynamic-character-bios-v1"),
        "characters": [
            {
                "basic_info": {
                    "character_key": "hero",
                    "canonical_name": "主角",
                    "aliases": [],
                    "importance": "core",
                    "identity": "线索追寻者",
                    "age": "成年",
                    "gender": "未设定",
                },
                "background": "主角一直寻找失踪的线索。",
                "personality_behavior_logic": "先观察，再用动作回应危险。",
                "dynamic_comic_setting": "行动时保持稳定轮廓和服装。",
                "plot_relation": "当前冲突的承受者与解决者。",
                "visual_anchor": {
                    "visual_facts": ["黑色短发"],
                    "face": "清晰的面部轮廓",
                    "hair": "黑色短发",
                    "body": "身形匀称",
                    "costume": "深色外套",
                    "accessories": [],
                    "immutable_features": ["黑色短发"],
                    "forbidden_changes": ["不得改变发型"],
                    "makeup_keyword_string": "清晰面部轮廓|黑色短发|深色外套",
                    "variants": [],
                    "current_variant": None,
                    "approved_reference_ids": [],
                },
                "behavior_evidence": {
                    "trigger": "发现发光的戒指",
                    "action": "抬手拿起戒指并作出决定",
                    "expression": "目光专注",
                    "voice": "短促吸气后低声回应",
                    "source_refs": ["scene-1"],
                },
            }
        ],
        "ensemble_anchors": [],
        "unresolved_character_refs": [],
    }


def _rhythm_payload() -> dict[str, Any]:
    event = {
        "event_id": "dynamic-1",
        "summary": "主角完成关键回应",
        "segment_id": "segment-1",
        "dynamic_level": 1.0,
        "visual_action": "主角举起戒指并作出决定",
        "source_refs": [],
    }
    return {
        "artifact_meta": _meta("rhythm-input", "dynamic-rhythm-v1"),
        "target_duration_sec": 30,
        "timeline": [
            {
                "segment_id": "segment-1",
                "ordinal": 1,
                "start_sec": 0,
                "end_sec": 30,
                "beat_type": "payoff",
                "narrative_task": "建立冲突并完成回应",
                "event_summary": "主角举起戒指并作出决定",
                "visual_action": "主角举起戒指并作出决定",
                "focus_characters": ["hero"],
                "dynamic_level": 0.8,
                "strong_dynamic_event_id": "dynamic-1",
                "source_refs": [],
            }
        ],
        "core_payoff": "主角完成关键回应",
        "small_beats": [],
        "strong_dynamic_budget": {
            "max_events": 1,
            "event_ids": ["dynamic-1"],
            "events": [event],
        },
        "foreshadowing": ["戒指的来历将在后续揭示"],
        "open_space": ["保留身份揭示空间"],
        "dialogue_suggestions": [],
        "sound_suggestions": [],
        "assumptions": ["目标时长为 30 秒"],
    }


def _script_payload() -> dict[str, Any]:
    return {
        "artifact_meta": _meta("script-input", "dynamic-script-v2"),
        "title": "阶段 5 屏障测试故事",
        "logline": "主角完成关键回应。",
        "target_duration_sec": 30,
        "scenes": [
            {
                "scene_id": "scene-1",
                "ordinal": 1,
                "scene_heading": "旧屋",
                "time_weather": "夜，阴",
                "location": "旧屋",
                "characters": ["主角"],
                "objective": "建立线索",
                "action": "主角举起戒指并作出决定",
                "dialogue": [
                    {
                        "dialogue_line_id": "line-1",
                        "speaker": "主角",
                        "text": "我来了。",
                        "start_sec": 0,
                        "end_sec": 1,
                    }
                ],
                "narration": ["屋外传来脚步声。"],
                "emotion": "紧张",
                "dynamic_level": 0.8,
                "sound_cues": [
                    {
                        "kind": "environment",
                        "description": "脚步声",
                        "start_sec": 1,
                        "end_sec": 2,
                    }
                ],
                "subtitle_style": "白字黑边",
                "duration_sec": 30,
                "transition_to_next": "",
                "source_refs": ["source:1"],
            }
        ],
        "strong_dynamic_event_ids": ["dynamic-1"],
        "assumptions": [],
        "open_questions": [],
    }


def _storyboard_payload(character_asset_id: str, scene_asset_id: str) -> dict[str, Any]:
    output = StoryboardOutput(
        artifact_meta=ArtifactMeta.model_validate(
            _meta("storyboard-input", "dynamic-storyboard-v2")
        ),
        target_duration_sec=30,
        shots=[
            StoryboardShot(
                external_key="storyboard:stage5:scene-1:shot-1",
                shot_no=1,
                ordinal=1,
                scene_id="scene-1",
                scene_no=1,
                scene_name="旧屋",
                time_weather="夜，阴",
                scene_asset_id=scene_asset_id,
                character_names=["主角"],
                character_asset_ids=[character_asset_id],
                prop_names=[],
                prop_asset_ids=[],
                positions=[{"character": "主角", "position": "中央"}],
                expressions=[{"character": "主角", "expression": "紧张"}],
                start_state="主角站在旧屋中央",
                action="主角举起戒指并作出决定",
                end_state="主角握紧戒指",
                next_hook="屋外传来脚步声",
                transition_from_prev="scene_change",
                duration_sec=15,
                provider_duration_sec=15,
                shot_type="中景",
                camera_angle="平视",
                camera_move="固定",
                dialogue=[
                    ScriptDialogue(
                        dialogue_line_id="line-1",
                        speaker="主角",
                        text="我来了。",
                    )
                ],
                narration="屋外传来脚步声。",
                sound_cues=[{"kind": "sfx", "description": "脚步声"}],
                dynamic_level=0.8,
                dynamic_event_id="dynamic-1",
                frame_description="旧屋中央的主角",
                source_refs=["source:1"],
                reference_bindings=[{"role": "character", "asset_id": character_asset_id}],
                prompt_intent={
                    "action_events": [{"action": "主角举起戒指并作出决定"}],
                },
            )
        ],
        scenes=[],
        assumptions=[],
        open_questions=[],
    )
    return output.model_dump(mode="json")


def _ready_gate() -> StageGateResult:
    return StageGateResult(status="ready", passed=True, diagnostics=[], can_confirm=True)


def _blocked_gate() -> StageGateResult:
    return StageGateResult(
        status="blocked",
        passed=False,
        diagnostics=[Diagnostic(level="blocking", code="stage5_test_blocked", message="测试阻断")],
        can_confirm=False,
    )


def _create_run(context: Stage5Context) -> dict[str, Any]:
    return context.service.create_run(
        WorkflowRunCreate(
            title="阶段 5 屏障测试",
            chapter_title="第一章",
            synopsis="主角在旧屋中发现一枚发光的戒指。",
            target_duration_sec=30,
        )
    )


def _save_stage(
    context: Stage5Context,
    run_id: str,
    stage_key: str,
    expected_revision: int,
    draft_output: dict[str, Any],
    gate_result: StageGateResult | None = None,
) -> dict[str, Any]:
    return context.service.save_stage_draft(
        run_id,
        stage_key,
        StageDraftUpdate(
            expected_revision=expected_revision,
            draft_output=draft_output,
            gate_result=gate_result or _ready_gate(),
        ),
    )


def _set_stage_ready(context: Stage5Context, run_id: str, stage_key: str) -> None:
    with context.database.engine.begin() as connection:
        connection.execute(
            update(workflow_stage)
            .where(
                workflow_stage.c.workflow_run_id == run_id,
                workflow_stage.c.stage_key == stage_key,
            )
            .values(status="ready", updated_at=utc_now())
        )


def _confirm_stage(
    context: Stage5Context,
    run_id: str,
    stage_key: str,
    expected_revision: int,
) -> dict[str, Any]:
    return context.service.confirm_stage(
        run_id,
        stage_key,
        StageConfirmRequest(expected_revision=expected_revision),
    )


def _insert_scene_asset(context: Stage5Context, project_id: str, asset_id: str) -> None:
    now = utc_now()
    with context.database.engine.begin() as connection:
        connection.execute(
            insert(asset).values(
                id=asset_id,
                project_id=project_id,
                asset_type="scene",
                name="旧屋",
                profile_json=json_dumps({}),
                visual_anchor_json=json_dumps({}),
                importance="normal",
                status="confirmed",
                source_signature="scene-source-signature",
                own_signature="scene-own-signature",
                revision=1,
                created_at=now,
                updated_at=now,
            )
        )


def _prepare_stage4(context: Stage5Context) -> Stage4Setup:
    created = _create_run(context)
    run_id = created["run"]["id"]
    assert created["run"]["project_id"]
    assert created["run"]["chapter_id"]
    assert len(created["stages"]) == 8

    source_saved = _save_stage(context, run_id, "0-source", 1, _source_payload())
    _set_stage_ready(context, run_id, "0-source")
    source_confirmed = _confirm_stage(context, run_id, "0-source", source_saved["run"]["revision"])

    bios_saved = _save_stage(
        context,
        run_id,
        "1-character-bios",
        source_confirmed["run"]["revision"],
        _bios_payload(),
    )
    _set_stage_ready(context, run_id, "1-character-bios")
    bios_confirmed = _confirm_stage(
        context,
        run_id,
        "1-character-bios",
        bios_saved["run"]["revision"],
    )

    rhythm_saved = _save_stage(
        context,
        run_id,
        "2-rhythm",
        bios_confirmed["run"]["revision"],
        _rhythm_payload(),
    )
    _set_stage_ready(context, run_id, "2-rhythm")
    rhythm_confirmed = _confirm_stage(
        context,
        run_id,
        "2-rhythm",
        rhythm_saved["run"]["revision"],
    )

    script_saved = _save_stage(
        context,
        run_id,
        "3-script",
        rhythm_confirmed["run"]["revision"],
        _script_payload(),
    )
    _set_stage_ready(context, run_id, "3-script")
    script_confirmed = _confirm_stage(
        context,
        run_id,
        "3-script",
        script_saved["run"]["revision"],
    )

    for stage_key in ("0-source", "1-character-bios", "2-rhythm", "3-script"):
        assert context.service.get_stage(run_id, stage_key)["status"] == "confirmed"

    character_asset_id = bios_confirmed["run"]["asset_mapping"][0]["asset_id"]
    scene_asset_id = f"scene-asset:{run_id}"
    _insert_scene_asset(context, created["run"]["project_id"], scene_asset_id)
    storyboard = _storyboard_payload(character_asset_id, scene_asset_id)
    assert not [
        item
        for item in validate_storyboard_output(
            storyboard,
            script=_script_payload(),
            rhythm=_rhythm_payload(),
            target_duration_sec=30,
        )
        if item.level == "blocking"
    ]

    stage4_saved = _save_stage(
        context,
        run_id,
        "4-storyboard",
        script_confirmed["run"]["revision"],
        storyboard,
    )
    _set_stage_ready(context, run_id, "4-storyboard")

    # 只保留阶段 5 barrier 本次确认的入队观察结果；上游确认产生的 Job 仍保留在数据库。
    context.enqueued.clear()
    context.enqueue_seen.clear()
    return Stage4Setup(run_id=run_id, storyboard=storyboard, stage4_saved=stage4_saved)


def _prompt_jobs(context: Stage5Context, run_id: str) -> list[dict[str, Any]]:
    with context.database.engine.connect() as connection:
        rows = connection.execute(
            select(job, workflow_stage.c.stage_key.label("workflow_stage_key"))
            .select_from(job.join(workflow_stage, job.c.workflow_stage_id == workflow_stage.c.id))
            .where(
                job.c.workflow_run_id == run_id,
                workflow_stage.c.stage_key.in_(("5a-image-prompts", "5b-video-prompts")),
            )
            .order_by(
                workflow_stage.c.ordinal,
                workflow_stage.c.stage_key,
                job.c.idempotency_key,
                job.c.id,
            )
        ).all()
    return [dict(row._mapping) for row in rows]


def _shot_count(context: Stage5Context, run_id: str) -> int:
    run = context.service.get_run(run_id)
    with context.database.engine.connect() as connection:
        return int(
            connection.scalar(
                select(func.count())
                .select_from(shot)
                .where(
                    shot.c.project_id == run["project_id"],
                    shot.c.chapter_id == run["chapter_id"],
                )
            )
            or 0
        )


def _set_prompt_status(
    context: Stage5Context,
    run_id: str,
    stage_key: str,
    status: str,
) -> None:
    with context.database.engine.begin() as connection:
        connection.execute(
            update(workflow_stage)
            .where(
                workflow_stage.c.workflow_run_id == run_id,
                workflow_stage.c.stage_key == stage_key,
            )
            .values(status=status, updated_at=utc_now())
        )


def _seed_queued_prompt_job(context: Stage5Context, run_id: str, stage_key: str) -> str:
    signature = f"existing-{stage_key}"
    with context.database.engine.begin() as connection:
        connection.execute(
            update(workflow_stage)
            .where(
                workflow_stage.c.workflow_run_id == run_id,
                workflow_stage.c.stage_key == stage_key,
            )
            .values(status="queued", input_signature=signature, updated_at=utc_now())
        )
        run_row = connection.execute(
            select(workflow_run).where(workflow_run.c.id == run_id)
        ).first()
        stage_row = connection.execute(
            select(workflow_stage).where(
                workflow_stage.c.workflow_run_id == run_id,
                workflow_stage.c.stage_key == stage_key,
            )
        ).first()
        assert run_row is not None
        assert stage_row is not None
        reference = context.service.job_bridge.ensure_stage_job_in_transaction(
            connection,
            dict(run_row._mapping),
            dict(stage_row._mapping),
        )
    return reference.job_id


def test_stage4_confirmation_creates_parallel_prompt_jobs_after_commit(
    stage5_context: Stage5Context,
) -> None:
    context = stage5_context
    setup = _prepare_stage4(context)

    confirmed = _confirm_stage(
        context,
        setup.run_id,
        "4-storyboard",
        setup.stage4_saved["run"]["revision"],
    )

    stages = {stage["stage_key"]: stage for stage in confirmed["stages"]}
    prompt_stages = {key: stages[key] for key in ("5a-image-prompts", "5b-video-prompts")}
    signatures = {key: stage["input_signature"] for key, stage in prompt_stages.items()}
    assert all(signatures.values())
    assert signatures["5a-image-prompts"] != signatures["5b-video-prompts"]
    assert all(
        signature != confirmed["run"]["input_signature"] for signature in signatures.values()
    )
    assert all(stage["status"] == "queued" for stage in prompt_stages.values())
    assert confirmed["run"]["current_stage"] == "5a-image-prompts"

    jobs = _prompt_jobs(context, setup.run_id)
    assert len(jobs) == 2
    jobs_by_stage = {item["workflow_stage_key"]: item for item in jobs}
    assert set(jobs_by_stage) == set(prompt_stages)
    for stage_key, stage in prompt_stages.items():
        dynamic_job = jobs_by_stage[stage_key]
        assert dynamic_job["job_type"] == "dynamic_stage_generate"
        assert dynamic_job["status"] == "queued"
        assert dynamic_job["workflow_stage_id"] == stage["id"]
        assert dynamic_job["input_signature"] == stage["input_signature"]
        assert dynamic_job["idempotency_key"] == build_dynamic_idempotency_key(
            stage["id"],
            "workflow_stage",
            stage["id"],
            stage["input_signature"],
            "dynamic_stage_generate",
        )

    assert context.enqueued == [
        jobs_by_stage["5a-image-prompts"]["id"],
        jobs_by_stage["5b-video-prompts"]["id"],
    ]
    assert context.enqueue_seen == [(job_id, True) for job_id in context.enqueued]


def test_prompt_stage_signature_changes_with_asset_revision_and_capability_snapshot(
    stage5_context: Stage5Context,
) -> None:
    context = stage5_context
    setup = _prepare_stage4(context)
    with context.database.engine.begin() as connection:
        run_row = connection.execute(
            select(workflow_run).where(workflow_run.c.id == setup.run_id)
        ).one()
        stage_row = context.service.get_stage(setup.run_id, "4-storyboard")
        signature_one, _ = context.service._prompt_stage_input_signature(
            connection,
            "5a-image-prompts",
            dict(run_row._mapping),
            stage_row,
        )
        character_id = setup.storyboard["shots"][0]["character_asset_ids"][0]
        connection.execute(
            update(asset).where(asset.c.id == character_id).values(revision=2)
        )
        run_after_asset = dict(
            connection.execute(
                select(workflow_run).where(workflow_run.c.id == setup.run_id)
            ).one()._mapping
        )
        signature_two, _ = context.service._prompt_stage_input_signature(
            connection,
            "5a-image-prompts",
            run_after_asset,
            stage_row,
        )
        config = json.loads(run_after_asset["config_snapshot_json"])
        config["image_provider_capabilities"] = {
            "capability_version": "changed-capability",
            "supports_multiple_images": True,
            "max_reference_images": 3,
        }
        connection.execute(
            update(workflow_run)
            .where(workflow_run.c.id == setup.run_id)
            .values(config_snapshot_json=json_dumps(config))
        )
        run_after_capability = dict(
            connection.execute(
                select(workflow_run).where(workflow_run.c.id == setup.run_id)
            ).one()._mapping
        )
        signature_three, _ = context.service._prompt_stage_input_signature(
            connection,
            "5a-image-prompts",
            run_after_capability,
            stage_row,
        )

    assert signature_one != signature_two
    assert signature_two != signature_three


def test_prompt_stage_outer_signature_changes_with_human_reference_override(
    stage5_context: Stage5Context,
) -> None:
    context = stage5_context
    setup = _prepare_stage4(context)
    confirmed = _confirm_stage(
        context,
        setup.run_id,
        "4-storyboard",
        setup.stage4_saved["run"]["revision"],
    )
    prompt_stage = context.service.get_stage(setup.run_id, "5a-image-prompts")
    with context.database.engine.begin() as connection:
        run_row = connection.execute(
            select(workflow_run).where(workflow_run.c.id == setup.run_id)
        ).one()
        stage4 = context.service.get_stage(setup.run_id, "4-storyboard")
        signature_before, _ = context.service._prompt_stage_input_signature(
            connection,
            "5a-image-prompts",
            dict(run_row._mapping),
            stage4,
        )
        prompt_job = connection.execute(
            select(job).where(job.c.workflow_stage_id == prompt_stage["id"])
        ).one()
        connection.execute(
            update(job)
            .where(job.c.id == prompt_job.id)
            .values(
                input_json=json_dumps(
                    {
                        "stage_key": "5a-image-prompts",
                        "overrides_by_external_key": {
                            setup.storyboard["shots"][0]["external_key"]: {
                                "image_prompt": "人工最终图片稿",
                                "reference_slots": setup.storyboard["shots"][0][
                                    "reference_bindings"
                                ],
                            }
                        },
                    }
                )
            )
        )
        signature_after, _ = context.service._prompt_stage_input_signature(
            connection,
            "5a-image-prompts",
            dict(run_row._mapping),
            stage4,
        )

    assert confirmed["run"]["current_stage"] == "5a-image-prompts"
    assert signature_before != signature_after


def test_stage4_repeat_confirmation_and_old_revision_do_not_duplicate_prompt_jobs(
    stage5_context: Stage5Context,
) -> None:
    context = stage5_context
    setup = _prepare_stage4(context)
    confirmed = _confirm_stage(
        context,
        setup.run_id,
        "4-storyboard",
        setup.stage4_saved["run"]["revision"],
    )
    jobs_before = _prompt_jobs(context, setup.run_id)
    job_ids_before = [item["id"] for item in jobs_before]
    revision_after_confirm = confirmed["run"]["revision"]

    with pytest.raises(ServiceError) as old_revision_error:
        _confirm_stage(
            context,
            setup.run_id,
            "4-storyboard",
            setup.stage4_saved["run"]["revision"],
        )
    assert old_revision_error.value.status_code == 409

    with pytest.raises(ServiceError) as repeated_error:
        _confirm_stage(context, setup.run_id, "4-storyboard", revision_after_confirm)
    assert repeated_error.value.status_code == 409

    assert [item["id"] for item in _prompt_jobs(context, setup.run_id)] == job_ids_before
    assert context.service.get_stage(setup.run_id, "4-storyboard")["status"] == "confirmed"
    assert context.service.get_run(setup.run_id)["revision"] == revision_after_confirm


def test_stage5_pause_and_resume_keeps_both_parallel_branches_recoverable(
    stage5_context: Stage5Context,
) -> None:
    context = stage5_context
    setup = _prepare_stage4(context)
    confirmed = _confirm_stage(
        context,
        setup.run_id,
        "4-storyboard",
        setup.stage4_saved["run"]["revision"],
    )
    context.enqueued.clear()

    paused = context.service.pause_run(setup.run_id, confirmed["run"]["revision"])
    assert paused["run"]["status"] == "blocked"
    assert {
        stage["status"]
        for stage in paused["stages"]
        if stage["stage_key"] in {"5a-image-prompts", "5b-video-prompts"}
    } == {"blocked"}

    resumed = context.service.resume_run(setup.run_id, paused["run"]["revision"])
    prompt_stages = {
        stage["stage_key"]: stage
        for stage in resumed["stages"]
        if stage["stage_key"] in {"5a-image-prompts", "5b-video-prompts"}
    }
    assert resumed["run"]["status"] == "queued"
    assert [prompt_stages[key]["status"] for key in ("5a-image-prompts", "5b-video-prompts")] == [
        "queued",
        "queued",
    ]
    jobs = _prompt_jobs(context, setup.run_id)
    assert [item["status"] for item in jobs] == ["queued", "queued"]
    assert context.enqueued == [item["id"] for item in jobs]


@pytest.mark.parametrize(
    "protected_status", ["queued", "running", "ready", "confirmed", "cancelled"]
)
def test_stage4_barrier_does_not_backtrack_existing_prompt_branch(
    stage5_context: Stage5Context,
    protected_status: str,
) -> None:
    context = stage5_context
    setup = _prepare_stage4(context)
    existing_job_id = None
    if protected_status == "queued":
        existing_job_id = _seed_queued_prompt_job(context, setup.run_id, "5a-image-prompts")
    else:
        _set_prompt_status(context, setup.run_id, "5a-image-prompts", protected_status)
    before = context.service.get_stage(setup.run_id, "5a-image-prompts")

    confirmed = _confirm_stage(
        context,
        setup.run_id,
        "4-storyboard",
        setup.stage4_saved["run"]["revision"],
    )

    after = context.service.get_stage(setup.run_id, "5a-image-prompts")
    other = context.service.get_stage(setup.run_id, "5b-video-prompts")
    assert after["status"] == protected_status
    assert after["input_signature"] == before["input_signature"]
    assert other["status"] == "queued"
    assert confirmed["run"]["current_stage"] == "5a-image-prompts"

    jobs = _prompt_jobs(context, setup.run_id)
    expected_job_count = 2 if existing_job_id is not None else 1
    assert len(jobs) == expected_job_count
    if existing_job_id is None:
        assert jobs[0]["workflow_stage_key"] == "5b-video-prompts"
    else:
        jobs_by_id = {item["id"]: item for item in jobs}
        assert jobs_by_id[existing_job_id]["workflow_stage_key"] == "5a-image-prompts"
        assert jobs_by_id[existing_job_id]["status"] == "queued"
        assert jobs_by_id[existing_job_id]["input_signature"] == before["input_signature"]
        assert [item["workflow_stage_key"] for item in jobs] == [
            "5a-image-prompts",
            "5b-video-prompts",
        ]
    assert len(context.enqueued) == 1
    if existing_job_id is None:
        assert jobs[0]["id"] == context.enqueued[0]
    else:
        assert context.enqueued[0] != existing_job_id


def test_stage4_blocking_gate_rolls_back_without_prompt_jobs(
    stage5_context: Stage5Context,
) -> None:
    context = stage5_context
    setup = _prepare_stage4(context)
    with context.database.engine.begin() as connection:
        connection.execute(
            update(workflow_stage)
            .where(
                workflow_stage.c.workflow_run_id == setup.run_id,
                workflow_stage.c.stage_key == "4-storyboard",
            )
            .values(gate_result_json=json_dumps(_blocked_gate().model_dump(mode="json")))
        )

    before_run = context.service.get_run(setup.run_id)
    before_stage = context.service.get_stage(setup.run_id, "4-storyboard")
    with pytest.raises(ServiceError) as error:
        _confirm_stage(context, setup.run_id, "4-storyboard", before_run["revision"])

    assert error.value.status_code == 409
    after_run = context.service.get_run(setup.run_id)
    after_stage = context.service.get_stage(setup.run_id, "4-storyboard")
    assert after_run["revision"] == before_run["revision"]
    assert after_run["status"] == before_run["status"]
    assert after_run["current_stage"] == before_run["current_stage"]
    assert after_stage["status"] == "ready"
    assert after_stage["draft_output"] == before_stage["draft_output"]
    assert after_stage["gate_result"]["status"] == "blocked"
    assert _prompt_jobs(context, setup.run_id) == []
    assert _shot_count(context, setup.run_id) == 0
    assert context.enqueued == []


def test_stage4_server_bound_rhythm_budget_blocks_before_shot_or_prompt_job_creation(
    stage5_context: Stage5Context,
) -> None:
    context = stage5_context
    setup = _prepare_stage4(context)
    run_id = setup.run_id
    rhythm = _rhythm_payload()
    rhythm["strong_dynamic_budget"]["max_events"] = 99
    rhythm["strong_dynamic_budget"]["event_ids"].append("dynamic-2")
    rhythm["strong_dynamic_budget"]["events"].append(
        {
            "event_id": "dynamic-2",
            "summary": "第二个强动态",
            "segment_id": "segment-1",
            "dynamic_level": 1.0,
            "visual_action": "主角再次挥击",
            "source_refs": [],
        }
    )
    storyboard = deepcopy(setup.storyboard)
    second = deepcopy(storyboard["shots"][0])
    second.update(
        {
            "external_key": f"{second['external_key']}:2",
            "shot_no": 2,
            "ordinal": 2,
            "duration_sec": 15,
            "provider_duration_sec": 15,
            "transition_from_prev": "same_scene_cut",
            "dynamic_event_id": "dynamic-2",
        }
    )
    storyboard["shots"].append(second)
    with context.database.engine.begin() as connection:
        run = connection.execute(
            select(workflow_run).where(workflow_run.c.id == run_id)
        ).one()
        config = json.loads(run.config_snapshot_json)
        config["strong_dynamic_max_events"] = 1
        connection.execute(
            update(workflow_run)
            .where(workflow_run.c.id == run_id)
            .values(config_snapshot_json=json_dumps(config))
        )
        connection.execute(
            update(workflow_stage)
            .where(
                workflow_stage.c.workflow_run_id == run_id,
                workflow_stage.c.stage_key == "2-rhythm",
            )
            .values(
                normalized_output_json=json_dumps(rhythm),
                draft_output_json=json_dumps(rhythm),
            )
        )
    saved = _save_stage(
        context,
        run_id,
        "4-storyboard",
        setup.stage4_saved["run"]["revision"],
        storyboard,
    )
    _set_stage_ready(context, run_id, "4-storyboard")

    with pytest.raises(ServiceError) as error:
        _confirm_stage(context, run_id, "4-storyboard", saved["run"]["revision"])

    assert error.value.status_code == 409
    assert _shot_count(context, run_id) == 0
    assert _prompt_jobs(context, run_id) == []
    stage = context.service.get_stage(run_id, "4-storyboard")
    assert "storyboard_rhythm_dynamic_budget_exceeded" in {
        item["code"] for item in stage["gate_result"]["diagnostics"]
    }


def test_stage4_materialization_failure_rolls_back_run_stage_and_prompt_jobs(
    stage5_context: Stage5Context,
) -> None:
    context = stage5_context
    setup = _prepare_stage4(context)

    class FailingMaterializer:
        def __init__(self, delegate: Any) -> None:
            self.delegate = delegate

        def validate_storyboard_output(self, *args: Any, **kwargs: Any) -> list[Any]:
            return self.delegate.validate_storyboard_output(*args, **kwargs)

        def build_shot_materialization_plan(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
            raise MaterializationError("阶段 4 物化失败")

    failing_service = DynamicWorkflowService(
        context.database,
        storage=context.service.storage,
        job_bridge=context.service.job_bridge,
        materializer=FailingMaterializer(context.service.materializer),
    )
    before_run = context.service.get_run(setup.run_id)
    before_stage = context.service.get_stage(setup.run_id, "4-storyboard")
    with pytest.raises(ServiceError) as error:
        failing_service.confirm_stage(
            setup.run_id,
            "4-storyboard",
            StageConfirmRequest(expected_revision=before_run["revision"]),
        )

    assert error.value.status_code == 400
    after_run = context.service.get_run(setup.run_id)
    after_stage = context.service.get_stage(setup.run_id, "4-storyboard")
    assert after_run["revision"] == before_run["revision"]
    assert after_run["status"] == before_run["status"]
    assert after_run["current_stage"] == before_run["current_stage"]
    assert after_stage["status"] == before_stage["status"] == "ready"
    assert after_stage["draft_output"] == before_stage["draft_output"] == setup.storyboard
    assert _prompt_jobs(context, setup.run_id) == []
    assert _shot_count(context, setup.run_id) == 0
    assert context.enqueued == []
