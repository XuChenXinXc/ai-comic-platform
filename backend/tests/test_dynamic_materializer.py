from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pytest

from app.dynamic_pipeline.materializer import (
    MaterializationConflict,
    MaterializationError,
    build_asset_materialization_plan,
    build_novel_analysis_projection,
    build_screenplay_projection,
    build_shot_materialization_plan,
    ensure_materialization_revision,
    extract_shot_asset_requirements,
    materialize_confirmed_stage,
)
from app.dynamic_pipeline.schemas import CharacterBiosOutput, SourceAnalysisOutput
from app.dynamic_pipeline.storyboard_stage import StoryboardOutput


def _meta(input_signature: str = "input-1") -> dict[str, Any]:
    return {
        "schema_version": "dynamic-test-v1",
        "normalizer_version": "dynamic-test-normalizer-v1",
        "input_signature": input_signature,
        "content_signature": "content-1",
        "source_attempt": 1,
    }


def _source() -> SourceAnalysisOutput:
    return SourceAnalysisOutput.model_validate(
        {
            "artifact_meta": _meta(),
            "metadata": {
                "source_kind": "full_text",
                "title": "雨夜反击",
                "chapter_title": "第一章",
                "source_signature": "source-1",
                "target_duration_sec": 6,
                "aspect_ratio": "9:16",
            },
            "episode_logline": "林默在雨夜完成第一次反击。",
            "scenes": [
                {
                    "scene_id": "scene-1",
                    "ordinal": 1,
                    "location": "废弃仓库",
                    "time_weather": "夜；暴雨",
                    "characters": [],
                    "narrative_task": "建立危机",
                    "plot_event": "反派发动袭击",
                    "conflict_or_turn": "林默被迫迎战",
                    "visual_focus": "刀锋逼近",
                    "estimated_duration_sec": 6,
                    "shootability": {
                        "status": "shootable",
                        "visible_action": "林默抬手格挡",
                        "required_asset_keys": [],
                        "production_risks": ["雨水连续性"],
                        "adaptation_note": None,
                    },
                    "source_refs": ["p1-l1"],
                }
            ],
            "character_roster": [
                {
                    "character_key": "hero",
                    "canonical_name": "林默",
                    "aliases": ["小林"],
                    "importance": "core",
                    "story_function": "冲突承受者",
                    "source_refs": ["p1-l1"],
                }
            ],
            "conflict_turns": [
                {
                    "kind": "conflict",
                    "summary": "反派发动袭击",
                    "scene_id": "scene-1",
                    "participants": ["林默", "反派"],
                    "source_refs": ["p1-l2"],
                }
            ],
            "visualizable_content": ["刀锋逼近"],
            "hard_to_visualize": ["林默的恐惧"],
            "rewrite_requirements": [],
            "assumptions": [],
            "open_questions": [],
            "continuation_outline": ["下一章揭示袭击者身份"],
        }
    )


def _bios() -> CharacterBiosOutput:
    return CharacterBiosOutput.model_validate(
        {
            "artifact_meta": _meta("bios-input-1"),
            "characters": [
                {
                    "basic_info": {
                        "character_key": "hero",
                        "canonical_name": "林默",
                        "aliases": ["小林"],
                        "importance": "core",
                        "identity": "年轻剑客",
                        "age": "二十岁",
                        "gender": "男",
                    },
                    "background": "曾是边城守卫。",
                    "personality_behavior_logic": "先观察，再迅速行动。",
                    "dynamic_comic_setting": "受伤时保持护心姿势。",
                    "plot_relation": "本集冲突的承受者。",
                    "visual_anchor": {
                        "visual_facts": ["左眉浅疤"],
                        "face": "窄脸，锐利眉眼",
                        "hair": "黑色短发",
                        "body": "清瘦有力",
                        "costume": "深灰风衣",
                        "accessories": ["银色耳钉"],
                        "immutable_features": ["左眉浅疤"],
                        "forbidden_changes": ["不得改为长发"],
                        "makeup_keyword_string": "窄脸|黑色短发|深灰风衣",
                        "variants": [],
                        "current_variant": None,
                        "approved_reference_ids": [],
                    },
                    "behavior_evidence": {
                        "trigger": "听见刀锋破空声",
                        "action": "抬手格挡并向前踏步",
                        "expression": "目光锁定来袭方向",
                        "voice": "短促吸气后低声回应",
                        "source_refs": ["scene-1"],
                    },
                }
            ],
            "ensemble_anchors": [],
            "unresolved_character_refs": [],
        }
    )


