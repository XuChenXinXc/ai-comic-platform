from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError

from app.dynamic_pipeline.schemas import ArtifactMeta, RhythmOutput, ScriptDialogue, ScriptOutput
from app.dynamic_pipeline.script_stage import (
    Stage3ValidationError,
    normalize_dialogue_line_id,
    normalize_script_output,
    script_output_to_legacy_screenplay,
    stable_dialogue_ids,
    stable_scene_key,
    validate_script_output,
)
from app.dynamic_pipeline.storyboard_stage import (
    StoryboardOutput,
    StoryboardShot,
    batched_shot_count_limits,
    normalize_storyboard_output,
    plan_storyboard_batches,
    stable_external_key,
    storyboard_to_shot_payloads,
    validate_storyboard_output,
)


def _meta() -> dict[str, Any]:
    return {
        "schema_version": "dynamic-script-v2",
        "normalizer_version": "dynamic-normalizer-v1",
        "input_signature": "input-script-1",
        "content_signature": "content-script-1",
        "source_attempt": 1,
    }


def _raw_scene(**overrides: Any) -> dict[str, Any]:
    scene: dict[str, Any] = {
        "scene_no": "1",
        "scene_name": "  雨夜仓库  ",
        "time": "夜",
        "weather": "暴雨",
        "location": "  废弃仓库 ",
        "characters": [" 林默 ", {"name": "反派"}],
        "objective": "  建立危机 ",
        "action": "  林默抬手格挡  ",
        "dialogue": {
            "character": " 林默 ",
            "line": " 你终于来了。 ",
            "start_sec": 0,
            "end_sec": 1,
        },
        "narration": "  雷声压过脚步声。 ",
        "emotion": "  紧张 ",
        "dynamic": "0.8",
        "sound": [
            {
                "kind": "environment",
                "description": " 雷声 ",
                "start_sec": 0,
                "end_sec": 30,
            }
        ],
        "subtitle": "白字黑边",
        "duration": "30",
        "transition": "切入近景",
    }
    scene.update(overrides)
    return scene


def _raw_script(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": "  雨夜反击 ",
        "summary": "  林默在雨夜完成第一次反击。 ",
        "scene_list": [_raw_scene()],
        "target_duration_sec": 5,
        "strong_events": [{"event_id": "dynamic-1"}],
        "assumption": "沿用人物设定",
        "questions": ["反派身份待确认"],
        "provider_unknown": "ignored",
    }
    payload.update(overrides)
    return payload


def _rhythm_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "target_duration_sec": 30,
        "artifact_meta": _meta(),
        "timeline": [
            {
                "segment_id": "segment-1",
                "ordinal": 1,
                "start_sec": 0,
                "end_sec": 30,
                "beat_type": "payoff",
                "narrative_task": "建立危机",
                "event_summary": "林默抬手格挡",
                "visual_action": "林默抬手格挡",
                "focus_characters": ["林默"],
                "dynamic_level": 0.8,
                "strong_dynamic_event_id": "dynamic-1",
                "source_refs": [],
            }
        ],
        "core_payoff": "完成反击",
        "small_beats": [],
        "strong_dynamic_budget": {
            "max_events": 1,
            "event_ids": ["dynamic-1"],
            "events": [
                {
                    "event_id": "dynamic-1",
                    "summary": "完成反击",
                    "segment_id": "segment-1",
                    "dynamic_level": 1,
                    "visual_action": "林默抬手格挡",
                    "source_refs": [],
                }
            ],
        },
        "foreshadowing": [],
        "open_space": [],
        "dialogue_suggestions": [],
        "sound_suggestions": [],
        "assumptions": [],
    }
    payload.update(overrides)
    return payload


