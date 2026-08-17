from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from app.dynamic_pipeline.schemas import (
    AssetMapping,
    ArtifactMeta,
    CharacterBiosOutput,
    CharacterVisualAnchor,
    ConflictTurnKind,
    DeliverySubstage,
    Diagnostic,
    DialogueCue,
    Importance,
    PipelineModel,
    ReferenceSlot,
    RhythmBeatType,
    RhythmOutput,
    RhythmSegment,
    SoundCue,
    SourceAnalysisOutput,
    SourceKind,
    SourceScene,
    SourceSceneShootability,
    StageGateResult,
    StageArtifactDraft,
    StageArtifactEnvelope,
    StageKey,
    ShootabilityStatus,
    StrongDynamicBudget,
    StrongDynamicEvent,
    WorkflowRunCreate,
    WorkflowStageDraft,
    WorkflowStatus,
)


@pytest.mark.parametrize(
    ("schema", "value"),
    [
        pytest.param(StageKey, "0-source", id="stage-key-0-source"),
        pytest.param(StageKey, "1-character-bios", id="stage-key-1-character-bios"),
        pytest.param(StageKey, "2-rhythm", id="stage-key-2-rhythm"),
        pytest.param(StageKey, "3-script", id="stage-key-3-script"),
        pytest.param(StageKey, "4-storyboard", id="stage-key-4-storyboard"),
        pytest.param(StageKey, "5a-image-prompts", id="stage-key-5a-image-prompts"),
        pytest.param(StageKey, "5b-video-prompts", id="stage-key-5b-video-prompts"),
        pytest.param(StageKey, "6-delivery", id="stage-key-6-delivery"),
        pytest.param(WorkflowStatus, "pending", id="workflow-status-pending"),
        pytest.param(WorkflowStatus, "queued", id="workflow-status-queued"),
        pytest.param(WorkflowStatus, "running", id="workflow-status-running"),
        pytest.param(WorkflowStatus, "blocked", id="workflow-status-blocked"),
        pytest.param(WorkflowStatus, "ready", id="workflow-status-ready"),
        pytest.param(WorkflowStatus, "confirmed", id="workflow-status-confirmed"),
        pytest.param(WorkflowStatus, "stale", id="workflow-status-stale"),
        pytest.param(WorkflowStatus, "partial_failed", id="workflow-status-partial-failed"),
        pytest.param(WorkflowStatus, "failed", id="workflow-status-failed"),
        pytest.param(WorkflowStatus, "interrupted", id="workflow-status-interrupted"),
        pytest.param(WorkflowStatus, "cancelled", id="workflow-status-cancelled"),
        pytest.param(SourceKind, "full_text", id="source-kind-full-text"),
        pytest.param(SourceKind, "synopsis", id="source-kind-synopsis"),
        pytest.param(DeliverySubstage, "image", id="delivery-substage-image"),
        pytest.param(DeliverySubstage, "keyframe", id="delivery-substage-keyframe"),
        pytest.param(DeliverySubstage, "video", id="delivery-substage-video"),
        pytest.param(DeliverySubstage, "audio", id="delivery-substage-audio"),
        pytest.param(DeliverySubstage, "subtitle", id="delivery-substage-subtitle"),
        pytest.param(DeliverySubstage, "composition", id="delivery-substage-composition"),
    ],
)
def test_literal_alias_with_known_value_is_accepted(schema: Any, value: str) -> None:
    assert TypeAdapter(schema).validate_python(value) == value