def _storyboard() -> StoryboardOutput:
    return StoryboardOutput.model_validate(
        {
            "artifact_meta": _meta("storyboard-input-1"),
            "target_duration_sec": 3,
            "shots": [
                {
                    "external_key": "storyboard:run-1:scene-1:shot-1",
                    "shot_no": 1,
                    "ordinal": 1,
                    "scene_id": "scene-1",
                    "scene_no": 1,
                    "scene_name": "废弃仓库",
                    "time_weather": "夜；暴雨",
                    "scene_asset_id": "scene-asset-1",
                    "character_names": ["林默"],
                    "character_asset_ids": ["asset-hero"],
                    "prop_names": ["长刀"],
                    "prop_asset_ids": ["asset-sword"],
                    "positions": [{"character": "林默", "position": "中央"}],
                    "expressions": [{"character": "林默", "expression": "紧张"}],
                    "start_state": "林默站在仓库中央",
                    "action": "林默抬手格挡",
                    "end_state": "林默保持格挡姿态",
                    "next_hook": "反派逼近",
                    "transition_from_prev": "scene_change",
                    "duration_sec": 3,
                    "provider_duration_sec": 3,
                    "dialogue": [
                        {
                            "dialogue_line_id": "line-1",
                            "speaker": "林默",
                            "text": "你终于来了。",
                        }
                    ],
                    "narration": "雷声压过脚步声。",
                    "sound_cues": [{"kind": "sfx", "description": "雷声"}],
                    "dynamic_level": 0.8,
                    "frame_description": "雨幕中的仓库中央",
                    "source_refs": ["p1-l1"],
                    "reference_bindings": [],
                    "prompt_intent": {},
                },
                {
                    "external_key": "storyboard:run-1:scene-1:shot-2",
                    "shot_no": 2,
                    "ordinal": 2,
                    "scene_id": "scene-1",
                    "scene_no": 1,
                    "scene_name": "废弃仓库",
                    "time_weather": "夜；暴雨",
                    "scene_asset_id": "scene-asset-1",
                    "character_names": ["林默"],
                    "character_asset_ids": ["asset-hero"],
                    "prop_names": [],
                    "prop_asset_ids": [],
                    "start_state": "林默保持格挡姿态",
                    "action": "林默后退一步",
                    "end_state": "林默停在门边",
                    "next_hook": "门外传来脚步",
                    "transition_from_prev": "same_scene_cut",
                    "duration_sec": 3,
                    "provider_duration_sec": 3,
                    "dialogue": [],
                    "narration": "",
                    "sound_cues": [],
                    "dynamic_level": 0.5,
                    "frame_description": "门边的林默",
                    "source_refs": ["p1-l2"],
                    "reference_bindings": [],
                    "prompt_intent": {},
                },
            ],
        }
    )


def test_source_projection_explicitly_maps_phase_two_fields() -> None:
    projection = build_novel_analysis_projection(
        _source(), chapter_id="chapter-1", source_signature="source-1", expected_revision=2
    )

    assert projection["summary"] == "林默在雨夜完成第一次反击。"
    assert projection["locations"] == ["废弃仓库"]
    assert projection["time_period"] == "夜；暴雨"
    assert projection["main_characters"] == ["林默", "反派"]
    assert projection["character_relations"][0]["from"] == "林默"
    assert projection["core_conflict"] == "反派发动袭击"
    assert projection["visualizable_content"] == ["刀锋逼近"]
    assert projection["production_risks"] == ["雨水连续性"]


def test_source_projection_missing_field_raises_blocking_diagnostic() -> None:
    raw = _source().model_dump(mode="json")
    raw.pop("episode_logline")

    with pytest.raises(MaterializationError) as error:
        build_novel_analysis_projection(
            raw, chapter_id="chapter-1", source_signature="source-1", expected_revision=1
        )

    assert any(item.level == "blocking" for item in error.value.diagnostics)
    assert any(item.code == "source_episode_logline_missing" for item in error.value.diagnostics)


