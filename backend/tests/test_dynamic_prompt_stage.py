from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from app.dynamic_pipeline.media_bridge import to_job_payload
from app.dynamic_pipeline.prompt_stage import (
    PromptStageOutput,
    compile_image_prompt_stage,
    compile_video_prompt_stage,
    stage_output_to_media_plans,
    stage_output_to_stage_json,
)


def _style() -> dict[str, Any]:
    return {
        "medium": "二维动画",
        "line_style": "清晰轮廓线",
        "color_palette": "暖灰与琥珀",
        "lighting_style": "柔和侧光",
        "material_style": "布料和金属可区分",
        "realism_level": "半写实二维",
        "mood": "克制",
        "style_version": "style-stage-1",
    }


def _caps() -> dict[str, Any]:
    return {
        "provider_id": "mock",
        "model": "mock-video",
        "supports_multiple_images": True,
        "max_reference_images": 3,
        "supported_durations": [4, 6, 8],
        "min_duration_sec": 4,
        "max_duration_sec": 8,
        "duration_step_sec": 2,
    }


def _anchor(asset_id: str, name: str, asset_type: str = "character") -> dict[str, Any]:
    return {
        "canonical_name": name,
        "asset_id": asset_id,
        "asset_type": asset_type,
        "visual_facts": [f"{name}视觉事实"],
        "face": "窄脸" if asset_type == "character" else "",
        "hair": "短黑发" if asset_type == "character" else "",
        "body": "瘦高" if asset_type == "character" else "",
        "costume": "深蓝外套" if asset_type == "character" else "",
        "layout": "站台纵深" if asset_type == "scene" else None,
        "shape": "圆形" if asset_type == "prop" else None,
        "material": "铜" if asset_type == "prop" else None,
        "color": "暗金" if asset_type == "prop" else None,
    }


def _plan(label: str, index: int = 1) -> dict[str, Any]:
    return {
        "slots": [
            {
                "index": index,
                "role": label,
                "asset_id": "asset-hero",
                "purpose": "clean_identity",
                "provider_url": f"https://cdn.example.test/{label}.png",
            }
        ],
        "actual_urls": [f"https://cdn.example.test/{label}.png"],
        "text_mapping": {
            f"@Image{index}": {
                "index": index,
                "label": label,
                "source": "url",
                "url": f"https://cdn.example.test/{label}.png",
                "value": f"https://cdn.example.test/{label}.png",
            }
        },
        "snapshot_hash": f"ref-{label}",
    }


def _storyboard() -> dict[str, Any]:
    return {
        "artifact_meta": {
            "schema_version": "dynamic-storyboard-v2",
            "normalizer_version": "dynamic-normalizer-v1",
            "input_signature": "storyboard-input",
            "content_signature": "storyboard-content",
        },
        "target_duration_sec": 10,
        "shots": [
            {
                "external_key": "shot-b",
                "shot_no": 2,
                "ordinal": 2,
                "scene_id": "scene-1",
                "scene_name": "旧车站",
                "scene_asset_id": "asset-station",
                "character_names": ["林舟"],
                "character_asset_ids": ["asset-hero"],
                "prop_names": ["怀表"],
                "prop_asset_ids": ["asset-watch"],
                "start_state": "怀表闭合，林舟站立",
                "action": "抬起怀表",
                "end_state": "怀表打开并停住",
                "camera_move": "向前推",
                "transition_from_prev": "continuous",
                "duration_sec": 5,
            },
            {
                "external_key": "shot-a",
                "shot_no": 1,
                "ordinal": 1,
                "scene_id": "scene-1",
                "scene_name": "旧车站",
                "scene_asset_id": "asset-station",
                "character_names": ["林舟"],
                "character_asset_ids": ["asset-hero"],
                "prop_names": ["怀表"],
                "prop_asset_ids": ["asset-watch"],
                "start_state": "林舟站在站台边",
                "action": "看向远处",
                "end_state": "视线停在轨道尽头",
                "camera_move": "固定",
                "transition_from_prev": "scene_change",
                "duration_sec": 5,
            },
        ],
    }


