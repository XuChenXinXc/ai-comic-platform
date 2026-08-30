from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from app.dynamic_pipeline.prompt_compiler import (
    COMPILER_VERSION,
    compile_image_prompt,
    compile_prompt_bundle,
    compile_video_prompt,
)
from app.dynamic_pipeline.prompt_ir import HumanOverrideSet
from app.dynamic_pipeline.reference_plan import build_reference_plan


def _style() -> dict[str, object]:
    return {
        "medium": "二维动画",
        "line_style": "清晰轮廓线",
        "color_palette": "暖灰与琥珀",
        "lighting_style": "左上方柔和主光",
        "material_style": "布料和金属有区分",
        "realism_level": "半写实二维比例",
        "mood": "克制紧张",
        "text_policy": "禁止字幕、Logo 和水印",
        "global_negative_rules": ["真人摄影"],
        "immutable_rules": ["保持角色比例和核心轮廓"],
        "style_version": "style-3",
    }


def _anchors() -> list[dict[str, object]]:
    return [
        {
            "canonical_name": "林舟",
            "asset_id": "asset-hero",
            "asset_type": "character",
            "asset_revision": 4,
            "visual_facts": ["左眉疤痕"],
            "immutable_features": ["黑色瞳孔", "左眉疤痕"],
            "forbidden_changes": ["禁止改成金发"],
            "face": "窄脸、左眉有疤",
            "hair": "短黑发",
            "body": "瘦高",
            "costume": "深蓝短外套",
            "accessories": ["铜色怀表"],
        },
        {
            "canonical_name": "旧车站",
            "asset_id": "asset-station",
            "asset_type": "scene",
            "asset_revision": 2,
            "visual_facts": ["湿石地面"],
            "layout": "站台沿画面纵深延伸",
            "axis": "轨道为主轴",
            "fixed_furnishings": ["旧长椅"],
            "lighting_direction": "月光从右侧进入",
        },
        {
            "canonical_name": "怀表",
            "asset_id": "asset-watch",
            "asset_type": "prop",
            "asset_revision": 1,
            "shape": "圆形翻盖",
            "material": "铜",
            "color": "暗金色",
            "current_holder": "林舟右手",
        },
    ]


def _shot(**updates: object) -> dict[str, object]:
    shot: dict[str, object] = {
        "shot_id": "shot-1",
        "subject": "林舟站在站台边",
        "action": "抬起怀表并看向表盘",
        "scene": "旧车站月台",
        "camera": "中近景，缓慢向前推",
        "lighting": "月光勾勒轮廓",
        "audio": "远处列车低鸣",
        "start_state": "林舟右手垂落，怀表闭合",
        "end_state": "怀表打开，林舟视线落在表盘",
        "duration_sec": 6,
    }
    shot.update(updates)
    return shot


def _plan() -> dict[str, object]:
    return {
        "slots": [
            {
                "index": 1,
                "role": "hero",
                "asset_id": "asset-hero",
                "purpose": "clean_identity",
                "provider_url": "https://cdn.example.test/hero.png",
            }
        ],
        "actual_urls": ["https://cdn.example.test/hero.png"],
        "text_mapping": {
            "@Image1": {
                "index": 1,
                "label": "hero",
                "source": "url",
                "url": "https://cdn.example.test/hero.png",
                "value": "https://cdn.example.test/hero.png",
            }
        },
        "snapshot_hash": "reference-snapshot-1",
    }


def test_compile_bundle_is_deterministic_and_hashes_all_upstream_inputs() -> None:
    inputs = {
        "style": _style(),
        "shot": _shot(),
        "anchors": _anchors(),
        "reference_plan": _plan(),
    }
    first = compile_prompt_bundle(**inputs)
    second = compile_prompt_bundle(**deepcopy(inputs))

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.compiler_version == COMPILER_VERSION
    assert first.input_hash
    assert first.base_compiler_hash
    assert first.input_hash != first.base_compiler_hash
    assert first.provenance["reference_plan_hash"] == "reference-snapshot-1"

    changed_asset = deepcopy(inputs)
    changed_asset["anchors"][0]["asset_revision"] = 5  # type: ignore[index]
    changed_style = deepcopy(inputs)
    changed_style["style"]["style_version"] = "style-4"  # type: ignore[index]
    changed_shot = deepcopy(inputs)
    changed_shot["shot"]["camera"] = "固定机位"  # type: ignore[index]

    assert compile_prompt_bundle(**changed_asset).input_hash != first.input_hash
    assert compile_prompt_bundle(**changed_style).input_hash != first.input_hash
    assert compile_prompt_bundle(**changed_shot).input_hash != first.input_hash


