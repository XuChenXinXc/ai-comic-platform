from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from app.dynamic_pipeline.schemas import (
    ArtifactMeta,
    CharacterBiosOutput,
    RhythmOutput,
    SourceAnalysisOutput,
    StageArtifactEnvelope,
)
from app.dynamic_pipeline.stage_gates import (
    GATE_VERSION,
    GateInputError,
    evaluate_character_bios_gate,
    evaluate_rhythm_gate,
    evaluate_source_gate,
    evaluate_stage_gate,
)


def _meta() -> dict[str, Any]:
    return {
        "schema_version": "dynamic-test-v1",
        "normalizer_version": "dynamic-normalizer-v1",
        "input_signature": "input-1",
        "content_signature": "content-1",
        "source_attempt": 1,
    }


def _source_scene(**overrides: Any) -> dict[str, Any]:
    scene = {
        "scene_id": "scene-1",
        "ordinal": 1,
        "location": "废弃仓库",
        "time_weather": "夜，暴雨",
        "characters": [],
        "narrative_task": "建立危机",
        "plot_event": "敌人发动袭击",
        "conflict_or_turn": "主角被迫迎战",
        "visual_focus": "刀锋逼近主角",
        "estimated_duration_sec": 30.0,
        "shootability": {
            "status": "shootable",
            "visible_action": "主角抬手格挡",
            "required_asset_keys": [],
            "production_risks": [],
            "adaptation_note": None,
        },
        "source_refs": [],
    }
    scene.update(overrides)
    return scene


def _source_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "artifact_meta": _meta(),
        "metadata": {
            "source_kind": "full_text",
            "title": "雨夜反击",
            "chapter_title": "第一章",
            "source_signature": "source-1",
            "target_duration_sec": 30.0,
            "aspect_ratio": "9:16",
        },
        "episode_logline": "主角在雨夜完成第一次反击。",
        "scenes": [_source_scene()],
        "character_roster": [],
        "conflict_turns": [],
        "visualizable_content": ["雨夜仓库"],
        "hard_to_visualize": [],
        "rewrite_requirements": [],
        "assumptions": [],
        "open_questions": [],
        "continuation_outline": [],
    }
    payload.update(overrides)
    return payload


def _visual_anchor(**overrides: Any) -> dict[str, Any]:
    anchor = {
        "visual_facts": ["左眉浅疤"],
        "face": "窄脸，锐利眉眼",
        "hair": "黑色短发",
        "body": "清瘦有力量感",
        "costume": "深灰风衣",
        "accessories": ["银色耳钉"],
        "immutable_features": ["左眉浅疤"],
        "forbidden_changes": ["不得改为长发"],
        "makeup_keyword_string": "窄脸|黑色短发|深灰风衣",
        "variants": [],
        "current_variant": None,
        "approved_reference_ids": ["ref-hero-1"],
    }
    anchor.update(overrides)
    return anchor


def _character(**overrides: Any) -> dict[str, Any]:
    character = {
        "basic_info": {
            "character_key": "hero",
            "canonical_name": "林默",
            "aliases": [],
            "importance": "core",
            "identity": "年轻剑客",
            "age": "二十岁",
            "gender": "男",
        },
        "background": "曾是边城守卫。",
        "personality_behavior_logic": "谨慎观察后迅速行动。",
        "dynamic_comic_setting": "受伤时仍保持护心姿势。",
        "plot_relation": "本集冲突的承受者与反击者。",
        "visual_anchor": _visual_anchor(),
        "behavior_evidence": {
            "trigger": "听见刀锋破空声",
            "action": "抬手格挡并向前踏步",
            "expression": "眉头收紧，目光锁定来袭方向",
            "voice": "短促吸气后用低声回应",
            "source_refs": ["scene-1"],
        },
    }
    character.update(overrides)
    return character


def _bios_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "artifact_meta": _meta(),
        "characters": [_character()],
        "ensemble_anchors": [],
        "unresolved_character_refs": [],
    }
    payload.update(overrides)
    return payload