def test_normalize_script_output_accepts_aliases_and_single_dialogue_object() -> None:
    result = normalize_script_output(
        _raw_script(),
        artifact_meta=_meta(),
        target_duration_sec=30,
        source_refs=("source:chapter-1",),
    )

    assert isinstance(result, ScriptOutput)
    assert result.title == "雨夜反击"
    assert result.logline == "林默在雨夜完成第一次反击。"
    assert result.target_duration_sec == 30
    assert result.scenes[0].scene_heading == "雨夜仓库"
    assert result.scenes[0].time_weather == "夜；暴雨"
    assert result.scenes[0].characters == ["林默", "反派"]
    assert result.scenes[0].dialogue[0].speaker == "林默"
    assert result.scenes[0].dialogue[0].text == "你终于来了。"
    assert result.scenes[0].dialogue[0].dialogue_line_id == "scene-1-dialogue-1"
    assert result.scenes[0].source_refs == ["source:chapter-1"]
    assert "provider_unknown" not in result.model_dump()
    assert result.scenes[0].plot_event_ids == [
        "scene-1:objective",
        "scene-1:action",
        "scene-1:narration-1",
    ]


def test_normalize_script_output_preserves_explicit_stable_ids_and_rejects_duplicates() -> None:
    raw = _raw_script(
        scenes=[
            _raw_scene(
                scene_id=" stable-scene ",
                dialogue=[
                    {"dialogue_line_id": " line-a ", "speaker": "林默", "text": "甲"},
                    {"speaker": "反派", "text": "乙"},
                ],
            )
        ]
    )
    result = normalize_script_output(raw, artifact_meta=_meta(), target_duration_sec=30)

    assert result.scenes[0].scene_id == "stable-scene"
    assert [item.dialogue_line_id for item in result.scenes[0].dialogue] == [
        "line-a",
        "scene-1-dialogue-2",
    ]
    assert normalize_dialogue_line_id("ignored", 1, 1, " stable ") == "stable"
    assert normalize_dialogue_line_id("ignored", 1, 1) == "scene-1-dialogue-1"
    assert stable_scene_key(result.scenes[0]) == "stable-scene"
    assert stable_dialogue_ids(result.scenes[0]) == ["line-a", "scene-1-dialogue-2"]

    duplicate_scene = _raw_script(
        scenes=[_raw_scene(scene_id="same"), _raw_scene(scene_id="same", scene_no=2)]
    )
    with pytest.raises(Stage3ValidationError, match="重复 scene_id"):
        normalize_script_output(duplicate_scene, artifact_meta=_meta(), target_duration_sec=30)

    duplicate_dialogue = _raw_script(
        scenes=[
            _raw_scene(
                dialogue=[
                    {"dialogue_line_id": "same-line", "text": "甲"},
                    {"dialogue_line_id": "same-line", "text": "乙"},
                ]
            )
        ]
    )
    with pytest.raises(Stage3ValidationError, match="重复 dialogue_line_id"):
        normalize_script_output(duplicate_dialogue, artifact_meta=_meta(), target_duration_sec=30)


def test_script_duration_does_not_inherit_legacy_five_second_shot_minimum() -> None:
    result = normalize_script_output(
        _raw_script(
            scenes=[
                _raw_scene(
                    duration="0.5",
                    dialogue=[
                        {
                            "character": "林默",
                            "line": "短句",
                            "start_sec": 0,
                            "end_sec": 0.25,
                        }
                    ],
                    sound=[
                        {
                            "kind": "environment",
                            "description": "雷声",
                            "start_sec": 0,
                            "end_sec": 0.5,
                        }
                    ],
                )
            ]
        ),
        artifact_meta=_meta(),
        target_duration_sec=0.5,
    )

    assert result.scenes[0].duration_sec == 0.5
    assert validate_script_output(result) == []


