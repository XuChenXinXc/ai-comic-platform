from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from pydantic import ValidationError

from app.dynamic_pipeline.diagnostics import (
    diagnostic,
    diagnostics_status,
    unknown_prompt_diagnostic_codes,
)
from app.dynamic_pipeline.prompt_ir import (
    AssetVisualAnchor,
    CompiledPromptBundle,
    ContinuityContext,
    HumanOverrideSet,
    ImagePromptBundle,
    ReferencePlan,
    ShotIntent,
    TimelineSegment,
    VideoPromptBundle,
    VisualStyleSpec,
    apply_human_override,
    calculate_prompt_bundle_hashes,
    prompt_input_hash,
)
from app.dynamic_pipeline.provider_capabilities import (
    DurationAdaptation,
    ProviderCapabilitiesSnapshot,
    adapt_generation_duration,
    normalize_capabilities,
    reference_limit,
)
from app.dynamic_pipeline.reference_plan import (
    calculate_reference_plan_snapshot_hash,
    render_reference_text,
    stable_reference_plan_payload,
    validate_reference_text,
)
from app.dynamic_pipeline.schemas import Diagnostic, DialogueCue, SoundCue
from app.dynamic_pipeline.visual_anchor import anchor_version

COMPILER_VERSION = "dynamic-prompt-2"


class PromptCompileError(ValueError):
    """Prompt 编译输入无法归一化时抛出的错误。"""


@dataclass(frozen=True)
class _CompilerContext:
    compiler_version: str
    style: VisualStyleSpec
    shot: ShotIntent
    anchors: tuple[AssetVisualAnchor, ...]
    continuity: ContinuityContext
    capabilities: ProviderCapabilitiesSnapshot
    duration: DurationAdaptation
    reference_plan: ReferencePlan | None
    reference_snapshot: dict[str, Any]
    reference_plan_hash: str
    dialogue_cues: tuple[DialogueCue, ...]
    sound_cues: tuple[SoundCue, ...]
    diagnostics: tuple[Diagnostic, ...]


_STYLE_CORE_FIELDS = (
    "medium",
    "line_style",
    "color_palette",
    "lighting_style",
    "material_style",
    "realism_level",
    "mood",
)

_CHARACTER_FIELDS = (
    ("Face", "face"),
    ("Hair", "hair"),
    ("Body", "body"),
    ("Costume", "costume"),
    ("Accessories", "accessories"),
)

_SCENE_FIELDS = (
    ("Layout", "layout"),
    ("Axis", "axis"),
    ("Entrances", "entrances"),
    ("Fixed furnishings", "fixed_furnishings"),
    ("Lighting direction", "lighting_direction"),
    ("Weather", "weather"),
    ("Time of day", "time_of_day"),
)

_PROP_FIELDS = (
    ("Shape", "shape"),
    ("Material", "material"),
    ("Color", "color"),
    ("Wear", "wear"),
    ("Owner", "owner"),
    ("Current holder", "current_holder"),
    ("Visibility", "visibility"),
)


def _dump(value: Any) -> Any:
    """复制可哈希的输入快照，避免把调用方对象带入可变结果。"""

    if hasattr(value, "model_dump"):
        return copy.deepcopy(value.model_dump(mode="json"))
    return copy.deepcopy(value)


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return f"{value:g}"
    return ""


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return any(_present(item) for item in value.values())
    if isinstance(value, list | tuple | set | frozenset):
        return any(_present(item) for item in value)
    return True


def _value_text(value: Any) -> str:
    """把 PromptValue 渲染为稳定且可读的短文本。"""

    text = _text(value)
    if text:
        return text
    if isinstance(value, Mapping):
        parts: list[str] = []
        for key in sorted(value, key=str):
            child = _value_text(value[key])
            if child:
                parts.append(f"{key}={child}")
        return "; ".join(parts)
    if isinstance(value, list | tuple | set | frozenset):
        parts = [_value_text(item) for item in value]
        return "、".join(item for item in parts if item)
    if hasattr(value, "model_dump"):
        return _value_text(value.model_dump(mode="python"))
    return ""


def _display(value: Any, fallback: str) -> str:
    return _value_text(value) or fallback