def test_asset_plan_defaults_to_run_isolated_assets_and_keeps_same_names_separate() -> None:
    first = _bios()
    second = first.model_copy(deep=True)
    second.characters[0].basic_info.character_key = "hero-copy"
    second.characters[0].basic_info.canonical_name = "林默"

    first_plan = build_asset_materialization_plan(first, project_id="project-1", run_id="run-1")
    second_plan = build_asset_materialization_plan(second, project_id="project-1", run_id="run-2")

    assert first_plan[0]["operation"] == "create"
    assert first_plan[0]["asset_id"] == "dynamic-asset:run-1:hero"
    assert second_plan[0]["asset_id"] == "dynamic-asset:run-2:hero-copy"
    assert first_plan[0]["asset_id"] != second_plan[0]["asset_id"]
    assert json.loads(first_plan[0]["visual_anchor_json"])["asset_id"] == first_plan[0]["asset_id"]


def test_asset_plan_only_reuses_explicit_approved_existing_mapping() -> None:
    output = _bios()
    mapping = {
        "workflow_asset_id": "hero",
        "asset_id": "canonical-hero",
        "reuse_mode": "approved_existing",
        "locked_revision": 4,
    }
    plan = build_asset_materialization_plan(
        output, project_id="project-1", run_id="run-1", reuse_mapping=(mapping,)
    )

    assert plan[0]["operation"] == "update"
    assert plan[0]["asset_id"] == "canonical-hero"
    assert plan[0]["reuse_mode"] == "approved_existing"
    assert plan[0]["locked_revision"] == 4
    assert plan[0]["revision"] == 4


def test_asset_plan_name_fallback_mapping_records_warning_diagnostic() -> None:
    output = _bios()
    # 只提供裸 character_key（非完整 workflow_asset_id），命中名称回退路径。
    mapping = {
        "workflow_asset_id": "hero",
        "asset_id": "canonical-hero",
        "reuse_mode": "approved_existing",
        "locked_revision": 4,
    }
    plan = build_asset_materialization_plan(
        output, project_id="project-1", run_id="run-1", reuse_mapping=(mapping,)
    )

    fallback_warnings = [
        item for item in plan.diagnostics if item.code == "asset_mapping_name_fallback"
    ]
    assert fallback_warnings
    assert fallback_warnings[0].level == "warning"
    assert fallback_warnings[0].details["workflow_asset_id"] == "dynamic-asset:run-1:hero"
    assert fallback_warnings[0].details["character_key"] == "hero"
    # 回退仍然生效，但结果与显式完整 ID 匹配一致。
    assert plan[0]["operation"] == "update"
    assert plan[0]["asset_id"] == "canonical-hero"


def test_asset_plan_full_workflow_id_mapping_has_no_fallback_warning() -> None:
    output = _bios()
    mapping = {
        "workflow_asset_id": "dynamic-asset:run-1:hero",
        "asset_id": "canonical-hero",
        "reuse_mode": "approved_existing",
        "locked_revision": 4,
    }
    plan = build_asset_materialization_plan(
        output, project_id="project-1", run_id="run-1", reuse_mapping=(mapping,)
    )

    assert not [item for item in plan.diagnostics if item.code == "asset_mapping_name_fallback"]
    assert plan[0]["operation"] == "update"
    assert plan[0]["asset_id"] == "canonical-hero"


def test_screenplay_projection_is_deterministic_and_contains_own_signature() -> None:
    script = {
        "artifact_meta": _meta("script-input-1"),
        "title": "雨夜反击",
        "logline": "林默完成反击。",
        "target_duration_sec": 3,
        "scenes": [
            {
                "scene_id": "scene-1",
                "ordinal": 1,
                "scene_heading": "仓库",
                "time_weather": "夜",
                "location": "仓库",
                "characters": ["林默"],
                "objective": "建立危机",
                "action": "林默抬手格挡",
                "dialogue": [{"dialogue_line_id": "line-1", "speaker": "林默", "text": "接招。"}],
                "narration": [],
                "emotion": "紧张",
                "dynamic_level": 0.8,
                "sound_cues": ["雷声"],
                "subtitle_style": "白字黑边",
                "duration_sec": 3,
                "transition_to_next": "",
                "source_refs": [],
            }
        ],
    }
    first = build_screenplay_projection(
        script, chapter_id="chapter-1", source_signature="source-1", expected_revision=3
    )
    second = build_screenplay_projection(
        script, chapter_id="chapter-1", source_signature="source-1", expected_revision=3
    )

    assert first == second
    assert first["content"]["scenes"][0]["dialogue"] == [{"speaker": "林默", "text": "接招。"}]
    assert first["own_signature"]
    assert first["revision"] == 4