def test_validate_script_output_reports_duration_and_rhythm_diagnostics() -> None:
    result = normalize_script_output(
        _raw_script(),
        artifact_meta=_meta(),
        target_duration_sec=30,
    )
    rhythm = RhythmOutput.model_validate(_rhythm_payload())

    ready_diagnostics = validate_script_output(result, rhythm=rhythm, target_duration_sec=30)
    assert ready_diagnostics == []

    mismatch = result.model_copy(deep=True)
    mismatch.scenes[0].duration_sec = 20
    mismatch.scenes[0].action = "林默转身离开"
    mismatch.scenes[0].objective = "等待援军"
    mismatch.strong_dynamic_event_ids = []
    diagnostics = validate_script_output(mismatch, rhythm=rhythm, target_duration_sec=30)
    codes = {item.code for item in diagnostics}
    assert "script_duration_mismatch" in codes
    assert "script_rhythm_duration_mismatch" in codes
    assert "script_rhythm_strong_dynamic_event_id_missing" in codes
    assert "script_rhythm_event_unfulfilled" in codes


def test_validate_script_output_allows_empty_dialogue_when_scene_has_narration() -> None:
    result = normalize_script_output(
        _raw_script(
            scenes=[
                _raw_scene(
                    action="",
                    dialogue=[],
                    narration=["雷声压过脚步声。"],
                    sound=[],
                )
            ]
        ),
        artifact_meta=_meta(),
        target_duration_sec=30,
    )

    diagnostics = validate_script_output(result)

    assert diagnostics == []


def test_validate_script_output_blocks_scene_without_action_narration_or_sound() -> None:
    result = normalize_script_output(
        _raw_script(
            scenes=[
                _raw_scene(scene_no=2, action="", dialogue=[], narration=[], sound=[]),
                _raw_scene(scene_no=1),
            ]
        ),
        artifact_meta=_meta(),
        target_duration_sec=30,
    )
    diagnostics = validate_script_output(result)
    codes = {item.code for item in diagnostics}

    assert "script_scene_order_invalid" in codes
    assert "script_scene_content_missing" in codes


def test_validate_script_output_blocks_dynamic_dialogue_without_time_contract() -> None:
    result = normalize_script_output(
        _raw_script(
            scenes=[
                _raw_scene(
                    dialogue=[{"speaker": "林默", "text": "没有时间绑定。"}],
                )
            ]
        ),
        artifact_meta=_meta(),
        target_duration_sec=30,
    )

    codes = {item.code for item in validate_script_output(result)}

    assert "script_dialogue_time_missing" in codes


def test_validate_script_output_blocks_sound_cue_outside_scene_duration() -> None:
    result = normalize_script_output(
        _raw_script(
            scenes=[
                _raw_scene(
                    sound=[
                        {
                            "kind": "environment",
                            "description": "雷声",
                            "start_sec": 0,
                            "end_sec": 31,
                        }
                    ]
                )
            ]
        ),
        artifact_meta=_meta(),
        target_duration_sec=30,
    )

    codes = {item.code for item in validate_script_output(result)}

    assert "script_sound_cue_time_out_of_bounds" in codes


def test_validate_script_output_warns_on_legacy_string_sound_cue() -> None:
    result = normalize_script_output(
        _raw_script(
            artifact_meta={**_meta(), "schema_version": "legacy-screenplay-v1"},
            scenes=[_raw_scene(sound=["雷声"])],
        ),
        artifact_meta={**_meta(), "schema_version": "legacy-screenplay-v1"},
        target_duration_sec=30,
    )

    diagnostics = validate_script_output(result)

    assert any(item.code == "script_legacy_sound_cue_string" for item in diagnostics)
    assert all(item.level != "blocking" for item in diagnostics)


def test_validate_script_output_warns_on_legacy_structured_sound_cue_without_time() -> None:
    legacy_meta = {**_meta(), "schema_version": "legacy-screenplay-v1"}
    result = normalize_script_output(
        _raw_script(scenes=[_raw_scene(sound=[{"kind": "environment", "description": "雷声"}])]),
        artifact_meta=legacy_meta,
        target_duration_sec=30,
    )

    diagnostics = validate_script_output(result)

    assert any(item.code == "script_sound_cue_time_missing" for item in diagnostics)
    assert all(item.level != "blocking" for item in diagnostics)


