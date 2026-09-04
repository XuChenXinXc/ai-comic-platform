from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.dynamic_pipeline.schemas import Diagnostic, PipelineModel

_MISSING = object()
_DEFAULT_DURATIONS = [6.0]


class ProviderCapabilitiesSnapshot(PipelineModel):
    provider_id: str = ""
    model: str = ""
    capability_version: str = "1"
    supports_multiple_images: bool = False
    max_reference_images: int = Field(default=1, ge=1)
    supported_durations: list[Annotated[float, Field(gt=0)]] = Field(
        default_factory=lambda: list(_DEFAULT_DURATIONS)
    )
    min_duration_sec: float = Field(default=6.0, gt=0)
    max_duration_sec: float = Field(default=6.0, gt=0)
    duration_step_sec: float = Field(default=1.0, gt=0)
    supports_end_frame: bool = False
    supports_audio: bool = False
    supports_dialogue: bool = False

    @model_validator(mode="after")
    def validate_duration_ranges(self) -> ProviderCapabilitiesSnapshot:
        durations = [
            *self.supported_durations,
            self.min_duration_sec,
            self.max_duration_sec,
            self.duration_step_sec,
        ]
        if any(not math.isfinite(value) for value in durations):
            raise ValueError("时长能力值必须是有限数")
        if self.max_duration_sec < self.min_duration_sec:
            raise ValueError("max_duration_sec 必须大于或等于 min_duration_sec")
        return self


class DurationAdaptation(PipelineModel):
    requested_duration_sec: float = Field(gt=0)
    effective_duration_sec: float = Field(gt=0)
    changed: bool
    diagnostics: list[Diagnostic] = Field(default_factory=list)


class ReferenceLimitResult(PipelineModel):
    requested_count: int = Field(ge=0)
    allowed_count: int = Field(ge=0)
    changed: bool
    status: Literal["ready", "blocked"]
    diagnostics: list[Diagnostic] = Field(default_factory=list)


def _read_capability(raw: Mapping[str, Any] | BaseModel, name: str) -> Any:
    if isinstance(raw, Mapping):
        return raw.get(name, _MISSING)
    return getattr(raw, name, _MISSING)


def _capability_value(raw: Mapping[str, Any] | BaseModel, name: str, default: Any) -> Any:
    value = _read_capability(raw, name)
    return default if value is _MISSING or value is None else value


def normalize_capabilities(
    raw: Mapping[str, Any] | BaseModel | None,
) -> ProviderCapabilitiesSnapshot:
    """将 Provider 能力转换为稳定、保守的能力快照。"""
    if raw is None:
        raw = {}
    elif not isinstance(raw, Mapping | BaseModel):
        raise TypeError("capabilities 必须是 Mapping 或 Pydantic model")

    payload: dict[str, Any] = {
        name: _capability_value(raw, name, default)
        for name, default in (
            ("provider_id", ""),
            ("model", ""),
            ("capability_version", "1"),
            ("supports_multiple_images", False),
            ("max_reference_images", 1),
            ("min_duration_sec", 6.0),
            ("max_duration_sec", 6.0),
            ("duration_step_sec", 1.0),
            ("supports_end_frame", False),
            ("supports_audio", False),
            ("supports_dialogue", False),
        )
    }
    duration_value = _read_capability(raw, "supported_durations")
    if duration_value is _MISSING or duration_value is None:
        payload["supported_durations"] = list(_DEFAULT_DURATIONS)
    elif isinstance(duration_value, int | float):
        # 标量 supported_durations 是常见写法：包成单元素列表。
        payload["supported_durations"] = [float(duration_value)]
    else:
        payload["supported_durations"] = duration_value
    # max_reference_images=0 由 Schema 的 ge=1 校验拒绝（保持既有契约）。
    return ProviderCapabilitiesSnapshot.model_validate(payload)


def _duration_equal(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-9)


def _clean_duration(value: float) -> float:
    return float(round(value, 12))


