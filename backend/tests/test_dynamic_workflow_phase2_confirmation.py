from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update

from app.db import Database
from app.dynamic_pipeline.schemas import (
    StageConfirmRequest,
    StageDraftUpdate,
    StageGateResult,
    WorkflowRunCreate,
)
from app.jobs.dynamic_bridge import DynamicJobBridge
from app.jobs.repository import JobRepository
from app.models.tables import job, workflow_stage
from app.services.dynamic_workflow_service import DynamicWorkflowService
from app.services.errors import ServiceError
from app.services.file_service import FileService
from app.services.signature_service import calculate_signature
from app.services.utils import utc_now


@dataclass
class Phase2Context:
    database: Database
    service: DynamicWorkflowService
    enqueued: list[str]


@pytest.fixture
def phase2_context(tmp_path: Path) -> Iterator[Phase2Context]:
    database = Database(f"sqlite:///{(tmp_path / 'phase2-confirmation.db').as_posix()}")
    database.initialize(seed_mock_providers=True)
    enqueued: list[str] = []

    def enqueue(job_id: str) -> bool:
        with database.engine.connect() as connection:
            assert connection.execute(select(job.c.id).where(job.c.id == job_id)).scalar() == job_id
        enqueued.append(job_id)
        return True

    storage = FileService(tmp_path / "workspace", database=database)
    service = DynamicWorkflowService(
        database,
        storage=storage,
        job_bridge=DynamicJobBridge(JobRepository(database), enqueue=enqueue),
    )
    try:
        yield Phase2Context(database=database, service=service, enqueued=enqueued)
    finally:
        database.dispose()