@pytest.mark.parametrize(
    ("schema", "value"),
    [
        pytest.param(StageKey, "unknown-stage", id="stage-key"),
        pytest.param(WorkflowStatus, "unknown-status", id="workflow-status"),
        pytest.param(SourceKind, "unknown-source-kind", id="source-kind"),
        pytest.param(DeliverySubstage, "unknown-substage", id="delivery-substage"),
    ],
)
def test_literal_alias_with_unknown_value_is_rejected(schema: Any, value: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(schema).validate_python(value)


def _workflow_run_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "title": "测试工作流",
        "chapter_title": "第一章",
        "source_text": "这是原文。",
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    ("source_field", "source_value", "other_field"),
    [
        pytest.param("source_text", "这是原文。", "synopsis", id="source-text"),
        pytest.param("synopsis", "这是故事梗概。", "source_text", id="synopsis"),
    ],
)
def test_workflow_run_create_with_one_non_empty_source_is_accepted(
    source_field: str,
    source_value: str,
    other_field: str,
) -> None:
    result = WorkflowRunCreate.model_validate(
        _workflow_run_payload(
            source_text=source_value if source_field == "source_text" else None,
            synopsis=source_value if source_field == "synopsis" else None,
        )
    )

    assert getattr(result, source_field) == source_value
    assert getattr(result, other_field) is None


def test_workflow_run_create_uses_expected_default_values() -> None:
    result = WorkflowRunCreate.model_validate(_workflow_run_payload())

    assert result.target_duration_sec == 30
    assert result.aspect_ratio == "9:16"
    assert result.generate_audio is True
    assert result.generate_subtitle is True
    assert result.generate_bgm is True
    assert result.generate_composition is True


@pytest.mark.parametrize(
    ("source_text", "synopsis"),
    [
        pytest.param(None, None, id="both-none"),
        pytest.param("", "", id="both-empty"),
        pytest.param("这是原文。", "这是梗概。", id="both-filled"),
        pytest.param("   ", None, id="source-text-whitespace"),
        pytest.param(None, "\t\n", id="synopsis-whitespace"),
        pytest.param("  ", "\n\t", id="both-whitespace"),
    ],
)
def test_workflow_run_create_with_invalid_source_selection_is_rejected(
    source_text: str | None,
    synopsis: str | None,
) -> None:
    with pytest.raises(ValidationError):
        WorkflowRunCreate.model_validate(
            _workflow_run_payload(source_text=source_text, synopsis=synopsis)
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("title", "", id="empty-title"),
        pytest.param("title", "   ", id="blank-title"),
        pytest.param("chapter_title", "", id="empty-chapter-title"),
        pytest.param("chapter_title", "   ", id="blank-chapter-title"),
    ],
)
def test_workflow_run_create_with_empty_title_or_chapter_is_rejected(
    field: str,
    value: str,
) -> None:
    with pytest.raises(ValidationError):
        WorkflowRunCreate.model_validate(_workflow_run_payload(**{field: value}))


@pytest.mark.parametrize(
    "target_duration_sec",
    [
        pytest.param(0, id="zero"),
        pytest.param(-1, id="negative"),
        pytest.param(3600.1, id="over-maximum"),
    ],
)
def test_workflow_run_create_with_out_of_range_duration_is_rejected(
    target_duration_sec: float,
) -> None:
    with pytest.raises(ValidationError):
        WorkflowRunCreate.model_validate(
            _workflow_run_payload(target_duration_sec=target_duration_sec)
        )


def _dialogue_cue_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "dialogue_line_id": "dialogue-1",
        "speaker": "小明",
        "text": "你好。",
        "start_time": 1.0,
        "end_time": 2.5,
    }
    payload.update(overrides)
    return payload


def _sound_cue_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "sound_id": "sound-1",
        "kind": "effect",
        "description": "脚步声",
        "start_time": 1.0,
        "end_time": 2.5,
        "volume": 0.8,
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    ("model", "payload_factory"),
    [
        pytest.param(DialogueCue, _dialogue_cue_payload, id="dialogue-cue"),
        pytest.param(SoundCue, _sound_cue_payload, id="sound-cue"),
    ],
)
def test_cue_with_increasing_time_is_constructed(
    model: type[BaseModel],
    payload_factory: Callable[[], dict[str, object]],
) -> None:
    result = model.model_validate(payload_factory())
    values = result.model_dump()

    assert values["start_time"] == 1.0
    assert values["end_time"] == 2.5


