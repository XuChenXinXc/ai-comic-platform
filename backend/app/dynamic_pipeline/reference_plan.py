from __future__ import annotations

import copy
import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from pydantic import Field

from app.dynamic_pipeline.diagnostics import diagnostic, diagnostics_status
from app.dynamic_pipeline.prompt_ir import ReferencePlan, prompt_input_hash
from app.dynamic_pipeline.provider_capabilities import (
    normalize_capabilities,
    reference_limit,
)
from app.dynamic_pipeline.schemas import Diagnostic, ReferenceSlot

ReferencePlanStatus = Literal["ready", "warning", "blocked"]

ALLOWED_REFERENCE_PURPOSES = frozenset(
    {
        "design_sheet",
        "clean_identity",
        "layout_reference",
        "prop_identity",
        "keyframe_reference",
        "previous_end_frame",
    }
)
_GENERATION_PURPOSES = ALLOWED_REFERENCE_PURPOSES - {"design_sheet"}
_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp"})
_IMAGE_TOKEN_RE = re.compile(r"@Image(\d+)")
_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:([/\\]|$)")
_VALID_URL_STATUSES = frozenset(
    {
        "",
        "active",
        "available",
        "fresh",
        "http_200",
        "ok",
        "ready",
        "signed",
        "success",
        "usable",
        "valid",
        "200",
    }
)


class ReferencePlanResult(ReferencePlan):
    """带门禁结果的参考图计划。

    该类型继承现有 ``ReferencePlan``，因此旧调用方仍可按计划读取
    ``slots``、实际文件数组和 ``text_mapping``；动态流水线还可以直接读取
    ``status`` 与 ``diagnostics``。
    """

    status: ReferencePlanStatus = "ready"
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    display_slots: list[ReferenceSlot] = Field(default_factory=list)
    provider_capability_snapshot: dict[str, Any] = Field(default_factory=dict)

    @property
    def plan(self) -> ReferencePlan:
        """兼容需要 ``result.plan`` 形式的调用方。"""

        return self

    @property
    def provider_capabilities(self) -> dict[str, Any]:
        """返回不含 Provider 密钥的能力快照。"""

        return copy.deepcopy(self.provider_capability_snapshot)


@dataclass(frozen=True)
class _ResolvedReference:
    slot: ReferenceSlot
    source: Literal["local", "url"]
    value: str
    label: str


@dataclass(frozen=True)
class _SourceIssue:
    code: str
    message: str
    details: dict[str, Any]


_MISSING = object()


def _payload(value: Any) -> dict[str, Any] | None:
    if isinstance(value, ReferenceSlot):
        return value.model_dump(mode="python")
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="python")
        if isinstance(dumped, Mapping):
            return copy.deepcopy(dict(dumped))
    return None


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Path):
        return str(value)
    return ""


def _raw_index(value: Any) -> tuple[int | None, str | None]:
    if value is _MISSING or value is None or (isinstance(value, str) and not value.strip()):
        return None, "missing"
    if isinstance(value, bool):
        return None, "invalid"
    if isinstance(value, int):
        index = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None, "invalid"
        index = int(value)
    elif isinstance(value, str):
        try:
            index = int(value.strip())
        except ValueError:
            return None, "invalid"
    else:
        return None, "invalid"
    return (index, None) if index >= 1 else (None, "invalid")


def _allowed_purpose(value: Any) -> bool:
    return _text(value) in ALLOWED_REFERENCE_PURPOSES


def _slot_label(slot: ReferenceSlot) -> str:
    for value in (slot.role, slot.asset_id, slot.keyframe_id, slot.image_version_id, slot.purpose):
        text = _text(value)
        if text:
            return text
    return f"reference-{slot.index}"


def _is_http_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except ValueError:
        return False
    return parsed.scheme.casefold() in {"http", "https"} and bool(parsed.netloc) and bool(hostname)


