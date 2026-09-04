from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.dynamic_pipeline.media_bridge import (
    build_keyframe_media_plan,
    build_video_media_plan,
    to_job_payload,
    to_media_version_payload,
)
from app.dynamic_pipeline.prompt_ir import CompiledPromptBundle, ImagePromptBundle


def _compiled(
    *,
    prompt: str = "Subject: hero",
    video_prompt: str = "Subject: hero\nAction: walk\nEnd state: stop\ntransition=scene_change",
    duration: float = 6,
    transition: str = "scene_change",
    status: str = "ready",
    diagnostics: list[dict[str, object]] | None = None,
    timeline: list[dict[str, object]] | None = None,
) -> CompiledPromptBundle:
    return CompiledPromptBundle.model_validate(
        {
            "status": status,
            "image": {
                "prompt": prompt,
                "negative_prompt": "no text",
                "keyframe_time_sec": 0,
                "reference_slots": [],
                "semantic_plan": {"current_state": "standing"},
            },
            "video": {
                "prompt": video_prompt,
                "negative_prompt": "no text",
                "edit_duration_sec": duration,
                "generation_duration_sec": 6,
                "timeline": timeline
                if timeline is not None
                else [{"start": 0, "end": 6, "action": "walk"}],
                "end_state": "stop",
                "reference_slots": [],
                "semantic_plan": {
                    "continuity": {"transition_type": transition},
                },
            },
            "diagnostics": diagnostics or [],
            "provenance": {
                "style_version": "style-1",
                "asset_versions": [{"asset_id": "asset-1", "revision": 2}],
                "provider_capabilities": {
                    "provider_id": "image-provider",
                    "model": "video-model",
                    "supports_multiple_images": True,
                    "max_reference_images": 3,
                },
            },
        }
    )


def _reference() -> dict[str, object]:
    return {
        "slots": [
            {
                "index": 1,
                "role": "hero",
                "purpose": "clean_identity",
                "provider_url": "https://cdn.example.test/hero.png",
            },
            {
                "index": 2,
                "role": "layout",
                "purpose": "layout_reference",
                "relative_path": "shots/layout.png",
            },
        ],
        "actual_local_files": ["shots/layout.png"],
        "actual_urls": ["https://cdn.example.test/hero.png"],
        "text_mapping": {
            "@Image1": {
                "index": 1,
                "label": "hero",
                "source": "url",
                "url": "https://cdn.example.test/hero.png",
            },
            "@Image2": {
                "index": 2,
                "label": "layout",
                "source": "local",
                "local_file": "shots/layout.png",
            },
        },
        "snapshot_hash": "reference-1",
    }


def _kwargs() -> dict[str, Any]:
    return {
        "workflow_run_id": "run-1",
        "workflow_stage_id": "stage-5",
        "shot_id": "shot-1",
        "external_key": "external-1",
        "input_signature": "shot-input-1",
        "provider_id": "provider-1",
        "provider_model": "model-1",
    }


def test_keyframe_plan_is_static_and_preserves_reference_arrays_and_provenance() -> None:
    bundle = _compiled()
    plan = build_keyframe_media_plan(
        bundle.model_copy(deep=True),
        **_kwargs(),
        reference_plan=_reference(),
    )

    assert plan.kind == "image"
    assert isinstance(plan.prompt_bundle, ImagePromptBundle)
    assert plan.prompt_bundle.keyframe_time_sec == 0
    assert plan.reference_inputs.local == ["shots/layout.png"]
    assert plan.reference_inputs.url == ["https://cdn.example.test/hero.png"]
    assert plan.provenance_json["workflow_run_id"] == "run-1"
    assert plan.provenance_json["workflow_stage_id"] == "stage-5"
    assert plan.provenance_json["compiler_hash"]
    assert plan.provenance_json["style_version"] == "style-1"
    assert plan.provenance_json["reference_plan_hash"] == "reference-1"
    assert plan.status == "ready"


