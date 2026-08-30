from __future__ import annotations

import json
from collections import OrderedDict
from typing import Any

import pytest

from app.dynamic_pipeline.normalizers import (
    NORMALIZER_VERSION,
    NormalizationError,
    normalize_character_bios_output,
    normalize_rhythm_output,
    normalize_source_output,
    normalize_stage_output,
)
from app.dynamic_pipeline.schemas import (
    CharacterBiosOutput,
    RhythmOutput,
    SourceAnalysisOutput,
    StrongDynamicEvent,
)


def _source_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "input_signature": "input-source-1",
        "source_kind": "full_text",
        "title": "  雨夜反击  ",
        "chapter_title": " 第一章 ",
        "target_duration_sec": 30,
        "aspect_ratio": " 9:16 ",
        "source_signature": "source-1",
        "source_attempt": 2,
        "provider_id": "provider-a",
        "provider_model": "model-a",
    }
    kwargs.update(overrides)
    return kwargs


def _canonical_source_raw(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "artifact_meta": {
            "schema_version": "provider-version",
            "normalizer_version": "provider-normalizer",
            "input_signature": "provider-input",
            "content_signature": "provider-content",
            "generated_at": "2020-01-01T00:00:00Z",
        },
        "metadata": {"provider_metadata": "ignored"},
        "episode_logline": " 林默在雨夜完成第一次反击。 ",
        "scenes": [
            {
                "scene_id": "scene-1",
                "ordinal": 1,
                "location": " 废弃仓库 ",
                "time_weather": " 夜，暴雨 ",
                "characters": [
                    {
                        "character_key": "hero",
                        "canonical_name": " 林默 ",
                        "importance": "core",
                        "story_function": " 承受冲突 ",
                    }
                ],
                "narrative_task": " 建立危机 ",
                "plot_event": " 反派发动袭击 ",
                "conflict_or_turn": " 主角被迫迎战 ",
                "visual_focus": " 刀锋与手臂 ",
                "estimated_duration_sec": 8,
                "shootability": {
                    "status": "shootable",
                    "visible_action": " 林默抬手格挡 ",
                },
            }
        ],
        "character_roster": [
            {
                "character_key": "hero",
                "canonical_name": "林默",
                "importance": "core",
                "story_function": "冲突承受者",
            }
        ],
        "conflict_turns": [
            {
                "kind": "conflict",
                "summary": "反派发动袭击",
                "scene_id": "scene-1",
                "participants": ["hero"],
            }
        ],
        "visualizable_content": [" 雨夜仓库 "],
        "hard_to_visualize": [],
        "rewrite_requirements": [],
        "assumptions": [],
        "open_questions": [],
        "continuation_outline": [],
        "raw_unknown": "must not be retained",
    }
    payload.update(overrides)
    return payload


def _canonical_bios_raw(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "artifact_meta": {"content_signature": "provider-content"},
        "characters": [
            {
                "basic_info": {
                    "character_key": "hero",
                    "canonical_name": " 林默 ",
                    "aliases": "小林",
                    "importance": "core",
                    "identity": "年轻剑客",
                },
                "background": "曾是边城守卫。",
                "personality_behavior_logic": "谨慎观察后迅速行动。",
                "dynamic_comic_setting": "受伤时仍保持左手护心。",
                "plot_relation": "本集冲突的承受者。",
                "visual_anchor": {
                    "visual_facts": [" 左眉浅疤 "],
                    "face": "窄脸",
                    "hair": "黑色短发",
                    "body": "清瘦",
                    "costume": "深灰风衣",
                    "accessories": "银色耳钉",
                    "immutable_features": "左眉浅疤",
                    "forbidden_changes": [],
                    "makeup_keyword_string": "窄脸|黑色短发",
                },
                "behavior_evidence": {
                    "trigger": "发现线索",
                    "action": "抬手查看",
                    "expression": "目光收紧",
                    "voice": "低声回应",
                    "source_refs": ["p1-l3"],
                },
            }
        ],
        "ensemble_anchors": [],
        "unresolved_character_refs": [],
        "raw_unknown": {"should": "drop"},
    }
    payload.update(overrides)
    return payload


