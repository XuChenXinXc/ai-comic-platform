from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.dynamic_pipeline.prompt_ir import (
    AssetVisualAnchor,
    CompiledPromptBundle,
    Diagnostic,
    HumanOverrideSet,
    ImagePromptBundle,
    ProviderCapabilities,
    ReferencePlan,
    ShotIntent,
    TimelineSegment,
    VideoPromptBundle,
    VisualStyleSpec,
    apply_human_override,
    canonical_prompt_json,
    prompt_input_hash,
)


def test_prompt_ir_models_forbid_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        VisualStyleSpec.model_validate({"medium": "2D", "unknown_field": "not-allowed"})

    with pytest.raises(ValidationError):
        ShotIntent.model_validate({"unknown_field": "not-allowed"})


def test_canonical_prompt_json_is_stable_for_dictionary_order() -> None:
    left = {"style": {"mood": "quiet", "medium": "2D"}, "assets": ["hero"]}
    right = {"assets": ["hero"], "style": {"medium": "2D", "mood": "quiet"}}

    assert canonical_prompt_json(left) == canonical_prompt_json(right)
    assert prompt_input_hash(left) == prompt_input_hash(right)


def test_prompt_input_hash_changes_for_asset_provider_compiler_and_override_versions() -> None:
    base = {
        "asset_versions": [{"asset_id": "hero", "revision": 1}],
        "provider_capabilities": {"provider_id": "mock", "capability_version": "1"},
        "compiler_version": "compiler-1",
        "human_override": {"cleared": True, "override_version": "1"},
    }

    assert prompt_input_hash(base) != prompt_input_hash(
        {**base, "asset_versions": [{"asset_id": "hero", "revision": 2}]}
    )
    assert prompt_input_hash(base) != prompt_input_hash(
        {**base, "provider_capabilities": {"provider_id": "mock", "capability_version": "2"}}
    )
    assert prompt_input_hash(base) != prompt_input_hash({**base, "compiler_version": "compiler-2"})
    assert prompt_input_hash(base) != prompt_input_hash(
        {**base, "human_override": {"cleared": False, "override_version": "2"}}
    )


def test_visual_style_and_asset_anchor_keep_supplemental_text_and_explicit_details() -> None:
    style = VisualStyleSpec(
        medium="2D digital comic",
        line_style="clean contour",
        color_palette=["warm amber"],
        style_prompt="用户补充偏好",
    )
    anchor = AssetVisualAnchor(
        canonical_name="hero",
        asset_id="asset-hero-1",
        asset_type="character",
        asset_revision=3,
        visual_facts=["短黑发"],
        immutable_features=["左眉疤痕"],
        details={"owner": "project-a"},
    )

    assert style.style_prompt == "用户补充偏好"
    assert style.immutable_rules == []
    assert anchor.asset_id == "asset-hero-1"
    assert anchor.asset_revision == 3
    assert anchor.details == {"owner": "project-a"}


def test_reference_plan_uses_current_reference_slot_shape() -> None:
    plan = ReferencePlan.model_validate(
        {
            "slots": [
                {
                    "index": 1,
                    "role": "hero",
                    "purpose": "clean_identity",
                    "asset_id": "asset-hero-1",
                }
            ],
            "actual_local_files": ["assets/hero.png"],
            "actual_urls": ["https://example.test/hero.png"],
            "text_mapping": {"@Image1": "assets/hero.png"},
            "snapshot_hash": "snapshot-1",
        }
    )

    assert plan.slots[0].index == 1
    assert plan.text_mapping["@Image1"] == "assets/hero.png"


def test_image_prompt_bundle_requires_zero_keyframe_time() -> None:
    image = ImagePromptBundle(prompt="静止关键帧")

    assert image.keyframe_time_sec == 0
    with pytest.raises(ValidationError):
        ImagePromptBundle.model_validate({"prompt": "错误时间", "keyframe_time_sec": 1})


def test_video_timeline_segment_requires_end_after_start() -> None:
    segment = TimelineSegment(start=0, end=1.5, action="抬手")
    video = VideoPromptBundle(
        prompt="视频动作",
        edit_duration_sec=1.5,
        generation_duration_sec=1.5,
        timeline=[segment],
        end_state={"pose": "hand_up"},
    )

    assert video.timeline[0].end == 1.5
    with pytest.raises(ValidationError):
        TimelineSegment(start=1, end=1, action="无效段")


def test_human_override_builds_final_layer_without_mutating_generated_layer() -> None:
    generated = {
        "image_prompt": "自动图片稿",
        "video_prompt": "自动视频稿",
        "negative_prompt": "默认负向",
        "camera": "中景",
    }
    override = HumanOverrideSet(
        image_prompt="人工图片稿",
        video_prompt="人工视频稿",
        negative_prompt="追加禁止文字",
        camera="近景",
    )

    final = apply_human_override(generated, override)

    assert final == {
        "image_prompt": "人工图片稿",
        "video_prompt": "人工视频稿",
        "negative_prompt": "追加禁止文字",
        "camera": "近景",
    }
    assert generated["image_prompt"] == "自动图片稿"


def test_human_override_applies_to_strict_nested_bundles() -> None:
    generated = {
        "image": ImagePromptBundle(prompt="自动图片"),
        "video": VideoPromptBundle(prompt="自动视频", end_state={"pose": "start"}),
    }
    override = HumanOverrideSet(
        image_prompt="人工图片",
        video_prompt="人工视频",
        negative_prompt="禁止 logo",
        end_state={"pose": "finished"},
    )

    final = apply_human_override(generated, override)

    assert isinstance(final, dict)
    image = final["image"]
    video = final["video"]
    assert isinstance(image, ImagePromptBundle)
    assert isinstance(video, VideoPromptBundle)
    assert image.prompt == "人工图片"
    assert video.prompt == "人工视频"
    assert image.negative_prompt == "禁止 logo"
    assert video.end_state == {"pose": "finished"}


def test_compiled_bundle_constructs_diagnostics_and_provider_capability_snapshot() -> None:
    diagnostic = Diagnostic(
        level="warning",
        code="STYLE_INCOMPLETE",
        message="风格规范不完整",
    )
    capabilities = ProviderCapabilities(
        provider_id="mock",
        model="mock-image-video",
        supports_multiple_images=True,
        max_reference_images=3,
        supported_durations=[2, 4, 6],
        min_duration_sec=2,
        max_duration_sec=6,
        duration_step_sec=2,
        supports_end_frame=True,
        supports_audio=True,
        supports_dialogue=True,
        capability_version="cap-1",
    )
    bundle = CompiledPromptBundle(
        image=ImagePromptBundle(prompt="图片"),
        diagnostics=[diagnostic],
        provenance={
            "asset_versions": [{"asset_id": "hero", "revision": 1}],
            "provider_capabilities": capabilities.model_dump(mode="json"),
        },
    )

    assert bundle.status == "ready"
    assert bundle.diagnostics[0].level == "warning"
    assert bundle.image is not None
    assert bundle.base_compiler_hash
    assert bundle.input_hash