def _rhythm_segment(**overrides: Any) -> dict[str, Any]:
    segment = {
        "segment_id": "segment-1",
        "ordinal": 1,
        "start_sec": 0.0,
        "end_sec": 30.0,
        "beat_type": "payoff",
        "narrative_task": "建立危机并完成反击",
        "event_summary": "刀锋逼近后被弹开",
        "visual_action": "主角抬手格挡并稳住身形",
        "focus_characters": ["hero"],
        "dynamic_level": 0.8,
        "strong_dynamic_event_id": "dynamic-1",
        "source_refs": [],
    }
    segment.update(overrides)
    return segment


def _rhythm_payload(**overrides: Any) -> dict[str, Any]:
    event = {
        "event_id": "dynamic-1",
        "summary": "主角完成第一次反击",
        "segment_id": "segment-1",
        "dynamic_level": 1.0,
        "visual_action": "刀锋被弹开",
        "source_refs": [],
    }
    payload: dict[str, Any] = {
        "target_duration_sec": 30.0,
        "artifact_meta": _meta(),
        "timeline": [_rhythm_segment()],
        "core_payoff": "主角完成第一次反击",
        "small_beats": [],
        "strong_dynamic_budget": {
            "max_events": 1,
            "event_ids": ["dynamic-1"],
            "events": [event],
        },
        "foreshadowing": ["敌人认出主角身份"],
        "open_space": ["保留下一集揭示身份的空间"],
        "dialogue_suggestions": [],
        "sound_suggestions": [],
        "assumptions": ["反派身份沿用章节设定"],
    }
    payload.update(overrides)
    return payload


def test_gate_version_is_stable() -> None:
    assert GATE_VERSION == "dynamic-gate-v1"


def test_three_phase_happy_paths_are_ready() -> None:
    assert (
        evaluate_source_gate(SourceAnalysisOutput.model_validate(_source_payload())).status
        == "ready"
    )
    assert (
        evaluate_character_bios_gate(CharacterBiosOutput.model_validate(_bios_payload())).status
        == "ready"
    )
    assert (
        evaluate_rhythm_gate(RhythmOutput.model_validate(_rhythm_payload()), 30).status == "ready"
    )


def test_stage_gate_extracts_envelope_and_ignores_client_gate_without_mutating_content() -> None:
    payload = _source_payload(gate_result={"status": "ready", "passed": True})
    original = deepcopy(payload)

    result = evaluate_stage_gate("0-source", {"content": payload}, now="fixed-now")

    assert result.status == "ready"
    assert result.checked_at == "fixed-now"
    assert payload == original


def test_stage_gate_accepts_typed_artifact_envelope() -> None:
    envelope = StageArtifactEnvelope(
        meta=ArtifactMeta.model_validate(_meta()),
        content=SourceAnalysisOutput.model_validate(_source_payload()),
    )

    result = evaluate_stage_gate("0-source", envelope)

    assert result.status == "ready"
    assert result.checked_at is None


def test_source_without_scenes_is_blocked() -> None:
    result = evaluate_stage_gate("0-source", _source_payload(scenes=[]))

    assert result.status == "blocked"
    assert result.passed is False
    assert result.can_confirm is False
    assert "source_no_scenes" in {diagnostic.code for diagnostic in result.diagnostics}


def test_source_unshootable_without_adaptation_is_blocked() -> None:
    scene = _source_scene(
        shootability={
            "status": "unshootable",
            "visible_action": "无法呈现",
            "production_risks": [],
            "adaptation_note": None,
        }
    )
    result = evaluate_stage_gate("0-source", _source_payload(scenes=[scene]))

    assert result.status == "blocked"
    assert "source_unshootable_without_adaptation" in {
        diagnostic.code for diagnostic in result.diagnostics
    }