def _source_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "artifact_meta": {
            "schema_version": "dynamic-source-v1",
            "normalizer_version": "dynamic-normalizer-v1",
            "input_signature": "source-input",
            "content_signature": "source-content",
            "source_attempt": 1,
        },
        "metadata": {
            "source_kind": "synopsis",
            "title": "动态测试故事",
            "chapter_title": "第一章",
            "source_signature": "source-signature",
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
                "source_refs": [],
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
    payload.update(overrides)
    return payload


def _bios_payload() -> dict[str, Any]:
    return {
        "artifact_meta": {
            "schema_version": "dynamic-character-bios-v1",
            "normalizer_version": "dynamic-normalizer-v1",
            "input_signature": "bios-input",
            "content_signature": "bios-content",
            "source_attempt": 1,
        },
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
                "personality_behavior_logic": "先观察，再用明确动作回应危险。",
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
                    "approved_reference_ids": ["reference-1"],
                },
                "behavior_evidence": {
                    "trigger": "发现发光的戒指",
                    "action": "抬手拿起戒指并作出决定",
                    "expression": "目光专注，眉头收紧",
                    "voice": "低声说出决定",
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
        "artifact_meta": {
            "schema_version": "dynamic-rhythm-v1",
            "normalizer_version": "dynamic-normalizer-v1",
            "input_signature": "rhythm-input",
            "content_signature": "rhythm-content",
            "source_attempt": 1,
        },
        "target_duration_sec": 30,
        "timeline": [
            {
                "segment_id": "segment-1",
                "ordinal": 1,
                "start_sec": 0,
                "end_sec": 30,
                "beat_type": "payoff",
                "narrative_task": "建立冲突并完成回应",
                "event_summary": "主角完成关键回应",
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


def _script_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "artifact_meta": {
            "schema_version": "dynamic-script-v2",
            "normalizer_version": "dynamic-normalizer-v1",
            "input_signature": "script-input",
            "content_signature": "script-content",
            "source_attempt": 1,
        },
        "title": "动态测试剧本",
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
                "plot_event_ids": ["scene-1:objective", "scene-1:action"],
                "dialogue": [
                    {
                        "dialogue_line_id": "scene-1-dialogue-1",
                        "speaker": "主角",
                        "text": "我知道该怎么做。",
                        "start_sec": 0,
                        "end_sec": 1,
                    }
                ],
                "narration": [],
                "emotion": "坚定",
                "dynamic_level": 0.8,
                "sound_cues": [],
                "subtitle_style": "",
                "duration_sec": 30,
                "transition_to_next": "",
                "source_refs": [],
            }
        ],
        "strong_dynamic_event_ids": ["dynamic-1"],
        "assumptions": [],
        "open_questions": [],
    }
    payload.update(overrides)
    return payload


def _ready_gate() -> StageGateResult:
    return StageGateResult(status="ready", passed=True, diagnostics=[], can_confirm=True)


def _create_run(
    context: Phase2Context,
    *,
    strong_dynamic_max_events: int = 3,
) -> dict[str, Any]:
    return context.service.create_run(
        WorkflowRunCreate(
            title="动态确认测试",
            chapter_title="第一章",
            synopsis="主角在旧屋中发现一枚发光的戒指。",
            target_duration_sec=30,
            strong_dynamic_max_events=strong_dynamic_max_events,
        )
    )


def _save_draft(
    context: Phase2Context,
    run_id: str,
    stage_key: str,
    expected_revision: int,
    draft_output: dict[str, Any],
) -> dict[str, Any]:
    return context.service.save_stage_draft(
        run_id,
        stage_key,
        StageDraftUpdate(
            expected_revision=expected_revision,
            draft_output=draft_output,
            gate_result=_ready_gate(),
        ),
    )


def _set_stage_status(context: Phase2Context, run_id: str, stage_key: str, status: str) -> None:
    with context.database.engine.begin() as connection:
        connection.execute(
            update(workflow_stage)
            .where(
                workflow_stage.c.workflow_run_id == run_id,
                workflow_stage.c.stage_key == stage_key,
            )
            .values(status=status, updated_at=utc_now())
        )


def _confirm(
    context: Phase2Context,
    run_id: str,
    stage_key: str,
    expected_revision: int,
) -> dict[str, Any]:
    return context.service.confirm_stage(
        run_id,
        stage_key,
        StageConfirmRequest(expected_revision=expected_revision),
    )


def _jobs(context: Phase2Context, run_id: str) -> list[dict[str, Any]]:
    with context.database.engine.connect() as connection:
        rows = connection.execute(
            select(job).where(job.c.workflow_run_id == run_id).order_by(job.c.created_at, job.c.id)
        ).all()
    return [dict(row._mapping) for row in rows]


def test_phase2_confirm_recalculates_blocking_gate_and_rejects_fake_ready_gate(
    phase2_context: Phase2Context,
) -> None:
    context = phase2_context
    created = _create_run(context)
    run_id = created["run"]["id"]
    saved = _save_draft(context, run_id, "0-source", 1, _source_payload(scenes=[]))
    _set_stage_status(context, run_id, "0-source", "ready")

    with pytest.raises(ServiceError) as error:
        _confirm(context, run_id, "0-source", saved["run"]["revision"])

    assert error.value.status_code == 409
    assert error.value.detail == "阶段 0-source 尚未满足确认门禁"
    stage = context.service.get_stage(run_id, "0-source")
    assert stage["status"] == "ready"
    assert stage["gate_result"]["status"] == "blocked"
    assert stage["gate_result"]["can_confirm"] is False
    assert "source_no_scenes" in {item["code"] for item in stage["gate_result"]["diagnostics"]}
    assert context.service.get_run(run_id)["revision"] == 3


def test_phase2_warning_gate_confirms_with_stable_unchecked_gate_and_enqueues_after_commit(
    phase2_context: Phase2Context,
) -> None:
    context = phase2_context
    created = _create_run(context)
    run_id = created["run"]["id"]
    warning_scene = {
        **_source_payload()["scenes"][0],
        "shootability": {
            "status": "needs_adaptation",
            "visible_action": "改为近景展示戒指",
            "required_asset_keys": [],
            "production_risks": ["动作需要简化"],
            "adaptation_note": "改为近景表现",
        },
    }
    saved = _save_draft(
        context,
        run_id,
        "0-source",
        1,
        _source_payload(scenes=[warning_scene]),
    )
    _set_stage_status(context, run_id, "0-source", "ready")

    confirmed = _confirm(context, run_id, "0-source", saved["run"]["revision"])

    source = next(stage for stage in confirmed["stages"] if stage["stage_key"] == "0-source")
    next_stage = next(
        stage for stage in confirmed["stages"] if stage["stage_key"] == "1-character-bios"
    )
    jobs = _jobs(context, run_id)
    assert source["status"] == "confirmed"
    assert source["gate_result"]["status"] == "warning"
    assert source["gate_result"]["checked_at"] is None
    assert next_stage["status"] == "queued"
    assert next_stage["input_signature"]
    assert len(jobs) == 1
    assert jobs[0]["workflow_stage_id"] == next_stage["id"]
    assert context.enqueued == [jobs[0]["id"]]
    assert confirmed["run"]["revision"] == 3
    assert confirmed["run"]["current_stage"] == "1-character-bios"

    with pytest.raises(ServiceError) as error:
        _confirm(context, run_id, "0-source", saved["run"]["revision"])
    assert error.value.status_code == 409


def test_phase2_barrier_confirms_character_and_rhythm_without_script_job(
    phase2_context: Phase2Context,
) -> None:
    context = phase2_context
    created = _create_run(context)
    run_id = created["run"]["id"]

    source_saved = _save_draft(context, run_id, "0-source", 1, _source_payload())
    _set_stage_status(context, run_id, "0-source", "ready")
    source_confirmed = _confirm(context, run_id, "0-source", source_saved["run"]["revision"])

    bios_saved = _save_draft(
        context,
        run_id,
        "1-character-bios",
        source_confirmed["run"]["revision"],
        _bios_payload(),
    )
    _set_stage_status(context, run_id, "1-character-bios", "ready")
    bios_confirmed = _confirm(context, run_id, "1-character-bios", bios_saved["run"]["revision"])

    rhythm_saved = _save_draft(
        context,
        run_id,
        "2-rhythm",
        bios_confirmed["run"]["revision"],
        _rhythm_payload(),
    )
    _set_stage_status(context, run_id, "2-rhythm", "ready")
    rhythm_confirmed = _confirm(context, run_id, "2-rhythm", rhythm_saved["run"]["revision"])

    jobs = _jobs(context, run_id)
    stage_by_id = {stage["id"]: stage["stage_key"] for stage in rhythm_confirmed["stages"]}
    assert {stage_by_id[item["workflow_stage_id"]] for item in jobs} == {
        "1-character-bios",
        "2-rhythm",
    }
    assert rhythm_confirmed["run"]["current_stage"] == "3-script"
    assert rhythm_confirmed["run"]["status"] == "ready"
    assert not any(
        stage["stage_key"] == "3-script" and stage["status"] == "queued"
        for stage in rhythm_confirmed["stages"]
    )


def test_phase2_character_confirmation_requires_confirmed_source(
    phase2_context: Phase2Context,
) -> None:
    context = phase2_context
    created = _create_run(context)

    with pytest.raises(ServiceError) as error:
        _confirm(context, created["run"]["id"], "1-character-bios", 1)

    assert error.value.status_code == 409
    assert error.value.detail == "阶段 '1-character-bios' 尚未满足前置确认: 0-source"


def test_phase2_rhythm_confirm_recalculates_blocking_gate_for_missing_timeline(
    phase2_context: Phase2Context,
) -> None:
    context = phase2_context
    created = _create_run(context)
    run_id = created["run"]["id"]

    source_saved = _save_draft(context, run_id, "0-source", 1, _source_payload())
    _set_stage_status(context, run_id, "0-source", "ready")
    source_confirmed = _confirm(context, run_id, "0-source", source_saved["run"]["revision"])
    bios_saved = _save_draft(
        context,
        run_id,
        "1-character-bios",
        source_confirmed["run"]["revision"],
        _bios_payload(),
    )
    _set_stage_status(context, run_id, "1-character-bios", "ready")
    bios_confirmed = _confirm(context, run_id, "1-character-bios", bios_saved["run"]["revision"])
    rhythm_saved = _save_draft(
        context,
        run_id,
        "2-rhythm",
        bios_confirmed["run"]["revision"],
        _rhythm_payload() | {"timeline": []},
    )
    _set_stage_status(context, run_id, "2-rhythm", "ready")

    with pytest.raises(ServiceError) as error:
        _confirm(context, run_id, "2-rhythm", rhythm_saved["run"]["revision"])

    assert error.value.status_code == 409
    rhythm = context.service.get_stage(run_id, "2-rhythm")
    assert rhythm["status"] == "ready"
    assert rhythm["gate_result"]["status"] == "blocked"
    assert "rhythm_empty_timeline" in {
        item["code"] for item in rhythm["gate_result"]["diagnostics"]
    }


def test_rhythm_budget_setting_is_persisted_on_save_and_rechecked_on_confirm(
    phase2_context: Phase2Context,
) -> None:
    context = phase2_context
    created = _create_run(context, strong_dynamic_max_events=1)
    run_id = created["run"]["id"]
    rhythm = _rhythm_payload()
    budget = rhythm["strong_dynamic_budget"]
    budget["max_events"] = 99
    budget["event_ids"].append("dynamic-2")
    budget["events"].append(
        {
            "event_id": "dynamic-2",
            "summary": "第二个强动态",
            "segment_id": "segment-1",
            "dynamic_level": 1.0,
            "visual_action": "主角再次挥击",
            "source_refs": [],
        }
    )

    saved = _save_draft(context, run_id, "2-rhythm", 1, rhythm)
    saved_stage = context.service.get_stage(run_id, "2-rhythm")
    assert saved_stage["draft_output"]["strong_dynamic_budget"]["max_events"] == 1
    assert saved_stage["gate_result"]["status"] == "blocked"

    _set_stage_status(context, run_id, "0-source", "confirmed")
    _set_stage_status(context, run_id, "1-character-bios", "confirmed")
    _set_stage_status(context, run_id, "2-rhythm", "ready")
    with pytest.raises(ServiceError) as error:
        _confirm(context, run_id, "2-rhythm", saved["run"]["revision"])

    assert error.value.status_code == 409
    confirmed_stage = context.service.get_stage(run_id, "2-rhythm")
    assert confirmed_stage["draft_output"]["strong_dynamic_budget"]["max_events"] == 1
    assert confirmed_stage["gate_result"]["status"] == "blocked"
    assert "rhythm_dynamic_budget_exceeded" in {
        item["code"] for item in confirmed_stage["gate_result"]["diagnostics"]
    }


def test_rhythm_save_and_confirm_persist_bounded_content_and_signature(
    phase2_context: Phase2Context,
) -> None:
    context = phase2_context
    created = _create_run(context, strong_dynamic_max_events=3)
    run_id = created["run"]["id"]
    rhythm = _rhythm_payload()
    budget = rhythm["strong_dynamic_budget"]
    budget["max_events"] = 99
    budget["event_ids"].append("dynamic-2")
    budget["events"].append(
        {
            "event_id": "dynamic-2",
            "summary": "第二个强动态",
            "segment_id": "segment-1",
            "dynamic_level": 1.0,
            "visual_action": "主角再次挥击",
            "source_refs": [],
        }
    )

    saved = _save_draft(context, run_id, "2-rhythm", 1, rhythm)
    saved_stage = context.service.get_stage(run_id, "2-rhythm")
    for field in ("normalized_output", "draft_output"):
        content = saved_stage[field]
        assert content["strong_dynamic_budget"]["max_events"] == 3
        expected_signature = calculate_signature(
            {key: value for key, value in content.items() if key != "artifact_meta"},
            content["artifact_meta"]["input_signature"],
        )
        assert content["artifact_meta"]["content_signature"] == expected_signature
    assert saved_stage["normalized_output"] == saved_stage["draft_output"]

    _set_stage_status(context, run_id, "0-source", "confirmed")
    _set_stage_status(context, run_id, "1-character-bios", "confirmed")
    _set_stage_status(context, run_id, "2-rhythm", "ready")
    _confirm(context, run_id, "2-rhythm", saved["run"]["revision"])
    confirmed_stage = context.service.get_stage(run_id, "2-rhythm")
    assert confirmed_stage["normalized_output"] == confirmed_stage["draft_output"]
    assert confirmed_stage["normalized_output"]["strong_dynamic_budget"]["max_events"] == 3


@pytest.mark.parametrize(
    ("stage_key", "confirmed_prerequisites"),
    [
        ("1-character-bios", ["0-source"]),
        ("2-rhythm", ["0-source", "1-character-bios"]),
    ],
)
def test_legacy_summary_is_blocked_for_typed_phase2_stages(
    phase2_context: Phase2Context,
    stage_key: str,
    confirmed_prerequisites: list[str],
) -> None:
    context = phase2_context
    created = _create_run(context)
    run_id = created["run"]["id"]
    for prerequisite in confirmed_prerequisites:
        _set_stage_status(context, run_id, prerequisite, "confirmed")

    saved = _save_draft(context, run_id, stage_key, 1, {"summary": "旧摘要"})
    _set_stage_status(context, run_id, stage_key, "ready")

    with pytest.raises(ServiceError) as error:
        _confirm(context, run_id, stage_key, saved["run"]["revision"])

    assert error.value.status_code == 409
    stage = context.service.get_stage(run_id, stage_key)
    assert stage["gate_result"]["status"] == "blocked"
    assert "legacy_summary_stage_unsupported" in {
        item["code"] for item in stage["gate_result"]["diagnostics"]
    }
    assert _jobs(context, run_id) == []


def test_script_confirmation_recalculates_typed_rhythm_and_duration_gate(
    phase2_context: Phase2Context,
) -> None:
    context = phase2_context
    created = _create_run(context)
    run_id = created["run"]["id"]

    source_saved = _save_draft(context, run_id, "0-source", 1, _source_payload())
    _set_stage_status(context, run_id, "0-source", "ready")
    source_confirmed = _confirm(context, run_id, "0-source", source_saved["run"]["revision"])
    bios_saved = _save_draft(
        context,
        run_id,
        "1-character-bios",
        source_confirmed["run"]["revision"],
        _bios_payload(),
    )
    _set_stage_status(context, run_id, "1-character-bios", "ready")
    bios_confirmed = _confirm(context, run_id, "1-character-bios", bios_saved["run"]["revision"])
    rhythm_saved = _save_draft(
        context,
        run_id,
        "2-rhythm",
        bios_confirmed["run"]["revision"],
        _rhythm_payload(),
    )
    _set_stage_status(context, run_id, "2-rhythm", "ready")
    rhythm_confirmed = _confirm(context, run_id, "2-rhythm", rhythm_saved["run"]["revision"])
    script_saved = _save_draft(
        context,
        run_id,
        "3-script",
        rhythm_confirmed["run"]["revision"],
        _script_payload(
            scenes=[
                {
                    **_script_payload()["scenes"][0],
                    "action": "主角转身离开",
                    "duration_sec": 20,
                }
            ],
            strong_dynamic_event_ids=[],
        ),
    )
    _set_stage_status(context, run_id, "3-script", "ready")

    with pytest.raises(ServiceError) as error:
        _confirm(context, run_id, "3-script", script_saved["run"]["revision"])

    assert error.value.status_code == 409
    assert error.value.detail == "阶段 3-script 尚未满足确认门禁"
    script_stage = context.service.get_stage(run_id, "3-script")
    assert script_stage["status"] == "ready"
    assert script_stage["gate_result"]["status"] == "blocked"
    codes = {item["code"] for item in script_stage["gate_result"]["diagnostics"]}
    assert "script_duration_mismatch" in codes
    assert "script_rhythm_duration_mismatch" in codes
    assert "script_rhythm_strong_dynamic_event_id_missing" in codes
    assert "script_rhythm_event_unfulfilled" in codes


def test_upstream_draft_change_stales_downstream_and_creates_new_signature_job(
    phase2_context: Phase2Context,
) -> None:
    context = phase2_context
    created = _create_run(context)
    run_id = created["run"]["id"]
    first_saved = _save_draft(context, run_id, "0-source", 1, _source_payload())
    _set_stage_status(context, run_id, "0-source", "ready")
    first_confirmed = _confirm(context, run_id, "0-source", first_saved["run"]["revision"])
    first_job = _jobs(context, run_id)[0]

    changed_saved = _save_draft(
        context,
        run_id,
        "0-source",
        first_confirmed["run"]["revision"],
        _source_payload(episode_logline="主角在旧屋中发现戒指并听见门外脚步声。"),
    )
    assert (
        next(
            stage for stage in changed_saved["stages"] if stage["stage_key"] == "1-character-bios"
        )["status"]
        == "stale"
    )
    _set_stage_status(context, run_id, "0-source", "ready")
    second_confirmed = _confirm(context, run_id, "0-source", changed_saved["run"]["revision"])

    jobs = _jobs(context, run_id)
    next_stage = next(
        stage for stage in second_confirmed["stages"] if stage["stage_key"] == "1-character-bios"
    )
    assert next_stage["status"] == "queued"
    assert len(jobs) == 2
    assert jobs[0]["id"] != jobs[1]["id"]
    assert jobs[0]["input_signature"] != jobs[1]["input_signature"]
    assert jobs[0]["status"] == "interrupted"
    assert first_job["input_signature"] != next_stage["input_signature"]
    assert context.enqueued == [jobs[0]["id"], jobs[1]["id"]]


@pytest.mark.parametrize("protected_status", ["cancelled", "confirmed"])
def test_phase2_confirmation_does_not_reactivate_protected_downstream_stage(
    phase2_context: Phase2Context,
    protected_status: str,
) -> None:
    context = phase2_context
    created = _create_run(context)
    run_id = created["run"]["id"]
    first_saved = _save_draft(context, run_id, "0-source", 1, _source_payload())
    _set_stage_status(context, run_id, "0-source", "ready")
    first_confirmed = _confirm(context, run_id, "0-source", first_saved["run"]["revision"])

    changed_saved = _save_draft(
        context,
        run_id,
        "0-source",
        first_confirmed["run"]["revision"],
        _source_payload(episode_logline="上游内容已修改。"),
    )
    _set_stage_status(context, run_id, "0-source", "ready")
    _set_stage_status(context, run_id, "1-character-bios", protected_status)

    confirmed = _confirm(context, run_id, "0-source", changed_saved["run"]["revision"])

    downstream = next(
        stage for stage in confirmed["stages"] if stage["stage_key"] == "1-character-bios"
    )
    assert downstream["status"] == protected_status
    assert len(_jobs(context, run_id)) == 1


def test_storyboard_confirm_rejects_shot_count_over_budget_hard_cap(
    phase2_context: Phase2Context,
) -> None:
    """注册在阶段 4：分镜镜头数超过预算硬上限时确认门禁必须阻断。"""
    import app.dynamic_pipeline.storyboard_stage as storyboard_stage

    context = phase2_context
    created = _create_run(context)
    run_id = created["run"]["id"]
    source_saved = _save_draft(context, run_id, "0-source", 1, _source_payload())
    _set_stage_status(context, run_id, "0-source", "ready")
    source_confirmed = _confirm(context, run_id, "0-source", source_saved["run"]["revision"])
    bios_saved = _save_draft(
        context,
        run_id,
        "1-character-bios",
        source_confirmed["run"]["revision"],
        _bios_payload(),
    )
    _set_stage_status(context, run_id, "1-character-bios", "ready")
    bios_confirmed = _confirm(context, run_id, "1-character-bios", bios_saved["run"]["revision"])
    rhythm_saved = _save_draft(
        context,
        run_id,
        "2-rhythm",
        bios_confirmed["run"]["revision"],
        _rhythm_payload(),
    )
    _set_stage_status(context, run_id, "2-rhythm", "ready")
    rhythm_confirmed = _confirm(context, run_id, "2-rhythm", rhythm_saved["run"]["revision"])

    script_payload = _script_payload()
    script_payload["scenes"] = [{**script_payload["scenes"][0], "duration_sec": 30}]
    script_saved = _save_draft(
        context,
        run_id,
        "3-script",
        rhythm_confirmed["run"]["revision"],
        script_payload,
    )
    _set_stage_status(context, run_id, "3-script", "ready")
    script_confirmed = _confirm(context, run_id, "3-script", script_saved["run"]["revision"])

    # 30 秒预算 → 建议区间 [2,4]，硬上限 4；构造 5 镜必须被阻断
    over_budget_shots = []
    for index in range(1, 6):
        over_budget_shots.append(
            storyboard_stage.StoryboardShot.model_validate(
                {
                    "external_key": f"storyboard:run-1:scene-1:shot-{index}:ordinal-{index}",
                    "shot_no": index,
                    "ordinal": index,
                    "scene_id": "scene-1",
                    "scene_no": 1,
                    "scene_name": "旧屋",
                    "scene_asset_id": "scene-asset-1",
                    "character_asset_ids": ["character-asset-1"],
                    "start_state": "主角站在旧屋中央",
                    "action": "主角举起戒指",
                    "end_state": "主角握紧戒指",
                    "shot_type": "中景",
                    "camera_angle": "平视",
                    "camera_move": "固定",
                    "duration_sec": 6,
                    "provider_duration_sec": 6,
                    "transition_from_prev": "continuous" if index > 1 else "scene_change",
                }
            )
        )
    storyboard_payload = storyboard_stage.merge_storyboard_batches(
        [
            storyboard_stage.StoryboardOutput(
                artifact_meta=storyboard_stage.ArtifactMeta.model_validate(
                    {
                        "schema_version": "dynamic-storyboard-v2",
                        "normalizer_version": "dynamic-normalizer-v1",
                        "input_signature": "storyboard-input",
                        "content_signature": "storyboard-content",
                        "source_attempt": 1,
                    }
                ),
                target_duration_sec=30,
                shots=over_budget_shots,
            )
        ],
        artifact_meta={
            "schema_version": "dynamic-storyboard-v2",
            "normalizer_version": "dynamic-normalizer-v1",
            "input_signature": "storyboard-input",
            "content_signature": "storyboard-content",
            "source_attempt": 1,
        },
        target_duration_sec=30,
    )
    storyboard_saved = _save_draft(
        context,
        run_id,
        "4-storyboard",
        script_confirmed["run"]["revision"],
        storyboard_payload.model_dump(mode="json"),
    )
    _set_stage_status(context, run_id, "4-storyboard", "ready")

    with pytest.raises(ServiceError) as error:
        _confirm(context, run_id, "4-storyboard", storyboard_saved["run"]["revision"])

    assert error.value.status_code == 409
    stage = context.service.get_stage(run_id, "4-storyboard")
    assert stage["gate_result"]["status"] == "blocked"
    codes = {item["code"] for item in stage["gate_result"]["diagnostics"]}
    assert "storyboard_shot_count_over_budget" in codes