def test_validate_script_output_blocks_dynamic_structured_sound_cue_without_time() -> None:
    result = normalize_script_output(
        _raw_script(scenes=[_raw_scene(sound=[{"kind": "environment", "description": "雷声"}])]),
        artifact_meta=_meta(),
        target_duration_sec=30,
    )

    diagnostics = validate_script_output(result)

    assert any(item.code == "script_sound_cue_time_missing" for item in diagnostics)
    assert any(item.level == "blocking" for item in diagnostics)


def test_script_output_to_legacy_screenplay_is_explicit_projection_only() -> None:
    result = normalize_script_output(
        _raw_script(),
        artifact_meta=_meta(),
        target_duration_sec=30,
    )
    legacy = script_output_to_legacy_screenplay(result)

    assert legacy == {
        "title": "雨夜反击",
        "logline": "林默在雨夜完成第一次反击。",
        "scenes": [
            {
                "scene_no": 1,
                "scene_name": "雨夜仓库",
                "time_weather": "夜；暴雨",
                "characters": ["林默", "反派"],
                "objective": "建立危机",
                "action": "林默抬手格挡",
                "dialogue": [{"speaker": "林默", "text": "你终于来了。"}],
                "narration": ["雷声压过脚步声。"],
                "emotion": "紧张",
                "transition_to_next": "切入近景",
            }
        ],
    }


def test_script_models_reject_unknown_fields_and_normalization_does_not_mutate_input() -> None:
    raw = _raw_script()
    original = deepcopy(raw)
    result = normalize_script_output(raw, artifact_meta=_meta(), target_duration_sec=30)

    assert raw == original
    assert result.scenes[0].dialogue[0].source_refs == []
    with pytest.raises(ValidationError):
        ScriptOutput.model_validate(
            {
                "artifact_meta": ArtifactMeta.model_validate(_meta()),
                "title": "标题",
                "logline": "梗概",
                "target_duration_sec": 1,
                "unknown": True,
            }
        )


def _storyboard_shot(**overrides: Any) -> StoryboardShot:
    payload: dict[str, Any] = {
        "external_key": "storyboard:run-1:scene-1:shot-1:ordinal-1",
        "shot_no": 1,
        "ordinal": 1,
        "scene_id": "scene-1",
        "scene_no": 1,
        "scene_name": "雨夜仓库",
        "time_weather": "夜；暴雨",
        "scene_asset_id": "scene-asset-1",
        "character_names": ["林默"],
        "character_asset_ids": ["character-1"],
        "prop_names": ["长刀"],
        "prop_asset_ids": ["prop-1"],
        "positions": [{"character": "林默", "position": "仓库中央"}],
        "expressions": [{"character": "林默", "expression": "紧张"}],
        "plot_event_ids": [
            "scene-1:objective",
            "scene-1:action",
            "scene-1:narration-1",
        ],
        "start_state": "林默站在仓库中央",
        "action": "林默抬手格挡",
        "end_state": "林默保持格挡姿态",
        "next_hook": "雷声中反派逼近",
        "transition_from_prev": "scene_change",
        "duration_sec": 10,
        "provider_duration_sec": 10,
        "shot_type": "中景",
        "camera_angle": "平视",
        "camera_move": "固定",
        "continuity_state": {"林默": "仓库中央"},
        "dialogue": [
            {
                "dialogue_line_id": "scene-1-dialogue-1",
                "speaker": "林默",
                "text": "你终于来了。",
            }
        ],
        "narration": "雷声压过脚步声。",
        "sound_cues": ["雷声"],
        "dynamic_level": 0.8,
        "dynamic_event_id": "dynamic-1",
        "frame_description": "雨幕中的仓库中央",
        "source_refs": ["source:chapter-1"],
        "reference_bindings": [],
        "prompt_intent": {},
    }
    payload.update(overrides)
    return StoryboardShot.model_validate(payload)