def test_image_compiler_emits_static_current_state_without_video_timeline_or_completion() -> None:
    result = compile_image_prompt(style=_style(), shot=_shot(), anchors=_anchors())

    assert result.keyframe_time_sec == 0
    assert "Current state:" in result.prompt
    assert "Timeline:" not in result.prompt
    assert "Action:" not in result.prompt
    assert "End state:" not in result.prompt
    assert "抬起怀表并看向表盘" not in result.prompt
    assert "怀表打开，林舟视线落在表盘" not in result.prompt
    for fact in (
        "窄脸、左眉有疤",
        "短黑发",
        "深蓝短外套",
        "铜色怀表",
        "站台沿画面纵深延伸",
        "圆形翻盖",
        "林舟右手",
    ):
        assert fact in result.prompt


@pytest.mark.parametrize(
    ("requested", "expected_generation", "expected_edit"),
    [(6, 6, 6), (5, 6, 5), (7, 6, 7)],
)
def test_video_compiler_adapts_generation_duration_without_changing_edit_duration(
    requested: float,
    expected_generation: float,
    expected_edit: float,
) -> None:
    result = compile_video_prompt(
        style=_style(),
        shot=_shot(duration_sec=requested),
        anchors=_anchors(),
    )

    assert result.generation_duration_sec == expected_generation
    assert result.edit_duration_sec == expected_edit
    assert result.timeline[0].start == 0
    assert result.timeline[-1].end == expected_generation


def test_video_compiler_has_one_action_camera_end_state_and_continuous_timeline() -> None:
    result = compile_video_prompt(style=_style(), shot=_shot(), anchors=_anchors())

    assert len(result.timeline) == 1
    assert result.semantic_plan["action"] == "抬起怀表并看向表盘"
    assert result.semantic_plan["camera"] == "中近景，缓慢向前推"
    assert result.end_state == "怀表打开，林舟视线落在表盘"
    for label in (
        "Subject:",
        "Action:",
        "Scene:",
        "Camera:",
        "Lighting/Style:",
        "Audio:",
        "Timeline:",
        "Continuity:",
        "Negative:",
        "Reference map:",
    ):
        assert label in result.prompt


def test_video_compiler_splits_action_and_audio_cues_into_ordered_timeline() -> None:
    result = compile_video_prompt(
        style=_style(),
        shot=_shot(
            action_events=[
                {"start_sec": 0, "end_sec": 2, "action": "抬起怀表"},
                {"start_sec": 2, "end_sec": 4, "action": "打开表盖"},
            ],
            dialogue_cues=[
                {
                    "dialogue_line_id": "line-1",
                    "speaker": "林舟",
                    "text": "看清楚了。",
                    "start_time": 2,
                    "end_time": 3,
                }
            ],
            sound_cues=[
                {
                    "kind": "sfx",
                    "description": "表盖弹开",
                    "start_time": 2,
                    "end_time": 2.5,
                }
            ],
        ),
        anchors=_anchors(),
    )

    assert len(result.timeline) == 5
    assert [(item.start, item.end) for item in result.timeline] == [
        (0, 2),
        (2, 2.5),
        (2.5, 3),
        (3, 4),
        (4, 6),
    ]
    assert "对白：2-3s 林舟: 看清楚了。" in result.prompt
    assert "音效：2-2.5s sfx: 表盖弹开" in result.prompt
    assert "single action" not in result.prompt


def test_compiler_reports_missing_asset_action_end_and_continuous_previous_frame() -> None:
    missing = compile_prompt_bundle(
        style=_style(),
        shot=_shot(action=None, end_state=None, is_continuous=True),
        anchors=[{"canonical_name": "林舟", "asset_type": "character"}],
        continuity={"transition_type": "continuous"},
    )
    codes = {item.code for item in missing.diagnostics}

    assert missing.status == "blocked"
    assert "asset_unresolved" in codes
    assert "video_action_missing" in codes
    assert "video_end_state_missing" in codes
    assert "continuous_previous_end_frame_missing" in codes


def test_compiler_injects_negative_rules_and_merges_reference_text_consistently() -> None:
    result = compile_prompt_bundle(
        style=_style(),
        shot=_shot(),
        anchors=_anchors(),
        reference_plan=_plan(),
    )

    assert "真人摄影" in result.image.negative_prompt
    assert "禁止改成金发" in result.video.negative_prompt
    assert "禁止字幕、Logo 和水印" in result.video.negative_prompt
    assert "@Image1[hero]" in result.image.prompt
    assert "@Image1[hero]" in result.video.prompt
    assert [slot.index for slot in result.video.reference_slots] == [1]
    assert not any(item.code == "reference_index_mismatch" for item in result.diagnostics)


