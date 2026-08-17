"""动态漫工作流的纯状态机和阶段屏障逻辑。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel

from .schemas import StageGateResult
from .stage_catalog import get_descendant_stage_keys, get_stage_prerequisites


class WorkflowStateError(ValueError):
    """工作流状态校验失败。"""


class InvalidWorkflowTransition(WorkflowStateError):
    """请求的状态转移不在状态机允许集合中。"""

    def __init__(self, current_status: str, target_status: str) -> None:
        self.current_status = current_status
        self.target_status = target_status
        super().__init__(f"不允许状态转移: {current_status!r} -> {target_status!r}")


class RevisionConflict(WorkflowStateError):
    """写入时携带的 revision 已经过期。"""

    def __init__(self, actual: int | None, expected: int) -> None:
        self.actual = actual
        self.expected = expected
        self.actual_revision = actual
        self.expected_revision = expected
        super().__init__(f"revision 冲突: expected={expected}, actual={actual}")


class StageBlocked(WorkflowStateError):
    """阶段因前置阶段或其它屏障条件未满足而被阻断。"""

    def __init__(self, stage_key: str, missing_keys: Iterable[str]) -> None:
        self.stage_key = stage_key
        self.missing_keys = tuple(missing_keys)
        missing = ", ".join(self.missing_keys)
        super().__init__(f"阶段 {stage_key!r} 尚未满足前置确认: {missing}")


_ALLOWED_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "pending": frozenset({"queued", "running", "blocked", "cancelled"}),
    "queued": frozenset({"running", "blocked", "cancelled"}),
    "running": frozenset(
        {"ready", "blocked", "partial_failed", "failed", "interrupted", "cancelled"}
    ),
    "ready": frozenset({"confirmed", "stale", "failed"}),
    "confirmed": frozenset({"stale"}),
    "blocked": frozenset({"queued", "cancelled"}),
    "failed": frozenset({"queued", "cancelled"}),
    "interrupted": frozenset({"queued", "cancelled"}),
    "partial_failed": frozenset({"queued", "cancelled"}),
    "stale": frozenset({"queued", "cancelled"}),
    "cancelled": frozenset(),
}
_VALID_STATUSES = frozenset(_ALLOWED_TRANSITIONS)


def can_transition_status(current: str, target: str) -> bool:
    """返回当前状态是否可以转移到目标状态。"""

    return current in _VALID_STATUSES and target in _ALLOWED_TRANSITIONS[current]


def transition_status(current: str, target: str) -> str:
    """校验并返回目标状态，不修改任何外部对象。"""

    if not can_transition_status(current, target):
        raise InvalidWorkflowTransition(current, target)
    return target


def require_revision(actual: int | None, expected: int | None) -> None:
    """校验乐观锁 revision；expected 为 ``None`` 时跳过校验。"""

    if expected is not None and actual != expected:
        raise RevisionConflict(actual, expected)


def _gate_field(gate_result: StageGateResult | Mapping[str, Any], field: str) -> Any:
    if isinstance(gate_result, Mapping):
        return gate_result.get(field)
    if isinstance(gate_result, BaseModel):
        return getattr(gate_result, field, None)
    return None


def _diagnostic_level(diagnostic: Any) -> Any:
    if isinstance(diagnostic, Mapping):
        return diagnostic.get("level")
    if isinstance(diagnostic, BaseModel):
        return getattr(diagnostic, "level", None)
    return getattr(diagnostic, "level", None)


def can_confirm_stage(
    status: str,
    gate_result: StageGateResult | Mapping[str, Any] | None,
) -> bool:
    """判断阶段是否满足确认门禁。

    阶段必须处于 ``ready``。门禁可以是 ``ready`` 或 ``warning``，但必须
    同时通过 ``passed``、``can_confirm``，且不能包含 blocking 诊断。
    """

    if status != "ready" or gate_result is None:
        return False
    if not isinstance(gate_result, Mapping | BaseModel):
        return False

    gate_status = _gate_field(gate_result, "status")
    if gate_status is not None and gate_status not in {"ready", "warning"}:
        return False
    if not _gate_field(gate_result, "passed") or not _gate_field(gate_result, "can_confirm"):
        return False

    diagnostics = _gate_field(gate_result, "diagnostics") or ()
    return all(_diagnostic_level(item) != "blocking" for item in diagnostics)


def ensure_stage_prerequisites_confirmed(
    stage_key: str,
    confirmed_keys: Iterable[str],
) -> None:
    """校验阶段的所有直接前置阶段均已确认。"""

    prerequisites = get_stage_prerequisites(stage_key)
    confirmed = frozenset(confirmed_keys)
    missing = tuple(key for key in prerequisites if key not in confirmed)
    if missing:
        raise StageBlocked(stage_key, missing)


def can_start_stage(stage_key: str, confirmed_keys: Iterable[str]) -> bool:
    """返回阶段是否可以在当前确认集合下启动。"""

    try:
        ensure_stage_prerequisites_confirmed(stage_key, confirmed_keys)
    except StageBlocked:
        return False
    return True


def mark_descendants_stale(
    stage_key: str,
    stage_statuses: Mapping[str, str],
) -> list[str]:
    """返回因上游变化而需要标记为 stale 的后代阶段键。

    返回值只包含阶段键，不会修改传入的状态映射。已经是 ``stale`` 的
    阶段无需重复返回；``cancelled`` 是不可自动复活的终态，也不会被返回。
    """

    return [
        descendant_key
        for descendant_key in get_descendant_stage_keys(stage_key)
        if stage_statuses.get(descendant_key) not in {"stale", "cancelled"}
    ]


__all__ = [
    "InvalidWorkflowTransition",
    "RevisionConflict",
    "StageBlocked",
    "WorkflowStateError",
    "can_confirm_stage",
    "can_start_stage",
    "can_transition_status",
    "ensure_stage_prerequisites_confirmed",
    "mark_descendants_stale",
    "require_revision",
    "transition_status",
]
