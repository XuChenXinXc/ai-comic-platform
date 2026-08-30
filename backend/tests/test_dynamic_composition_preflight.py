from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from app.dynamic_pipeline.composition_preflight import (
    CompositionPreflightResult,
    PreflightItem,
    can_start_composition,
    run_composition_preflight,
    validate_relative_media_path,
)
from app.dynamic_pipeline.schemas import Diagnostic


def _write(root: Path, relative_path: str, content: bytes = b"media") -> str:
    target = root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return relative_path


def _snapshot(root: Path, *, shots: int = 2) -> dict[str, Any]:
    shot_records: list[dict[str, Any]] = []
    for index in range(1, shots + 1):
        shot_id = f"shot-{index}"
        shot_records.append(
            {
                "id": shot_id,
                "shot_order": index,
                "project_id": "project-1",
                "keyframe": {
                    "id": f"keyframe-{index}",
                    "project_id": "project-1",
                    "status": "completed",
                    "file_path": _write(root, f"keyframes/{shot_id}.png"),
                    "input_signature": "run-signature",
                },
                "video": {
                    "id": f"video-{index}",
                    "project_id": "project-1",
                    "status": "completed",
                    "file_path": _write(root, f"videos/{shot_id}.mp4"),
                    "input_signature": "run-signature",
                },
                "audio": {
                    "id": f"audio-{index}",
                    "project_id": "project-1",
                    "status": "completed",
                    "file_path": _write(root, f"audio/{shot_id}.mp3"),
                    "input_signature": "run-signature",
                },
                "subtitle": {
                    "id": f"subtitle-{index}",
                    "project_id": "project-1",
                    "status": "completed",
                    "text": f"台词 {index}",
                    "start_time": 0,
                    "end_time": 1,
                    "input_signature": "run-signature",
                },
            }
        )
    return {
        "run": {
            "id": "run-1",
            "project_id": "project-1",
            "chapter_id": "chapter-1",
            "input_signature": "run-signature",
        },
        "shots": shot_records,
        "bgm": {
            "id": "bgm-1",
            "project_id": "project-1",
            "status": "completed",
            "file_path": _write(root, "bgm/theme.mp3"),
            "input_signature": "run-signature",
        },
    }


@pytest.mark.parametrize("kind", ["video", "audio", "subtitle", "keyframe"])
def test_missing_required_media_blocks_strict_preflight(tmp_path: Path, kind: str) -> None:
    snapshot = _snapshot(tmp_path, shots=1)
    shot = snapshot["shots"][0]
    shot.pop(kind)

    result = run_composition_preflight(snapshot, project_root=tmp_path)

    assert result.status == "blocked"
    assert can_start_composition(result) is False
    assert any(item.kind == kind for item in result.missing)
    assert result.estimated_jobs == 0


def test_failed_media_is_reported_without_starting_a_job(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, shots=1)
    snapshot["shots"][0]["video"]["status"] = "failed"
    snapshot["shots"][0]["video"]["error"] = "provider failed"

    result = run_composition_preflight(snapshot, project_root=tmp_path)

    assert result.status == "blocked"
    assert [(item.kind, item.status) for item in result.failed] == [("video", "failed")]
    assert result.failed[0].reason == "provider failed"


def test_path_validation_rejects_traversal_absolute_empty_and_symlink(tmp_path: Path) -> None:
    valid = tmp_path / "valid.mp4"
    valid.write_bytes(b"x")
    assert validate_relative_media_path(tmp_path, "valid.mp4") == valid.resolve()

    traversal = validate_relative_media_path(tmp_path, "../valid.mp4")
    absolute = validate_relative_media_path(tmp_path, str(valid))
    empty = tmp_path / "empty.mp4"
    empty.touch()
    empty_result = validate_relative_media_path(tmp_path, "empty.mp4")

    assert isinstance(traversal, Diagnostic)
    assert traversal.code == "path_traversal"
    assert isinstance(absolute, Diagnostic)
    assert absolute.code == "path_absolute"
    assert isinstance(empty_result, Diagnostic)
    assert empty_result.code == "path_empty"

    link = tmp_path / "link.mp4"
    try:
        link.symlink_to(valid)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接")
    link_result = validate_relative_media_path(tmp_path, "link.mp4")
    assert isinstance(link_result, Diagnostic)
    assert link_result.code == "path_symlink"


def test_bgm_from_another_project_is_always_blocking(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, shots=1)
    snapshot["bgm"]["project_id"] = "other-project"

    result = run_composition_preflight(
        snapshot,
        allow_partial=True,
        project_root=tmp_path,
    )

    assert result.status == "blocked"
    assert can_start_composition(result) is False
    assert any(
        item["code"] == "preflight_bgm_cross_project" for item in result.provenance["diagnostics"]
    )


