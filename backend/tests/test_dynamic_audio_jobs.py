from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError

from app.dynamic_pipeline.audio_delivery import (
    AudioDeliveryError,
    AudioDeliveryPlan,
    AudioJobPlan,
    DialogueLine,
    audio_job_to_job_payload,
    audio_job_to_shot_audio_payload,
    build_audio_delivery_plan,
    build_audio_job_plans,
    normalize_dialogue_lines,
)


def _lines(dialogues: object) -> list[DialogueLine]:
    return normalize_dialogue_lines(
        dialogues,
        external_key="shot-ext-1",
        shot_id="shot-1",
        source_refs=("screenplay:1",),
    )


def _job_kwargs(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "workflow_run_id": "run-1",
        "workflow_stage_id": "stage-6",
        "shot_id": "shot-1",
        "external_key": "shot-ext-1",
        "input_signature": "storyboard-input-1",
        "provider_id": "tts-provider-1",
        "provider_model": "tts-model-1",
        "voice_by_speaker": {"林默": "voice-a"},
        "params": {"speed": 1.0},
    }
    values.update(overrides)
    return values


def _delivery_kwargs(**overrides: Any) -> dict[str, Any]:
    values = _job_kwargs(**overrides)
    values.pop("shot_id", None)
    values.pop("external_key", None)
    return values


def test_normalize_dialogue_lines_accepts_aliases_and_single_object() -> None:
    result = _lines({"character": "林默", "line": "你终于来了。"})

    assert len(result) == 1
    assert result[0].dialogue_line_id == "shot-ext-1-dialogue-1"
    assert result[0].line_order == 1
    assert result[0].speaker == "林默"
    assert result[0].text == "你终于来了。"
    assert result[0].source_text_snapshot == "你终于来了。"
    assert result[0].source_refs == ["screenplay:1"]


def test_normalize_dialogue_lines_preserves_explicit_ids_and_input() -> None:
    source = [
        {"dialogue_line_id": " stable-line ", "speaker": "甲", "text": "第一句"},
        {"name": "乙", "content": "第二句"},
    ]
    before = deepcopy(source)

    result = _lines(source)

    assert source == before
    assert [line.dialogue_line_id for line in result] == [
        "stable-line",
        "shot-ext-1-dialogue-2",
    ]
    assert [line.line_order for line in result] == [1, 2]


def test_normalize_dialogue_lines_keeps_empty_text_as_blocking_candidate() -> None:
    result = _lines({"speaker": "林默", "text": "  "})

    assert len(result) == 1
    assert result[0].source_text_snapshot == "  "
    plan = build_audio_delivery_plan(
        {"shot-ext-1": {"shot_id": "shot-1", "dialogue": result}},
        **_delivery_kwargs(),
    )
    assert plan.status == "blocked"
    assert plan.jobs == []
    assert plan.diagnostics[0].code == "audio_dialogue_text_missing"


def test_dialogue_and_job_models_are_strict() -> None:
    with pytest.raises(ValidationError):
        DialogueLine.model_validate(
            {
                "dialogue_line_id": "line-1",
                "line_order": 1,
                "shot_id": "shot-1",
                "external_key": "shot-1",
                "source_text_snapshot": "text",
                "unknown": "must-fail",
            }
        )
    with pytest.raises(ValidationError):
        AudioJobPlan.model_validate(
            {
                "workflow_run_id": "run-1",
                "workflow_stage_id": "stage-1",
                "shot_id": "shot-1",
                "external_key": "shot-1",
                "dialogue_line_id": "line-1",
                "line_order": 1,
                "source_text_snapshot": "text",
                "provider_id": "provider-1",
                "input_signature": "signature",
                "idempotency_key": "key",
                "unknown": "must-fail",
            }
        )
    with pytest.raises(ValidationError):
        AudioDeliveryPlan.model_validate(
            {
                "workflow_run_id": "run-1",
                "workflow_stage_id": "stage-1",
                "input_signature": "signature",
                "provider_id": "provider-1",
                "unknown": "must-fail",
            }
        )


def test_build_audio_job_plans_creates_one_job_per_dialogue_without_overwrite() -> None:
    lines = _lines(
        [
            {"speaker": "林默", "text": "第一句"},
            {"speaker": "反派", "text": "第二句"},
        ]
    )

    plans = build_audio_job_plans(lines, **_job_kwargs())

    assert len(plans) == 2
    assert [plan.dialogue_line_id for plan in plans] == [
        "shot-ext-1-dialogue-1",
        "shot-ext-1-dialogue-2",
    ]
    assert [plan.line_order for plan in plans] == [1, 2]
    assert len({plan.idempotency_key for plan in plans}) == 2
    assert all(plan.depends_on == [] for plan in plans)
    assert all(plan.status == "ready" for plan in plans)