@pytest.mark.parametrize(
    ("model", "payload_factory"),
    [
        pytest.param(DialogueCue, _dialogue_cue_payload, id="dialogue-cue"),
        pytest.param(SoundCue, _sound_cue_payload, id="sound-cue"),
    ],
)
@pytest.mark.parametrize(
    ("start_time", "end_time"),
    [
        pytest.param(1.0, 1.0, id="equal"),
        pytest.param(2.0, 1.0, id="decreasing"),
    ],
)
def test_cue_with_end_time_not_greater_than_start_time_is_rejected(
    model: type[BaseModel],
    payload_factory: Callable[[], dict[str, object]],
    start_time: float,
    end_time: float,
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(
            payload_factory().copy() | {"start_time": start_time, "end_time": end_time}
        )


@pytest.mark.parametrize(
    ("model", "payload_factory"),
    [
        pytest.param(DialogueCue, _dialogue_cue_payload, id="dialogue-cue"),
        pytest.param(SoundCue, _sound_cue_payload, id="sound-cue"),
    ],
)
@pytest.mark.parametrize("time_field", ["start_time", "end_time"])
def test_cue_with_negative_time_is_rejected(
    model: type[BaseModel],
    payload_factory: Callable[[], dict[str, object]],
    time_field: str,
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload_factory().copy() | {time_field: -0.1})


@pytest.mark.parametrize("volume", [-0.1, 1.1])
def test_sound_cue_with_invalid_volume_is_rejected(volume: float) -> None:
    with pytest.raises(ValidationError):
        SoundCue.model_validate(_sound_cue_payload(volume=volume))


def _reference_slot_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "index": 1,
        "role": "主角",
        "purpose": "锁定角色外观",
    }
    payload.update(overrides)
    return payload


def test_reference_slot_with_valid_index_and_purpose_is_constructed() -> None:
    result = ReferenceSlot.model_validate(_reference_slot_payload())

    assert result.index == 1
    assert result.purpose == "锁定角色外观"


@pytest.mark.parametrize("index", [0, -1])
def test_reference_slot_with_index_below_one_is_rejected(index: int) -> None:
    with pytest.raises(ValidationError):
        ReferenceSlot.model_validate(_reference_slot_payload(index=index))


@pytest.mark.parametrize("purpose", [None, ""])
def test_reference_slot_with_empty_purpose_is_rejected(purpose: str | None) -> None:
    with pytest.raises(ValidationError):
        ReferenceSlot.model_validate(_reference_slot_payload(purpose=purpose))


def test_pipeline_model_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        PipelineModel.model_validate({"unknown_field": "not allowed"})


def test_stage_gate_result_constructs_with_blocking_diagnostic() -> None:
    result = StageGateResult(
        status="blocked",
        passed=False,
        diagnostics=[
            Diagnostic(
                level="blocking",
                code="missing_input",
                message="缺少必要输入",
            )
        ],
        can_confirm=False,
    )

    assert result.status == "blocked"
    assert result.passed is False
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0].level == "blocking"
    assert result.diagnostics[0].code == "missing_input"


def test_workflow_stage_draft_with_stage_key_is_constructible() -> None:
    result = WorkflowStageDraft(stage_key="0-source")

    assert result.stage_key == "0-source"
    assert result.raw_output == {}
    assert result.diagnostics == []


@pytest.mark.parametrize("reuse_mode", ["new", "approved_existing"])
def test_asset_mapping_with_supported_reuse_mode_is_constructed(reuse_mode: str) -> None:
    result = AssetMapping.model_validate(_asset_mapping_payload(reuse_mode=reuse_mode))

    assert result.reuse_mode == reuse_mode


def _asset_mapping_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "workflow_asset_id": "workflow-asset-1",
        "usage": "角色参考",
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize("reuse_mode", ["reuse", "approved", ""])
def test_asset_mapping_with_invalid_reuse_mode_is_rejected(reuse_mode: str) -> None:
    with pytest.raises(ValidationError):
        AssetMapping.model_validate(_asset_mapping_payload(reuse_mode=reuse_mode))