def _parts(values: Sequence[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = _value_text(value)
        if text and text not in result:
            result.append(text)
    return result


def _label_value(label: str, value: Any) -> str:
    text = _value_text(value)
    return f"{label}: {text}" if text else ""


def _validate_model(model_type: Any, value: Any, label: str) -> Any:
    try:
        if isinstance(value, model_type):
            return value.model_copy(deep=True)
        return model_type.model_validate(copy.deepcopy(value))
    except (TypeError, ValueError, ValidationError) as exc:
        raise PromptCompileError(f"{label} 无法归一化: {exc}") from exc


def _normalize_style(style: VisualStyleSpec | Mapping[str, Any]) -> VisualStyleSpec:
    return _validate_model(VisualStyleSpec, style, "style")


def _normalize_shot(shot: ShotIntent | Mapping[str, Any]) -> ShotIntent:
    return _validate_model(ShotIntent, shot, "shot")


def _normalize_continuity(
    continuity: ContinuityContext | Mapping[str, Any] | None,
) -> ContinuityContext:
    if continuity is None:
        return ContinuityContext()
    return _validate_model(ContinuityContext, continuity, "continuity")


def _normalize_anchor(
    value: AssetVisualAnchor | Mapping[str, Any], index: int
) -> AssetVisualAnchor:
    payload = _dump(value)
    if isinstance(payload, Mapping) and not _text(payload.get("asset_id")):
        # 允许把缺少 canonical ID 的 anchor 编译成带阻断诊断的结果，而不是
        # 在 Pydantic 的 min_length 校验处提前丢失诊断上下文。
        payload = dict(payload)
        payload["asset_id"] = None
    return _validate_model(AssetVisualAnchor, payload, f"anchors[{index}]")


def _normalize_anchors(
    anchors: Sequence[AssetVisualAnchor | Mapping[str, Any]],
) -> tuple[AssetVisualAnchor, ...]:
    if isinstance(anchors, str | bytes | Mapping) or not isinstance(anchors, Sequence):
        raise PromptCompileError("anchors 必须是 AssetVisualAnchor/Mapping 的序列")
    return tuple(_normalize_anchor(value, index) for index, value in enumerate(anchors))


def _normalize_override(overrides: HumanOverrideSet | Mapping[str, Any] | None) -> HumanOverrideSet:
    if overrides is None:
        return HumanOverrideSet(cleared=True)
    return _validate_model(HumanOverrideSet, overrides, "overrides")


def _reference_payload(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="python")
        if isinstance(dumped, Mapping):
            return copy.deepcopy(dict(dumped))
    return None


def _diagnostic_values(values: Any, *, path_prefix: str = "") -> list[Diagnostic]:
    if not isinstance(values, Sequence) or isinstance(values, str | bytes):
        return []
    result: list[Diagnostic] = []
    for index, value in enumerate(values):
        try:
            item = value if isinstance(value, Diagnostic) else Diagnostic.model_validate(value)
        except (TypeError, ValueError, ValidationError):
            continue
        if path_prefix and item.path is None:
            item = item.model_copy(update={"path": f"{path_prefix}[{index}]"})
        result.append(item)
    return result


def _normalize_reference_plan(
    value: ReferencePlan | Mapping[str, Any] | None,
) -> tuple[ReferencePlan | None, dict[str, Any], list[Diagnostic]]:
    if value is None:
        return None, {"plan": None}, []

    raw = _reference_payload(value)
    if raw is None:
        raise PromptCompileError("reference_plan 必须是 ReferencePlan 或 Mapping")

    plan_fields = {
        "slots": raw.get("slots", []),
        "actual_local_files": raw.get("actual_local_files", []),
        "actual_urls": raw.get("actual_urls", []),
        "text_mapping": raw.get("text_mapping", {}),
        "snapshot_hash": raw.get("snapshot_hash", ""),
    }
    try:
        plan = ReferencePlan.model_validate(plan_fields)
    except (TypeError, ValueError, ValidationError) as exc:
        raise PromptCompileError(f"reference_plan 无法归一化: {exc}") from exc

    extra_diagnostics = _diagnostic_values(raw.get("diagnostics"), path_prefix="reference_plan")
    raw_snapshot = {
        "plan": plan.model_dump(mode="json"),
        "status": raw.get("status"),
        "diagnostics": [item.model_dump(mode="json") for item in extra_diagnostics],
        "provider_capability_snapshot": _dump(raw.get("provider_capability_snapshot", {})),
    }
    return plan, raw_snapshot, extra_diagnostics


def _reference_values_from_mapping(plan: ReferencePlan) -> tuple[list[str], list[str]]:
    local_files: list[str] = []
    urls: list[str] = []
    for slot in plan.slots:
        token = f"@Image{slot.index}"
        value = plan.text_mapping.get(token)
        payload = _reference_payload(value)
        if payload is not None:
            source = _text(payload.get("source"))
            local = _text(payload.get("local_file") or payload.get("local") or payload.get("path"))
            url = _text(payload.get("url"))
            if source == "url" and url:
                urls.append(url)
            elif local:
                local_files.append(local)
            elif url:
                urls.append(url)
            continue
        value_text = _text(value)
        if value_text.casefold().startswith(("http://", "https://")):
            urls.append(value_text)
        elif value_text:
            local_files.append(value_text)
    return local_files, urls


def _limit_reference_plan(
    plan: ReferencePlan | None,
    capabilities: ProviderCapabilitiesSnapshot,
) -> tuple[ReferencePlan | None, list[Diagnostic]]:
    if plan is None:
        return None, []
    limit = reference_limit(capabilities, len(plan.slots))
    if not limit.changed:
        return plan, list(limit.diagnostics)

    kept_slots = list(plan.slots[: limit.allowed_count])
    kept_tokens = {f"@Image{slot.index}" for slot in kept_slots}
    kept_mapping = {
        token: copy.deepcopy(value)
        for token, value in plan.text_mapping.items()
        if token in kept_tokens
    }
    kept_plan_base = ReferencePlan(
        slots=kept_slots,
        actual_local_files=[],
        actual_urls=[],
        text_mapping=kept_mapping,
        snapshot_hash=plan.snapshot_hash,
    )
    local_files, urls = _reference_values_from_mapping(kept_plan_base)
    effective = kept_plan_base.model_copy(
        update={"actual_local_files": local_files, "actual_urls": urls},
        deep=True,
    )
    # 截断后计划内容已变化：使用与原计划相同的 hash 算法重算 snapshot_hash，
    # 避免截断前的快照继续代表当前实际参考集。
    effective = effective.model_copy(
        update={
            "snapshot_hash": calculate_reference_plan_snapshot_hash(effective),
        }
    )
    return effective, list(limit.diagnostics)


def _normalize_cue(
    value: Any,
    cue_type: type[DialogueCue] | type[SoundCue],
    path: str,
) -> tuple[DialogueCue | SoundCue | None, Diagnostic | None]:
    payload = _reference_payload(value)
    try:
        cue = cue_type.model_validate(payload if payload is not None else value)
    except (TypeError, ValueError, ValidationError) as exc:
        return None, diagnostic(
            "blocking",
            "dialogue_over_time",
            f"{path} 的 cue 文本或时间绑定无效",
            path=path,
            details={"reason": "invalid_cue", "error_type": type(exc).__name__},
        )
    return cue, None


def _validate_cue_bounds(
    cue: DialogueCue | SoundCue,
    *,
    cue_kind: str,
    index: int,
    bound: float,
    requested_duration: float,
) -> Diagnostic | None:
    if isinstance(cue, DialogueCue):
        text = cue.text.strip()
        path = f"dialogue_cues[{index}]"
    else:
        text = cue.description.strip()
        path = f"sound_cues[{index}]"
    details = {
        "cue_kind": cue_kind,
        "start_time": cue.start_time,
        "end_time": cue.end_time,
        "duration_sec": bound,
        "requested_duration_sec": requested_duration,
    }
    if not text:
        return diagnostic(
            "blocking",
            "dialogue_over_time",
            f"{path} 缺少可绑定的文本描述",
            path=path,
            details={**details, "reason": "empty_text"},
        )
    if cue.start_time >= bound or cue.end_time <= 0:
        return diagnostic(
            "blocking",
            "dialogue_over_time",
            f"{path} 完全落在有效时长之外",
            path=path,
            details={**details, "reason": "outside_duration"},
        )
    if cue.end_time > bound:
        return diagnostic(
            "warning",
            "dialogue_over_time",
            f"{path} 结束时间超过有效 cue 时长",
            path=path,
            details={**details, "reason": "end_after_duration"},
        )
    return None


def _normalize_cues(
    shot: ShotIntent,
    duration: DurationAdaptation,
) -> tuple[tuple[DialogueCue, ...], tuple[SoundCue, ...], list[Diagnostic]]:
    # Cue 的绑定边界取用户请求时长和 Provider 实际生成时长的较小值，避免
    # Provider 为补齐时长而产生的尾部稳定段被误当成原镜头的对白时间。
    bound = min(duration.requested_duration_sec, duration.effective_duration_sec)
    dialogue: list[DialogueCue] = []
    sound: list[SoundCue] = []
    diagnostics: list[Diagnostic] = []

    for index, value in enumerate(shot.dialogue_cues):
        cue, issue = _normalize_cue(value, DialogueCue, f"dialogue_cues[{index}]")
        if issue is not None:
            diagnostics.append(issue)
            continue
        assert isinstance(cue, DialogueCue)
        dialogue.append(cue)
        bounds_issue = _validate_cue_bounds(
            cue,
            cue_kind="dialogue",
            index=index,
            bound=bound,
            requested_duration=duration.requested_duration_sec,
        )
        if bounds_issue is not None:
            diagnostics.append(bounds_issue)

    for index, value in enumerate(shot.sound_cues):
        cue, issue = _normalize_cue(value, SoundCue, f"sound_cues[{index}]")
        if issue is not None:
            diagnostics.append(issue)
            continue
        assert isinstance(cue, SoundCue)
        sound.append(cue)
        bounds_issue = _validate_cue_bounds(
            cue,
            cue_kind="sound",
            index=index,
            bound=bound,
            requested_duration=duration.requested_duration_sec,
        )
        if bounds_issue is not None:
            diagnostics.append(bounds_issue)

    return tuple(dialogue), tuple(sound), diagnostics


def _anchor_diagnostics(anchors: Sequence[AssetVisualAnchor]) -> list[Diagnostic]:
    if not anchors:
        return [
            diagnostic(
                "blocking",
                "asset_unresolved",
                "当前镜头没有可用的 visual anchor",
                path="anchors",
                details={"reason": "empty"},
            )
        ]

    diagnostics: list[Diagnostic] = []
    for index, anchor in enumerate(anchors):
        if not _text(anchor.asset_id):
            diagnostics.append(
                diagnostic(
                    "blocking",
                    "asset_unresolved",
                    f"资产 {anchor.canonical_name} 缺少 canonical asset_id",
                    path=f"anchors[{index}].asset_id",
                    details={"canonical_name": anchor.canonical_name, "reason": "asset_id_missing"},
                )
            )

        if anchor.asset_type == "character":
            required = {
                field: _present(getattr(anchor, field))
                for field in ("face", "hair", "body", "costume")
            }
            if not all(required.values()) or not _present(anchor.visual_facts):
                diagnostics.append(
                    diagnostic(
                        "warning",
                        "character_drift_risk",
                        f"角色 {anchor.canonical_name} 的视觉事实不足，存在身份漂移风险",
                        path=f"anchors[{index}]",
                        details={"missing_fields": [key for key, ok in required.items() if not ok]},
                    )
                )

        references = [
            payload
            for value in anchor.approved_references
            if (payload := _reference_payload(value)) is not None
        ]
        if references and all(
            _text(item.get("purpose") or item.get("reference_purpose")).casefold() == "design_sheet"
            or _text(item.get("type") or item.get("reference_type") or item.get("kind")).casefold()
            in {"design_sheet", "setting_sheet"}
            for item in references
        ):
            diagnostics.append(
                diagnostic(
                    "warning",
                    "design_sheet_as_reference",
                    f"资产 {anchor.canonical_name} 只有 design_sheet 参考，不能提供干净身份参考",
                    path=f"anchors[{index}].approved_references",
                    details={"canonical_name": anchor.canonical_name},
                )
            )
    return diagnostics


def _style_diagnostics(style: VisualStyleSpec) -> list[Diagnostic]:
    if all(not _present(getattr(style, field)) for field in _STYLE_CORE_FIELDS):
        return [
            diagnostic(
                "warning",
                "style_incomplete",
                "结构化视觉风格核心字段全部为空",
                path="style",
                details={"fields": list(_STYLE_CORE_FIELDS)},
            )
        ]
    return []


def _continuity_diagnostics(shot: ShotIntent, continuity: ContinuityContext) -> list[Diagnostic]:
    transition = _transition_type(shot, continuity)
    is_continuous = shot.is_continuous or transition == "continuous"
    if is_continuous and not _text(continuity.previous_end_frame_path):
        return [
            diagnostic(
                "blocking",
                "continuous_previous_end_frame_missing",
                "continuous 镜头缺少上一镜末帧路径",
                path="continuity.previous_end_frame_path",
                details={"transition_type": transition or "continuous"},
            )
        ]
    return []


def _transition_type(shot: ShotIntent, continuity: ContinuityContext) -> str:
    continuity_transition = _text(continuity.transition_type).casefold()
    shot_transition = _text(shot.transition_from_prev).casefold()
    if continuity_transition and continuity_transition != "scene_change":
        return continuity_transition
    return shot_transition or continuity_transition or "scene_change"


def _video_required_diagnostics(shot: ShotIntent) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    if not _present(shot.action):
        diagnostics.append(
            diagnostic(
                "blocking",
                "video_action_missing",
                "视频镜头缺少主要动作",
                path="shot.action",
                details={"reason": "action_missing"},
            )
        )
    if not _present(shot.end_state):
        diagnostics.append(
            diagnostic(
                "blocking",
                "video_end_state_missing",
                "视频镜头缺少明确结束状态",
                path="shot.end_state",
                details={"reason": "end_state_missing"},
            )
        )
    return diagnostics


def _reference_plan_diagnostics(plan: ReferencePlan | None) -> list[Diagnostic]:
    if plan is None:
        return []
    result: list[Diagnostic] = []
    for slot in plan.slots:
        if _text(slot.purpose).casefold() == "design_sheet":
            result.append(
                diagnostic(
                    "warning",
                    "design_sheet_as_reference",
                    "design_sheet 仅用于设定展示，不能作为生成参考图",
                    path=f"reference_plan.slots[{slot.index}].purpose",
                    details={"index": slot.index},
                )
            )
    return result


def _dedupe_diagnostics(values: Sequence[Diagnostic]) -> list[Diagnostic]:
    result: list[Diagnostic] = []
    seen: set[str] = set()
    for item in values:
        key = json.dumps(item.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _prepare_context(
    *,
    style: VisualStyleSpec | Mapping[str, Any],
    shot: ShotIntent | Mapping[str, Any],
    anchors: Sequence[AssetVisualAnchor | Mapping[str, Any]],
    continuity: ContinuityContext | Mapping[str, Any] | None,
    reference_plan: ReferencePlan | Mapping[str, Any] | None,
    capabilities: ProviderCapabilitiesSnapshot | Mapping[str, Any] | None,
    compiler_version: str,
    channel: Literal["image", "video", "both"] = "both",
) -> _CompilerContext:
    if not isinstance(compiler_version, str) or not compiler_version.strip():
        raise PromptCompileError("compiler_version 不能为空")

    normalized_style = _normalize_style(style)
    normalized_shot = _normalize_shot(shot)
    normalized_anchors = _normalize_anchors(anchors)
    normalized_continuity = _normalize_continuity(continuity)
    try:
        normalized_capabilities = normalize_capabilities(capabilities)
    except (TypeError, ValueError, ValidationError) as exc:
        raise PromptCompileError(f"capabilities 无法归一化: {exc}") from exc

    try:
        duration = adapt_generation_duration(
            normalized_shot.duration_sec,
            normalized_capabilities,
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise PromptCompileError(f"shot.duration_sec 无法适配: {exc}") from exc

    normalized_plan, reference_snapshot, plan_diagnostics = _normalize_reference_plan(
        reference_plan
    )
    effective_plan, limit_diagnostics = _limit_reference_plan(
        normalized_plan,
        normalized_capabilities,
    )
    if normalized_plan is not None:
        reference_snapshot["effective_plan"] = (
            effective_plan.model_dump(mode="json") if effective_plan is not None else None
        )
    reference_plan_hash = (
        _text(normalized_plan.snapshot_hash)
        if normalized_plan is not None and _text(normalized_plan.snapshot_hash)
        else prompt_input_hash(reference_snapshot)
    )

    dialogue_cues, sound_cues, cue_diagnostics = _normalize_cues(normalized_shot, duration)
    # 显式 channel：图片编译路径不生成视频专属诊断（action/end_state 缺失、
    # continuous 末帧、Provider 时长适配），无需调用方注入占位值或事后过滤。
    skip_video_diagnostics = channel == "image"
    diagnostics = [
        *(duration.diagnostics if not skip_video_diagnostics else []),
        *plan_diagnostics,
        *limit_diagnostics,
        *_reference_plan_diagnostics(effective_plan),
        *_anchor_diagnostics(normalized_anchors),
        *_style_diagnostics(normalized_style),
        *(
            []
            if skip_video_diagnostics
            else _continuity_diagnostics(normalized_shot, normalized_continuity)
        ),
        *([] if skip_video_diagnostics else _video_required_diagnostics(normalized_shot)),
        *cue_diagnostics,
    ]
    return _CompilerContext(
        compiler_version=compiler_version,
        style=normalized_style,
        shot=normalized_shot,
        anchors=normalized_anchors,
        continuity=normalized_continuity,
        capabilities=normalized_capabilities,
        duration=duration,
        reference_plan=effective_plan,
        reference_snapshot=reference_snapshot,
        reference_plan_hash=reference_plan_hash,
        dialogue_cues=dialogue_cues,
        sound_cues=sound_cues,
        diagnostics=tuple(_dedupe_diagnostics(diagnostics)),
    )


def _anchor_text(anchor: AssetVisualAnchor) -> str:
    identity = f"{anchor.canonical_name} (asset_id={_text(anchor.asset_id) or 'unresolved'})"
    clauses = [identity]
    common = (
        ("Visual facts", anchor.visual_facts),
        ("Immutable features", anchor.immutable_features),
        ("Makeup keywords", anchor.makeup_keyword_string),
        ("Variants", anchor.variants),
        ("Current variant", anchor.current_variant),
    )
    for label, value in common:
        if clause := _label_value(label, value):
            clauses.append(clause)
    for label, field in (*_CHARACTER_FIELDS, *_SCENE_FIELDS, *_PROP_FIELDS):
        if clause := _label_value(label, getattr(anchor, field)):
            clauses.append(clause)
    return "; ".join(clauses)


def _anchor_facts(anchors: Sequence[AssetVisualAnchor]) -> str:
    return " | ".join(_anchor_text(anchor) for anchor in anchors) or "绑定资产未解析"


def _style_text(style: VisualStyleSpec) -> str:
    fields = (
        ("Medium", style.medium),
        ("Line style", style.line_style),
        ("Color palette", style.color_palette),
        ("Lighting style", style.lighting_style),
        ("Material style", style.material_style),
        ("Realism", style.realism_level),
        ("Mood", style.mood),
        ("Text policy", style.text_policy),
        ("Immutable rules", style.immutable_rules),
        ("Style note", style.style_prompt),
    )
    clauses = [clause for label, value in fields if (clause := _label_value(label, value))]
    return "; ".join(clauses) or "结构化风格规范待补全"


def _negative_text(style: VisualStyleSpec, anchors: Sequence[AssetVisualAnchor]) -> str:
    values: list[Any] = [*style.global_negative_rules]
    for anchor in anchors:
        values.extend(anchor.forbidden_changes)
    values.append(style.text_policy)
    negatives = _parts(values)
    return "；".join(negatives) or "无额外负向约束"


def _reference_text(plan: ReferencePlan | None) -> str:
    if plan is None:
        return ""
    return render_reference_text(plan)


def _reference_map_text(plan: ReferencePlan | None) -> str:
    return _reference_text(plan) or "无计划参考图"


def _cue_text(cue: DialogueCue | SoundCue) -> str:
    if isinstance(cue, DialogueCue):
        speaker = f"{cue.speaker.strip()}: " if cue.speaker.strip() else ""
        return f"{cue.start_time:g}-{cue.end_time:g}s {speaker}{cue.text.strip()}"
    return f"{cue.start_time:g}-{cue.end_time:g}s {cue.kind}: {cue.description.strip()}"


def _cue_plan(
    dialogue_cues: Sequence[DialogueCue],
    sound_cues: Sequence[SoundCue],
) -> dict[str, Any]:
    return {
        "dialogue": [cue.model_dump(mode="json") for cue in dialogue_cues],
        "sound": [cue.model_dump(mode="json") for cue in sound_cues],
    }


def _action_event_text(value: Any) -> str:
    if isinstance(value, Mapping):
        return _text(
            value.get("action")
            or value.get("visual_action")
            or value.get("main_action")
            or value.get("description")
            or value.get("event_summary")
            or value.get("summary")
        )
    return _text(value)


def _action_event_time(value: Mapping[str, Any], *names: str) -> float | None:
    for name in names:
        candidate = value.get(name)
        if isinstance(candidate, int | float) and not isinstance(candidate, bool):
            return float(candidate)
        if isinstance(candidate, str):
            try:
                return float(candidate.strip())
            except ValueError:
                continue
    return None


def _timeline_events(
    context: _CompilerContext,
) -> tuple[list[tuple[float, float, str]], list[Diagnostic]]:
    raw_events = list(context.shot.action_events)
    if not raw_events:
        raw_events = [
            {
                "action": _text(context.shot.action),
                "start": 0,
                "end": context.duration.effective_duration_sec,
            }
        ]
    parsed: list[tuple[float | None, float | None, float | None, str]] = []
    for value in raw_events:
        mapping = dict(value) if isinstance(value, Mapping) else {}
        text = _action_event_text(value)
        if not text:
            continue
        parsed.append(
            (
                _action_event_time(mapping, "start", "start_sec", "start_time"),
                _action_event_time(mapping, "end", "end_sec", "end_time"),
                _action_event_time(mapping, "duration", "duration_sec", "length"),
                text,
            )
        )

    duration = context.duration.effective_duration_sec
    if not parsed:
        return (
            [
                (
                    0,
                    duration,
                    _text(context.shot.action) or "保持当前状态",
                )
            ],
            [],
        )

    result: list[tuple[float, float, str]] = []
    warnings: list[Diagnostic] = []
    cursor = 0.0
    implicit_count = sum(
        start is None and end is None and length is None
        for start, end, length, _ in parsed
    )
    remaining = duration
    for index, (start, end, length, text) in enumerate(parsed):
        raw_start, raw_end = start, end
        if start is None and end is None:
            start = cursor
            if length is None:
                length = remaining / max(implicit_count, 1)
            end = start + length
            implicit_count -= 1
        elif start is None and end is not None:
            if length is None:
                # 只含 end 无 start/length：无法推导有效时间段，不能静默丢弃。
                warnings.append(
                    diagnostic(
                        "warning",
                        "timeline_event_dropped",
                        f"action_events[{index}] 缺少 start/length，无法推导有效时间段，已被丢弃",
                        path=f"shot.action_events[{index}]",
                        details={
                            "reason": "end_only_without_start_or_length",
                            "event": text,
                        },
                    )
                )
                continue
            start = end - length
        elif end is None and start is not None:
            if length is None:
                # 只含 start 无 end/length：无法推导有效时间段，不能静默丢弃。
                warnings.append(
                    diagnostic(
                        "warning",
                        "timeline_event_dropped",
                        f"action_events[{index}] 缺少 end/length，无法推导有效时间段，已被丢弃",
                        path=f"shot.action_events[{index}]",
                        details={
                            "reason": "start_only_without_end_or_length",
                            "event": text,
                        },
                    )
                )
                continue
            end = start + length
        if start is None or end is None:
            continue
        clamped = False
        if start < 0 or start > duration:
            clamped = True
        if end < 0 or end > duration:
            clamped = True
        start = max(0.0, min(start, duration))
        end = max(0.0, min(end, duration))
        if clamped:
            warnings.append(
                diagnostic(
                    "warning",
                    "timeline_event_clamped",
                    f"action_events[{index}] 时间段超出有效时长，已被裁剪到 [0, {duration:g}s]",
                    path=f"shot.action_events[{index}]",
                    details={
                        "raw_start": raw_start,
                        "raw_end": raw_end,
                        "clamped_start": start,
                        "clamped_end": end,
                        "duration_sec": duration,
                    },
                )
            )
        if end <= start:
            warnings.append(
                diagnostic(
                    "warning",
                    "timeline_event_dropped",
                    f"action_events[{index}] 归一化后时间段为空，已被丢弃",
                    path=f"shot.action_events[{index}]",
                    details={"start": start, "end": end, "event": text},
                )
            )
            continue
        result.append((start, end, text))
        cursor = max(cursor, end)
        remaining = max(0.0, duration - cursor)
    return sorted(result, key=lambda item: (item[0], item[1], item[2])), warnings


def _video_timeline(context: _CompilerContext) -> tuple[list[TimelineSegment], list[Diagnostic]]:
    duration = context.duration.effective_duration_sec
    action_events, timeline_warnings = _timeline_events(context)
    boundaries = {0.0, duration}
    for start, end, _ in action_events:
        boundaries.update((start, end))
    for cue in (*context.dialogue_cues, *context.sound_cues):
        boundaries.update(
            (
                max(0.0, min(duration, cue.start_time)),
                max(0.0, min(duration, cue.end_time)),
            )
        )
    points = sorted(point for point in boundaries if 0 <= point <= duration)
    segments: list[TimelineSegment] = []
    for start, end in zip(points, points[1:], strict=False):
        if end - start <= 0.001:
            continue
        active_actions = [
            text
            for event_start, event_end, text in action_events
            if min(end, event_end) - max(start, event_start) > 0.001
        ]
        instructions = active_actions or ["保持当前状态，不新增动作"]
        for cue in (*context.dialogue_cues, *context.sound_cues):
            if min(end, cue.end_time) - max(start, cue.start_time) <= 0.001:
                continue
            instructions.append(
                f"对白：{_cue_text(cue)}"
                if isinstance(cue, DialogueCue)
                else f"音效：{_cue_text(cue)}"
            )
        segments.append(
            TimelineSegment(start=start, end=end, action="；".join(instructions))
        )
    if segments and _present(context.shot.end_state):
        last = segments[-1]
        end_state_text = _display(context.shot.end_state, "结束状态")
        segments[-1] = last.model_copy(
            update={
                "action": (
                    f"{last.action}；到达结束状态：{end_state_text}，"
                    "随后保持稳定"
                )
            }
        )
    return segments, timeline_warnings


def _base_semantic_plan(context: _CompilerContext) -> dict[str, Any]:
    stable_reference = (
        stable_reference_plan_payload(context.reference_plan)
        if context.reference_plan is not None
        else None
    )
    return {
        "subject": _dump(context.shot.subject),
        "action": _dump(context.shot.action),
        "action_events": copy.deepcopy(context.shot.action_events),
        "scene": _dump(context.shot.scene),
        "camera": _dump(context.shot.camera),
        "end_state": _dump(context.shot.end_state),
        "continuity_state": copy.deepcopy(context.shot.continuity_state),
        "continuity": context.continuity.model_dump(mode="json"),
        "reference_mapping": (
            copy.deepcopy(stable_reference.get("text_mapping", {}))
            if stable_reference is not None
            else {}
        ),
        "cues": _cue_plan(context.dialogue_cues, context.sound_cues),
        "asset_anchors": [anchor.model_dump(mode="json") for anchor in context.anchors],
    }


def _compile_image_bundle(context: _CompilerContext) -> ImagePromptBundle:
    shot = context.shot
    semantic_plan = _base_semantic_plan(context)
    # 图片只保存当前静止状态；动作和 end_state 不进入图片语义计划或文本。
    semantic_plan["action"] = None
    semantic_plan["end_state"] = None
    semantic_plan["current_state"] = (
        _dump(shot.start_state) if _present(shot.start_state) else _dump(shot.subject)
    )
    semantic_plan["cues"] = {"dialogue": [], "sound": []}

    subject = _display(shot.subject, "当前镜头绑定主体")
    scene = _display(shot.scene, "当前镜头场景")
    camera = _display(shot.camera, "固定机位")
    lighting = _display(shot.lighting, context.style.lighting_style or "按场景光源保持一致")
    current_state = _display(shot.start_state, "当前静止姿态与表情")
    prompt_lines = [
        f"Subject: {subject}",
        f"Current state: {current_state}",
        f"Asset facts: {_anchor_facts(context.anchors)}",
        f"Scene: {scene}",
        f"Camera: {camera}",
        f"Lighting: {lighting}",
        f"Style: {_style_text(context.style)}",
        f"Reference map: {_reference_map_text(context.reference_plan)}",
    ]
    prompt = "\n".join(prompt_lines)
    return ImagePromptBundle(
        prompt=prompt,
        negative_prompt=_negative_text(context.style, context.anchors),
        keyframe_time_sec=0,
        reference_slots=copy.deepcopy(list(context.reference_plan.slots))
        if context.reference_plan is not None
        else [],
        semantic_plan=semantic_plan,
    )


def _compile_video_bundle(context: _CompilerContext) -> VideoPromptBundle:
    shot = context.shot
    action = _display(shot.action, "主要动作未指定")
    camera = _display(shot.camera, "固定机位，保持构图稳定")
    end_state = _display(shot.end_state, "结束状态未指定")
    subject = _display(shot.subject, "当前镜头绑定主体")
    scene = _display(shot.scene, "当前镜头场景")
    lighting = _display(shot.lighting, context.style.lighting_style or "按场景光源保持一致")
    audio = _display(shot.audio, "无额外音频意图")
    dialogue_text = [_cue_text(cue) for cue in context.dialogue_cues]
    sound_text = [_cue_text(cue) for cue in context.sound_cues]
    cue_text = [*dialogue_text, *sound_text]
    if cue_text:
        audio = f"{audio}；cues: {'；'.join(cue_text)}"

    timeline, timeline_warnings = _video_timeline(context)
    transition = _transition_type(shot, context.continuity)
    continuity_text = (
        f"transition={transition}; "
        f"previous_shot={_display(context.continuity.previous_shot_id, 'none')}; "
        f"previous_end_frame={_display(context.continuity.previous_end_frame_path, 'none')}; "
        f"previous_end_state={_display(context.continuity.previous_end_state, 'none')}; "
        "previous_structured_state="
        f"{_display(context.continuity.previous_continuity_state, 'none')}; "
        f"current_structured_state={_display(context.shot.continuity_state, 'none')}"
    )
    reference_text = _reference_map_text(context.reference_plan)
    prompt_lines = [
        f"Subject: {subject}",
        f"Action: {action}",
        f"End state: {end_state}",
        f"Asset facts: {_anchor_facts(context.anchors)}",
        f"Scene: {scene}",
        f"Camera: {camera}",
        f"Lighting/Style: {lighting}; {_style_text(context.style)}",
        f"Audio: {audio}",
        "Timeline:\n"
        + "\n".join(
            f"{segment.start:g}-{segment.end:g}s: {segment.action}" for segment in timeline
        ),
        f"Continuity: {continuity_text}",
        f"Negative: {_negative_text(context.style, context.anchors)}",
        f"Reference map: {reference_text or '无计划参考图'}",
    ]
    semantic_plan = _base_semantic_plan(context)
    semantic_plan["timeline"] = [segment.model_dump(mode="json") for segment in timeline]
    semantic_plan["generation_duration_sec"] = context.duration.effective_duration_sec
    semantic_plan["edit_duration_sec"] = shot.duration_sec
    return VideoPromptBundle(
        prompt="\n".join(prompt_lines),
        negative_prompt=_negative_text(context.style, context.anchors),
        edit_duration_sec=shot.duration_sec,
        generation_duration_sec=context.duration.effective_duration_sec,
        timeline=timeline,
        end_state=_dump(shot.end_state),
        reference_slots=copy.deepcopy(list(context.reference_plan.slots))
        if context.reference_plan is not None
        else [],
        semantic_plan=semantic_plan,
        # 时间轴归一化过程中静默丢弃/裁剪的事件以 warning 诊断保留，
        # 由上层入口合并到最终 bundle diagnostics。
        diagnostics=list(timeline_warnings),
    )


def _validate_bundle_reference_text(
    bundle: ImagePromptBundle | VideoPromptBundle,
    plan: ReferencePlan | None,
) -> list[Diagnostic]:
    if plan is None:
        return []
    return validate_reference_text(bundle.prompt, plan)


def _asset_versions(anchors: Sequence[AssetVisualAnchor]) -> list[dict[str, Any]]:
    versions: list[dict[str, Any]] = []
    for anchor in anchors:
        versions.append(
            {
                "asset_id": anchor.asset_id,
                "revision": anchor.asset_revision,
                "asset_revision": anchor.asset_revision,
                "asset_version": anchor.asset_version,
                "version_hash": anchor_version(anchor),
            }
        )
    return versions


def compile_image_prompt(
    *,
    style: VisualStyleSpec | Mapping[str, Any],
    shot: ShotIntent | Mapping[str, Any],
    anchors: Sequence[AssetVisualAnchor | Mapping[str, Any]],
    continuity: ContinuityContext | Mapping[str, Any] | None = None,
    reference_plan: ReferencePlan | Mapping[str, Any] | None = None,
    capabilities: ProviderCapabilitiesSnapshot | Mapping[str, Any] | None = None,
    compiler_version: str = COMPILER_VERSION,
    _context: _CompilerContext | None = None,
) -> ImagePromptBundle:
    """纯函数式编译图片静态关键帧（channel=image，跳过视频专属诊断）。"""
    context = _context or _prepare_context(
        style=style,
        shot=shot,
        anchors=anchors,
        continuity=continuity,
        reference_plan=reference_plan,
        capabilities=capabilities,
        compiler_version=compiler_version,
        channel="image",
    )
    bundle = _compile_image_bundle(context)
    # 携带门禁诊断：单通道编译不再静默吞掉阻断问题（如锚点缺失、参考图超限）。
    bundle.diagnostics = list(context.diagnostics)
    return bundle


def compile_video_prompt(
    *,
    style: VisualStyleSpec | Mapping[str, Any],
    shot: ShotIntent | Mapping[str, Any],
    anchors: Sequence[AssetVisualAnchor | Mapping[str, Any]],
    continuity: ContinuityContext | Mapping[str, Any] | None = None,
    reference_plan: ReferencePlan | Mapping[str, Any] | None = None,
    capabilities: ProviderCapabilitiesSnapshot | Mapping[str, Any] | None = None,
    compiler_version: str = COMPILER_VERSION,
    _context: _CompilerContext | None = None,
) -> VideoPromptBundle:
    """纯函数式编译视频有序动作与声音时间轴（channel=video）。"""
    context = _context or _prepare_context(
        style=style,
        shot=shot,
        anchors=anchors,
        continuity=continuity,
        reference_plan=reference_plan,
        capabilities=capabilities,
        compiler_version=compiler_version,
        channel="video",
    )
    bundle = _compile_video_bundle(context)
    # 携带门禁诊断：单通道编译不再静默吞掉阻断问题；
    # 并保留时间轴归一化产生的 warning 诊断。
    bundle.diagnostics = [*context.diagnostics, *bundle.diagnostics]
    return bundle


def compile_prompt_bundle(
    *,
    style: VisualStyleSpec | Mapping[str, Any],
    shot: ShotIntent | Mapping[str, Any],
    anchors: Sequence[AssetVisualAnchor | Mapping[str, Any]],
    continuity: ContinuityContext | Mapping[str, Any] | None = None,
    reference_plan: ReferencePlan | Mapping[str, Any] | None = None,
    capabilities: ProviderCapabilitiesSnapshot | Mapping[str, Any] | None = None,
    overrides: HumanOverrideSet | Mapping[str, Any] | None = None,
    compiler_version: str = COMPILER_VERSION,
    channel: Literal["image", "video", "both"] = "both",
) -> CompiledPromptBundle:
    """编译图片和视频两套 Provider-neutral Prompt Bundle。

    ``channel`` 显式声明编译通道：``image`` 只编译图片静态关键帧并跳过视频专属
    诊断；``video`` 只编译视频时间轴；``both`` 保持双通道编译（默认，兼容旧调用）。
    """

    context = _prepare_context(
        style=style,
        shot=shot,
        anchors=anchors,
        continuity=continuity,
        reference_plan=reference_plan,
        capabilities=capabilities,
        compiler_version=compiler_version,
        channel=channel,
    )
    image_bundle: ImagePromptBundle | None = None
    video_bundle: VideoPromptBundle | None = None
    if channel in ("both", "image"):
        image_bundle = compile_image_prompt(
            style=context.style,
            shot=context.shot,
            anchors=context.anchors,
            continuity=context.continuity,
            reference_plan=context.reference_plan,
            capabilities=context.capabilities,
            compiler_version=context.compiler_version,
            _context=context,
        )
    if channel in ("both", "video"):
        video_bundle = compile_video_prompt(
            style=context.style,
            shot=context.shot,
            anchors=context.anchors,
            continuity=context.continuity,
            reference_plan=context.reference_plan,
            capabilities=context.capabilities,
            compiler_version=context.compiler_version,
            _context=context,
        )

    diagnostics = [
        *context.diagnostics,
        *(video_bundle.diagnostics if video_bundle is not None else []),
        *(
            _validate_bundle_reference_text(image_bundle, context.reference_plan)
            if image_bundle is not None
            else []
        ),
        *(
            _validate_bundle_reference_text(video_bundle, context.reference_plan)
            if video_bundle is not None
            else []
        ),
    ]
    diagnostics = _dedupe_diagnostics(diagnostics)
    # 编译出口接线：bundle diagnostics 的 code 必须是已注册诊断代码，
    # 防止未登记的新 code 静默进入下游门禁体系。
    unknown_codes = unknown_prompt_diagnostic_codes(diagnostics)
    if unknown_codes:
        raise PromptCompileError(f"编译产生了未注册的诊断代码：{unknown_codes}")
    gate = diagnostics_status(diagnostics)
    ordered_diagnostics = gate.diagnostics

    normalized_override = _normalize_override(overrides)
    generated: dict[str, Any] = {}
    if image_bundle is not None:
        generated["image"] = image_bundle
    if video_bundle is not None:
        generated["video"] = video_bundle
    if normalized_override.cleared:
        final: Any = copy.deepcopy(generated)
        override_status = "cleared"
    else:
        final = apply_human_override(generated, normalized_override)
        override_status = "active"

    stable_reference = (
        stable_reference_plan_payload(context.reference_plan)
        if context.reference_plan is not None
        else None
    )
    provenance = {
        "compiler_version": context.compiler_version,
        "style_version": context.style.style_version,
        "asset_versions": _asset_versions(context.anchors),
        "provider_capabilities": context.capabilities.model_dump(mode="json"),
        "effective_reference_plan": stable_reference,
        "reference_plan_hash": context.reference_plan_hash,
        "diagnostics": [item.model_dump(mode="json") for item in ordered_diagnostics],
        "human_override": normalized_override.model_dump(mode="json"),
        "requested_duration_sec": context.duration.requested_duration_sec,
        "generation_duration_sec": context.duration.effective_duration_sec,
    }
    bundle = CompiledPromptBundle(
        compiler_version=context.compiler_version,
        status=cast("Literal['ready', 'warning', 'blocked']", gate.status),
        image=image_bundle,
        video=video_bundle,
        diagnostics=ordered_diagnostics,
        provenance=provenance,
        generated=generated,
        human_override=normalized_override,
        final=final,
        override_status=override_status,
    )
    # model_validator 已在构造时基于同一份 provenance 计算确定性 hash，
    # 这里直接复用已算值，避免对同一输入重复计算两遍。
    base_compiler_hash = bundle.base_compiler_hash
    input_hash = bundle.input_hash
    provenance.update(
        {
            "input_hash": input_hash,
            "base_compiler_hash": base_compiler_hash,
        }
    )
    return bundle.model_copy(
        deep=True,
        update={
            "input_hash": input_hash,
            "base_compiler_hash": base_compiler_hash,
            "provenance": provenance,
        },
    )


__all__ = [
    "COMPILER_VERSION",
    "PromptCompileError",
    "calculate_prompt_bundle_hashes",
    "compile_image_prompt",
    "compile_prompt_bundle",
    "compile_video_prompt",
]
