from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.dynamic_pipeline.schemas import Diagnostic, StageGateResult
from app.dynamic_pipeline.stage_catalog import (
    STAGE_DEFINITIONS,
    STAGE_KEYS,
    get_descendant_stage_keys,
    get_next_stage_key,
    get_stage_definition,
    get_stage_ordinal,
    get_stage_prerequisites,
)
from app.dynamic_pipeline.state_machine import (
    InvalidWorkflowTransition,
    RevisionConflict,
    StageBlocked,
    can_confirm_stage,
    can_start_stage,
    can_transition_status,
    ensure_stage_prerequisites_confirmed,
    mark_descendants_stale,
    require_revision,
    transition_status,
)


def test_stage_catalog_preserves_order_dependencies_and_parallel_branches() -> None:
    assert STAGE_KEYS == (
        "0-source",
        "1-character-bios",
        "2-rhythm",
        "3-script",
        "4-storyboard",
        "5a-image-prompts",
        "5b-video-prompts",
        "6-delivery",
    )
    assert [get_stage_ordinal(key) for key in STAGE_KEYS] == [0, 1, 2, 3, 4, 5, 5, 6]
    assert [get_stage_prerequisites(key) for key in STAGE_KEYS] == [
        (),
        ("0-source",),
        ("1-character-bios",),
        ("2-rhythm",),
        ("3-script",),
        ("4-storyboard",),
        ("4-storyboard",),
        ("5a-image-prompts", "5b-video-prompts"),
    ]
    assert STAGE_DEFINITIONS["5a-image-prompts"].parallelizable is True
    assert STAGE_DEFINITIONS["5b-video-prompts"].parallelizable is True
    assert STAGE_DEFINITIONS["4-storyboard"].parallelizable is False
    assert get_descendant_stage_keys("4-storyboard") == (
        "5a-image-prompts",
        "5b-video-prompts",
        "6-delivery",
    )
    assert get_next_stage_key("4-storyboard") == "5a-image-prompts"
    assert get_next_stage_key("5a-image-prompts") == "6-delivery"
    assert get_next_stage_key("6-delivery") is None


def test_stage_catalog_definitions_are_immutable() -> None:
    with pytest.raises(TypeError):
        STAGE_DEFINITIONS["new-stage"] = get_stage_definition("0-source")  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        get_stage_definition("0-source").ordinal = 99  # type: ignore[misc]


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("pending", "queued"),
        ("pending", "running"),
        ("pending", "blocked"),
        ("pending", "cancelled"),
        ("queued", "running"),
        ("queued", "blocked"),
        ("queued", "cancelled"),
        ("running", "ready"),
        ("running", "blocked"),
        ("running", "partial_failed"),
        ("running", "failed"),
        ("running", "interrupted"),
        ("running", "cancelled"),
        ("ready", "confirmed"),
        ("ready", "stale"),
        ("ready", "failed"),
        ("confirmed", "stale"),
        ("blocked", "queued"),
        ("failed", "queued"),
        ("interrupted", "queued"),
        ("partial_failed", "queued"),
        ("stale", "queued"),
    ],
)
def test_status_transition_allowed_returns_target(current: str, target: str) -> None:
    assert can_transition_status(current, target) is True
    assert transition_status(current, target) == target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("pending", "ready"),
        ("queued", "confirmed"),
        ("running", "confirmed"),
        ("confirmed", "queued"),
        ("cancelled", "queued"),
        ("unknown", "running"),
    ],
)
def test_status_transition_invalid_is_rejected(current: str, target: str) -> None:
    assert can_transition_status(current, target) is False
    with pytest.raises(InvalidWorkflowTransition, match="不允许状态转移"):
        transition_status(current, target)


def test_stage_confirmation_gate_allows_warning_without_blocking_diagnostic() -> None:
    gate_result = StageGateResult(
        status="warning",
        passed=True,
        can_confirm=True,
        diagnostics=[Diagnostic(level="warning", code="soft_warning", message="可继续")],
    )

    assert can_confirm_stage("ready", gate_result) is True
    assert can_confirm_stage("ready", gate_result.model_dump()) is True


@pytest.mark.parametrize("status", ["blocked", "failed", "stale", "partial_failed"])
def test_stage_confirmation_gate_rejects_blocking_or_unready_status(status: str) -> None:
    gate_result = {
        "status": "ready",
        "passed": True,
        "can_confirm": True,
        "diagnostics": [{"level": "blocking", "code": "missing", "message": "缺少内容"}],
    }

    assert can_confirm_stage(status, gate_result) is False
    assert can_confirm_stage("ready", {**gate_result, "status": "blocked"}) is False


def test_revision_requirement_skips_none_and_rejects_mismatch() -> None:
    require_revision(3, None)
    require_revision(3, 3)

    with pytest.raises(RevisionConflict, match="revision 冲突") as error:
        require_revision(3, 2)

    assert error.value.actual == 3
    assert error.value.expected == 2


def test_stage_prerequisites_block_until_direct_parent_is_confirmed() -> None:
    assert can_start_stage("0-source", set()) is True
    assert can_start_stage("5a-image-prompts", {"4-storyboard"}) is True
    assert can_start_stage("5b-video-prompts", {"4-storyboard"}) is True
    assert can_start_stage("6-delivery", {"5a-image-prompts"}) is False

    with pytest.raises(StageBlocked, match="5b-video-prompts") as error:
        ensure_stage_prerequisites_confirmed("5b-video-prompts", set())
    assert error.value.missing_keys == ("4-storyboard",)

    with pytest.raises(StageBlocked, match="6-delivery"):
        ensure_stage_prerequisites_confirmed("6-delivery", {"5a-image-prompts"})


def test_descendant_stale_propagation_returns_keys_without_mutating_statuses() -> None:
    stage_statuses = {
        "4-storyboard": "confirmed",
        "5a-image-prompts": "stale",
        "5b-video-prompts": "running",
        "6-delivery": "confirmed",
    }
    original_statuses = stage_statuses.copy()

    assert mark_descendants_stale("4-storyboard", stage_statuses) == [
        "5b-video-prompts",
        "6-delivery",
    ]
    assert stage_statuses == original_statuses
    assert mark_descendants_stale("3-script", {"4-storyboard": "cancelled"}) == [
        "5a-image-prompts",
        "5b-video-prompts",
        "6-delivery",
    ]


def test_descendant_stale_propagation_excludes_cancelled_descendants() -> None:
    stage_statuses = {
        "5a-image-prompts": "cancelled",
        "5b-video-prompts": "confirmed",
        "6-delivery": "stale",
    }

    assert mark_descendants_stale("4-storyboard", stage_statuses) == ["5b-video-prompts"]


@pytest.mark.parametrize(
    "function",
    [
        get_stage_definition,
        get_stage_ordinal,
        get_stage_prerequisites,
        get_descendant_stage_keys,
        get_next_stage_key,
        lambda key: can_start_stage(key, set()),
    ],
)
def test_invalid_stage_key_raises_value_error(function) -> None:
    with pytest.raises(ValueError, match="非法阶段键"):
        function("9-invalid")
