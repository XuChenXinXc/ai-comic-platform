from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.dynamic_pipeline.prompt_ir import ReferencePlan
from app.dynamic_pipeline.reference_plan import (
    build_reference_plan,
    calculate_reference_plan_snapshot_hash,
    stable_reference_plan_payload,
)

_PURPOSE_BY_ROLE = {
    "character": "clean_identity",
    "character_identity": "clean_identity",
    "scene": "layout_reference",
    "scene_layout": "layout_reference",
    "prop": "prop_identity",
    "prop_identity": "prop_identity",
    "previous": "previous_end_frame",
    "previous_end_frame": "previous_end_frame",
    "previous_keyframe": "keyframe_reference",
    "keyframe": "keyframe_reference",
    "keyframe_reference": "keyframe_reference",
}


def adapt_reference_bindings(value: Any) -> list[dict[str, Any]]:
    """只做 raw binding 到 ReferenceSlot 字段的形状适配，不解析数据库 ID。"""

    if isinstance(value, Mapping):
        if isinstance(value.get("slots"), list):
            values = value["slots"]
        elif "role" in value or "purpose" in value:
            values = [value]
        else:
            values = []
    elif isinstance(value, list | tuple):
        values = list(value)
    else:
        values = []

    result: list[dict[str, Any]] = []
    for position, raw in enumerate(values, start=1):
        if not isinstance(raw, Mapping):
            result.append({"index": position, "role": "reference", "purpose": ""})
            continue
        role = str(raw.get("role") or raw.get("reference_role") or "reference").strip()
        purpose = str(raw.get("purpose") or raw.get("reference_purpose") or "").strip()
        purpose = purpose or _PURPOSE_BY_ROLE.get(role.casefold(), role)
        payload: dict[str, Any] = {
            "index": raw.get("index", position),
            "role": role,
            "purpose": purpose,
            "required": bool(raw.get("required", False)),
        }
        aliases = {
            "asset_id": ("asset_id",),
            "keyframe_id": ("keyframe_id",),
            "image_version_id": ("image_version_id", "asset_image_id"),
            "relative_path": ("relative_path", "file_path", "path"),
            "provider_url": ("provider_url", "url"),
            "provider_url_status": ("provider_url_status", "url_status"),
        }
        for target, names in aliases.items():
            for name in names:
                candidate = raw.get(name)
                if candidate not in (None, ""):
                    payload[target] = candidate
                    break
        result.append(payload)
    return result


def reference_plan_snapshot_hash(
    value: Any,
    *,
    provider_capabilities: Mapping[str, Any] | None = None,
    video: bool = False,
    project_root: str | Path | None = None,
) -> str:
    """为 raw binding 或 canonical plan 生成 canonical snapshot hash。

    输入中的 snapshot_hash 只是展示快照，不能作为当前数据库状态的信任边界。
    """

    raw = value.model_dump(mode="python") if hasattr(value, "model_dump") else value
    plan = build_reference_plan(
        adapt_reference_bindings(raw),
        project_root=project_root,
        provider_capabilities=provider_capabilities,
        video=video,
    )
    payload = stable_reference_plan_payload(
        {
            **plan.model_dump(mode="python"),
            "status": getattr(plan, "status", "ready"),
            "diagnostics": getattr(plan, "diagnostics", []),
            "provider_capability_snapshot": getattr(
                plan,
                "provider_capability_snapshot",
                {},
            ),
        }
    )
    return calculate_reference_plan_snapshot_hash(payload, video=video)

def resolve_reference_inputs(
    plan: ReferencePlan | Mapping[str, Any],
) -> tuple[list[str], list[str]]:
    """返回计划实际保留的本地文件数组和 URL 数组，不重新排序或重新编号。"""

    if not isinstance(plan, ReferencePlan):
        plan = ReferencePlan.model_validate(plan)
    return list(plan.actual_local_files), list(plan.actual_urls)


__all__ = [
    "adapt_reference_bindings",
    "reference_plan_snapshot_hash",
    "resolve_reference_inputs",
]