def test_source_needs_adaptation_and_optional_findings_are_warnings() -> None:
    scene = _source_scene(
        shootability={
            "status": "needs_adaptation",
            "visible_action": "需要改写后呈现",
            "production_risks": ["群演规模较大"],
            "adaptation_note": "改为近景表现",
        }
    )
    result = evaluate_stage_gate(
        "0-source",
        _source_payload(
            scenes=[scene],
            hard_to_visualize=["抽象梦境"],
            open_questions=["敌人身份待确认"],
            assumptions=["默认沿用上一集设定"],
            continuation_outline=["下一集揭示真相"],
        ),
    )

    assert result.status == "warning"
    assert result.passed is True
    assert result.can_confirm is True
    assert all(diagnostic.level == "warning" for diagnostic in result.diagnostics)


def test_source_duplicate_scene_ids_and_ordinals_are_blocking() -> None:
    duplicate = _source_scene(scene_id="scene-1", ordinal=1)
    result = evaluate_stage_gate("0-source", _source_payload(scenes=[_source_scene(), duplicate]))

    codes = {diagnostic.code for diagnostic in result.diagnostics}
    assert result.status == "blocked"
    assert "source_duplicate_scene_id" in codes
    assert "source_duplicate_scene_ordinal" in codes


def test_source_missing_required_model_input_raises_gate_input_error() -> None:
    with pytest.raises(GateInputError):
        evaluate_stage_gate("0-source", _source_payload() | {"episode_logline": None})


def test_character_core_missing_immutable_and_forbidden_features_is_blocked() -> None:
    character = _character(
        visual_anchor=_visual_anchor(immutable_features=[], forbidden_changes=[])
    )
    result = evaluate_stage_gate("1-character-bios", _bios_payload(characters=[character]))

    codes = {diagnostic.code for diagnostic in result.diagnostics}
    assert result.status == "blocked"
    assert "character_core_immutable_features_empty" in codes
    assert "character_core_forbidden_changes_empty" in codes


def test_character_core_missing_behavior_evidence_is_blocked() -> None:
    result = evaluate_stage_gate(
        "1-character-bios",
        _bios_payload(
            characters=[
                _character(
                    behavior_evidence={
                        "trigger": "",
                        "action": "",
                        "expression": "",
                        "voice": "",
                    }
                )
            ]
        ),
    )

    codes = {diagnostic.code for diagnostic in result.diagnostics}
    assert result.status == "blocked"
    assert {
        "character_core_behavior_evidence_trigger_empty",
        "character_core_behavior_evidence_action_empty",
        "character_core_behavior_evidence_expression_empty",
        "character_core_behavior_evidence_voice_empty",
    }.issubset(codes)


def test_character_without_approved_reference_is_warning_and_confirmable() -> None:
    result = evaluate_stage_gate(
        "1-character-bios",
        _bios_payload(
            characters=[_character(visual_anchor=_visual_anchor(approved_reference_ids=[]))]
        ),
    )

    assert result.status == "warning"
    assert result.passed is True
    assert result.can_confirm is True
    assert all(diagnostic.level == "warning" for diagnostic in result.diagnostics)


def test_character_duplicate_key_and_ensemble_key_are_blocking() -> None:
    duplicate_character = _character(
        basic_info=_character()["basic_info"] | {"canonical_name": "另一个林默"}
    )
    result = evaluate_stage_gate(
        "1-character-bios",
        _bios_payload(
            characters=[_character(), duplicate_character],
            ensemble_anchors=[
                {"ensemble_key": "guards", "canonical_name": "守卫群"},
                {"ensemble_key": "guards", "canonical_name": "另一组守卫"},
            ],
        ),
    )

    codes = {diagnostic.code for diagnostic in result.diagnostics}
    assert result.status == "blocked"
    assert "character_duplicate_character_key" in codes
    assert "character_duplicate_ensemble_key" in codes


