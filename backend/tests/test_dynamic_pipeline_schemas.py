from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from app.dynamic_pipeline.schemas import (
    AssetMapping,
    DeliverySubstage,
    Diagnostic,
    DialogueCue,
    PipelineModel,
    ReferenceSlot,
    SoundCue,
    SourceKind,
    StageGateResult,
    StageKey,
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