def _url_issue(value: str, status: str) -> _SourceIssue | None:
    if not _is_http_url(value):
        return _SourceIssue(
            code="reference_wrong_type",
            message="参考图 URL 只允许使用有效的 http/https 地址",
            details={"source": "provider_url", "reason": "invalid_scheme_or_url"},
        )
    normalized_status = status.casefold().strip()
    if normalized_status not in _VALID_URL_STATUSES:
        return _SourceIssue(
            code="reference_missing",
            message="参考图 URL 已失效或不可用",
            details={"source": "provider_url", "reason": "url_unavailable"},
        )
    return None


def _is_reparse_point(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    return path.is_symlink() or bool(getattr(metadata, "st_file_attributes", 0) & 0x400)


def _relative_parts(value: str) -> tuple[tuple[str, ...], str | None]:
    normalized = value.replace("\\", "/")
    if not normalized or "\x00" in normalized:
        return (), "invalid_path"
    if (
        normalized.startswith("/")
        or normalized.startswith("//")
        or _DRIVE_PATH_RE.match(normalized)
    ):
        return (), "absolute_path"
    try:
        if urlsplit(normalized).scheme:
            return (), "absolute_path"
    except ValueError:
        return (), "invalid_path"
    parts = tuple(part for part in normalized.split("/") if part not in {"", "."})
    if any(part == ".." for part in parts):
        return (), "path_traversal"
    return parts, None


def _resolve_local_path(
    value: str,
    project_root: str | Path | None,
) -> tuple[Path | None, _SourceIssue | None]:
    parts, reason = _relative_parts(value)
    if reason:
        return None, _SourceIssue(
            code="reference_missing",
            message="参考图路径必须是项目目录内的安全相对路径",
            details={"source": "relative_path", "reason": reason},
        )
    if Path(value).suffix.casefold() not in _IMAGE_EXTENSIONS:
        return None, _SourceIssue(
            code="reference_wrong_type",
            message="参考图本地文件扩展名不是受支持的图片类型",
            details={"source": "relative_path", "reason": "unsupported_extension"},
        )
    if project_root is None:
        return None, _SourceIssue(
            code="reference_missing",
            message="未提供 project_root，无法解析本地参考图",
            details={"source": "relative_path", "reason": "project_root_missing"},
        )

    root = Path(project_root).resolve()
    candidate = root.joinpath(*parts)
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None, _SourceIssue(
            code="reference_missing",
            message="参考图路径超出允许的项目目录",
            details={"source": "relative_path", "reason": "path_outside_project"},
        )

    # 与 FileService 的 _inside/is_reparse_point 规则保持一致：不接受链接或联接。
    current = root
    for part in parts:
        current = current / part
        if _is_reparse_point(current):
            return None, _SourceIssue(
                code="reference_missing",
                message="参考图路径不能经过符号链接或目录联接",
                details={"source": "relative_path", "reason": "reparse_point"},
            )
    try:
        if not resolved.is_file() or resolved.stat().st_size <= 0:
            return None, _SourceIssue(
                code="reference_missing",
                message="参考图本地文件不存在、不是文件或为空",
                details={"source": "relative_path", "reason": "file_unavailable"},
            )
    except OSError:
        return None, _SourceIssue(
            code="reference_missing",
            message="参考图本地文件不可读取",
            details={"source": "relative_path", "reason": "file_unavailable"},
        )
    return resolved, None


def _source_issue_diagnostic(
    issue: _SourceIssue,
    *,
    path: str,
    level: Literal["warning", "blocking"],
) -> Diagnostic:
    return diagnostic(level, issue.code, issue.message, path=path, details=issue.details)


def _capability_value(raw: Any, name: str, default: Any = None) -> Any:
    if isinstance(raw, Mapping):
        return raw.get(name, default)
    return getattr(raw, name, default)


def _resolve_slot_source(
    slot: ReferenceSlot,
    *,
    project_root: str | Path | None,
    path: str,
) -> tuple[_ResolvedReference | None, list[Diagnostic]]:
    local_value = _text(slot.relative_path)
    url_value = _text(slot.provider_url)
    local_path: Path | None = None
    local_issue: _SourceIssue | None = None
    url_issue: _SourceIssue | None = None

    if local_value:
        local_path, local_issue = _resolve_local_path(local_value, project_root)
    if url_value:
        url_issue = _url_issue(url_value, _text(slot.provider_url_status))

    diagnostics: list[Diagnostic] = []
    if local_path is not None:
        if url_issue is not None:
            diagnostics.append(
                _source_issue_diagnostic(url_issue, path=f"{path}.provider_url", level="warning")
            )
        return (
            _ResolvedReference(
                slot=slot,
                source="local",
                value=str(local_path),
                label=_slot_label(slot),
            ),
            diagnostics,
        )

    if url_value and url_issue is None:
        if local_issue is not None:
            diagnostics.append(
                _source_issue_diagnostic(
                    local_issue,
                    path=f"{path}.relative_path",
                    level="warning",
                )
            )
        return (
            _ResolvedReference(
                slot=slot,
                source="url",
                value=url_value,
                label=_slot_label(slot),
            ),
            diagnostics,
        )

    if local_issue is not None:
        diagnostics.append(
            _source_issue_diagnostic(
                local_issue,
                path=f"{path}.relative_path",
                level="blocking",
            )
        )
    if url_issue is not None:
        diagnostics.append(
            _source_issue_diagnostic(
                url_issue,
                path=f"{path}.provider_url",
                level="blocking",
            )
        )
    if not local_value and not url_value:
        diagnostics.append(
            diagnostic(
                "blocking",
                "reference_missing",
                "参考图缺少本地路径或 Provider URL",
                path=path,
                details={"reason": "source_missing"},
            )
        )
    return None, diagnostics


def _source_payload(value: Any) -> dict[str, Any]:
    payload = _payload(value)
    if payload is not None:
        if not payload.get("relative_path") and payload.get("file_path"):
            payload["relative_path"] = payload["file_path"]
        if not payload.get("provider_url") and payload.get("url"):
            payload["provider_url"] = payload["url"]
        return payload
    text = _text(value)
    if _is_http_url(text) or text.casefold().startswith(("http:", "https:")):
        return {"provider_url": text}
    return {"relative_path": text}


def _append_previous_end_frame(
    payloads: list[dict[str, Any]],
    previous_end_frame: Any,
) -> None:
    if previous_end_frame is None:
        return
    previous_positions = [
        index
        for index, payload in enumerate(payloads)
        if _text(payload.get("purpose")) == "previous_end_frame"
    ]
    if previous_positions:
        position = previous_positions[0]
        payload = payloads[position]
        if not _text(payload.get("relative_path")) and not _text(payload.get("provider_url")):
            source = _source_payload(previous_end_frame)
            payload.update(
                {
                    key: value
                    for key, value in source.items()
                    if key in {"relative_path", "provider_url"} and value
                }
            )
        return

    source = _source_payload(previous_end_frame)
    indexes = [_raw_index(payload.get("index", _MISSING))[0] for payload in payloads]
    valid_indexes = [index for index in indexes if index is not None]
    source.update(
        {
            "index": max(valid_indexes, default=0) + 1,
            "role": "previous_end_frame",
            "required": True,
            "purpose": "previous_end_frame",
        }
    )
    keyframe_positions = [
        index
        for index, payload in enumerate(payloads)
        if _text(payload.get("purpose")) == "keyframe_reference"
    ]
    insert_at = keyframe_positions[0] + 1 if keyframe_positions else len(payloads)
    payloads.insert(insert_at, source)


def _prepare_slots(
    slots: Sequence[Any] | None,
    *,
    video: bool,
    previous_end_frame: Any,
) -> tuple[list[dict[str, Any]], list[ReferenceSlot], list[Diagnostic], bool]:
    payloads: list[dict[str, Any]] = []
    if slots is not None:
        for value in slots:
            payload = _payload(value)
            payloads.append(payload if payload is not None else {})
    input_payload_count = len(payloads)
    if video:
        _append_previous_end_frame(payloads, previous_end_frame)

    diagnostics: list[Diagnostic] = []
    valid_indices: list[int] = []
    index_invalid = False
    parsed_slots: list[ReferenceSlot] = []

    for position, payload in enumerate(payloads):
        raw_index = payload.get("index", _MISSING)
        parsed_index, index_error = _raw_index(raw_index)
        if parsed_index is not None and position < input_payload_count:
            valid_indices.append(parsed_index)
        if index_error and position < input_payload_count:
            index_invalid = True

        purpose = _text(payload.get("purpose"))
        purpose_invalid = not _allowed_purpose(purpose)
        try:
            slot = ReferenceSlot.model_validate(payload)
        except (TypeError, ValueError):
            slot = None
        if slot is None:
            if purpose_invalid or not index_error:
                diagnostics.append(
                    diagnostic(
                        "blocking",
                        "reference_wrong_type",
                        (
                            f"参考图用途 {purpose or '为空'} 不受支持"
                            if purpose_invalid
                            else "参考图槽位结构不符合 ReferenceSlot"
                        ),
                        path=(
                            f"slots[{position}].purpose"
                            if purpose_invalid
                            else f"slots[{position}]"
                        ),
                        details={
                            "purpose": purpose,
                            "allowed_purposes": sorted(ALLOWED_REFERENCE_PURPOSES),
                            "reason": (
                                "invalid_reference_slot"
                                if not purpose_invalid
                                else "invalid_purpose"
                            ),
                        },
                    )
                )
            continue
        parsed_slots.append(slot)
        if purpose_invalid:
            diagnostics.append(
                diagnostic(
                    "blocking",
                    "reference_wrong_type",
                    f"参考图用途 {purpose or '为空'} 不受支持",
                    path=f"slots[{position}].purpose",
                    details={
                        "purpose": purpose,
                        "allowed_purposes": sorted(ALLOWED_REFERENCE_PURPOSES),
                    },
                )
            )

    expected_indices = list(range(1, input_payload_count + 1))
    if index_invalid or valid_indices != expected_indices:
        diagnostics.append(
            diagnostic(
                "blocking",
                "reference_index_mismatch",
                "参考图 index 必须从 1 开始连续且唯一，已保留原 index，未自动重排",
                path="slots",
                details={
                    "actual_indices": valid_indices,
                    "expected_indices": expected_indices,
                },
            )
        )

    return payloads, parsed_slots, diagnostics, index_invalid or valid_indices != expected_indices


def filter_reference_slots(
    slots: Sequence[ReferenceSlot | Mapping[str, Any]],
    *,
    video: bool = False,
) -> list[ReferenceSlot]:
    """过滤不能进入生成计划的用途，并保持输入 index 与相对顺序。"""

    filtered: list[ReferenceSlot] = []
    for value in slots:
        payload = _payload(value)
        if payload is None:
            continue
        try:
            slot = ReferenceSlot.model_validate(payload)
        except (TypeError, ValueError):
            continue
        if slot.purpose == "design_sheet":
            continue
        if slot.purpose not in _GENERATION_PURPOSES:
            continue
        if slot.purpose == "previous_end_frame" and not video:
            continue
        filtered.append(slot)
    return filtered


def _text_mapping(
    references: Sequence[_ResolvedReference],
) -> tuple[list[str], list[str], dict[str, Any]]:
    actual_local_files: list[str] = []
    actual_urls: list[str] = []
    mapping: dict[str, Any] = {}
    for reference in references:
        token = f"@Image{reference.slot.index}"
        value = {
            "index": reference.slot.index,
            "label": reference.label,
            "source": reference.source,
            "local_file": reference.value if reference.source == "local" else None,
            "local": reference.value if reference.source == "local" else None,
            "url": reference.value if reference.source == "url" else None,
            "path": reference.value if reference.source == "local" else None,
            "value": reference.value,
            "actual": reference.value,
        }
        mapping[token] = value
        if reference.source == "local":
            actual_local_files.append(reference.value)
        else:
            actual_urls.append(reference.value)
    return actual_local_files, actual_urls, mapping


def stable_reference_plan_payload(plan: ReferencePlan | Mapping[str, Any]) -> dict[str, Any]:
    """返回不包含工作目录绝对路径和输入 snapshot_hash 的计划投影。"""

    if isinstance(plan, ReferencePlan):
        raw = plan.model_dump(mode="python")
    elif isinstance(plan, Mapping):
        raw = copy.deepcopy(dict(plan))
    else:
        raw = {}

    stable_slots: list[dict[str, Any]] = []
    for value in raw.get("slots", []) if isinstance(raw.get("slots", []), list) else []:
        payload = _payload(value)
        if payload is None:
            continue
        try:
            slot = ReferenceSlot.model_validate(payload)
        except (TypeError, ValueError):
            stable_slots.append(payload)
            continue
        stable = slot.model_dump(mode="json")
        if stable.get("relative_path"):
            stable["relative_path"] = _text(stable["relative_path"]).replace("\\", "/")
        stable_slots.append(stable)

    raw_mapping = raw.get("text_mapping")
    raw_mapping = raw_mapping if isinstance(raw_mapping, Mapping) else {}
    stable_mapping: dict[str, Any] = {}
    stable_local_files: list[str] = []
    stable_urls: list[str] = []
    for slot_payload in stable_slots:
        index = slot_payload.get("index")
        token = f"@Image{index}"
        original = raw_mapping.get(token)
        original = original if isinstance(original, Mapping) else {}
        relative_path = _text(slot_payload.get("relative_path")).replace("\\", "/")
        provider_url = _text(slot_payload.get("provider_url"))
        source = "local" if relative_path else "url" if provider_url else ""
        value = relative_path or provider_url
        try:
            label = _slot_label(ReferenceSlot.model_validate(slot_payload))
        except (TypeError, ValueError):
            label = f"reference-{index}"
        stable_value = {
            "index": index,
            "label": _text(original.get("label") or original.get("name"))
            or label,
            "source": source,
            "local_file": relative_path if source == "local" else None,
            "local": relative_path if source == "local" else None,
            "url": provider_url if source == "url" else None,
            "path": relative_path if source == "local" else None,
            "value": value,
            "actual": value,
        }
        stable_mapping[token] = stable_value
        if source == "local":
            stable_local_files.append(relative_path)
        elif source == "url":
            stable_urls.append(provider_url)

    result: dict[str, Any] = {
        "slots": stable_slots,
        "actual_local_files": stable_local_files,
        "actual_urls": stable_urls,
        "text_mapping": stable_mapping,
    }
    for key in ("status", "diagnostics", "provider_capability_snapshot"):
        if key in raw:
            value = raw[key]
            if key == "diagnostics" and isinstance(value, Sequence) and not isinstance(
                value, str | bytes
            ):
                result[key] = [
                    item.model_dump(mode="json")
                    if hasattr(item, "model_dump")
                    else copy.deepcopy(item)
                    for item in value
                ]
            else:
                result[key] = copy.deepcopy(value)
    return result


def calculate_reference_plan_snapshot_hash(
    plan: ReferencePlan | Mapping[str, Any],
    *,
    video: bool = False,
    previous_end_frame: Any = None,
) -> str:
    """使用统一 Prompt hash 算法计算稳定的 ReferencePlan snapshot。"""

    stable_previous_end_frame = _source_payload(previous_end_frame)
    if stable_previous_end_frame.get("relative_path"):
        previous_path = _text(stable_previous_end_frame["relative_path"]).replace("\\", "/")
        stable_previous_end_frame["relative_path"] = (
            Path(previous_path).name if Path(previous_path).is_absolute() else previous_path
        )
    stable_previous_end_frame.pop("provider_url", None)
    return prompt_input_hash(
        "reference-plan-snapshot-v2",
        stable_reference_plan_payload(plan),
        video,
        stable_previous_end_frame,
    )


def build_reference_plan(
    slots: Sequence[ReferenceSlot | Mapping[str, Any]] | None,
    *,
    project_root: str | Path | None = None,
    public_url_base: str | None = None,
    provider_capabilities: Any = None,
    video: bool = False,
    previous_end_frame: Any = None,
) -> ReferencePlanResult:
    """生成同时驱动文本、实际输入数组和快照的参考图计划。

    函数只读取 ``project_root`` 下的文件，不访问数据库或网络。``public_url_base``
    保留在接口中供上游传递媒体配置；当前计划直接使用已签发的
    ``provider_url``，不会自行拼接或记录公共地址中的密钥。
    """

    del public_url_base
    _, parsed_slots, diagnostics, index_mismatch = _prepare_slots(
        slots,
        video=video,
        previous_end_frame=previous_end_frame,
    )

    display_slots = list(parsed_slots)
    candidates: list[ReferenceSlot] = []
    for slot in parsed_slots:
        if slot.purpose == "design_sheet":
            diagnostics.append(
                diagnostic(
                    "warning",
                    "design_sheet_as_reference",
                    "design_sheet 仅用于页面展示，不能作为生成参考图",
                    path=f"slots[{slot.index}].purpose",
                    details={"index": slot.index, "video": video},
                )
            )
            continue
        if slot.purpose not in _GENERATION_PURPOSES:
            continue
        if slot.purpose == "previous_end_frame" and not video:
            diagnostics.append(
                diagnostic(
                    "warning",
                    "reference_wrong_type",
                    "previous_end_frame 只用于视频参考图计划",
                    path=f"slots[{slot.index}].purpose",
                    details={"index": slot.index, "video": video},
                )
            )
            continue
        candidates.append(slot)

    resolved: list[_ResolvedReference] = []
    for slot in candidates:
        reference, source_diagnostics = _resolve_slot_source(
            slot,
            project_root=project_root,
            path=f"slots[{slot.index}]",
        )
        diagnostics.extend(source_diagnostics)
        if reference is not None:
            resolved.append(reference)

    capability_snapshot: dict[str, Any] = {}
    allowed_count: int | None = None
    if provider_capabilities is not None:
        try:
            normalized_capabilities = normalize_capabilities(provider_capabilities)
            capability_snapshot = normalized_capabilities.model_dump(mode="json")
            limit_result = reference_limit(normalized_capabilities, len(resolved))
            diagnostics.extend(limit_result.diagnostics)
            allowed_count = limit_result.allowed_count
        except (TypeError, ValueError) as exc:
            diagnostics.append(
                diagnostic(
                    "blocking",
                    "provider_reference_limit",
                    "Provider 参考图能力快照无效，无法安全确定参考图数量上限",
                    path="provider_capabilities",
                    details={"reason": "invalid_capabilities", "error_type": type(exc).__name__},
                )
            )
            # 只保存已知能力字段，避免把 api_key 等配置字段带入快照。
            if isinstance(provider_capabilities, Mapping):
                capability_snapshot = {
                    key: copy.deepcopy(provider_capabilities[key])
                    for key in (
                        "provider_id",
                        "model",
                        "capability_version",
                        "supports_multiple_images",
                        "max_reference_images",
                    )
                    if key in provider_capabilities
                }
            else:
                capability_snapshot = {
                    key: copy.deepcopy(_capability_value(provider_capabilities, key))
                    for key in (
                        "provider_id",
                        "model",
                        "capability_version",
                        "supports_multiple_images",
                        "max_reference_images",
                    )
                    if _capability_value(provider_capabilities, key, _MISSING) is not _MISSING
                }
            raw_max = _capability_value(provider_capabilities, "max_reference_images", _MISSING)
            raw_multiple = _capability_value(
                provider_capabilities,
                "supports_multiple_images",
                _MISSING,
            )
            if isinstance(raw_max, int) and not isinstance(raw_max, bool) and raw_max >= 0:
                allowed_count = min(
                    len(resolved),
                    raw_max if raw_multiple is not False else min(raw_max, 1),
                )
                if allowed_count != len(resolved):
                    diagnostics.append(
                        diagnostic(
                            "blocking",
                            "provider_reference_limit",
                            "请求参考图数量超过 Provider 允许的数量",
                            path="provider_capabilities",
                            details={
                                "requested_count": len(resolved),
                                "allowed_count": allowed_count,
                                "max_reference_images": raw_max,
                                "supports_multiple_images": raw_multiple,
                            },
                        )
                    )

    if allowed_count is not None:
        resolved = resolved[:allowed_count]

    final_slots = [reference.slot for reference in resolved]
    if index_mismatch:
        # index 错误时保留计划槽位和快照，但不生成任何可能错位的引用数组/文本。
        actual_local_files: list[str] = []
        actual_urls: list[str] = []
        text_mapping: dict[str, Any] = {}
    else:
        actual_local_files, actual_urls, text_mapping = _text_mapping(resolved)

    stable_local_files = [
        _text(slot.relative_path).replace("\\", "/")
        for slot in final_slots
        if _text(slot.relative_path)
    ]
    stable_urls = [
        _text(slot.provider_url)
        for slot in final_slots
        if _text(slot.provider_url)
    ]
    stable_mapping: dict[str, Any] = {}
    for slot in final_slots:
        token = f"@Image{slot.index}"
        value = text_mapping.get(token)
        if isinstance(value, Mapping):
            stable_value = dict(value)
            if stable_value.get("source") == "local" and slot.relative_path:
                stable_path = _text(slot.relative_path).replace("\\", "/")
                for key in ("local_file", "local", "path", "value", "actual"):
                    if key in stable_value:
                        stable_value[key] = stable_path
            stable_mapping[token] = stable_value
        elif value is not None:
            stable_mapping[token] = value
    gate = diagnostics_status(diagnostics)
    snapshot_hash = calculate_reference_plan_snapshot_hash(
        {
            "slots": final_slots,
            "actual_local_files": stable_local_files,
            "actual_urls": stable_urls,
            "text_mapping": stable_mapping,
            "status": gate.status,
            "diagnostics": gate.diagnostics,
            "provider_capability_snapshot": capability_snapshot,
        },
        video=video,
        previous_end_frame=previous_end_frame,
    )
    return ReferencePlanResult(
        slots=final_slots,
        actual_local_files=actual_local_files,
        actual_urls=actual_urls,
        text_mapping=text_mapping,
        snapshot_hash=snapshot_hash,
        status=cast(ReferencePlanStatus, gate.status),
        diagnostics=gate.diagnostics,
        display_slots=display_slots,
        provider_capability_snapshot=capability_snapshot,
    )


def project_reference_plan(
    plan: ReferencePlan | Mapping[str, Any],
    selected_slots: Sequence[ReferenceSlot | Mapping[str, Any]],
) -> ReferencePlan:
    """从 canonical 计划投影编译器实际使用的槽位，不手工重建数组。"""

    if isinstance(plan, ReferencePlan):
        source = plan
    else:
        source = ReferencePlan.model_validate(
            {
                key: plan.get(key, default)
                for key, default in (
                    ("slots", []),
                    ("actual_local_files", []),
                    ("actual_urls", []),
                    ("text_mapping", {}),
                    ("snapshot_hash", ""),
                )
            }
        )
    slots = [
        slot if isinstance(slot, ReferenceSlot) else ReferenceSlot.model_validate(slot)
        for slot in selected_slots
    ]
    selected_tokens = {f"@Image{slot.index}" for slot in slots}
    mapping = {
        token: copy.deepcopy(value)
        for token, value in source.text_mapping.items()
        if token in selected_tokens
    }
    actual_local_files: list[str] = []
    actual_urls: list[str] = []
    for slot in slots:
        value = mapping.get(f"@Image{slot.index}")
        payload = value if isinstance(value, Mapping) else {}
        source_kind = _text(payload.get("source"))
        local = _text(payload.get("local_file") or payload.get("local") or payload.get("path"))
        url = _text(payload.get("url"))
        if source_kind == "url" and url:
            actual_urls.append(url)
        elif local:
            actual_local_files.append(local)
        elif url:
            actual_urls.append(url)
    snapshot_hash = prompt_input_hash(
        "effective-reference-plan-v1",
        source.snapshot_hash,
        slots,
    )
    return ReferencePlan(
        slots=slots,
        actual_local_files=actual_local_files,
        actual_urls=actual_urls,
        text_mapping=mapping,
        snapshot_hash=snapshot_hash,
    )


def _as_reference_plan(plan: ReferencePlan | Mapping[str, Any]) -> ReferencePlan:
    if isinstance(plan, ReferencePlan):
        return plan
    return ReferencePlan.model_validate(plan)


def _mapping_label(value: Any, token: str) -> str:
    if isinstance(value, Mapping):
        label = _text(value.get("label") or value.get("name"))
        return label or token
    text = _text(value)
    if _is_http_url(text):
        return "remote reference"
    return text or token


def render_reference_text(plan: ReferencePlan | Mapping[str, Any]) -> str:
    """只渲染计划中实际保留的 ``@ImageN`` 映射，绝不重新编号。"""

    resolved_plan = _as_reference_plan(plan)
    mapping = resolved_plan.text_mapping
    rendered: list[str] = []
    seen: set[str] = set()
    for slot in resolved_plan.slots:
        token = f"@Image{slot.index}"
        if token not in mapping or token in seen:
            continue
        rendered.append(f"{token}[{_mapping_label(mapping[token], token)}]")
        seen.add(token)
    return "；".join(rendered)


def _plan_mapping_diagnostics(plan: ReferencePlan) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    slot_indexes = [slot.index for slot in plan.slots]
    slot_tokens = {f"@Image{index}" for index in slot_indexes}
    mapping = plan.text_mapping
    for token in mapping:
        if token not in slot_tokens:
            diagnostics.append(
                diagnostic(
                    "blocking",
                    "reference_index_mismatch",
                    f"文本映射 {token} 不对应计划中的实际参考图槽位",
                    path="text_mapping",
                    details={"token": token},
                )
            )

    expected_local: list[str] = []
    expected_urls: list[str] = []
    for slot in plan.slots:
        token = f"@Image{slot.index}"
        value = mapping.get(token)
        if isinstance(value, Mapping):
            source = value.get("source")
            local_file = _text(value.get("local_file"))
            url = _text(value.get("url"))
            if source == "local" and local_file:
                expected_local.append(local_file)
            elif source == "url" and url:
                expected_urls.append(url)
        elif value is not None:
            value_text = _text(value)
            if _is_http_url(value_text):
                expected_urls.append(value_text)
            elif value_text:
                expected_local.append(value_text)
    if expected_local != list(plan.actual_local_files) or expected_urls != list(plan.actual_urls):
        diagnostics.append(
            diagnostic(
                "blocking",
                "reference_index_mismatch",
                "@ImageN 文本映射与实际参考图数组不一致",
                path="reference_plan",
                details={
                    "expected_local_count": len(expected_local),
                    "actual_local_count": len(plan.actual_local_files),
                    "expected_url_count": len(expected_urls),
                    "actual_url_count": len(plan.actual_urls),
                },
            )
        )
    return diagnostics


def validate_reference_text(
    text: str,
    plan: ReferencePlan | Mapping[str, Any],
) -> list[Diagnostic]:
    """检查文本中的 ``@ImageN`` 是否都来自计划中的实际映射。"""

    resolved_plan = _as_reference_plan(plan)
    diagnostics = _plan_mapping_diagnostics(resolved_plan)
    mapping = resolved_plan.text_mapping
    for match in _IMAGE_TOKEN_RE.finditer(text or ""):
        token = match.group(0)
        if token not in mapping:
            diagnostics.append(
                diagnostic(
                    "blocking",
                    "reference_index_mismatch",
                    f"文本引用 {token}，但计划中没有对应的实际参考图",
                    path="prompt",
                    details={"token": token, "index": int(match.group(1))},
                )
            )
    return diagnostics


__all__ = [
    "ALLOWED_REFERENCE_PURPOSES",
    "ReferencePlanResult",
    "build_reference_plan",
    "calculate_reference_plan_snapshot_hash",
    "filter_reference_slots",
    "project_reference_plan",
    "render_reference_text",
    "stable_reference_plan_payload",
    "validate_reference_text",
]