def test_compiler_warns_for_design_sheet_only_and_incomplete_style_and_character_facts() -> None:
    result = compile_prompt_bundle(
        style={"style_version": "incomplete"},
        shot=_shot(),
        anchors=[
            {
                "canonical_name": "薄弱角色",
                "asset_id": "asset-weak",
                "asset_type": "character",
                "approved_references": [
                    {"purpose": "design_sheet", "approved": True, "relative_path": "sheet.png"}
                ],
            }
        ],
    )
    codes = {item.code for item in result.diagnostics}

    assert result.status == "warning"
    assert "design_sheet_as_reference" in codes
    assert "style_incomplete" in codes
    assert "character_drift_risk" in codes


def test_compiler_reports_cue_text_and_time_binding_diagnostics() -> None:
    result = compile_prompt_bundle(
        style=_style(),
        shot=_shot(
            dialogue_cues=[
                {
                    "dialogue_line_id": "line-1",
                    "speaker": "林舟",
                    "text": "太迟了。",
                    "start_time": 5.5,
                    "end_time": 6.5,
                }
            ],
            sound_cues=[
                {
                    "kind": "impact",
                    "description": "",
                    "start_time": 1,
                    "end_time": 2,
                }
            ],
        ),
        anchors=_anchors(),
    )

    cue_diagnostics = [item for item in result.diagnostics if item.code == "dialogue_over_time"]
    assert len(cue_diagnostics) == 2
    assert any(item.level == "warning" for item in cue_diagnostics)
    assert any(item.level == "blocking" for item in cue_diagnostics)


def test_human_override_keeps_generated_and_final_three_layers_separate() -> None:
    override = HumanOverrideSet(
        image_prompt="人工图片稿",
        video_prompt="人工视频稿",
        negative_prompt="人工追加负向",
        camera="人工固定机位",
    )
    result = compile_prompt_bundle(
        style=_style(),
        shot=_shot(),
        anchors=_anchors(),
        overrides=override,
    )

    assert result.override_status == "active"
    assert result.generated["image"].prompt != result.final["image"].prompt
    assert result.generated["image"].prompt != "人工图片稿"
    assert result.final["image"].prompt == "人工图片稿"
    assert result.final["video"].prompt == "人工视频稿"
    assert result.final["video"].negative_prompt == "人工追加负向"
    assert result.generated["video"].semantic_plan["camera"] != "人工固定机位"

    cleared = compile_prompt_bundle(
        style=_style(),
        shot=_shot(),
        anchors=_anchors(),
        overrides={"cleared": True},
    )
    assert cleared.override_status == "cleared"
    assert cleared.final == cleared.generated


def test_compiler_preserves_input_objects_and_no_reference_plan_means_no_image_tokens() -> None:
    style = _style()
    shot = _shot()
    anchors = _anchors()
    original = deepcopy((style, shot, anchors))

    result = compile_prompt_bundle(style=style, shot=shot, anchors=anchors)

    assert (style, shot, anchors) == original
    assert "@Image" not in result.image.prompt
    assert "@Image" not in result.video.prompt
    assert result.image.reference_slots == []


def test_bundle_hash_is_stable_for_identical_reference_content_under_different_project_roots(
    tmp_path: Path,
) -> None:
    relative_path = "references/hero.png"
    first_root = tmp_path / "project-one"
    second_root = tmp_path / "project-two"
    for root in (first_root, second_root):
        target = root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"same-reference-content")

    slot = {
        "index": 1,
        "role": "hero",
        "asset_id": "asset-hero",
        "purpose": "clean_identity",
        "relative_path": relative_path,
    }
    first_plan = build_reference_plan([slot], project_root=first_root)
    second_plan = build_reference_plan([slot], project_root=second_root)
    first = compile_prompt_bundle(
        style=_style(),
        shot=_shot(),
        anchors=_anchors(),
        reference_plan=first_plan,
    )
    second = compile_prompt_bundle(
        style=_style(),
        shot=_shot(),
        anchors=_anchors(),
        reference_plan=second_plan,
    )

    assert first_plan.actual_local_files != second_plan.actual_local_files
    assert first_plan.snapshot_hash == second_plan.snapshot_hash
    assert first.base_compiler_hash == second.base_compiler_hash
    assert first.input_hash == second.input_hash
    assert first.image is not None and second.image is not None
    assert first.image.semantic_plan == second.image.semantic_plan
