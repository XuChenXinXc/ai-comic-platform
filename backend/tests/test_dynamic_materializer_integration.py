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
from app.dynamic_pipeline.schemas import (
    Diagnostic,
    StageConfirmRequest,
    StageDraftUpdate,
    StageGateResult,
    WorkflowRunCreate,
)
from app.models.tables import asset, novel_analysis, screenplay, shot, workflow_run, workflow_stage
from app.services.dynamic_workflow_service import DynamicWorkflowService
from app.services.errors import ServiceError
from app.services.file_service import FileService
from app.services.utils import json_dumps, utc_now


@dataclass
class MaterializerContext:
    database: Database
    service: DynamicWorkflowService


@pytest.fixture
def materializer_context(tmp_path: Path) -> Iterator[MaterializerContext]:
    database = Database(f"sqlite:///{(tmp_path / 'dynamic-materializer.db').as_posix()}")
    database.initialize(seed_mock_providers=True)
    service = DynamicWorkflowService(
        database,
        storage=FileService(tmp_path / "workspace", database=database),
    )
    try:
        yield MaterializerContext(database=database, service=service)
    finally:
        database.dispose()


def _meta(signature: str = "dynamic-input") -> dict[str, Any]:
    return {
        "schema_version": "dynamic-test-v1",
        "normalizer_version": "dynamic-test-normalizer-v1",
        "input_signature": signature,
        "content_signature": "content-signature",
        "source_attempt": 1,
    }


def _source(**overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "artifact_meta": _meta("source-input"),
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
    result.update(overrides)
    return result


def _bios(name: str = "主角", character_key: str = "hero") -> dict[str, Any]:
    return {
        "artifact_meta": _meta("bios-input"),
        "characters": [
            {
                "basic_info": {
                    "character_key": character_key,
                    "canonical_name": name,
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
                    "voice": "低声回应",
                    "source_refs": ["scene-1"],
                },
            }
        ],
        "ensemble_anchors": [],
        "unresolved_character_refs": [],
    }


def _rhythm() -> dict[str, Any]:
    event = {
        "event_id": "dynamic-1",
        "summary": "主角完成关键回应",
        "segment_id": "segment-1",
        "dynamic_level": 1.0,
        "visual_action": "主角举起戒指并作出决定",
        "source_refs": [],
    }
    return {
        "artifact_meta": _meta("rhythm-input"),
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
        "foreshadowing": [],
        "open_space": [],
        "dialogue_suggestions": [],
        "sound_suggestions": [],
        "assumptions": [],
    }


def _script() -> dict[str, Any]:
    return {
        "artifact_meta": _meta("script-input"),
        "title": "动态测试故事",
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
                "source_refs": [],
            }
        ],
        "strong_dynamic_event_ids": ["dynamic-1"],
        "assumptions": [],
        "open_questions": [],
    }


def _storyboard(
    *,
    external_key: str = "storyboard:dynamic:scene-1:shot-1",
    character_asset_id: str = "character-asset-1",
    scene_asset_id: str = "scene-asset-1",
) -> dict[str, Any]:
    return {
        "artifact_meta": _meta("storyboard-input"),
        "target_duration_sec": 5,
        "shots": [
            {
                "external_key": external_key,
                "shot_no": 1,
                "ordinal": 1,
                "scene_id": "scene-1",
                "scene_no": 1,
                "scene_name": "旧屋",
                "time_weather": "夜，阴",
                "scene_asset_id": scene_asset_id,
                "character_names": ["主角"],
                "character_asset_ids": [character_asset_id],
                "prop_names": [],
                "prop_asset_ids": [],
                "positions": [{"character": "主角", "position": "中央"}],
                "expressions": [{"character": "主角", "expression": "紧张"}],
                "start_state": "主角站在旧屋中央",
                "action": "主角举起戒指并作出决定",
                "end_state": "主角握紧戒指",
                "next_hook": "屋外传来脚步声",
                "transition_from_prev": "scene_change",
                "duration_sec": 5,
                "provider_duration_sec": 5,
                "shot_type": "中景",
                "camera_angle": "平视",
                "camera_move": "固定",
                "dialogue": [{"dialogue_line_id": "line-1", "speaker": "主角", "text": "我来了。"}],
                "narration": "屋外传来脚步声。",
                "sound_cues": [{"kind": "sfx", "description": "脚步声"}],
                "dynamic_level": 0.8,
                "dynamic_event_id": "dynamic-1",
                "frame_description": "旧屋中央的主角",
                "source_refs": ["source:1"],
                "reference_bindings": [{"role": "character", "asset_id": character_asset_id}],
                "prompt_intent": {"action_events": [{"action": "主角举起戒指"}]},
            }
        ],
        "scenes": [],
        "assumptions": [],
        "open_questions": [],
    }