def _storyboard_output(*shots: StoryboardShot, target_duration_sec: float = 30) -> StoryboardOutput:
    return StoryboardOutput(
        artifact_meta=ArtifactMeta.model_validate(_meta()),
        target_duration_sec=target_duration_sec,
        shots=list(shots),
    )


def test_storyboard_external_key_is_stable_and_normalize_accepts_aliases_and_singletons() -> None:
    key = stable_external_key("run-1", "scene-1", 1, 1)
    assert key == stable_external_key("run-1", "scene-1", 1, 1)
    assert key != stable_external_key("run-1", "scene-1", 1, 2)

    raw: dict[str, Any] = {
        "镜头列表": [
            {
                "shot_number": "1",
                "scene": {"id": "scene-1", "no": "1", "name": "雨夜仓库"},
                "time": "夜",
                "weather": "暴雨",
                "characters": {"name": "林默", "asset_id": "character-1"},
                "props": {"name": "长刀", "asset_id": "prop-1"},
                "position": {"name": "林默", "location": "中央"},
                "expression": {"name": "林默", "emotion": "紧张"},
                "start": "站立",
                "visual_action": "林默抬手格挡",
                "end": "保持格挡姿态",
                "transition": "scene_change",
                "duration": "0.5",
                "dialogue": {"character": "林默", "line": "你终于来了。"},
                "sound": {"kind": "sfx", "description": "雷声"},
            }
        ]
    }
    result = normalize_storyboard_output(
        raw,
        artifact_meta=_meta(),
        target_duration_sec=0.5,
        run_id="run-1",
    )

    assert result.target_duration_sec == 0.5
    assert result.shots[0].external_key == key
    assert result.shots[0].time_weather == "夜；暴雨"
    assert result.shots[0].character_names == ["林默"]
    assert result.shots[0].character_asset_ids == ["character-1"]
    dialogue = result.shots[0].dialogue[0]
    if isinstance(dialogue, ScriptDialogue):
        assert dialogue.dialogue_line_id == "shot-1-dialogue-1"
    else:
        assert dialogue["dialogue_line_id"] == "shot-1-dialogue-1"
    assert result.shots[0].sound_cues == [{"kind": "sfx", "description": "雷声"}]
    assert raw["镜头列表"][0]["duration"] == "0.5"


def test_storyboard_transition_rules_are_independent() -> None:
    first_continuous = _storyboard_shot(transition_from_prev="continuous")
    codes = {item.code for item in validate_storyboard_output(_storyboard_output(first_continuous))}
    assert "storyboard_continuous_previous_missing" in codes

    previous = _storyboard_shot(end_state="林默保持格挡姿态")
    continuous = _storyboard_shot(
        external_key="storyboard:run-1:scene-1:shot-2:ordinal-2",
        shot_no=2,
        ordinal=2,
        start_state="林默保持格挡姿态",
        transition_from_prev="continuous",
        duration_sec=10,
        provider_duration_sec=10,
        dialogue=[
            {
                "dialogue_line_id": "scene-1-dialogue-2",
                "speaker": "反派",
                "text": "接招。",
            }
        ],
    )
    assert not {
        item.code
        for item in validate_storyboard_output(
            _storyboard_output(
                previous,
                continuous,
                target_duration_sec=20,
            )
        )
        if item.level == "blocking"
    }

    elliptical = _storyboard_shot(transition_from_prev="elliptical", next_hook="")
    codes = {item.code for item in validate_storyboard_output(_storyboard_output(elliptical))}
    assert "storyboard_elliptical_next_hook_missing" in codes

    same_scene_cut = _storyboard_shot(
        transition_from_prev="same_scene_cut",
        reference_bindings=[{"role": "previous_keyframe", "keyframe_id": "kf-1"}],
    )
    codes = {item.code for item in validate_storyboard_output(_storyboard_output(same_scene_cut))}
    assert "storyboard_same_scene_cut_inherits_previous_frame" in codes

    scene_change = _storyboard_shot(
        transition_from_prev="scene_change",
        prompt_intent={"previous_shot_id": "shot-0"},
    )
    codes = {item.code for item in validate_storyboard_output(_storyboard_output(scene_change))}
    assert "storyboard_scene_change_references_previous_shot" in codes