def test_keyframe_signature_is_stable_and_changes_with_input() -> None:
    bundle = _compiled()
    first = build_keyframe_media_plan(bundle, **_kwargs(), reference_plan=_reference())
    repeated = build_keyframe_media_plan(
        bundle.model_copy(deep=True), **_kwargs(), reference_plan=deepcopy(_reference())
    )
    changed = build_keyframe_media_plan(
        bundle,
        **{**_kwargs(), "input_signature": "shot-input-2"},
        reference_plan=_reference(),
    )

    assert first.model_dump(mode="json") == repeated.model_dump(mode="json")
    assert first.input_signature == repeated.input_signature
    assert first.idempotency_key == repeated.idempotency_key
    assert first.input_signature != changed.input_signature
    assert first.idempotency_key != changed.idempotency_key


def test_video_plan_keeps_edit_and_generation_duration_and_keyframe_dependency() -> None:
    bundle = _compiled(duration=5)
    plan = build_video_media_plan(
        bundle,
        **_kwargs(),
        reference_plan=_reference(),
        keyframe_job_id="job-keyframe-1",
    )

    assert plan.kind == "video"
    assert plan.requested_duration_sec == 5
    assert plan.generation_duration_sec == 6
    assert plan.depends_on == ["job-keyframe-1"]
    assert plan.provenance_json["requested_duration_sec"] == 5
    assert plan.provenance_json["generation_duration_sec"] == 6
    assert plan.status == "ready"


def test_continuous_video_without_previous_end_frame_is_blocked() -> None:
    bundle = _compiled(transition="continuous")
    plan = build_video_media_plan(
        bundle,
        **_kwargs(),
        keyframe_job_id="job-keyframe-1",
        previous_video_job_id="job-video-0",
    )

    assert plan.status == "blocked"
    assert any(
        item.code == "continuous_previous_end_frame_missing" and item.level == "blocking"
        for item in plan.diagnostics
    )
    assert plan.depends_on == ["job-keyframe-1", "job-video-0"]


def test_first_continuous_video_has_no_previous_end_frame_dependency() -> None:
    plan = build_video_media_plan(
        _compiled(transition="continuous"),
        **_kwargs(),
        keyframe_job_id="job-keyframe-1",
    )

    assert plan.status == "ready"
    assert plan.depends_on == ["job-keyframe-1"]


def test_reference_array_mismatch_is_blocking_without_rewriting_plan() -> None:
    reference = _reference()
    reference["actual_urls"] = []
    plan = build_keyframe_media_plan(
        _compiled(
            prompt="Subject: hero @Image1 and @Image2",
        ),
        **_kwargs(),
        reference_plan=reference,
    )

    assert plan.status == "blocked"
    assert any(item.code == "reference_index_mismatch" for item in plan.diagnostics)
    assert plan.reference_inputs.url == []
    assert plan.reference_inputs.local == ["shots/layout.png"]


def test_image_motion_diagnostic_blocks_enqueue_state() -> None:
    plan = build_keyframe_media_plan(
        _compiled(
            prompt="Subject: hero\nTimeline: 0-6s\nAction: complete",
        ),
        **_kwargs(),
    )

    assert plan.status == "blocked"
    assert to_job_payload(plan)["queueable"] is False
    assert to_job_payload(plan)["status"] == "blocked"


def test_video_empty_timeline_is_blocked_with_explicit_diagnostic() -> None:
    plan = build_video_media_plan(
        _compiled(timeline=[]),
        **_kwargs(),
        keyframe_job_id="job-keyframe-1",
    )

    assert plan.status == "blocked"
    empty = [item for item in plan.diagnostics if item.code == "timeline_discontinuous"]
    assert empty and empty[0].details.get("reason") == "empty_timeline"
    assert to_job_payload(plan)["queueable"] is False