@pytest.mark.parametrize("locked_revision", [None, 1, 3])
def test_asset_mapping_with_valid_locked_revision_is_constructed(
    locked_revision: int | None,
) -> None:
    result = AssetMapping.model_validate(_asset_mapping_payload(locked_revision=locked_revision))

    assert result.locked_revision == locked_revision


@pytest.mark.parametrize("locked_revision", [0, -1])
def test_asset_mapping_with_non_positive_locked_revision_is_rejected(
    locked_revision: int,
) -> None:
    with pytest.raises(ValidationError):
        AssetMapping.model_validate(_asset_mapping_payload(locked_revision=locked_revision))


def _artifact_meta_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "dynamic-comic-phase-2",
        "normalizer_version": "normalizer-1",
        "input_signature": "input-signature",
        "content_signature": "content-signature",
        "source_attempt": 1,
    }
    payload.update(overrides)
    return payload


def _source_character_ref_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "character_key": "hero",
        "canonical_name": "林默",
        "aliases": ["小林"],
        "importance": "core",
        "story_function": "推动冲突并完成反击",
        "source_refs": ["p1-l3"],
    }
    payload.update(overrides)
    return payload


def _source_scene_shootability_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": "shootable",
        "visible_action": "林默抬手挡住落下的刀",
        "required_asset_keys": ["hero", "knife"],
        "production_risks": [],
        "adaptation_note": None,
    }
    payload.update(overrides)
    return payload


def _source_scene_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "scene_id": "scene-1",
        "ordinal": 1,
        "location": "废弃仓库",
        "time_weather": "夜，暴雨",
        "characters": [_source_character_ref_payload()],
        "narrative_task": "建立危机",
        "plot_event": "反派突然袭击",
        "conflict_or_turn": "主角被迫迎战",
        "visual_focus": "刀锋与主角的手臂",
        "estimated_duration_sec": 8.0,
        "shootability": _source_scene_shootability_payload(),
        "source_refs": ["p1-l3"],
    }
    payload.update(overrides)
    return payload


def _source_analysis_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "artifact_meta": _artifact_meta_payload(),
        "metadata": {
            "source_kind": "full_text",
            "title": "雨夜反击",
            "chapter_title": "第一章",
            "source_signature": "source-signature",
            "target_duration_sec": 30,
            "aspect_ratio": "9:16",
        },
        "episode_logline": "林默在雨夜仓库中完成第一次反击。",
        "scenes": [_source_scene_payload()],
        "character_roster": [_source_character_ref_payload()],
        "conflict_turns": [
            {
                "kind": "conflict",
                "summary": "反派发动袭击",
                "scene_id": "scene-1",
                "participants": ["hero", "villain"],
                "source_refs": ["p1-l3"],
            }
        ],
        "visualizable_content": ["雨夜仓库", "格挡动作"],
        "hard_to_visualize": [],
        "rewrite_requirements": [],
        "assumptions": ["反派身份沿用章节设定"],
        "open_questions": [],
        "continuation_outline": ["下一集揭示袭击者身份"],
    }
    payload.update(overrides)
    return payload


def _character_visual_anchor_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "visual_facts": ["左眉有一道浅疤"],
        "face": "窄脸，锐利眉眼",
        "hair": "黑色短发，额前碎发",
        "body": "清瘦但有力量感",
        "costume": "深灰风衣与黑色长裤",
        "accessories": ["银色耳钉"],
        "immutable_features": ["左眉浅疤", "银色耳钉"],
        "forbidden_changes": ["不得改为长发"],
        "makeup_keyword_string": "窄脸|锐利眉眼|黑色短发|左眉浅疤",
        "variants": ["雨夜湿身"],
        "current_variant": "雨夜湿身",
        "approved_reference_ids": [],
    }
    payload.update(overrides)
    return payload