def test_storyboard_gate_checks_order_duplicates_continuity_script_and_rhythm() -> None:
    first = _storyboard_shot()
    duplicate = _storyboard_shot(
        external_key=first.external_key,
        shot_no=1,
        ordinal=2,
    )
    duplicate_codes = {
        item.code for item in validate_storyboard_output(_storyboard_output(first, duplicate))
    }
    assert "storyboard_duplicate_external_key" in duplicate_codes
    assert "storyboard_duplicate_shot_no" in duplicate_codes

    script = normalize_script_output(
        _raw_script(scenes=[_raw_scene(duration="30")]),
        artifact_meta=_meta(),
        target_duration_sec=30,
    )
    rhythm = RhythmOutput.model_validate(_rhythm_payload())
    ready_shots = [
        first,
        _storyboard_shot(
            external_key="storyboard:run-1:scene-1:shot-2:ordinal-2",
            shot_no=2,
            ordinal=2,
            duration_sec=10,
            provider_duration_sec=10,
            transition_from_prev="continuous",
            start_state="林默保持格挡姿态",
            dialogue=[],
        ),
        _storyboard_shot(
            external_key="storyboard:run-1:scene-1:shot-3:ordinal-3",
            shot_no=3,
            ordinal=3,
            duration_sec=10,
            provider_duration_sec=10,
            transition_from_prev="continuous",
            start_state="林默保持格挡姿态",
            dialogue=[],
        ),
    ]
    ready = validate_storyboard_output(
        _storyboard_output(*ready_shots, target_duration_sec=30),
        script=script,
        rhythm=rhythm,
        target_duration_sec=30,
    )
    assert not [item for item in ready if item.level == "blocking"]

    mismatch = first.model_copy(deep=True)
    mismatch.action = "林默转身离开"
    mismatch.dynamic_event_id = None
    mismatch.prompt_intent = {}
    diagnostics = validate_storyboard_output(
        _storyboard_output(mismatch),
        script=script,
        rhythm=rhythm,
        target_duration_sec=30,
    )
    codes = {item.code for item in diagnostics}
    assert "storyboard_script_action_unfulfilled" in codes
    assert "storyboard_rhythm_event_unfulfilled" in codes
    assert "storyboard_rhythm_strong_dynamic_event_id_missing" in codes


def test_storyboard_gate_rejects_unplanned_dialogue_and_structured_state_jump() -> None:
    script = normalize_script_output(
        _raw_script(scenes=[_raw_scene(duration="30")]),
        artifact_meta=_meta(),
        target_duration_sec=30,
    )
    previous = _storyboard_shot(
        end_state="林默保持格挡姿态",
        continuity_state={"end": {"characters": {"林默": {"position": "中央"}}}},
    )
    current = _storyboard_shot(
        external_key="storyboard:run-1:scene-1:shot-2:ordinal-2",
        shot_no=2,
        ordinal=2,
        start_state="林默保持格挡姿态",
        transition_from_prev="continuous",
        duration_sec=10,
        provider_duration_sec=10,
        plot_event_ids=["scene-1:objective", "scene-1:action"],
        continuity_state={"start": {"characters": {"林默": {"position": "门口"}}}},
        dialogue=[
            {"dialogue_line_id": "scene-1-dialogue-1", "speaker": "林默", "text": "改写台词"},
            {"dialogue_line_id": "new-line", "speaker": "反派", "text": "新增台词"},
        ],
    )
    codes = {
        item.code
        for item in validate_storyboard_output(
            _storyboard_output(previous, current, target_duration_sec=31),
            script=script,
        )
    }
    assert "storyboard_script_dialogue_text_mismatch" in codes
    assert "storyboard_script_dialogue_extra" in codes
    assert "storyboard_continuity_structured_state_mismatch" in codes