def test_shot_plan_is_chapter_isolated_preserves_keys_and_json_increment_fields() -> None:
    original = _storyboard()
    before = original.model_dump(mode="json")
    plan = build_shot_materialization_plan(
        original,
        chapter_id="chapter-2",
        project_id="project-1",
        source_signature="source-1",
        existing_shot_numbers=(2, 8),
    )

    assert [item["shot_no"] for item in plan] == [9, 10]
    assert [item["external_key"] for item in plan] == [
        "storyboard:run-1:scene-1:shot-1",
        "storyboard:run-1:scene-1:shot-2",
    ]
    for item in plan:
        assert json.loads(item["action_events_json"])
        assert "dialogue_cues_json" in item
        assert "sound_cues_json" in item
        assert "continuity_json" in item
        assert "pipeline_metadata_json" in item
        assert item["own_signature"]
        assert "media" not in item
    assert original.model_dump(mode="json") == before


def test_shot_plan_missing_explicit_asset_ids_is_blocking() -> None:
    shot = _storyboard().model_copy(deep=True)
    shot.shots[0].character_asset_ids = []

    with pytest.raises(MaterializationError) as error:
        build_shot_materialization_plan(
            shot,
            chapter_id="chapter-1",
            project_id="project-1",
            source_signature="source-1",
        )

    assert any(item.code == "shot_character_asset_ids_missing" for item in error.value.diagnostics)


def test_shot_asset_requirements_extracts_expected_types_without_database_access() -> None:
    plan = build_shot_materialization_plan(
        _storyboard(),
        chapter_id="chapter-1",
        project_id="project-1",
        source_signature="source-1",
    )

    requirements = extract_shot_asset_requirements(plan)

    assert {(item["asset_id"], item["expected_asset_type"]) for item in requirements} == {
        ("scene-asset-1", "scene"),
        ("asset-hero", "character"),
        ("asset-sword", "prop"),
    }


def test_stage_two_is_explicit_no_op_and_canonical_payload_has_provenance() -> None:
    result = materialize_confirmed_stage(
        "2-rhythm",
        {"anything": "is not materialized"},
        project_id="project-1",
        chapter_id="chapter-1",
        run_id="run-1",
        source_signature="source-1",
        expected_revision=2,
    )

    assert result.shot_payloads == []
    assert result.created_asset_ids == []
    assert result.canonical_payload == {}
    assert result.input_signature == "source-1"
    assert result.revision == 2
    assert result.diagnostics == []


def test_revision_conflict_is_reported_by_materialize_preflight() -> None:
    with pytest.raises(MaterializationConflict) as error:
        ensure_materialization_revision(actual_revision=7, expected_revision=6)

    assert error.value.expected_revision == 6
    assert error.value.actual_revision == 7
    assert error.value.diagnostics[0].level == "blocking"


def test_materialize_confirmed_stage_does_not_mutate_inputs() -> None:
    source = _source()
    original = deepcopy(source.model_dump(mode="json"))

    result = materialize_confirmed_stage(
        "0-source",
        source,
        project_id="project-1",
        chapter_id="chapter-1",
        run_id="run-1",
        source_signature="source-1",
        expected_revision=1,
    )

    assert result.canonical_payload["summary"] == source.episode_logline
    assert source.model_dump(mode="json") == original


def test_expected_revision_must_be_positive_for_projection_builders() -> None:
    with pytest.raises(MaterializationError):
        build_novel_analysis_projection(
            _source(), chapter_id="chapter-1", source_signature="source-1", expected_revision=0
        )