def _canonical_rhythm_raw(**overrides: Any) -> dict[str, Any]:
    event = {
        "event_id": "dynamic-1",
        "summary": "主角完成反击",
        "segment_id": "segment-1",
        "dynamic_level": 1,
        "visual_action": "刀锋被弹开",
    }
    payload: dict[str, Any] = {
        "artifact_meta": {"schema_version": "bad-provider-version"},
        "target_duration_sec": 999,
        "timeline": [
            {
                "segment_id": "segment-1",
                "ordinal": 1,
                "start_sec": 0,
                "end_sec": 3,
                "beat_type": "hook",
                "narrative_task": "建立危机",
                "event_summary": "刀锋逼近",
                "visual_action": "主角抬手格挡",
                "focus_characters": ["hero"],
                "dynamic_level": 0.8,
            }
        ],
        "core_payoff": event,
        "small_beats": ["敌人短暂迟疑"],
        "strong_dynamic_budget": {
            "max_events": 1,
            "event_ids": ["dynamic-1"],
            "events": [event],
        },
        "foreshadowing": [" 敌人认出主角 "],
        "open_space": [],
        "dialogue_suggestions": [
            {"segment_id": "segment-1", "speaker": "林默", "text": " 来者何人？ "}
        ],
        "sound_suggestions": [
            {"segment_id": "segment-1", "kind": "impact", "description": " 金属碰撞声 "}
        ],
        "assumptions": [],
        "raw_unknown": "drop",
    }
    payload.update(overrides)
    return payload


def test_normalize_source_output_accepts_canonical_raw_and_rebuilds_meta() -> None:
    result = normalize_source_output(_canonical_source_raw(), **_source_kwargs())

    assert isinstance(result, SourceAnalysisOutput)
    assert result.metadata.title == "雨夜反击"
    assert result.episode_logline == "林默在雨夜完成第一次反击。"
    assert result.artifact_meta.schema_version == "dynamic-source-v1"
    assert result.artifact_meta.normalizer_version == NORMALIZER_VERSION
    assert result.artifact_meta.input_signature == "input-source-1"
    assert result.artifact_meta.source_attempt == 2
    assert result.artifact_meta.provider_id == "provider-a"
    assert result.artifact_meta.provider_model == "model-a"
    assert result.artifact_meta.generated_at is None
    assert result.scenes[0].location == "废弃仓库"
    assert "raw_unknown" not in result.model_dump()


def test_normalize_character_bios_output_accepts_canonical_raw() -> None:
    result = normalize_character_bios_output(
        _canonical_bios_raw(),
        input_signature="input-bios-1",
        provider_id="provider-b",
        provider_model="model-b",
    )

    assert isinstance(result, CharacterBiosOutput)
    assert result.characters[0].basic_info.canonical_name == "林默"
    assert result.characters[0].basic_info.aliases == ["小林"]
    assert result.characters[0].visual_anchor.face == "窄脸"
    assert result.characters[0].visual_anchor.accessories == ["银色耳钉"]
    assert result.characters[0].behavior_evidence.trigger == "发现线索"
    assert result.characters[0].behavior_evidence.action == "抬手查看"
    assert result.characters[0].behavior_evidence.expression == "目光收紧"
    assert result.characters[0].behavior_evidence.voice == "低声回应"
    assert result.artifact_meta.schema_version == "dynamic-character-bios-v1"
    assert result.artifact_meta.provider_id == "provider-b"
    assert "raw_unknown" not in result.model_dump()


def test_normalize_rhythm_output_accepts_canonical_raw_and_forces_duration() -> None:
    result = normalize_rhythm_output(
        _canonical_rhythm_raw(),
        input_signature="input-rhythm-1",
        target_duration_sec=45,
    )

    assert isinstance(result, RhythmOutput)
    assert result.target_duration_sec == 45
    assert result.timeline[0].end_sec == 3
    assert result.artifact_meta.schema_version == "dynamic-rhythm-v1"
    assert isinstance(result.core_payoff, StrongDynamicEvent)
    assert result.core_payoff.summary == "主角完成反击"