def test_storyboard_projection_contains_incremental_json_fields_without_media() -> None:
    shot = _storyboard_shot(image_prompt="静止关键帧", video_prompt="单一动作")
    payload = storyboard_to_shot_payloads(_storyboard_output(shot))[0]

    assert payload["external_key"] == shot.external_key
    assert payload["action_events_json"]
    assert payload["dialogue_cues_json"]
    assert payload["sound_cues_json"]
    assert payload["continuity_json"]
    assert payload["pipeline_metadata_json"]
    assert payload["character_ids"] == ["character-1"]
    assert payload["image_prompt"] == "静止关键帧"
    assert payload["video_prompt"] == "单一动作"
    assert "media" not in payload


def test_storyboard_short_duration_and_validation_do_not_mutate_input() -> None:
    raw = {
        "shots": [
            {
                "shot_no": 1,
                "scene_no": 1,
                "scene_name": "近景",
                "scene_asset_id": "scene-1",
                "action": "抬眼",
                "start_state": "低头",
                "end_state": "抬头",
                "duration_sec": 5,
                "shot_type": "近景",
                "camera_angle": "平视",
                "camera_move": "固定",
            }
        ]
    }
    original = deepcopy(raw)
    result = normalize_storyboard_output(
        raw,
        artifact_meta=_meta(),
        target_duration_sec=5,
        chapter_id="chapter-1",
    )
    assert result.shots[0].duration_sec == 5
    assert validate_storyboard_output(result, target_duration_sec=5) == []
    assert raw == original


def test_plan_storyboard_batches_splits_by_duration_limit() -> None:
    """批次必须按累计时长真正拆批：90 秒上限下 40+40+40 应拆为 2 批。"""
    script = normalize_script_output(
        _raw_script(
            scenes=[
                _raw_scene(scene_no=1, duration="40"),
                _raw_scene(scene_no=2, duration="40"),
                _raw_scene(scene_no=3, duration="40"),
            ]
        ),
        artifact_meta=_meta(),
        target_duration_sec=120,
    )
    plans = plan_storyboard_batches(script, max_batch_duration_sec=90)

    assert [len(plan.scene_indexes) for plan in plans] == [2, 1]
    assert plans[0].end_sec == 80
    assert plans[1].start_sec == 80


def test_plan_storyboard_batches_keeps_oversized_scene_alone() -> None:
    """单场景超过上限时必须独立成批，不得切割场景语义。"""
    script = normalize_script_output(
        _raw_script(
            scenes=[
                _raw_scene(scene_no=1, duration="100"),
                _raw_scene(scene_no=2, duration="10"),
            ]
        ),
        artifact_meta=_meta(),
        target_duration_sec=110,
    )
    plans = plan_storyboard_batches(script, max_batch_duration_sec=60)

    assert [plan.scene_indexes for plan in plans] == [[0], [1]]


@pytest.mark.parametrize(
    ("budget", "expected_lower", "expected_suggested"),
    [
        (15, 1, 3),
        (30, 2, 4),
        (60, 4, 6),
        (61, 5, 7),
    ],
)
def test_batched_shot_count_limits_match_legacy_density_policy(
    budget: int, expected_lower: int, expected_suggested: int
) -> None:
    lower, suggested = batched_shot_count_limits(budget)
    assert (lower, suggested) == (expected_lower, expected_suggested)
    assert lower * 15 >= budget
