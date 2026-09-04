from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from app.dynamic_pipeline.reference_plan import (
    build_reference_plan,
    filter_reference_slots,
    render_reference_text,
    validate_reference_text,
)
from app.dynamic_pipeline.reference_resolver import (
    adapt_reference_bindings,
)

_PNG_BYTES = b"\x89PNG\r\n\x1a\n"
_JPG_BYTES = b"\xff\xd8\xff\xd9"


def _write_image(root: Path, relative_path: str) -> str:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.casefold() in {".jpg", ".jpeg"}:
        path.write_bytes(_JPG_BYTES)
    else:
        path.write_bytes(_PNG_BYTES)
    return relative_path


def _slot(
    index: int | None,
    *,
    role: str,
    purpose: str = "clean_identity",
    **source: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": role, "purpose": purpose}
    if index is not None:
        payload["index"] = index
    payload.update(source)
    return payload


def _diagnostics_with_code(plan: Any, code: str) -> list[Any]:
    return [item for item in plan.diagnostics if item.code == code]


def test_build_reference_plan_keeps_single_and_multiple_reference_order(tmp_path: Path) -> None:
    hero = _write_image(tmp_path, "references/hero.png")
    _write_image(tmp_path, "references/villain.jpg")

    single = build_reference_plan(
        [_slot(1, role="hero", relative_path=hero)],
        project_root=tmp_path,
    )
    assert single.status == "ready"
    assert [slot.index for slot in single.slots] == [1]
    assert single.actual_local_files == [str((tmp_path / hero).resolve())]
    assert single.actual_urls == []
    assert list(single.text_mapping) == ["@Image1"]
    assert render_reference_text(single) == "@Image1[hero]"

    url = "https://cdn.example.test/villain.jpg"
    slots = [
        _slot(1, role="hero", relative_path=hero),
        _slot(2, role="villain", provider_url=url),
    ]
    plan = build_reference_plan(slots, project_root=tmp_path)

    assert plan.status == "ready"
    assert [slot.index for slot in plan.slots] == [1, 2]
    assert plan.actual_local_files == [str((tmp_path / hero).resolve())]
    assert plan.actual_urls == [url]
    assert list(plan.text_mapping) == ["@Image1", "@Image2"]
    assert plan.text_mapping["@Image1"]["index"] == 1
    assert plan.text_mapping["@Image1"]["value"] == plan.actual_local_files[0]
    assert plan.text_mapping["@Image2"]["index"] == 2
    assert plan.text_mapping["@Image2"]["value"] == plan.actual_urls[0]
    assert render_reference_text(plan) == "@Image1[hero]；@Image2[villain]"

    reordered_slots = [
        {key: value for key, value in reversed(list(slot.items()))} for slot in slots
    ]
    reordered_plan = build_reference_plan(reordered_slots, project_root=tmp_path)
    assert plan.snapshot_hash
    assert reordered_plan.snapshot_hash == plan.snapshot_hash


def test_reference_snapshot_hash_ignores_absolute_project_root(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    relative = "references/hero.png"
    _write_image(first_root, relative)
    _write_image(second_root, relative)

    first = build_reference_plan(
        [_slot(1, role="hero", relative_path=relative)],
        project_root=first_root,
    )
    second = build_reference_plan(
        [_slot(1, role="hero", relative_path=relative)],
        project_root=second_root,
    )

    assert first.actual_local_files != second.actual_local_files
    assert first.snapshot_hash == second.snapshot_hash


def test_reference_resolver_adapts_raw_ids_and_sources_into_canonical_slot_fields() -> None:
    adapted = adapt_reference_bindings(
        [
            {
                "role": "character_identity",
                "asset_image_id": "asset-image-1",
                "url": "https://cdn.example.test/hero.png",
            }
        ]
    )

    assert adapted == [
        {
            "index": 1,
            "role": "character_identity",
            "purpose": "clean_identity",
            "required": False,
            "image_version_id": "asset-image-1",
            "provider_url": "https://cdn.example.test/hero.png",
        }
    ]


@pytest.mark.parametrize(
    "indexes",
    [
        pytest.param([1, 1], id="duplicate"),
        pytest.param([1, 3], id="gap"),
        pytest.param([None, 2], id="missing"),
    ],
)
def test_build_reference_plan_blocks_index_mismatch_without_reordering(
    tmp_path: Path,
    indexes: list[int | None],
) -> None:
    slots = [
        _slot(
            index,
            role=f"reference-{position}",
            relative_path=_write_image(tmp_path, f"reference-{position}.png"),
        )
        for position, index in enumerate(indexes, start=1)
    ]

    plan = build_reference_plan(slots, project_root=tmp_path)

    mismatch = _diagnostics_with_code(plan, "reference_index_mismatch")
    assert plan.status == "blocked"
    assert len(mismatch) == 1
    assert mismatch[0].level == "blocking"
    assert mismatch[0].details["actual_indices"] == [
        index for index in indexes if index is not None
    ]
    assert [slot.index for slot in plan.display_slots] == [
        index for index in indexes if index is not None
    ]
    assert [slot.index for slot in plan.slots] == [index for index in indexes if index is not None]
    assert plan.actual_local_files == []
    assert plan.actual_urls == []
    assert plan.text_mapping == {}
    assert render_reference_text(plan) == ""


@pytest.mark.parametrize(
    ("relative_path", "expected_code"),
    [
        ("../outside.png", "reference_missing"),
        ("missing.png", "reference_missing"),
        ("notes.txt", "reference_wrong_type"),
    ],
)
def test_build_reference_plan_reports_invalid_local_reference_sources(
    tmp_path: Path,
    relative_path: str,
    expected_code: str,
) -> None:
    plan = build_reference_plan(
        [_slot(1, role="hero", relative_path=relative_path)],
        project_root=tmp_path,
    )

    source_diagnostics = _diagnostics_with_code(plan, expected_code)
    assert plan.status == "blocked"
    assert len(source_diagnostics) == 1
    assert source_diagnostics[0].level == "blocking"


def test_build_reference_plan_rejects_absolute_local_reference_path(tmp_path: Path) -> None:
    relative_path = _write_image(tmp_path, "inside.png")
    absolute_path = str((tmp_path / relative_path).resolve())

    plan = build_reference_plan(
        [_slot(1, role="hero", relative_path=absolute_path)],
        project_root=tmp_path,
    )

    diagnostics = _diagnostics_with_code(plan, "reference_missing")
    assert plan.status == "blocked"
    assert len(diagnostics) == 1
    assert diagnostics[0].level == "blocking"


@pytest.mark.parametrize(
    ("provider_url", "provider_url_status", "expected_code"),
    [
        ("ftp://cdn.example.test/hero.png", "", "reference_wrong_type"),
        ("https://cdn.example.test/hero.png", "expired", "reference_missing"),
    ],
)
def test_build_reference_plan_reports_invalid_provider_urls(
    tmp_path: Path,
    provider_url: str,
    provider_url_status: str,
    expected_code: str,
) -> None:
    plan = build_reference_plan(
        [
            _slot(
                1,
                role="hero",
                provider_url=provider_url,
                provider_url_status=provider_url_status,
            )
        ],
        project_root=tmp_path,
    )

    diagnostics = _diagnostics_with_code(plan, expected_code)
    assert plan.status == "blocked"
    assert len(diagnostics) == 1
    assert diagnostics[0].level == "blocking"


def test_video_plan_keeps_design_sheet_for_display_only_and_orders_previous_end_frame(
    tmp_path: Path,
) -> None:
    design_sheet = _write_image(tmp_path, "references/design.png")
    keyframe = _write_image(tmp_path, "references/keyframe.jpg")
    end_frame = _write_image(tmp_path, "references/end.jpg")
    slots = [
        _slot(1, role="design-sheet", purpose="design_sheet", relative_path=design_sheet),
        _slot(2, role="keyframe", purpose="keyframe_reference", relative_path=keyframe),
    ]
    previous_end_frame = {"relative_path": end_frame}

    plan = build_reference_plan(
        slots,
        project_root=tmp_path,
        video=True,
        previous_end_frame=previous_end_frame,
    )

    assert plan.status == "warning"
    assert [slot.purpose for slot in plan.display_slots] == [
        "design_sheet",
        "keyframe_reference",
        "previous_end_frame",
    ]
    assert [slot.index for slot in plan.display_slots] == [1, 2, 3]
    assert [slot.purpose for slot in plan.slots] == [
        "keyframe_reference",
        "previous_end_frame",
    ]
    assert [slot.index for slot in plan.slots] == [2, 3]
    assert plan.slots[1].required is True
    assert plan.actual_local_files == [
        str((tmp_path / keyframe).resolve()),
        str((tmp_path / end_frame).resolve()),
    ]
    assert render_reference_text(plan) == "@Image2[keyframe]；@Image3[previous_end_frame]"
    design_diagnostics = _diagnostics_with_code(plan, "design_sheet_as_reference")
    assert len(design_diagnostics) == 1
    assert design_diagnostics[0].level == "warning"
    assert design_diagnostics[0].details["video"] is True

    repeated = build_reference_plan(
        slots,
        project_root=tmp_path,
        video=True,
        previous_end_frame=previous_end_frame,
    )
    assert repeated.snapshot_hash == plan.snapshot_hash
    assert repeated.slots == plan.slots
    assert repeated.actual_local_files == plan.actual_local_files


def test_provider_reference_capabilities_block_single_and_over_max_without_fake_tokens(
    tmp_path: Path,
) -> None:
    image_paths = [
        _write_image(tmp_path, "references/one.png"),
        _write_image(tmp_path, "references/two.jpg"),
        _write_image(tmp_path, "references/three.png"),
    ]
    slots = [
        _slot(index, role=f"reference-{index}", relative_path=path)
        for index, path in enumerate(image_paths, start=1)
    ]

    single_plan = build_reference_plan(
        slots[:2],
        project_root=tmp_path,
        provider_capabilities={"supports_multiple_images": False, "max_reference_images": 2},
    )
    single_limit = _diagnostics_with_code(single_plan, "provider_reference_limit")
    assert single_plan.status == "blocked"
    assert len(single_limit) == 1
    assert single_limit[0].level == "blocking"
    assert [slot.index for slot in single_plan.slots] == [1]
    assert list(single_plan.text_mapping) == ["@Image1"]
    assert "@Image2" not in render_reference_text(single_plan)

    over_max_plan = build_reference_plan(
        slots,
        project_root=tmp_path,
        provider_capabilities={"supports_multiple_images": True, "max_reference_images": 2},
    )
    over_max_limit = _diagnostics_with_code(over_max_plan, "provider_reference_limit")
    assert over_max_plan.status == "blocked"
    assert len(over_max_limit) == 1
    assert over_max_limit[0].level == "blocking"
    assert [slot.index for slot in over_max_plan.slots] == [1, 2]
    assert list(over_max_plan.text_mapping) == ["@Image1", "@Image2"]
    assert "@Image3" not in render_reference_text(over_max_plan)

    legal_plan = build_reference_plan(
        slots[:2],
        project_root=tmp_path,
        provider_capabilities={"supports_multiple_images": True, "max_reference_images": 2},
    )
    assert legal_plan.status == "ready"
    assert legal_plan.diagnostics == []
    assert [slot.index for slot in legal_plan.slots] == [1, 2]
    assert list(legal_plan.text_mapping) == ["@Image1", "@Image2"]


def test_validate_reference_text_blocks_unknown_token_and_accepts_plan_tokens(
    tmp_path: Path,
) -> None:
    first = _write_image(tmp_path, "references/first.png")
    second = _write_image(tmp_path, "references/second.jpg")
    plan = build_reference_plan(
        [
            _slot(1, role="first", relative_path=first),
            _slot(2, role="second", relative_path=second),
        ],
        project_root=tmp_path,
    )

    assert validate_reference_text("使用 @Image1 和 @Image2", plan) == []
    diagnostics = validate_reference_text("错误引用 @Image3", plan)
    assert any(
        item.level == "blocking" and item.code == "reference_index_mismatch" for item in diagnostics
    )


def test_filter_reference_slots_preserves_input_indices_order_and_values() -> None:
    slots = [
        _slot(4, role="prop", purpose="prop_identity"),
        _slot(1, role="sheet", purpose="design_sheet"),
        _slot(2, role="end", purpose="previous_end_frame"),
        _slot(7, role="layout", purpose="layout_reference"),
    ]
    original = deepcopy(slots)

    filtered = filter_reference_slots(slots, video=True)

    assert [slot.index for slot in filtered] == [4, 2, 7]
    assert [slot.purpose for slot in filtered] == [
        "prop_identity",
        "previous_end_frame",
        "layout_reference",
    ]
    assert slots == original