def test_audio_job_signature_is_idempotent_and_changes_with_source_voice_provider_or_params() -> (
    None
):
    lines = _lines({"speaker": "林默", "text": "原台词"})
    first = build_audio_job_plans(lines, **_job_kwargs())[0]
    repeated = build_audio_job_plans(deepcopy(lines), **_job_kwargs())[0]
    changed_text = build_audio_job_plans(
        _lines({"speaker": "林默", "text": "新台词"}), **_job_kwargs()
    )[0]
    changed_voice = build_audio_job_plans(
        lines, **_job_kwargs(voice_by_speaker={"林默": "voice-b"})
    )[0]
    changed_provider = build_audio_job_plans(
        lines, **_job_kwargs(provider_id="tts-provider-2", provider_model="model-2")
    )[0]
    changed_params = build_audio_job_plans(lines, **_job_kwargs(params={"speed": 1.1}))[0]

    assert first.model_dump(mode="json") == repeated.model_dump(mode="json")
    assert first.input_signature == repeated.input_signature
    assert first.idempotency_key == repeated.idempotency_key
    assert first.input_signature != changed_text.input_signature
    assert first.input_signature != changed_voice.input_signature
    assert first.input_signature != changed_provider.input_signature
    assert first.input_signature != changed_params.input_signature


def test_audio_job_captures_provider_model_voice_and_source_snapshot() -> None:
    plan = build_audio_job_plans(
        _lines({"speaker": "林默", "text": "原台词"}),
        **_job_kwargs(),
    )[0]

    assert plan.provider_id == "tts-provider-1"
    assert plan.provider_model == "tts-model-1"
    assert plan.voice_id == "voice-a"
    assert plan.source_text_snapshot == "原台词"
    assert plan.provenance["provider_model"] == "tts-model-1"
    assert plan.provenance["voice_id"] == "voice-a"
    assert plan.provenance["source_text_snapshot"] == "原台词"


def test_build_audio_delivery_plan_orders_shots_and_keeps_all_dialogues() -> None:
    plan = build_audio_delivery_plan(
        {
            "b": {
                "shot_id": "shot-2",
                "external_key": "shot-ext-2",
                "dialogues": [{"speaker": "乙", "text": "二"}, {"speaker": "丙", "text": "三"}],
            },
            "a": {
                "shot_id": "shot-1",
                "external_key": "shot-ext-1",
                "dialogue": [{"speaker": "甲", "text": "一"}],
            },
        },
        **_delivery_kwargs(),
    )

    assert plan.status == "ready"
    assert [(line.external_key, line.line_order) for line in plan.lines] == [
        ("shot-ext-1", 1),
        ("shot-ext-2", 1),
        ("shot-ext-2", 2),
    ]
    assert len(plan.jobs) == 3
    assert [job.source_text_snapshot for job in plan.jobs] == ["一", "二", "三"]


def test_audio_job_payload_contains_existing_job_fields_and_tts_contract() -> None:
    plan = build_audio_job_plans(_lines({"speaker": "林默", "text": "请接招"}), **_job_kwargs())[0]

    payload = audio_job_to_job_payload(plan)

    assert payload["job_type"] == "generate_dialogue_audio"
    assert payload["target_type"] == "shot_audio"
    assert payload["provider_kind"] == "tts"
    assert payload["workflow_run_id"] == "run-1"
    assert payload["workflow_stage_id"] == "stage-6"
    assert payload["input_signature"] == plan.input_signature
    assert payload["idempotency_key"] == plan.idempotency_key
    assert payload["depends_on"] == []
    assert payload["input_data"]["source_text_snapshot"] == "请接招"
    assert payload["input_data"]["voice_id"] == "voice-a"
    assert payload["input_data"]["provider_model"] == "tts-model-1"


def test_audio_job_to_shot_audio_payload_writes_dynamic_shot_audio_columns() -> None:
    plan = build_audio_job_plans(_lines({"speaker": "林默", "text": "请接招"}), **_job_kwargs())[0]

    payload = audio_job_to_shot_audio_payload(
        plan,
        audio_id="audio-1",
        version_no=2,
        file_path="audio/shot-1-line-1.mp3",
        duration_sec=1.25,
        provenance_extra={"worker": "test"},
    )

    assert payload["dialogue_line_id"] == plan.dialogue_line_id
    assert payload["line_order"] == 1
    assert payload["source_text_snapshot"] == "请接招"
    assert payload["input_signature"] == plan.input_signature
    provenance = json.loads(payload["provenance_json"])
    assert provenance["dialogue_line_id"] == plan.dialogue_line_id
    assert provenance["worker"] == "test"
    assert payload["is_current"] == 1


def test_audio_job_payload_projection_does_not_call_mock_tts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    async def fail_if_called(**_kwargs: object) -> bytes:
        calls.append(_kwargs)
        raise AssertionError("纯合同构造不应调用 Mock TTS")

    from app.providers.mock import MockTTSProvider

    monkeypatch.setattr(MockTTSProvider, "synthesize", fail_if_called)
    plans = build_audio_job_plans(_lines({"speaker": "林默", "text": "不应调用"}), **_job_kwargs())
    audio_job_to_job_payload(plans[0])
    assert calls == []


def test_build_audio_delivery_plan_reports_invalid_shot_as_blocked() -> None:
    plan = build_audio_delivery_plan(
        {"shot": {"shot_id": "", "external_key": "", "dialogue": []}},
        **_delivery_kwargs(),
    )

    assert plan.status == "blocked"
    assert plan.jobs == []
    assert plan.diagnostics[0].level == "blocking"


def test_audio_payload_rejects_invalid_delivery_values() -> None:
    plan = build_audio_job_plans(_lines({"text": "文本"}), **_job_kwargs())[0]

    with pytest.raises(AudioDeliveryError):
        audio_job_to_shot_audio_payload(
            plan,
            audio_id="audio-1",
            version_no=0,
            file_path="audio.mp3",
            duration_sec=1,
        )