def test_character_empty_collection_is_blocked() -> None:
    result = evaluate_stage_gate("1-character-bios", _bios_payload(characters=[]))

    assert result.status == "blocked"
    assert "character_no_characters" in {diagnostic.code for diagnostic in result.diagnostics}


def test_rhythm_accepts_one_thirty_second_segment_without_legacy_minimum() -> None:
    result = evaluate_stage_gate("2-rhythm", _rhythm_payload(), target_duration_sec=30)

    assert result.status == "ready"
    assert result.passed is True


@pytest.mark.parametrize(
    "timeline",
    [
        pytest.param([_rhythm_segment(start_sec=1.0)], id="non-zero-start"),
        pytest.param(
            [
                _rhythm_segment(end_sec=10.0),
                _rhythm_segment(segment_id="segment-2", ordinal=2, start_sec=11.0),
            ],
            id="gap",
        ),
        pytest.param(
            [
                _rhythm_segment(end_sec=10.0),
                _rhythm_segment(segment_id="segment-2", ordinal=2, start_sec=9.0),
            ],
            id="overlap",
        ),
    ],
)
def test_rhythm_invalid_timeline_is_blocked(timeline: list[dict[str, Any]]) -> None:
    result = evaluate_stage_gate(
        "2-rhythm", _rhythm_payload(timeline=timeline), target_duration_sec=30
    )

    assert result.status == "blocked"
    assert any(diagnostic.level == "blocking" for diagnostic in result.diagnostics)


def test_rhythm_end_not_equal_to_target_is_blocked() -> None:
    result = evaluate_stage_gate(
        "2-rhythm",
        _rhythm_payload(timeline=[_rhythm_segment(end_sec=20.0)]),
        target_duration_sec=30,
    )

    assert result.status == "blocked"
    assert "rhythm_timeline_end_mismatch" in {diagnostic.code for diagnostic in result.diagnostics}


def test_rhythm_duplicate_segment_id_is_blocked() -> None:
    timeline = [
        _rhythm_segment(end_sec=15.0),
        _rhythm_segment(segment_id="segment-1", ordinal=2, start_sec=15.0),
    ]
    result = evaluate_stage_gate(
        "2-rhythm", _rhythm_payload(timeline=timeline), target_duration_sec=30
    )

    assert result.status == "blocked"
    assert "rhythm_duplicate_segment_id" in {diagnostic.code for diagnostic in result.diagnostics}


def test_rhythm_strong_dynamic_budget_overflow_is_blocked() -> None:
    event = {
        "event_id": "dynamic-2",
        "summary": "第二个强动态",
        "segment_id": "segment-1",
        "dynamic_level": 1.0,
        "visual_action": "再次挥击",
        "source_refs": [],
    }
    budget = _rhythm_payload()["strong_dynamic_budget"]
    budget["event_ids"].append("dynamic-2")
    budget["events"].append(event)
    result = evaluate_stage_gate(
        "2-rhythm",
        _rhythm_payload(strong_dynamic_budget=budget),
        target_duration_sec=30,
        config_snapshot={"strong_dynamic_max_events": 1},
    )

    assert result.status == "blocked"
    assert "rhythm_dynamic_budget_exceeded" in {
        diagnostic.code for diagnostic in result.diagnostics
    }


def test_rhythm_model_max_events_cannot_expand_server_budget() -> None:
    event = {
        "event_id": "dynamic-2",
        "summary": "第二个强动态",
        "segment_id": "segment-1",
        "dynamic_level": 1.0,
        "visual_action": "再次挥击",
        "source_refs": [],
    }
    budget = _rhythm_payload()["strong_dynamic_budget"]
    budget["max_events"] = 99
    budget["event_ids"].append("dynamic-2")
    budget["events"].append(event)
    result = evaluate_stage_gate(
        "2-rhythm",
        _rhythm_payload(strong_dynamic_budget=budget),
        target_duration_sec=30,
    )

    assert result.status == "ready"