def _ready_gate() -> StageGateResult:
    return StageGateResult(status="ready", passed=True, diagnostics=[], can_confirm=True)


def _blocked_gate() -> StageGateResult:
    return StageGateResult(
        status="blocked",
        passed=False,
        diagnostics=[Diagnostic(level="blocking", code="test_blocked", message="测试阻断")],
        can_confirm=False,
    )


def _create_run(context: MaterializerContext) -> dict[str, Any]:
    return context.service.create_run(
        WorkflowRunCreate(
            title="动态物化测试",
            chapter_title="第一章",
            synopsis="主角在旧屋中发现一枚发光的戒指。",
            target_duration_sec=30,
        )
    )


def _save(
    context: MaterializerContext,
    run_id: str,
    stage_key: str,
    expected_revision: int,
    output: dict[str, Any],
    gate: StageGateResult | None = None,
) -> dict[str, Any]:
    return context.service.save_stage_draft(
        run_id,
        stage_key,
        StageDraftUpdate(
            expected_revision=expected_revision,
            draft_output=output,
            gate_result=gate or _ready_gate(),
        ),
    )


def _ready(context: MaterializerContext, run_id: str, stage_key: str) -> None:
    with context.database.engine.begin() as connection:
        connection.execute(
            update(workflow_stage)
            .where(
                workflow_stage.c.workflow_run_id == run_id,
                workflow_stage.c.stage_key == stage_key,
            )
            .values(status="ready", updated_at=utc_now())
        )


def _confirm(
    context: MaterializerContext,
    run_id: str,
    stage_key: str,
    expected_revision: int,
) -> dict[str, Any]:
    return context.service.confirm_stage(
        run_id,
        stage_key,
        StageConfirmRequest(expected_revision=expected_revision),
    )


def _count(context: MaterializerContext, table: Any) -> int:
    with context.database.engine.connect() as connection:
        return int(connection.scalar(select(func.count()).select_from(table)) or 0)


def _record(
    context: MaterializerContext, table: Any, key: Any, value: Any
) -> dict[str, Any] | None:
    with context.database.engine.connect() as connection:
        row = connection.execute(select(table).where(key == value)).first()
    return dict(row._mapping) if row is not None else None