def _character_bios_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "artifact_meta": _artifact_meta_payload(),
        "characters": [
            {
                "basic_info": {
                    "character_key": "hero",
                    "canonical_name": "林默",
                    "aliases": ["小林"],
                    "importance": "core",
                    "identity": "被追杀的年轻剑客",
                    "age": "二十岁左右",
                    "gender": "男",
                },
                "background": "曾是边城守卫。",
                "personality_behavior_logic": "谨慎观察后迅速行动。",
                "dynamic_comic_setting": "受伤时仍保持左手护心的习惯。",
                "plot_relation": "本集冲突的承受者与反击者。",
                "visual_anchor": _character_visual_anchor_payload(),
            }
        ],
        "ensemble_anchors": [
            {
                "ensemble_key": "guards",
                "canonical_name": "城门守卫群",
                "visual_facts": ["统一深蓝短甲"],
                "usage_notes": "只作为背景群像出现",
            }
        ],
        "unresolved_character_refs": [],
    }
    payload.update(overrides)
    return payload


def _rhythm_segment_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "segment_id": "segment-1",
        "ordinal": 1,
        "start_sec": 0.0,
        "end_sec": 3.0,
        "beat_type": "hook",
        "narrative_task": "快速建立危机",
        "event_summary": "刀锋突然逼近",
        "visual_action": "主角抬手格挡",
        "focus_characters": ["hero"],
        "dynamic_level": 0.8,
        "strong_dynamic_event_id": "dynamic-1",
        "source_refs": ["scene-1"],
    }
    payload.update(overrides)
    return payload


def _rhythm_output_payload(**overrides: object) -> dict[str, object]:
    strong_event = {
        "event_id": "dynamic-1",
        "summary": "主角完成第一次格挡",
        "segment_id": "segment-1",
        "dynamic_level": 1.0,
        "visual_action": "刀锋被弹开",
        "source_refs": ["scene-1"],
    }
    payload: dict[str, object] = {
        "target_duration_sec": 30.0,
        "artifact_meta": _artifact_meta_payload(),
        "timeline": [_rhythm_segment_payload()],
        "core_payoff": strong_event,
        "small_beats": ["雨声压迫感", "敌人短暂迟疑"],
        "strong_dynamic_budget": {
            "max_events": 1,
            "event_ids": ["dynamic-1"],
            "events": [strong_event],
        },
        "foreshadowing": ["敌人认出主角身份"],
        "open_space": ["保留下一集揭示身份的空间"],
        "dialogue_suggestions": [
            {
                "segment_id": "segment-1",
                "speaker": "林默",
                "text": "来者何人？",
                "purpose": "确认敌意",
                "source_refs": ["scene-1"],
            }
        ],
        "sound_suggestions": [
            {
                "segment_id": "segment-1",
                "kind": "impact",
                "description": "金属碰撞声",
                "intensity": 0.9,
                "source_refs": ["scene-1"],
            }
        ],
        "assumptions": [],
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    ("model", "payload_factory"),
    [
        pytest.param(SourceAnalysisOutput, _source_analysis_payload, id="source-analysis"),
        pytest.param(CharacterBiosOutput, _character_bios_payload, id="character-bios"),
        pytest.param(RhythmOutput, _rhythm_output_payload, id="rhythm"),
    ],
)
def test_phase2_output_happy_path_is_constructible(
    model: type[BaseModel],
    payload_factory: Callable[[], dict[str, object]],
) -> None:
    result = model.model_validate(payload_factory())

    assert result.model_config.get("extra") == "forbid"


@pytest.mark.parametrize(
    ("model", "payload_factory"),
    [
        pytest.param(SourceAnalysisOutput, _source_analysis_payload, id="source-analysis"),
        pytest.param(CharacterBiosOutput, _character_bios_payload, id="character-bios"),
        pytest.param(RhythmOutput, _rhythm_output_payload, id="rhythm"),
    ],
)
def test_phase2_output_rejects_unknown_fields(
    model: type[BaseModel],
    payload_factory: Callable[[], dict[str, object]],
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload_factory() | {"unknown_field": "not allowed"})


def test_source_scene_and_shootability_keep_required_fields() -> None:
    scene = SourceScene.model_validate(_source_scene_payload())
    shootability = SourceSceneShootability.model_validate(_source_scene_shootability_payload())

    assert scene.shootability.status == "shootable"
    assert scene.characters[0].importance == "core"
    assert shootability.visible_action == "林默抬手挡住落下的刀"