def test_normalizers_accept_envelope_json_code_fence_and_pydantic_model() -> None:
    source_json = json.dumps({"content": _canonical_source_raw()}, ensure_ascii=False)
    source = normalize_source_output(
        f"```json\n{source_json}\n```",
        **_source_kwargs(source_attempt=1),
    )
    assert source.scenes[0].scene_id == "scene-1"

    bios = normalize_character_bios_output(
        {"data": json.dumps(_canonical_bios_raw(), ensure_ascii=False)},
        input_signature="input-bios-envelope",
    )
    assert bios.characters[0].basic_info.character_key == "hero"

    rhythm = normalize_rhythm_output(
        {
            "output": {
                "segments": _canonical_rhythm_raw()["timeline"][0],
                "payoff": "核心反击",
            }
        },
        input_signature="input-rhythm-single",
        target_duration_sec=20,
    )
    assert rhythm.timeline[0].segment_id == "segment-1"
    assert rhythm.core_payoff == "核心反击"

    source_model = SourceAnalysisOutput.model_validate(
        normalize_source_output(_canonical_source_raw(), **_source_kwargs()).model_dump()
    )
    from_model = normalize_source_output(source_model, **_source_kwargs(source_attempt=1))
    assert from_model.episode_logline == source_model.episode_logline


def test_normalizers_apply_aliases_and_conservative_visual_fallbacks() -> None:
    source = normalize_source_output(
        {
            "logline": " 一句话梗概 ",
            "scene_list": {
                "scene_no": "1",
                "scene_name": "仓库",
                "time": "夜",
                "weather": "雨",
                "characters": " 林默 ",
                "objective": "建立危机",
                "action": "林默抬手格挡",
                "conflict": "被迫迎战",
                "visual_focus": "刀锋",
                "duration": "4",
                "shootable": "可拍",
            },
            "characters": ["林默"],
        },
        **_source_kwargs(),
    )
    scene = source.scenes[0]
    assert scene.location == "仓库"
    assert scene.time_weather == "夜；雨"
    assert scene.characters[0].importance == "supporting"
    assert scene.characters[0].canonical_name == "林默"
    assert scene.shootability.status == "shootable"

    bios = normalize_character_bios_output(
        {
            "bios": {
                "hero": {
                    "name": "林默",
                    "importance": "main",
                    "appearance": "黑发、旧风衣",
                    "backstory": "曾是守卫。",
                    "personality": "谨慎。",
                    "dynamic_comic_settings": "受伤仍护心。",
                    "relationship": "冲突承受者。",
                    "behavior_evidence": {
                        "trigger_condition": "听见危险声响",
                        "response_action": "抬手护住胸口",
                        "facial_expression": "警觉",
                        "voice_style": "压低声音",
                        "references": ["alias-scene-1"],
                    },
                }
            },
            "group_anchors": "守卫群",
        },
        input_signature="input-alias-bios",
    )
    character = bios.characters[0]
    assert character.basic_info.importance == "core"
    assert character.visual_anchor.face == "黑发、旧风衣"
    assert character.visual_anchor.hair == "黑发、旧风衣"
    assert character.visual_anchor.makeup_keyword_string == "黑发、旧风衣"
    assert character.behavior_evidence.trigger == "听见危险声响"
    assert character.behavior_evidence.action == "抬手护住胸口"
    assert character.behavior_evidence.expression == "警觉"
    assert character.behavior_evidence.voice == "压低声音"
    assert character.behavior_evidence.source_refs == ["alias-scene-1"]
    assert bios.ensemble_anchors[0].canonical_name == "守卫群"

    rhythm = normalize_rhythm_output(
        {
            "segments": {
                "segment_no": 1,
                "start": 0,
                "duration": 4,
                "beat": "开场",
                "task": "建立危机",
                "event": "刀锋逼近",
                "action": "主角格挡",
                "intensity": 0.6,
                "characters": "hero",
            },
            "payoff": "完成反击",
            "dynamic_budget": ["主角完成反击"],
            "dialogue": "来者何人？",
            "sound": "金属碰撞声",
        },
        input_signature="input-alias-rhythm",
        target_duration_sec=12,
    )
    segment = rhythm.timeline[0]
    assert segment.end_sec == 4
    assert segment.beat_type == "hook"
    assert segment.narrative_task == "建立危机"
    assert segment.event_summary == "刀锋逼近"
    assert segment.visual_action == "主角格挡"
    assert segment.focus_characters == ["hero"]
    assert rhythm.strong_dynamic_budget.events[0].summary == "主角完成反击"
    assert rhythm.dialogue_suggestions[0].text == "来者何人？"
    assert rhythm.sound_suggestions[0].description == "金属碰撞声"