def _prepare_phase2(
    context: MaterializerContext,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    created = _create_run(context)
    run_id = created["run"]["id"]
    source_saved = _save(context, run_id, "0-source", 1, _source())
    _ready(context, run_id, "0-source")
    source_confirmed = _confirm(context, run_id, "0-source", source_saved["run"]["revision"])
    bios_saved = _save(
        context,
        run_id,
        "1-character-bios",
        source_confirmed["run"]["revision"],
        _bios(),
    )
    _ready(context, run_id, "1-character-bios")
    bios_confirmed = _confirm(context, run_id, "1-character-bios", bios_saved["run"]["revision"])
    rhythm_saved = _save(
        context,
        run_id,
        "2-rhythm",
        bios_confirmed["run"]["revision"],
        _rhythm(),
    )
    _ready(context, run_id, "2-rhythm")
    rhythm_confirmed = _confirm(context, run_id, "2-rhythm", rhythm_saved["run"]["revision"])
    return created, bios_confirmed, rhythm_confirmed


def _prepare_stage4_asset_case(
    context: MaterializerContext,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    created, _, rhythm_confirmed = _prepare_phase2(context)
    run_id = created["run"]["id"]
    script_saved = _save(
        context,
        run_id,
        "3-script",
        rhythm_confirmed["run"]["revision"],
        _script(),
    )
    _ready(context, run_id, "3-script")
    script_confirmed = _confirm(context, run_id, "3-script", script_saved["run"]["revision"])
    return created, script_confirmed, rhythm_confirmed["run"]["asset_mapping"][0]["asset_id"]


def _insert_asset_row(
    context: MaterializerContext,
    *,
    asset_id: str,
    project_id: str,
    asset_type: str,
    revision: int = 1,
) -> None:
    with context.database.engine.begin() as connection:
        connection.execute(
            insert(asset).values(
                id=asset_id,
                project_id=project_id,
                asset_type=asset_type,
                name=f"阶段二-{asset_id}",
                profile_json=json_dumps({"canonical_name": asset_id}),
                visual_anchor_json=json_dumps({}),
                importance="normal",
                status="confirmed",
                source_signature="asset-source",
                own_signature="asset-own",
                revision=revision,
                created_at=utc_now(),
                updated_at=utc_now(),
            )
        )


def test_stage0_confirmation_materializes_novel_analysis_and_rejects_revision_conflict(
    materializer_context: MaterializerContext,
) -> None:
    context = materializer_context
    created = _create_run(context)
    run_id = created["run"]["id"]
    saved = _save(context, run_id, "0-source", 1, _source())
    _ready(context, run_id, "0-source")

    confirmed = _confirm(context, run_id, "0-source", saved["run"]["revision"])
    analysis = _record(
        context, novel_analysis, novel_analysis.c.chapter_id, confirmed["run"]["chapter_id"]
    )
    assert analysis is not None
    assert analysis["status"] == "confirmed"
    assert analysis["generated_by"] == f"dynamic:{run_id}"
    assert json.loads(analysis["content_json"])["summary"] == _source()["episode_logline"]
    assert analysis["source_signature"] == created["run"]["input_signature"]

    with pytest.raises(ServiceError) as repeated:
        _confirm(context, run_id, "0-source", saved["run"]["revision"])
    assert repeated.value.status_code == 409

    with context.database.engine.begin() as connection:
        connection.execute(
            update(novel_analysis)
            .where(novel_analysis.c.chapter_id == confirmed["run"]["chapter_id"])
            .values(content_json=json_dumps({"summary": "用户内容"}), revision=9)
        )
        connection.execute(
            update(workflow_stage)
            .where(
                workflow_stage.c.workflow_run_id == run_id,
                workflow_stage.c.stage_key == "0-source",
            )
            .values(status="ready", draft_output_json=json_dumps(_source()))
        )
    with pytest.raises(ServiceError) as conflict_error:
        _confirm(context, run_id, "0-source", saved["run"]["revision"])
    assert conflict_error.value.status_code == 409
    preserved = _record(
        context, novel_analysis, novel_analysis.c.chapter_id, confirmed["run"]["chapter_id"]
    )
    assert preserved is not None
    assert json.loads(preserved["content_json"]) == {"summary": "用户内容"}
    assert preserved["revision"] == 9


def test_unconfirmed_or_blocked_phase_does_not_materialize_and_materialization_failure_rolls_back(
    materializer_context: MaterializerContext,
) -> None:
    context = materializer_context
    created = _create_run(context)
    run_id = created["run"]["id"]
    before = _count(context, novel_analysis)
    with pytest.raises(ServiceError):
        _confirm(context, run_id, "0-source", 1)
    assert _count(context, novel_analysis) == before

    saved = _save(context, run_id, "0-source", 2, _source(scenes=[]), _blocked_gate())
    _ready(context, run_id, "0-source")
    with pytest.raises(ServiceError):
        _confirm(context, run_id, "0-source", saved["run"]["revision"])
    assert _count(context, novel_analysis) == before
    assert context.service.get_stage(run_id, "0-source")["status"] == "ready"

    class FailingMaterializer:
        def build_novel_analysis_projection(self, *_: Any, **__: Any) -> dict[str, Any]:
            raise RuntimeError("materializer failure")

    # 注入异常 builder，确认事务应回滚 stage/run，而不删除原始草稿。
    failing_context = DynamicWorkflowService(
        context.database,
        storage=context.service.storage,
        materializer=FailingMaterializer(),
    )
    run = context.service.create_run(
        WorkflowRunCreate(
            title="失败回滚测试",
            chapter_title="第一章",
            synopsis="主角发现线索。",
        )
    )
    failed_run_id = run["run"]["id"]
    saved_failure = failing_context.save_stage_draft(
        failed_run_id,
        "0-source",
        StageDraftUpdate(expected_revision=1, draft_output=_source(), gate_result=_ready_gate()),
    )
    _ready(context, failed_run_id, "0-source")
    with pytest.raises(ServiceError):
        failing_context.confirm_stage(
            failed_run_id,
            "0-source",
            StageConfirmRequest(expected_revision=saved_failure["run"]["revision"]),
        )
    assert failing_context.get_stage(failed_run_id, "0-source")["status"] == "ready"
    assert failing_context.get_stage(failed_run_id, "0-source")["draft_output"] == _source()
    assert failing_context.get_run(failed_run_id)["revision"] == saved_failure["run"]["revision"]


def test_stage1_materialization_isolated_assets_and_explicit_approved_reuse(
    materializer_context: MaterializerContext,
) -> None:
    context = materializer_context
    created = _create_run(context)
    run_id = created["run"]["id"]
    with context.database.engine.begin() as connection:
        connection.execute(
            insert(asset).values(
                id="existing-same-name",
                project_id=created["run"]["project_id"],
                asset_type="character",
                name="主角",
                profile_json=json_dumps({"old": True}),
                visual_anchor_json=json_dumps({"old_anchor": True}),
                importance="normal",
                status="confirmed",
                source_signature="old-source",
                own_signature="old-own",
                revision=3,
                created_at=utc_now(),
                updated_at=utc_now(),
            )
        )
    saved = _save(context, run_id, "0-source", 1, _source())
    _ready(context, run_id, "0-source")
    source_confirmed = _confirm(context, run_id, "0-source", saved["run"]["revision"])
    bios_saved = _save(
        context, run_id, "1-character-bios", source_confirmed["run"]["revision"], _bios()
    )
    _ready(context, run_id, "1-character-bios")
    confirmed = _confirm(context, run_id, "1-character-bios", bios_saved["run"]["revision"])

    with context.database.engine.connect() as connection:
        assets = [dict(row._mapping) for row in connection.execute(select(asset)).all()]
    dynamic_assets = [item for item in assets if item["id"].startswith(f"dynamic-asset:{run_id}:")]
    assert len(dynamic_assets) == 1
    assert dynamic_assets[0]["id"] != "existing-same-name"
    assert dynamic_assets[0]["name"] != "主角"
    assert (
        json.loads(dynamic_assets[0]["visual_anchor_json"])["asset_id"] == dynamic_assets[0]["id"]
    )
    assert json.loads(dynamic_assets[0]["profile_json"])["basic_info"]["canonical_name"] == "主角"
    assert confirmed["run"]["asset_mapping"] == [
        {
            "workflow_asset_id": f"dynamic-asset:{run_id}:hero",
            "asset_id": dynamic_assets[0]["id"],
            "usage": "character",
            "locked_revision": None,
            "reuse_mode": "new",
        }
    ]

    reuse_run = context.service.create_run(
        WorkflowRunCreate(
            project_id=created["run"]["project_id"],
            title="动态复用测试",
            chapter_title="第二章",
            synopsis="主角在旧屋中再次发现线索。",
        )
    )
    reuse_id = reuse_run["run"]["id"]
    reuse_source = _save(context, reuse_id, "0-source", 1, _source())
    _ready(context, reuse_id, "0-source")
    reuse_source_confirmed = _confirm(
        context, reuse_id, "0-source", reuse_source["run"]["revision"]
    )
    mapping = [
        {
            "workflow_asset_id": f"dynamic-asset:{reuse_id}:hero",
            "asset_id": "existing-same-name",
            "usage": "character",
            "locked_revision": 3,
            "reuse_mode": "approved_existing",
        }
    ]
    with context.database.engine.begin() as connection:
        connection.execute(
            update(workflow_run)
            .where(workflow_run.c.id == reuse_id)
            .values(asset_mapping_json=json_dumps(mapping))
        )
    reuse_bios = _save(
        context,
        reuse_id,
        "1-character-bios",
        reuse_source_confirmed["run"]["revision"],
        _bios(),
    )
    _ready(context, reuse_id, "1-character-bios")
    _confirm(context, reuse_id, "1-character-bios", reuse_bios["run"]["revision"])
    reused = _record(context, asset, asset.c.id, "existing-same-name")
    assert reused is not None
    assert reused["revision"] == 4
    assert json.loads(reused["profile_json"])["basic_info"]["canonical_name"] == "主角"
    persisted_reuse_run = context.service.get_run(reuse_id)
    assert persisted_reuse_run["asset_mapping"][0]["locked_revision"] == 4

    reuse_rhythm_saved = _save(
        context,
        reuse_id,
        "2-rhythm",
        persisted_reuse_run["revision"],
        _rhythm(),
    )
    _ready(context, reuse_id, "2-rhythm")
    reuse_rhythm_confirmed = _confirm(
        context,
        reuse_id,
        "2-rhythm",
        reuse_rhythm_saved["run"]["revision"],
    )
    reuse_script_saved = _save(
        context,
        reuse_id,
        "3-script",
        reuse_rhythm_confirmed["run"]["revision"],
        _script(),
    )
    _ready(context, reuse_id, "3-script")
    reuse_script_confirmed = _confirm(
        context,
        reuse_id,
        "3-script",
        reuse_script_saved["run"]["revision"],
    )
    reuse_scene_id = f"reuse-scene:{reuse_id}"
    _insert_asset_row(
        context,
        asset_id=reuse_scene_id,
        project_id=created["run"]["project_id"],
        asset_type="scene",
    )
    reuse_storyboard_saved = _save(
        context,
        reuse_id,
        "4-storyboard",
        reuse_script_confirmed["run"]["revision"],
        _storyboard(character_asset_id="existing-same-name", scene_asset_id=reuse_scene_id),
    )
    _ready(context, reuse_id, "4-storyboard")
    reuse_storyboard_confirmed = _confirm(
        context,
        reuse_id,
        "4-storyboard",
        reuse_storyboard_saved["run"]["revision"],
    )

    assert reuse_storyboard_confirmed["run"]["current_stage"] == "5a-image-prompts"
    assert _count(context, shot) == 1


@pytest.mark.parametrize("existing_asset_type", ["scene", "prop"])
def test_stage1_approved_existing_rejects_non_character_asset_type(
    materializer_context: MaterializerContext,
    existing_asset_type: str,
) -> None:
    context = materializer_context
    created = _create_run(context)
    run_id = created["run"]["id"]
    existing_id = f"approved-{existing_asset_type}:{run_id}"
    _insert_asset_row(
        context,
        asset_id=existing_id,
        project_id=created["run"]["project_id"],
        asset_type=existing_asset_type,
        revision=3,
    )
    source_saved = _save(context, run_id, "0-source", 1, _source())
    _ready(context, run_id, "0-source")
    source_confirmed = _confirm(context, run_id, "0-source", source_saved["run"]["revision"])
    mapping = [
        {
            "workflow_asset_id": f"dynamic-asset:{run_id}:hero",
            "asset_id": existing_id,
            "usage": "character",
            "locked_revision": 3,
            "reuse_mode": "approved_existing",
        }
    ]
    with context.database.engine.begin() as connection:
        connection.execute(
            update(workflow_run)
            .where(workflow_run.c.id == run_id)
            .values(asset_mapping_json=json_dumps(mapping))
        )
    bios_saved = _save(
        context,
        run_id,
        "1-character-bios",
        source_confirmed["run"]["revision"],
        _bios(),
    )
    _ready(context, run_id, "1-character-bios")

    with pytest.raises(ServiceError) as error:
        _confirm(context, run_id, "1-character-bios", bios_saved["run"]["revision"])

    assert error.value.status_code == 400
    existing = _record(context, asset, asset.c.id, existing_id)
    assert existing is not None
    assert existing["asset_type"] == existing_asset_type
    assert existing["revision"] == 3
    assert context.service.get_stage(run_id, "1-character-bios")["status"] == "ready"


def test_stage2_is_noop_stage3_writes_screenplay_and_stage4_inserts_isolated_shots(
    materializer_context: MaterializerContext,
) -> None:
    context = materializer_context
    created, _, rhythm_confirmed = _prepare_phase2(context)
    run_id = created["run"]["id"]
    assert _count(context, screenplay) == 0
    assert _count(context, shot) == 0

    script_saved = _save(
        context, run_id, "3-script", rhythm_confirmed["run"]["revision"], _script()
    )
    _ready(context, run_id, "3-script")
    script_confirmed = _confirm(context, run_id, "3-script", script_saved["run"]["revision"])
    screenplay_record = _record(
        context,
        screenplay,
        screenplay.c.chapter_id,
        created["run"]["chapter_id"],
    )
    assert screenplay_record is not None
    assert screenplay_record["status"] == "confirmed"
    assert screenplay_record["own_signature"]
    assert _count(context, shot) == 0

    scene_asset = "scene-asset-1"
    character_asset = rhythm_confirmed["run"]["asset_mapping"][0]["asset_id"]
    with context.database.engine.begin() as connection:
        connection.execute(
            insert(asset).values(
                id=scene_asset,
                project_id=created["run"]["project_id"],
                asset_type="scene",
                name="旧屋",
                profile_json=json_dumps({}),
                visual_anchor_json=json_dumps({}),
                importance="normal",
                status="confirmed",
                source_signature="source",
                own_signature="own",
                revision=1,
                created_at=utc_now(),
                updated_at=utc_now(),
            )
        )
    storyboard_saved = _save(
        context,
        run_id,
        "4-storyboard",
        script_confirmed["run"]["revision"],
        _storyboard(character_asset_id=character_asset, scene_asset_id=scene_asset),
    )
    _ready(context, run_id, "4-storyboard")
    storyboard_confirmed = _confirm(
        context,
        run_id,
        "4-storyboard",
        storyboard_saved["run"]["revision"],
    )
    shot_record = _record(context, shot, shot.c.chapter_id, created["run"]["chapter_id"])
    assert shot_record is not None
    assert shot_record["source_type"] == "screenplay"
    assert shot_record["status"] == "confirmed"
    assert shot_record["source_signature"] == created["run"]["input_signature"]
    assert json.loads(shot_record["action_events_json"])
    assert json.loads(shot_record["dialogue_cues_json"])[0]["dialogue_line_id"] == "line-1"
    assert json.loads(shot_record["sound_cues_json"])[0]["description"] == "脚步声"
    assert json.loads(shot_record["continuity_json"])["transition_from_prev"] == "scene_change"
    assert json.loads(shot_record["pipeline_metadata_json"])["scene_id"] == "scene-1"
    assert json.loads(shot_record["reference_bindings_json"])[0]["asset_id"] == character_asset
    assert storyboard_confirmed["run"]["current_stage"] == "5a-image-prompts"


def test_stage4_duplicate_external_key_and_missing_asset_ids_roll_back(
    materializer_context: MaterializerContext,
) -> None:
    context = materializer_context
    created, _, rhythm_confirmed = _prepare_phase2(context)
    run_id = created["run"]["id"]
    script_saved = _save(
        context, run_id, "3-script", rhythm_confirmed["run"]["revision"], _script()
    )
    _ready(context, run_id, "3-script")
    script_confirmed = _confirm(context, run_id, "3-script", script_saved["run"]["revision"])
    character_asset = rhythm_confirmed["run"]["asset_mapping"][0]["asset_id"]
    storyboard = _storyboard(character_asset_id=character_asset)
    with context.database.engine.begin() as connection:
        connection.execute(
            insert(asset).values(
                id="scene-asset-1",
                project_id=created["run"]["project_id"],
                asset_type="scene",
                name="旧屋",
                profile_json=json_dumps({}),
                visual_anchor_json=json_dumps({}),
                importance="normal",
                status="confirmed",
                source_signature="source",
                own_signature="own",
                revision=1,
                created_at=utc_now(),
                updated_at=utc_now(),
            )
        )
    first = _save(
        context,
        run_id,
        "4-storyboard",
        script_confirmed["run"]["revision"],
        storyboard,
    )
    _ready(context, run_id, "4-storyboard")
    _confirm(context, run_id, "4-storyboard", first["run"]["revision"])
    shot_count = _count(context, shot)

    second_saved = _save(
        context,
        run_id,
        "4-storyboard",
        context.service.get_run(run_id)["revision"],
        _storyboard(character_asset_id=character_asset),
    )
    _ready(context, run_id, "4-storyboard")
    with pytest.raises(ServiceError) as duplicate_error:
        _confirm(context, run_id, "4-storyboard", second_saved["run"]["revision"])
    assert duplicate_error.value.status_code == 409
    assert _count(context, shot) == shot_count
    assert context.service.get_stage(run_id, "4-storyboard")["status"] == "ready"

    third_run = _create_run(context)
    third_id = third_run["run"]["id"]
    source_saved = _save(context, third_id, "0-source", 1, _source())
    _ready(context, third_id, "0-source")
    source_confirmed = _confirm(context, third_id, "0-source", source_saved["run"]["revision"])
    bios_saved = _save(
        context, third_id, "1-character-bios", source_confirmed["run"]["revision"], _bios()
    )
    _ready(context, third_id, "1-character-bios")
    bios_confirmed = _confirm(context, third_id, "1-character-bios", bios_saved["run"]["revision"])
    rhythm_saved = _save(
        context, third_id, "2-rhythm", bios_confirmed["run"]["revision"], _rhythm()
    )
    _ready(context, third_id, "2-rhythm")
    rhythm_confirmed = _confirm(context, third_id, "2-rhythm", rhythm_saved["run"]["revision"])
    script_saved = _save(
        context, third_id, "3-script", rhythm_confirmed["run"]["revision"], _script()
    )
    _ready(context, third_id, "3-script")
    script_confirmed = _confirm(context, third_id, "3-script", script_saved["run"]["revision"])
    missing_ids = _storyboard(character_asset_id="", scene_asset_id="scene-asset-1")
    missing_saved = _save(
        context,
        third_id,
        "4-storyboard",
        script_confirmed["run"]["revision"],
        missing_ids,
    )
    _ready(context, third_id, "4-storyboard")
    with pytest.raises(ServiceError) as missing_error:
        _confirm(context, third_id, "4-storyboard", missing_saved["run"]["revision"])
    assert missing_error.value.status_code == 400
    assert _count(context, shot) == shot_count
    assert context.service.get_stage(third_id, "4-storyboard")["status"] == "ready"


@pytest.mark.parametrize("invalid_kind", ["cross_project", "wrong_type", "missing"])
def test_stage4_asset_binding_validation_blocks_before_any_shot_insert(
    materializer_context: MaterializerContext,
    invalid_kind: str,
) -> None:
    context = materializer_context
    created, script_confirmed, character_asset = _prepare_stage4_asset_case(context)
    run_id = created["run"]["id"]
    scene_asset_id = f"stage4-scene-{invalid_kind}"
    scene_project_id = created["run"]["project_id"]
    if invalid_kind == "cross_project":
        other = _create_run(context)
        scene_project_id = other["run"]["project_id"]
    if invalid_kind != "missing":
        _insert_asset_row(
            context,
            asset_id=scene_asset_id,
            project_id=scene_project_id,
            asset_type="prop" if invalid_kind == "wrong_type" else "scene",
        )
    storyboard = _storyboard(character_asset_id=character_asset, scene_asset_id=scene_asset_id)
    saved = _save(
        context,
        run_id,
        "4-storyboard",
        script_confirmed["run"]["revision"],
        storyboard,
    )
    _ready(context, run_id, "4-storyboard")

    with pytest.raises(ServiceError) as error:
        _confirm(context, run_id, "4-storyboard", saved["run"]["revision"])

    assert error.value.status_code in {400, 409}
    assert _count(context, shot) == 0
    assert context.service.get_stage(run_id, "4-storyboard")["status"] == "ready"


def test_stage4_approved_existing_locked_revision_change_blocks_before_shot_insert(
    materializer_context: MaterializerContext,
) -> None:
    context = materializer_context
    created, script_confirmed, character_asset = _prepare_stage4_asset_case(context)
    run_id = created["run"]["id"]
    scene_asset_id = f"stage4-scene-locked:{run_id}"
    _insert_asset_row(
        context,
        asset_id=scene_asset_id,
        project_id=created["run"]["project_id"],
        asset_type="scene",
    )
    mapping = [
        {
            "workflow_asset_id": "hero",
            "asset_id": character_asset,
            "usage": "character",
            "locked_revision": 1,
            "reuse_mode": "approved_existing",
        }
    ]
    with context.database.engine.begin() as connection:
        connection.execute(
            update(workflow_run)
            .where(workflow_run.c.id == run_id)
            .values(asset_mapping_json=json_dumps(mapping))
        )
        connection.execute(
            update(asset).where(asset.c.id == character_asset).values(revision=2)
        )
    saved = _save(
        context,
        run_id,
        "4-storyboard",
        script_confirmed["run"]["revision"],
        _storyboard(character_asset_id=character_asset, scene_asset_id=scene_asset_id),
    )
    _ready(context, run_id, "4-storyboard")

    with pytest.raises(ServiceError) as error:
        _confirm(context, run_id, "4-storyboard", saved["run"]["revision"])

    assert error.value.status_code == 409
    assert _count(context, shot) == 0


def test_stage4_revision_change_after_binding_validation_rolls_back_all_shots(
    materializer_context: MaterializerContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = materializer_context
    created, script_confirmed, character_asset = _prepare_stage4_asset_case(context)
    run_id = created["run"]["id"]
    scene_asset_id = f"stage4-scene-race:{run_id}"
    _insert_asset_row(
        context,
        asset_id=scene_asset_id,
        project_id=created["run"]["project_id"],
        asset_type="scene",
    )
    storyboard = _storyboard(character_asset_id=character_asset, scene_asset_id=scene_asset_id)
    saved = _save(
        context,
        run_id,
        "4-storyboard",
        script_confirmed["run"]["revision"],
        storyboard,
    )
    _ready(context, run_id, "4-storyboard")

    validate = context.service._validate_shot_asset_bindings

    def validate_then_change(connection: Any, run: Any, plan: Any) -> list[dict[str, Any]]:
        snapshots = validate(connection, run, plan)
        connection.execute(
            update(asset).where(asset.c.id == character_asset).values(revision=2)
        )
        return snapshots

    monkeypatch.setattr(context.service, "_validate_shot_asset_bindings", validate_then_change)
    with pytest.raises(ServiceError) as error:
        _confirm(context, run_id, "4-storyboard", saved["run"]["revision"])

    assert error.value.status_code == 409
    assert _count(context, shot) == 0
    assert context.service.get_stage(run_id, "4-storyboard")["status"] == "ready"


def test_stage4_multiple_shots_validate_all_assets_before_partial_write(
    materializer_context: MaterializerContext,
) -> None:
    context = materializer_context
    created, script_confirmed, character_asset = _prepare_stage4_asset_case(context)
    run_id = created["run"]["id"]
    scene_asset_id = f"stage4-scene-multi:{run_id}"
    _insert_asset_row(
        context,
        asset_id=scene_asset_id,
        project_id=created["run"]["project_id"],
        asset_type="scene",
    )
    storyboard = _storyboard(character_asset_id=character_asset, scene_asset_id=scene_asset_id)
    second = deepcopy(storyboard["shots"][0])
    second.update(
        {
            "external_key": f"{second['external_key']}:2",
            "shot_no": 2,
            "ordinal": 2,
            "character_asset_ids": ["missing-character"],
        }
    )
    storyboard["shots"].append(second)
    saved = _save(
        context,
        run_id,
        "4-storyboard",
        script_confirmed["run"]["revision"],
        storyboard,
    )
    _ready(context, run_id, "4-storyboard")

    with pytest.raises(ServiceError):
        _confirm(context, run_id, "4-storyboard", saved["run"]["revision"])

    assert _count(context, shot) == 0


def test_legacy_project_and_chapter_rows_remain_unchanged_by_dynamic_service(
    materializer_context: MaterializerContext,
) -> None:
    context = materializer_context
    project = context.service._get_free_project(_create_run(context)["run"]["project_id"])
    assert project["is_free_create"] == 1
    chapter_id = context.service.get_run(context.service.list_runs(project["id"])[0]["id"])[
        "chapter_id"
    ]
    with context.database.engine.connect() as connection:
        chapter_record = connection.execute(
            select(workflow_run.c.chapter_id).where(workflow_run.c.chapter_id == chapter_id)
        ).first()
    assert chapter_record is not None


def test_dynamic_stage2_does_not_create_any_canonical_media_rows(
    materializer_context: MaterializerContext,
) -> None:
    context = materializer_context
    created, _, rhythm_confirmed = _prepare_phase2(context)
    assert rhythm_confirmed["run"]["current_stage"] == "3-script"
    assert _count(context, novel_analysis) == 1
    assert _count(context, asset) == 1
    assert _count(context, screenplay) == 0
    assert _count(context, shot) == 0