def test_character_visual_anchor_allows_empty_approved_references() -> None:
    result = CharacterVisualAnchor.model_validate(_character_visual_anchor_payload())

    assert result.immutable_features == ["左眉浅疤", "银色耳钉"]
    assert result.approved_reference_ids == []


@pytest.mark.parametrize(
    "schema", [Importance, ShootabilityStatus, ConflictTurnKind, RhythmBeatType]
)
def test_phase2_literal_rejects_unknown_value(schema: Any) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(schema).validate_python("unknown-value")


@pytest.mark.parametrize(
    ("model", "payload_factory", "field", "value"),
    [
        pytest.param(
            ArtifactMeta,
            _artifact_meta_payload,
            "source_attempt",
            0,
            id="artifact-attempt",
        ),
        pytest.param(
            SourceScene,
            _source_scene_payload,
            "estimated_duration_sec",
            -1,
            id="scene-duration",
        ),
        pytest.param(
            RhythmOutput,
            _rhythm_output_payload,
            "target_duration_sec",
            -1,
            id="rhythm-duration",
        ),
    ],
)
def test_phase2_models_reject_invalid_non_negative_values(
    model: type[BaseModel],
    payload_factory: Callable[[], dict[str, object]],
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload_factory() | {field: value})


@pytest.mark.parametrize(
    ("model", "payload_factory", "field"),
    [
        pytest.param(
            ArtifactMeta,
            _artifact_meta_payload,
            "schema_version",
            id="artifact-schema-version",
        ),
        pytest.param(
            SourceScene,
            _source_scene_payload,
            "location",
            id="scene-location",
        ),
        pytest.param(
            RhythmSegment,
            _rhythm_segment_payload,
            "narrative_task",
            id="segment-narrative-task",
        ),
    ],
)
def test_phase2_models_reject_empty_required_text(
    model: type[BaseModel],
    payload_factory: Callable[[], dict[str, object]],
    field: str,
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload_factory() | {field: ""})


@pytest.mark.parametrize(
    ("start_sec", "end_sec"),
    [
        pytest.param(1.0, 1.0, id="equal"),
        pytest.param(2.0, 1.0, id="decreasing"),
    ],
)
def test_rhythm_segment_requires_end_after_start(start_sec: float, end_sec: float) -> None:
    with pytest.raises(ValidationError):
        RhythmSegment.model_validate(_rhythm_segment_payload(start_sec=start_sec, end_sec=end_sec))


@pytest.mark.parametrize("dynamic_level", [-0.1, 1.1])
def test_rhythm_segment_rejects_dynamic_level_outside_zero_to_one(dynamic_level: float) -> None:
    with pytest.raises(ValidationError):
        RhythmSegment.model_validate(_rhythm_segment_payload(dynamic_level=dynamic_level))


def test_rhythm_output_accepts_thirty_second_segment_without_five_second_minimum() -> None:
    result = RhythmOutput.model_validate(
        _rhythm_output_payload(
            timeline=[_rhythm_segment_payload(start_sec=0, end_sec=30)],
        )
    )

    assert result.target_duration_sec == 30
    assert result.timeline[0].end_sec == 30


def test_stage_artifact_envelope_and_draft_are_constructible() -> None:
    meta = ArtifactMeta.model_validate(_artifact_meta_payload())
    normalized = SourceAnalysisOutput.model_validate(_source_analysis_payload())
    envelope = StageArtifactEnvelope(meta=meta, content=normalized)
    draft = StageArtifactDraft(stage_key="0-source", meta=meta, content=normalized)
    raw_envelope = StageArtifactEnvelope(meta=meta, content={"provider_field": "raw"})

    assert envelope.content is normalized
    assert draft.stage_key == "0-source"
    assert raw_envelope.content == {"provider_field": "raw"}


def test_stage_artifact_envelope_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        StageArtifactEnvelope.model_validate(
            {
                "meta": _artifact_meta_payload(),
                "content": {"provider_field": "raw"},
                "unknown_field": "not allowed",
            }
        )