def test_meta_and_content_signature_are_deterministic_and_ignore_raw_meta() -> None:
    first_raw = _canonical_source_raw()
    second_raw = OrderedDict(reversed(list(first_raw.items())))
    second_raw["artifact_meta"] = {"content_signature": "different", "generated_at": "now"}

    first = normalize_source_output(first_raw, **_source_kwargs())
    second = normalize_source_output(second_raw, **_source_kwargs())
    changed_input = normalize_source_output(
        first_raw,
        **_source_kwargs(input_signature="input-source-2"),
    )

    assert first.artifact_meta.content_signature == second.artifact_meta.content_signature
    assert first.artifact_meta.content_signature != changed_input.artifact_meta.content_signature
    assert first.artifact_meta.content_signature != "provider-content"
    assert first.artifact_meta.schema_version == "dynamic-source-v1"


def test_normalize_stage_output_dispatches_only_phase_2a_stages() -> None:
    source = normalize_stage_output("0-source", _canonical_source_raw(), **_source_kwargs())
    bios = normalize_stage_output(
        "1-character-bios",
        _canonical_bios_raw(),
        input_signature="stage-bios",
    )
    rhythm = normalize_stage_output(
        "2-rhythm",
        _canonical_rhythm_raw(),
        input_signature="stage-rhythm",
        target_duration_sec=31,
    )

    assert isinstance(source, SourceAnalysisOutput)
    assert isinstance(bios, CharacterBiosOutput)
    assert bios.characters[0].behavior_evidence.voice == "低声回应"
    assert isinstance(rhythm, RhythmOutput)

    with pytest.raises(NormalizationError):
        normalize_stage_output("3-script", {}, input_signature="invalid")
    with pytest.raises(NormalizationError):
        normalize_stage_output("0-source", {}, input_signature="missing")
    with pytest.raises(NormalizationError):
        normalize_stage_output("2-rhythm", {}, input_signature="missing")


@pytest.mark.parametrize(
    ("function", "raw", "kwargs"),
    [
        pytest.param(
            normalize_source_output,
            {"scenes": []},
            _source_kwargs(),
            id="source-missing-logline",
        ),
        pytest.param(
            normalize_character_bios_output,
            {"characters": [{"name": "林默", "background": "背景"}]},
            {"input_signature": "bad-bios"},
            id="bios-missing-visual-core-fields",
        ),
        pytest.param(
            normalize_rhythm_output,
            {"timeline": []},
            {"input_signature": "bad-rhythm", "target_duration_sec": 30},
            id="rhythm-missing-payoff",
        ),
    ],
)
def test_missing_required_semantics_raise_normalization_error(
    function: Any,
    raw: dict[str, Any],
    kwargs: dict[str, Any],
) -> None:
    with pytest.raises(NormalizationError):
        function(raw, **kwargs)


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("{not-json", id="malformed-json"),
        pytest.param('```json\n{"x": 1}\n', id="unclosed-code-fence"),
        pytest.param([], id="non-object"),
    ],
)
def test_malformed_provider_output_is_not_silently_accepted(raw: Any) -> None:
    with pytest.raises(NormalizationError):
        normalize_character_bios_output(raw, input_signature="malformed")


def test_normalization_does_not_call_database_or_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("normalizer must not call external services")

    monkeypatch.setattr("app.dynamic_pipeline.normalizers._decode_json_text", fail_if_called)
    result = normalize_character_bios_output(
        _canonical_bios_raw(),
        input_signature="pure-model-input",
    )
    assert result.characters[0].basic_info.character_key == "hero"