def test_partial_result_requires_explicit_selection_and_records_reason(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, shots=1)
    snapshot["shots"][0].pop("audio")

    strict_result = run_composition_preflight(snapshot, project_root=tmp_path)
    partial_result = run_composition_preflight(
        snapshot,
        allow_partial=True,
        project_root=tmp_path,
    )

    assert strict_result.status == "blocked"
    assert can_start_composition(strict_result) is False
    assert partial_result.status == "warning"
    assert partial_result.allow_partial is True
    assert can_start_composition(partial_result) is True
    assert any(item.kind == "audio" for item in partial_result.skipped)
    assert partial_result.skipped[0].reason


def test_signature_mismatch_is_blocking_even_for_partial_selection(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, shots=1)
    snapshot["shots"][0]["video"]["input_signature"] = "different-signature"

    result = run_composition_preflight(
        snapshot,
        allow_partial=True,
        project_root=tmp_path,
    )

    assert result.status == "blocked"
    assert can_start_composition(result) is False
    assert any(
        item["code"] == "preflight_signature_mismatch" for item in result.provenance["diagnostics"]
    )


def test_manifest_is_deterministic_and_snapshot_is_not_modified(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, shots=2)
    before = deepcopy(snapshot)
    reversed_snapshot = deepcopy(snapshot)
    reversed_snapshot["shots"] = list(reversed(reversed_snapshot["shots"]))
    for shot in reversed_snapshot["shots"]:
        for key in ("audio", "subtitle"):
            if isinstance(shot.get(key), list):
                shot[key] = list(reversed(shot[key]))

    first = run_composition_preflight(snapshot, project_root=tmp_path)
    second = run_composition_preflight(reversed_snapshot, project_root=tmp_path)

    assert first.status == "ready"
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert snapshot == before
    assert first.provenance["manifest_hash"] == second.provenance["manifest_hash"]


def test_inline_subtitle_with_pending_file_status_is_ready(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, shots=1)
    subtitle = snapshot["shots"][0]["subtitle"]
    subtitle["status"] = "missing"

    result = run_composition_preflight(snapshot, project_root=tmp_path)

    assert result.status == "ready"
    assert not [item for item in result.missing if item.kind == "subtitle"]
    assert not [item for item in result.failed if item.kind == "subtitle"]


def test_derived_media_signature_uses_confirmed_stage_chain(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, shots=1)
    snapshot["stages"] = [
        {
            "id": "stage-6",
            "stage_key": "6-delivery",
            "status": "running",
            "input_signature": "stage-signature",
        }
    ]
    snapshot["shots"][0]["video"]["input_signature"] = "derived-video-signature"
    snapshot["shots"][0]["video"]["provenance"] = {
        "workflow_run_id": "run-1",
        "workflow_stage_id": "stage-6",
        "upstream_input_signature": "stage-signature",
    }

    result = run_composition_preflight(snapshot, project_root=tmp_path)

    assert result.status == "ready"
    assert not [item for item in result.failed if item.kind == "video"]

    wrong_run = deepcopy(snapshot)
    wrong_run["shots"][0]["video"]["provenance"]["workflow_run_id"] = "run-other"
    blocked = run_composition_preflight(wrong_run, project_root=tmp_path)

    assert blocked.status == "blocked"
    assert any(
        item["code"] == "preflight_workflow_run_mismatch"
        for item in blocked.provenance["diagnostics"]
    )


def test_missing_composition_is_planned_and_counts_as_one_estimated_job(tmp_path: Path) -> None:
    result = run_composition_preflight(_snapshot(tmp_path, shots=1), project_root=tmp_path)

    composition = [item for item in result.items if item.kind == "composition"]
    assert result.status == "ready"
    assert result.estimated_jobs == 1
    assert [(item.status, item.reason) for item in composition] == [
        ("planned", "成片尚未创建，但已具备可计划状态")
    ]


def test_preflight_does_not_call_legacy_composition_or_mutate_legacy_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import composition_service

    snapshot = _snapshot(tmp_path, shots=1)
    before = deepcopy(snapshot)
    calls: list[object] = []
    monkeypatch.setattr(
        composition_service.subprocess,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    result = run_composition_preflight(snapshot, project_root=tmp_path)

    assert result.status == "ready"
    assert calls == []
    assert snapshot == before


def test_preflight_models_forbid_unknown_fields() -> None:
    with pytest.raises(ValueError):
        PreflightItem.model_validate(
            {
                "kind": "video",
                "id": "video-1",
                "status": "ready",
                "required": True,
                "unexpected": True,
            }
        )
    with pytest.raises(ValueError):
        CompositionPreflightResult.model_validate(
            {
                "workflow_run_id": "run-1",
                "project_id": "project-1",
                "chapter_id": "chapter-1",
                "strict": True,
                "allow_partial": False,
                "status": "ready",
                "unexpected": True,
            }
        )