def _nearest_supported_duration(requested: float, supported: list[float]) -> float:
    return min(supported, key=lambda candidate: (abs(candidate - requested), candidate))


def _step_aligned_duration(requested: float, capabilities: ProviderCapabilitiesSnapshot) -> float:
    minimum = capabilities.min_duration_sec
    maximum = capabilities.max_duration_sec
    clamped = min(max(requested, minimum), maximum)
    offset = (clamped - minimum) / capabilities.duration_step_sec
    lower_steps = math.floor(offset + 1e-12)
    upper_steps = lower_steps + 1
    candidates = [
        minimum + lower_steps * capabilities.duration_step_sec,
        minimum + upper_steps * capabilities.duration_step_sec,
        minimum,
        maximum,
    ]
    bounded = [min(max(candidate, minimum), maximum) for candidate in candidates]
    return min(bounded, key=lambda candidate: (abs(candidate - clamped), candidate))


def adapt_generation_duration(
    requested: float,
    caps: ProviderCapabilitiesSnapshot | Mapping[str, Any] | BaseModel,
) -> DurationAdaptation:
    if isinstance(requested, bool):
        raise ValueError("requested_duration_sec 必须大于 0")
    try:
        requested_value = float(requested)
    except (TypeError, ValueError) as exc:
        raise ValueError("requested_duration_sec 必须是大于 0 的有限数") from exc
    if not math.isfinite(requested_value) or requested_value <= 0:
        raise ValueError("requested_duration_sec 必须大于 0")

    capabilities = normalize_capabilities(caps)
    if capabilities.supported_durations:
        selected = _nearest_supported_duration(requested_value, capabilities.supported_durations)
    else:
        selected = _step_aligned_duration(requested_value, capabilities)
    effective = (
        requested_value if _duration_equal(requested_value, selected) else _clean_duration(selected)
    )
    changed = not _duration_equal(requested_value, effective)
    diagnostics: list[Diagnostic] = []
    if changed:
        diagnostics.append(
            Diagnostic(
                level="warning",
                code="provider_duration_unsupported",
                message=(
                    f"请求时长 {requested_value:g} 秒不受 Provider 支持，已适配为 {effective:g} 秒"
                ),
                details={
                    "requested_duration_sec": requested_value,
                    "effective_duration_sec": effective,
                },
            )
        )
    return DurationAdaptation(
        requested_duration_sec=requested_value,
        effective_duration_sec=effective,
        changed=changed,
        diagnostics=diagnostics,
    )


def reference_limit(
    caps: ProviderCapabilitiesSnapshot | Mapping[str, Any] | BaseModel,
    count: int,
) -> ReferenceLimitResult:
    if isinstance(count, bool) or not isinstance(count, int):
        raise ValueError("参考图数量必须是非负整数")
    if count < 0:
        raise ValueError("参考图数量不能为负数")

    capabilities = normalize_capabilities(caps)
    allowed_limit = capabilities.max_reference_images
    if not capabilities.supports_multiple_images:
        allowed_limit = min(allowed_limit, 1)
    allowed_count = min(count, allowed_limit)
    changed = allowed_count != count
    diagnostics: list[Diagnostic] = []
    if changed:
        diagnostics.append(
            Diagnostic(
                level="blocking",
                code="provider_reference_limit",
                message=(
                    f"请求 {count} 个参考图超过 Provider 允许的 {allowed_limit} 个，"
                    f"最多可使用 {allowed_count} 个"
                ),
                details={
                    "requested_count": count,
                    "allowed_count": allowed_count,
                    "max_reference_images": capabilities.max_reference_images,
                    "supports_multiple_images": capabilities.supports_multiple_images,
                },
            )
        )
    return ReferenceLimitResult(
        requested_count=count,
        allowed_count=allowed_count,
        changed=changed,
        status="blocked" if changed else "ready",
        diagnostics=diagnostics,
    )