def _anchors() -> dict[str, dict[str, Any]]:
    return {
        "asset-hero": _anchor("asset-hero", "林舟"),
        "asset-station": _anchor("asset-station", "旧车站", "scene"),
        "asset-watch": _anchor("asset-watch", "怀表", "prop"),
    }


def _common() -> dict[str, Any]:
    return {
        "style": _style(),
        "anchors_by_asset_id": _anchors(),
        "references_by_external_key": {
            "shot-a": _plan("hero-a"),
            "shot-b": _plan("hero-b"),
        },
        "capabilities": _caps(),
        "workflow_run_id": "run-stage",
        "workflow_stage_id": "stage-5",
        "input_signature": "stage-input-1",
        "provider_id": "mock",
        "provider_model": "mock-video",
    }


def test_image_stage_compiles_stable_static_bundles_without_timeline_or_motion() -> None:
    result = compile_image_prompt_stage(_storyboard(), **_common())

    assert isinstance(result, PromptStageOutput)
    assert result.stage_key == "5a-image-prompts"
    assert list(result.bundles) == ["shot-a", "shot-b"]
    for bundle in result.bundles.values():
        assert bundle.video is None
        image = bundle.image
        assert image is not None
        assert image.keyframe_time_sec == 0
        assert "Timeline:" not in image.prompt
        assert "Action:" not in image.prompt
        assert "End state:" not in image.prompt
    image = result.bundles["shot-a"].image
    assert image is not None
    assert image.semantic_plan["action"] is None


def test_video_stage_compiles_one_timeline_and_adapts_duration_from_capabilities() -> None:
    result = compile_video_prompt_stage(
        _storyboard(),
        **_common(),
        previous_end_frames={"shot-b": "shots/shot-a-end.png"},
        previous_references_by_external_key={
            "shot-b": {"previous_shot_id": "shot-a", "previous_end_state": "视线停在轨道尽头"}
        },
    )

    assert result.stage_key == "5b-video-prompts"
    first = result.bundles["shot-a"]
    second = result.bundles["shot-b"]
    assert first.image is None and second.image is None
    assert first.video is not None and second.video is not None
    assert second.video.generation_duration_sec == 4
    assert second.video.edit_duration_sec == 5
    assert len(second.video.timeline) == 1
    assert second.video.timeline[-1].end == 4
    assert "shots/shot-a-end.png" in second.video.prompt
    assert not any(item.level == "blocking" for item in second.diagnostics)


def test_video_continuity_requires_explicit_previous_frame_and_reference_mapping() -> None:
    result = compile_video_prompt_stage(_storyboard(), **_common())

    diagnostics = result.bundles["shot-b"].diagnostics
    codes = {item.code for item in diagnostics}
    assert result.bundles["shot-b"].status == "blocked"
    assert "continuous_previous_end_frame_missing" in codes
    assert "continuous_previous_reference_missing" in codes


def test_missing_anchor_is_blocking_and_does_not_fabricate_anchor() -> None:
    anchors = _anchors()
    del anchors["asset-watch"]
    result = compile_image_prompt_stage(
        _storyboard(),
        **{**_common(), "anchors_by_asset_id": anchors},
    )

    bundle = result.bundles["shot-a"]
    assert bundle.status == "blocked"
    assert any(item.code == "asset_unresolved" for item in bundle.diagnostics)
    assert bundle.image is not None
    assert "怀表视觉事实" not in bundle.image.prompt


