from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from app.dynamic_pipeline.schemas import Diagnostic, StageGateResult

PROMPT_DIAGNOSTIC_CODES = frozenset(
    {
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
)

_ALLOWED_LEVELS = ("warning", "blocking")


def diagnostic(
    level: str,
    code: str,
    message: str,
    path: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> Diagnostic:
    if level not in _ALLOWED_LEVELS:
        raise ValueError("level 必须是 warning 或 blocking")
    return Diagnostic(
        level=level,
        code=code,
        message=message,
        path=path,
        details=dict(details) if details is not None else {},
    )


def sort_diagnostics(diagnostics: Iterable[Diagnostic]) -> list[Diagnostic]:
    return sorted(
        diagnostics,
        key=lambda item: (item.code, item.path or "", item.message),
    )


def diagnostics_status(
    diagnostics: Iterable[Diagnostic],
    checked_at: str | None = None,
) -> StageGateResult:
    ordered = sort_diagnostics(diagnostics)
    has_blocking = any(item.level == "blocking" for item in ordered)
    if has_blocking:
        status = "blocked"
        passed = False
        can_confirm = False
    elif ordered:
        status = "warning"
        passed = True
        can_confirm = True
    else:
        status = "ready"
        passed = True
        can_confirm = True

    return StageGateResult(
        status=status,
        passed=passed,
        diagnostics=ordered,
        checked_at=checked_at,
        can_confirm=can_confirm,
    )


__all__ = [
    "PROMPT_DIAGNOSTIC_CODES",
    "diagnostic",
    "diagnostics_status",
    "sort_diagnostics",
]