def test_rhythm_longer_target_requires_small_beats() -> None:
    result = evaluate_stage_gate(
        "2-rhythm",
        _rhythm_payload(target_duration_sec=60),
        target_duration_sec=60,
    )

    assert result.status == "blocked"
    assert "rhythm_small_beats_insufficient" in {
        diagnostic.code for diagnostic in result.diagnostics
    }


def test_rhythm_target_mismatch_is_blocked() -> None:
    result = evaluate_stage_gate("2-rhythm", _rhythm_payload(), target_duration_sec=31)

    assert result.status == "blocked"
    assert "rhythm_target_duration_mismatch" in {
        diagnostic.code for diagnostic in result.diagnostics
    }


def test_rhythm_out_of_range_cue_is_warning_not_blocking() -> None:
    result = evaluate_stage_gate(
        "2-rhythm",
        _rhythm_payload(
            dialogue_suggestions=[
                {"segment_id": "missing-segment", "speaker": "林默", "text": "等等！"}
            ],
            sound_suggestions=[
                {
                    "segment_id": "missing-segment",
                    "kind": "impact",
                    "description": "金属碰撞声",
                }
            ],
        ),
        target_duration_sec=30,
    )

    assert result.status == "warning"
    assert result.passed is True
    assert result.can_confirm is True
    assert all(diagnostic.level == "warning" for diagnostic in result.diagnostics)


def test_warning_is_confirmable_but_blocking_is_not() -> None:
    warning = evaluate_stage_gate(
        "1-character-bios",
        _bios_payload(
            characters=[_character(visual_anchor=_visual_anchor(approved_reference_ids=[]))]
        ),
    )
    blocking = evaluate_stage_gate("1-character-bios", _bios_payload(characters=[]))

    assert warning.passed is True and warning.can_confirm is True
    assert blocking.passed is False and blocking.can_confirm is False


def test_gate_is_pure_and_diagnostics_have_stable_order() -> None:
    payload = _source_payload(
        scenes=[
            _source_scene(
                shootability={
                    "status": "needs_adaptation",
                    "visible_action": "改写动作",
                    "production_risks": ["风险"],
                    "adaptation_note": "改为近景",
                }
            )
        ],
        hard_to_visualize=["抽象内容"],
        assumptions=["假设"],
    )
    original = deepcopy(payload)

    first = evaluate_stage_gate("0-source", payload)
    second = evaluate_stage_gate("0-source", payload)

    assert first.model_dump() == second.model_dump()
    assert payload == original
    assert [
        (diagnostic.code, diagnostic.path, diagnostic.message) for diagnostic in first.diagnostics
    ] == sorted(
        (diagnostic.code, diagnostic.path or "", diagnostic.message)
        for diagnostic in first.diagnostics
    )
    assert first.checked_at is None


@pytest.mark.parametrize(
    ("stage_key", "content", "kwargs"),
    [
        pytest.param("3-script", {}, {}, id="unknown-stage"),
        pytest.param("0-source", {"content": {"not_source": True}}, {}, id="invalid-content"),
        pytest.param("0-source", _bios_payload(), {}, id="wrong-model"),
        pytest.param("2-rhythm", _rhythm_payload(), {}, id="missing-target"),
    ],
)
def test_unknown_or_illegal_gate_input_raises_gate_input_error(
    stage_key: str,
    content: Any,
    kwargs: dict[str, Any],
) -> None:
    with pytest.raises(GateInputError):
        evaluate_stage_gate(stage_key, content, **kwargs)


def test_wrong_declared_stage_is_rejected() -> None:
    with pytest.raises(GateInputError):
        evaluate_stage_gate("0-source", {"stage_key": "2-rhythm", "content": _source_payload()})


def test_rhythm_non_positive_target_is_blocked() -> None:
    result = evaluate_stage_gate("2-rhythm", _rhythm_payload(), target_duration_sec=0)

    assert result.status == "blocked"
    assert "rhythm_invalid_target_duration" in {
        diagnostic.code for diagnostic in result.diagnostics
    }