def test_video_timeline_with_gap_between_segments_is_blocked() -> None:
    plan = build_video_media_plan(
        _compiled(
            timeline=[
                {"start": 0, "end": 2, "action": "walk"},
                {"start": 3, "end": 6, "action": "stop"},
            ]
        ),
        **_kwargs(),
        keyframe_job_id="job-keyframe-1",
    )

    assert plan.status == "blocked"
    gap = [item for item in plan.diagnostics if item.code == "timeline_discontinuous"]
    assert gap and gap[0].details.get("reason") == "segment_gap_or_overlap"


def test_video_timeline_with_overlapping_segments_is_blocked() -> None:
    plan = build_video_media_plan(
        _compiled(
            timeline=[
                {"start": 0, "end": 4, "action": "walk"},
                {"start": 2, "end": 6, "action": "stop"},
            ]
        ),
        **_kwargs(),
        keyframe_job_id="job-keyframe-1",
    )

    assert plan.status == "blocked"
    overlap = [item for item in plan.diagnostics if item.code == "timeline_discontinuous"]
    assert overlap and overlap[0].details.get("reason") == "segment_gap_or_overlap"


def test_video_timeline_last_segment_end_mismatch_is_blocked() -> None:
    plan = build_video_media_plan(
        _compiled(
            timeline=[
                {"start": 0, "end": 2, "action": "walk"},
                {"start": 2, "end": 5, "action": "stop"},
            ]
        ),
        **_kwargs(),
        keyframe_job_id="job-keyframe-1",
    )

    assert plan.status == "blocked"
    mismatch = [item for item in plan.diagnostics if item.code == "timeline_discontinuous"]
    assert mismatch and mismatch[0].details.get("reason") == "last_segment_end_mismatch"
    assert mismatch[0].details.get("generation_duration_sec") == 6


def test_video_timeline_first_segment_not_from_zero_is_blocked() -> None:
    plan = build_video_media_plan(
        _compiled(
            timeline=[
                {"start": 1, "end": 6, "action": "walk"},
            ]
        ),
        **_kwargs(),
        keyframe_job_id="job-keyframe-1",
    )

    assert plan.status == "blocked"
    nonzero = [item for item in plan.diagnostics if item.code == "timeline_discontinuous"]
    assert nonzero and nonzero[0].details.get("reason") == "first_segment_start_nonzero"


def test_video_continuous_legal_timeline_stays_ready() -> None:
    plan = build_video_media_plan(
        _compiled(
            timeline=[
                {"start": 0, "end": 2, "action": "walk"},
                {"start": 2, "end": 6, "action": "stop"},
            ]
        ),
        **_kwargs(),
        keyframe_job_id="job-keyframe-1",
    )

    assert plan.status == "ready"
    assert not any(item.code == "timeline_discontinuous" for item in plan.diagnostics)


def test_payload_projections_include_prompt_ir_provenance_and_dependencies() -> None:
    image = build_keyframe_media_plan(_compiled(), **_kwargs())
    video = build_video_media_plan(
        _compiled(transition="continuous"),
        **_kwargs(),
        keyframe_job_id="job-keyframe-1",
        previous_video_job_id="job-video-0",
        previous_end_frame_path="shots/end-0.png",
    )

    media_payload = to_media_version_payload(video)
    job_payload = to_job_payload(video)

    assert media_payload["prompt_ir_json"]
    assert media_payload["provenance_json"]
    assert media_payload["requested_duration_sec"] == 6
    assert media_payload["generation_duration_sec"] == 6
    assert job_payload["job_type"] == "generate_video"
    assert job_payload["target_type"] == "keyframe"
    assert job_payload["depends_on"] == ["job-keyframe-1", "job-video-0"]
    assert job_payload["input_data"]["reference_inputs"]["local"] == []
    assert job_payload["input_signature"] == video.input_signature

    image_job = to_job_payload(image)
    assert image_job["job_type"] == "generate_keyframe"
    assert image_job["target_type"] == "shot"
    assert image_job["depends_on"] == []
