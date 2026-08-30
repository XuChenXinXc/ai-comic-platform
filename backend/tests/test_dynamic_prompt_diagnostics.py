from __future__ import annotations

import pytest

from app.dynamic_pipeline.diagnostics import (
    PROMPT_DIAGNOSTIC_CODES,
    diagnostic,
    diagnostics_status,
    sort_diagnostics,
)

EXPECTED_PROMPT_DIAGNOSTIC_CODES = {
    "asset_unresolved",
    "reference_missing",
    "reference_wrong_type",
    "reference_index_mismatch",
    "timeline_discontinuous",
    "continuous_previous_end_frame_missing",
    "provider_reference_limit",
    "provider_duration_unsupported",
    "video_action_missing",
    "video_end_state_missing",
    "image_contains_motion",
    "design_sheet_as_reference",
    "style_incomplete",
    "character_drift_risk",
    "dialogue_over_time",
}


def test_diagnostic_builds_allowed_level_and_copies_details() -> None:
    details = {"expected": 2}

    result = diagnostic(
        "warning",
        "reference_missing",
        "缺少参考图",
        path="shots[0].references",
        details=details,
    )

    assert result.level == "warning"
    assert result.code == "reference_missing"
    assert result.message == "缺少参考图"
    assert result.path == "shots[0].references"
    assert result.details == details
    assert result.details is not details


@pytest.mark.parametrize("level", ["info", "error", ""])
def test_diagnostic_rejects_unsupported_level(level: str) -> None:
    with pytest.raises(ValueError):
        diagnostic(level, "test_code", "测试诊断")


def test_sort_diagnostics_is_stable_and_does_not_modify_input() -> None:
    first = diagnostic("warning", "b_code", "同级排序后最后")
    second = diagnostic("warning", "a_code", "消息二", path="b")
    third = diagnostic("blocking", "a_code", "消息一", path="a")
    fourth = diagnostic("warning", "a_code", "消息一", path="a")
    source = [first, second, third, fourth]
    original = list(source)

    result = sort_diagnostics(source)

    assert source == original
    assert result == [third, fourth, second, first]
    assert result[0] is third
    assert result[1] is fourth


def test_diagnostics_status_is_ready_without_diagnostics() -> None:
    result = diagnostics_status([], checked_at="fixed-check")

    assert result.status == "ready"
    assert result.passed is True
    assert result.can_confirm is True
    assert result.diagnostics == []
    assert result.checked_at == "fixed-check"


def test_diagnostics_status_is_warning_for_warnings_only() -> None:
    warning = diagnostic("warning", "style_incomplete", "风格不完整")

    result = diagnostics_status([warning])

    assert result.status == "warning"
    assert result.passed is True
    assert result.can_confirm is True
    assert result.diagnostics == [warning]


def test_diagnostics_status_is_blocked_when_any_diagnostic_is_blocking() -> None:
    warning = diagnostic("warning", "style_incomplete", "风格不完整")
    blocking = diagnostic("blocking", "asset_unresolved", "资产未解析")

    result = diagnostics_status(item for item in [warning, blocking])

    assert result.status == "blocked"
    assert result.passed is False
    assert result.can_confirm is False
    assert result.diagnostics == [blocking, warning]


def test_prompt_diagnostic_codes_include_all_required_codes() -> None:
    assert EXPECTED_PROMPT_DIAGNOSTIC_CODES.issubset(PROMPT_DIAGNOSTIC_CODES)