def test_stage_provenance_and_serialized_json_keep_input_signature_and_reference_hashes() -> None:
    result = compile_image_prompt_stage(_storyboard(), **_common())
    payload = stage_output_to_stage_json(result)

    assert result.provenance["workflow_run_id"] == "run-stage"
    assert result.provenance["workflow_stage_id"] == "stage-5"
    assert result.provenance["input_signature"] == "stage-input-1"
    assert result.provenance["reference_plan_hashes"]["shot-a"] == "ref-hero-a"
    assert payload["artifact_meta"]["input_signature"] == "stage-input-1"
    assert payload["bundles"]["shot-a"]["provenance"]["external_key"] == "shot-a"
    assert payload["shot_map"]["shot-b"]["ordinal"] == 2
    json.dumps(payload, ensure_ascii=False)


def test_image_stage_media_plan_is_static_and_queueable() -> None:
    result = compile_image_prompt_stage(_storyboard(), **_common())
    plans = stage_output_to_media_plans(
        result,
        provider_id="mock",
        provider_model="mock-video",
    )

    assert [target.kind for target in plans.targets] == ["image", "image"]
    assert all(target.requested_duration_sec is None for target in plans.targets)
    assert all(to_job_payload(target)["queueable"] is True for target in plans.targets)


def test_media_bridge_preserves_order_dependencies_and_idempotency() -> None:
    result = compile_video_prompt_stage(
        _storyboard(),
        **_common(),
        previous_end_frames={"shot-b": "shots/shot-a-end.png"},
        previous_references_by_external_key={"shot-b": {"previous_shot_id": "shot-a"}},
    )
    plans = stage_output_to_media_plans(
        result,
        provider_id="mock",
        provider_model="mock-video",
        keyframe_job_ids={"shot-a": "key-a", "shot-b": "key-b"},
        previous_video_job_ids={"shot-b": "video-a"},
        previous_end_frames={"shot-b": "shots/shot-a-end.png"},
    )
    repeated = stage_output_to_media_plans(
        result,
        provider_id="mock",
        provider_model="mock-video",
        keyframe_job_ids={"shot-a": "key-a", "shot-b": "key-b"},
        previous_video_job_ids={"shot-b": "video-a"},
        previous_end_frames={"shot-b": "shots/shot-a-end.png"},
    )

    assert [target.external_key for target in plans.targets] == ["shot-a", "shot-b"]
    assert plans.targets[1].depends_on == ["key-b", "video-a"]
    assert plans.targets[0].idempotency_key == repeated.targets[0].idempotency_key
    assert plans.targets[1].idempotency_key == repeated.targets[1].idempotency_key


def test_blocked_stage_target_is_not_queueable() -> None:
    result = compile_video_prompt_stage(_storyboard(), **_common())
    plans = stage_output_to_media_plans(
        result,
        provider_id="mock",
        provider_model="mock-video",
        keyframe_job_ids={"shot-a": "key-a", "shot-b": "key-b"},
    )

    blocked = next(target for target in plans.targets if target.external_key == "shot-b")
    assert blocked.status == "blocked"
    assert to_job_payload(blocked)["queueable"] is False
    assert plans.status == "blocked"


def test_stage_compilation_does_not_mutate_storyboard_or_mappings() -> None:
    storyboard = _storyboard()
    common = _common()
    original = deepcopy((storyboard, common))

    compile_image_prompt_stage(storyboard, **common)

    assert (storyboard, common) == original


def test_reference_slot_override_requires_complete_canonical_slot_semantics() -> None:
    common = _common()
    common["overrides_by_external_key"] = {
        "shot-a": {
            "reference_slots": [
                {
                    "index": 1,
                    "role": "hero-a",
                    "asset_id": "asset-hero",
                    "purpose": "prop_identity",
                    "provider_url": "https://cdn.example.test/hero-a.png",
                }
            ]
        }
    }

    result = compile_image_prompt_stage(_storyboard(), **common)

    assert result.status == "blocked"
    codes = {item.code for item in result.bundles["shot-a"].diagnostics}
    assert "reference_slot_override_semantic_mismatch" in codes
