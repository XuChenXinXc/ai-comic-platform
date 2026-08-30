"""5a/5b 的纯 Prompt stage 层。

本模块只把已确认的标准分镜投影为 Prompt IR 和媒体计划，不访问数据库、文件系统或
Provider。图片和视频阶段分别只暴露自己的 channel，避免把图片静态关键帧和视频动作
语义混在同一个可入队产物中。
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal, cast

from pydantic import ConfigDict, Field, ValidationError, model_validator

from app.dynamic_pipeline.diagnostics import diagnostic, diagnostics_status
from app.dynamic_pipeline.media_bridge import (
    DynamicMediaBridgePlan,
    build_keyframe_media_plan,
    build_video_media_plan,
)
from app.dynamic_pipeline.prompt_compiler import COMPILER_VERSION, compile_prompt_bundle
from app.dynamic_pipeline.prompt_ir import (
    AssetVisualAnchor,
    CompiledPromptBundle,
    ContinuityContext,
    HumanOverrideSet,
    ReferencePlan,
    ShotIntent,
    VisualStyleSpec,
    calculate_prompt_bundle_hashes,
    prompt_input_hash,
)
from app.dynamic_pipeline.provider_capabilities import normalize_capabilities
from app.dynamic_pipeline.reference_plan import (
    project_reference_plan,
    stable_reference_plan_payload,
    validate_reference_text,
)
from app.dynamic_pipeline.schemas import ArtifactMeta, Diagnostic, PipelineModel, ReferenceSlot
from app.dynamic_pipeline.storyboard_stage import StoryboardOutput, StoryboardShot

PromptStageKey = Literal["5a-image-prompts", "5b-video-prompts"]


class PromptStageError(ValueError):
    """Prompt stage 输入或输出不满足严格合同。"""


class PromptStageOutput(PipelineModel):
    """5a/5b 的可恢复纯数据输出。"""

    model_config = ConfigDict(extra="forbid")

    stage_key: PromptStageKey
    artifact_meta: ArtifactMeta
    bundles: dict[str, CompiledPromptBundle] = Field(default_factory=dict)
    target_duration_sec: float = Field(gt=0)
    status: Literal["ready", "warning", "blocked"] = "ready"
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_stage_bundles(self) -> PromptStageOutput:
        for key, bundle in self.bundles.items():
            if not key.strip():
                raise ValueError("bundles 的 key 不能为空")
            if self.stage_key == "5a-image-prompts" and bundle.image is None:
                raise ValueError("5a bundle 必须包含 image")
            if self.stage_key == "5b-video-prompts" and bundle.video is None:
                raise ValueError("5b bundle 必须包含 video")
        return self


def _copy(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return copy.deepcopy(value.model_dump(mode="python"))
    return copy.deepcopy(value)


def _json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    return copy.deepcopy(value)


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    return ""


def _required_text(value: Any, name: str) -> str:
    text = _text(value)
    if not text:
        raise PromptStageError(f"{name} 不能为空")
    return text


def _as_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="python")
        if isinstance(dumped, Mapping):
            return dumped
    raise PromptStageError(f"{name} 必须是 Mapping 或 Pydantic 模型")


def _diagnostic(
    level: Literal["warning", "blocking"],
    code: str,
    message: str,
    *,
    path: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> Diagnostic:
    return diagnostic(level, code, message, path=path, details=details)


def _dedupe_diagnostics(values: Sequence[Diagnostic]) -> list[Diagnostic]:
    result: list[Diagnostic] = []
    seen: set[str] = set()
    for item in values:
        key = json.dumps(item.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _storyboard_content(storyboard: StoryboardOutput | Mapping[str, Any]) -> StoryboardOutput:
    try:
        return (
            storyboard
            if isinstance(storyboard, StoryboardOutput)
            else StoryboardOutput.model_validate(_copy(storyboard))
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise PromptStageError("storyboard 不符合 StoryboardOutput 合同") from exc


def _map_lookup(mapping: Mapping[Any, Any], key: str) -> Any:
    """只按显式 external_key 读取，不按名称、编号或相邻项猜测。"""

    if key in mapping:
        return mapping[key]
    return None


def _explicit_frame_path(value: Any) -> str:
    if isinstance(value, Mapping):
        for name in (
            "previous_end_frame_path",
            "end_frame_path",
            "relative_path",
            "local_file",
            "path",
            "provider_url",
            "url",
        ):
            text = _text(value.get(name))
            if text:
                return text
        return ""
    return _text(value)


def _asset_ids(shot: StoryboardShot) -> list[str]:
    values = [
        *shot.character_asset_ids,
        *shot.prop_asset_ids,
        shot.scene_asset_id or "",
    ]
    result: list[str] = []
    for value in values:
        text = _text(value)
        if text and text not in result:
            result.append(text)
    return result


def _anchors_for_shot(
    shot: StoryboardShot,
    anchors_by_asset_id: Mapping[str, AssetVisualAnchor | Mapping[str, Any]],
    *,
    path: str,
) -> tuple[list[AssetVisualAnchor | Mapping[str, Any]], list[Diagnostic]]:
    anchors: list[AssetVisualAnchor | Mapping[str, Any]] = []
    diagnostics: list[Diagnostic] = []
    if shot.scene_name and not shot.scene_asset_id:
        diagnostics.append(
            _diagnostic(
                "blocking",
                "asset_unresolved",
                "场景名称存在但缺少显式 scene_asset_id，不能按名称查找 anchor",
                path=f"{path}.scene_asset_id",
                details={"scene_name": shot.scene_name, "reason": "asset_id_missing"},
            )
        )
    for label, names, asset_ids in (
        ("character", shot.character_names, shot.character_asset_ids),
        ("prop", shot.prop_names, shot.prop_asset_ids),
    ):
        if len(asset_ids) < len(names):
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "asset_unresolved",
                    f"{label} 名称存在但缺少显式 asset_id，不能按名称查找 anchor",
                    path=f"{path}.{label}_asset_ids",
                    details={
                        "name_count": len(names),
                        "asset_id_count": len(asset_ids),
                        "reason": "asset_id_missing",
                    },
                )
            )
    for asset_id in _asset_ids(shot):
        value = _map_lookup(anchors_by_asset_id, asset_id)
        if value is None:
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "asset_unresolved",
                    f"镜头缺少 asset_id={asset_id} 的 visual anchor",
                    path=f"{path}.anchors_by_asset_id[{asset_id!r}]",
                    details={"asset_id": asset_id, "reason": "mapping_missing"},
                )
            )
            continue
        try:
            anchor = (
                value
                if isinstance(value, AssetVisualAnchor)
                else AssetVisualAnchor.model_validate(_copy(value))
            )
        except (TypeError, ValueError, ValidationError):
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "asset_unresolved",
                    f"asset_id={asset_id} 的 visual anchor 无效",
                    path=f"{path}.anchors_by_asset_id[{asset_id!r}]",
                    details={"asset_id": asset_id, "reason": "invalid_anchor"},
                )
            )
            continue
        if _text(anchor.asset_id) != asset_id:
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "asset_unresolved",
                    f"visual anchor 的 asset_id 与显式绑定不一致：{asset_id}",
                    path=f"{path}.anchors_by_asset_id[{asset_id!r}].asset_id",
                    details={"expected_asset_id": asset_id, "actual_asset_id": anchor.asset_id},
                )
            )
            continue
        anchors.append(anchor)
    return anchors, diagnostics


def _dialogue_cues(shot: StoryboardShot) -> list[dict[str, Any]]:
    cues: list[dict[str, Any]] = []
    for index, value in enumerate(shot.dialogue, start=1):
        payload = _copy(value)
        if isinstance(payload, str):
            payload = {"text": payload}
        if not isinstance(payload, Mapping):
            payload = {"text": _text(payload)}
        text = _text(
            payload.get("text")
            or payload.get("content")
            or payload.get("line")
            or payload.get("speech")
        )
        start = next(
            (
                payload.get(name)
                for name in ("start_time", "start_sec", "start")
                if payload.get(name) is not None
            ),
            0,
        )
        end = next(
            (
                payload.get(name)
                for name in ("end_time", "end_sec", "end")
                if payload.get(name) is not None
            ),
            shot.duration_sec,
        )
        cues.append(
            {
                "dialogue_line_id": _text(
                    payload.get("dialogue_line_id")
                    or payload.get("line_id")
                    or payload.get("id")
                    or f"{shot.external_key}-dialogue-{index}"
                ),
                "speaker": _text(
                    payload.get("speaker")
                    or payload.get("character")
                    or payload.get("character_name")
                ),
                "text": text,
                "start_time": start,
                "end_time": end,
            }
        )
    return cues


def _sound_cues(shot: StoryboardShot) -> list[dict[str, Any]]:
    cues: list[dict[str, Any]] = []
    for index, value in enumerate(shot.sound_cues, start=1):
        payload = _copy(value)
        if isinstance(payload, str):
            payload = {"description": payload}
        if not isinstance(payload, Mapping):
            payload = {"description": _text(payload)}
        cues.append(
            {
                "sound_id": _text(payload.get("sound_id") or payload.get("id"))
                or f"{shot.external_key}-sound-{index}",
                "kind": _text(payload.get("kind") or payload.get("type")) or "sound",
                "description": _text(
                    payload.get("description") or payload.get("text") or payload.get("value")
                ),
                "start_time": next(
                    (
                        payload.get(name)
                        for name in ("start_time", "start_sec", "start")
                        if payload.get(name) is not None
                    ),
                    0,
                ),
                "end_time": next(
                    (
                        payload.get(name)
                        for name in ("end_time", "end_sec", "end")
                        if payload.get(name) is not None
                    ),
                    shot.duration_sec,
                ),
                "volume": payload.get("volume", 1.0),
            }
        )
    return cues


def _action_events(shot: StoryboardShot) -> list[Any]:
    raw = shot.prompt_intent.get("action_events")
    if raw is None:
        raw = shot.prompt_intent.get("actions", shot.prompt_intent.get("timeline"))
    if raw is None:
        return []
    if isinstance(raw, list):
        return _copy(raw)
    return [_copy(raw)]


def _shot_intent(shot: StoryboardShot, *, image: bool) -> ShotIntent:
    subject: dict[str, Any] = {
        "characters": list(shot.character_names),
        "character_asset_ids": list(shot.character_asset_ids),
        "props": list(shot.prop_names),
        "prop_asset_ids": list(shot.prop_asset_ids),
        "positions": _copy(shot.positions),
        "expressions": _copy(shot.expressions),
    }
    subject = {key: value for key, value in subject.items() if value not in ([], {}, "")}
    scene: dict[str, Any] = {
        "scene_id": shot.scene_id,
        "scene_name": shot.scene_name,
        "time_weather": shot.time_weather,
        "scene_asset_id": shot.scene_asset_id,
        "frame_description": shot.frame_description,
    }
    scene = {key: value for key, value in scene.items() if value not in (None, "")}
    camera: dict[str, Any] = {
        "shot_type": shot.shot_type,
        "angle": shot.camera_angle,
        "move": shot.camera_move,
    }
    camera = {key: value for key, value in camera.items() if value}
    if not camera:
        camera = {"move": "固定机位"}
    return ShotIntent(
        shot_id=shot.external_key,
        external_key=shot.external_key,
        keyframe_time_sec=0,
        subject=subject or None,
        action=None if image else shot.action,
        scene=scene or None,
        camera=camera,
        lighting=shot.time_weather or None,
        audio=None
        if image
        else {"narration": shot.narration, "sound_cues": _copy(shot.sound_cues)},
        start_state=shot.start_state or None,
        end_state=None if image else (shot.end_state or None),
        next_hook=None if image else (shot.next_hook or None),
        transition_from_prev="scene_change" if image else shot.transition_from_prev,
        duration_sec=shot.duration_sec,
        action_events=[] if image else _action_events(shot),
        dialogue_cues=[] if image else _dialogue_cues(shot),
        sound_cues=[] if image else _sound_cues(shot),
        continuity_state={} if image else _copy(shot.continuity_state),
        is_continuous=not image and shot.transition_from_prev == "continuous",
    )


def _override(value: Any) -> HumanOverrideSet | Mapping[str, Any] | None:
    if value is None:
        return None
    return (
        value if isinstance(value, Mapping) or isinstance(value, HumanOverrideSet) else _copy(value)
    )


def prompt_stage_override_hash(value: Any) -> str:
    """计算外层 Prompt stage 使用的人工覆盖 hash。"""

    return prompt_input_hash(
        "dynamic-prompt-stage-human-overrides-v1",
        _json_value(value) if value not in (None, {}) else {},
    )


def _reference_plan(
    references_by_external_key: Mapping[str, ReferencePlan | Mapping[str, Any]],
    external_key: str,
    *,
    path: str,
) -> tuple[ReferencePlan | Mapping[str, Any] | None, list[Diagnostic]]:
    value = _map_lookup(references_by_external_key, external_key)
    if value is None:
        return None, []
    try:
        raw = _copy(value)
        if not isinstance(raw, Mapping):
            raise TypeError("reference plan 必须是 Mapping")
        if not isinstance(value, ReferencePlan):
            ReferencePlan.model_validate(
                {
                    name: raw.get(name, default)
                    for name, default in (
                        ("slots", []),
                        ("actual_local_files", []),
                        ("actual_urls", []),
                        ("text_mapping", {}),
                        ("snapshot_hash", ""),
                    )
                }
            )
    except (TypeError, ValueError, ValidationError):
        return (
            None,
            [
                _diagnostic(
                    "blocking",
                    "reference_wrong_type",
                    "镜头的显式 reference plan 无法归一化",
                    path=f"{path}.references_by_external_key[{external_key!r}]",
                    details={"external_key": external_key, "reason": "invalid_reference_plan"},
                )
            ],
        )
    # 保留 ReferencePlanResult 的 status/diagnostics 等快照字段，供 media_bridge
    # 复用同一份计划；这里不根据名称、资产或槽位重新排布。
    plan_diagnostics: list[Diagnostic] = []
    raw_status = _text(raw.get("status")) if isinstance(raw, Mapping) else ""
    raw_diagnostics = raw.get("diagnostics", []) if isinstance(raw, Mapping) else []
    if isinstance(raw_diagnostics, Sequence) and not isinstance(raw_diagnostics, str | bytes):
        for item in raw_diagnostics:
            try:
                diagnostic_value = _copy(item)
                if isinstance(diagnostic_value, Mapping):
                    diagnostic_value = {
                        name: diagnostic_value.get(name)
                        for name in ("level", "code", "message", "path", "details")
                    }
                plan_diagnostics.append(
                    item
                    if isinstance(item, Diagnostic)
                    else Diagnostic.model_validate(diagnostic_value)
                )
            except (TypeError, ValueError, ValidationError):
                plan_diagnostics.append(
                    _diagnostic(
                        "blocking",
                        "reference_wrong_type",
                        "reference plan 包含无效诊断",
                        path=f"{path}.diagnostics",
                    )
                )
                break
    if raw_status == "blocked" and not any(item.level == "blocking" for item in plan_diagnostics):
        plan_diagnostics.append(
            _diagnostic(
                "blocking",
                "reference_plan_blocked",
                "ReferencePlan 已被阻断，不能安全编译媒体 Prompt",
                path=f"{path}.reference_plan",
            )
        )
    elif raw_status == "warning" and not plan_diagnostics:
        plan_diagnostics.append(
            _diagnostic(
                "warning",
                "reference_plan_warning",
                "ReferencePlan 带有警告",
                path=f"{path}.reference_plan",
            )
        )
    elif raw_status not in {"", "ready", "warning", "blocked"}:
        plan_diagnostics.append(
            _diagnostic(
                "blocking",
                "reference_wrong_type",
                f"ReferencePlan status 无效：{raw_status}",
                path=f"{path}.status",
                details={"status": raw_status},
            )
        )
    safe_raw = dict(raw)
    if plan_diagnostics:
        safe_raw["diagnostics"] = [
            item.model_dump(
                mode="json",
                include={"level", "code", "message", "path", "details"},
            )
            for item in plan_diagnostics
        ]
    return safe_raw, plan_diagnostics


def _effective_reference_plan(
    reference_plan: ReferencePlan | Mapping[str, Any] | None,
    channel: Any,
) -> ReferencePlan | dict[str, Any] | None:
    """按编译后 channel 的槽位裁剪计划，保持原 index 和映射，不重新编号。"""

    if reference_plan is None:
        return None
    raw = dict(_as_mapping(reference_plan, "reference_plan"))
    raw_slots = raw.get("slots", [])
    channel_slots = getattr(channel, "reference_slots", [])
    selected_slots = (
        [_json_value(slot) for slot in channel_slots] if channel_slots is not None else []
    )
    if not selected_slots and isinstance(raw_slots, list) and not raw_slots:
        return raw
    return project_reference_plan(raw, selected_slots)


def _reference_slot_override_diagnostics(
    reference_plan: ReferencePlan | Mapping[str, Any] | None,
    override: HumanOverrideSet | Mapping[str, Any] | None,
    *,
    path: str,
) -> list[Diagnostic]:
    if override is None:
        return []
    override_payload = _as_mapping(override, "human_override")
    raw_slots = override_payload.get("reference_slots")
    if raw_slots is None:
        return []
    override_slots = raw_slots if isinstance(raw_slots, list) else []
    plan_slots = []
    if reference_plan is not None:
        plan_slots = list(_as_mapping(reference_plan, "reference_plan").get("slots", []))

    def slot_payload(value: Any) -> dict[str, Any]:
        if isinstance(value, Mapping):
            payload = dict(value)
        elif hasattr(value, "model_dump"):
            dumped = value.model_dump(mode="python")
            payload = dict(dumped) if isinstance(dumped, Mapping) else {}
        else:
            return {}
        try:
            return ReferenceSlot.model_validate(payload).model_dump(mode="python")
        except (TypeError, ValueError, ValidationError):
            return payload

    canonical_payloads = [slot_payload(slot) for slot in plan_slots]
    override_payloads = [slot_payload(slot) for slot in override_slots]
    canonical_indexes = [slot.get("index") for slot in canonical_payloads]
    override_indexes = [slot.get("index") for slot in override_payloads]
    if override_indexes != canonical_indexes:
        return [
            _diagnostic(
                "blocking",
                "reference_slot_override_index_mismatch",
                "人工参考槽位覆盖不能改变 canonical index 或顺序",
                path=f"{path}.reference_slots",
                details={
                    "canonical_indexes": canonical_indexes,
                    "override_indexes": override_indexes,
                },
            )
        ]

    def source_semantics(slot: Mapping[str, Any]) -> tuple[str, str]:
        relative_path = _text(slot.get("relative_path") or slot.get("file_path"))
        provider_url = _text(slot.get("provider_url") or slot.get("url"))
        return relative_path.replace("\\", "/"), provider_url

    semantic_fields = (
        "index",
        "role",
        "purpose",
        "asset_id",
        "image_version_id",
        "keyframe_id",
        "required",
        "provider_url_status",
    )
    mismatches: list[dict[str, Any]] = []
    for index, (canonical, override_value) in enumerate(
        zip(canonical_payloads, override_payloads, strict=False)
    ):
        canonical_semantics = {
            key: canonical.get(key) for key in semantic_fields
        }
        override_semantics = {key: override_value.get(key) for key in semantic_fields}
        canonical_semantics["source"] = source_semantics(canonical)
        override_semantics["source"] = source_semantics(override_value)
        if canonical_semantics != override_semantics:
            mismatches.append(
                {
                    "position": index,
                    "canonical": canonical_semantics,
                    "override": override_semantics,
                }
            )
    if mismatches:
        return [
            _diagnostic(
                "blocking",
                "reference_slot_override_semantic_mismatch",
                "人工参考槽位覆盖必须保持 canonical slot 的用途、资产和来源语义",
                path=f"{path}.reference_slots",
                details={"mismatches": mismatches},
            )
        ]
    return []


def _bundle_with_channel(
    bundle: CompiledPromptBundle,
    *,
    stage_key: PromptStageKey,
    shot: StoryboardShot,
    reference_plan: ReferencePlan | Mapping[str, Any] | None,
    continuity: ContinuityContext,
    extra_diagnostics: Sequence[Diagnostic] = (),
) -> CompiledPromptBundle:
    diagnostics = _dedupe_diagnostics([*bundle.diagnostics, *extra_diagnostics])
    if stage_key == "5a-image-prompts":
        diagnostics = [
            item
            for item in diagnostics
            if item.code
            not in {
                "provider_duration_unsupported",
                "video_action_missing",
                "video_end_state_missing",
                "continuous_previous_end_frame_missing",
                "continuous_previous_reference_missing",
            }
        ]
    gate = diagnostics_status(diagnostics)
    provenance = _copy(bundle.provenance)
    if stage_key == "5a-image-prompts":
        provenance.pop("requested_duration_sec", None)
        provenance.pop("generation_duration_sec", None)
    provenance.update(
        {
            "stage_key": stage_key,
            "shot_id": shot.external_key,
            "external_key": shot.external_key,
            "shot_no": shot.shot_no,
            "ordinal": shot.ordinal,
            "reference_plan": _copy(reference_plan),
            "reference_plan_hash": (
                _text(_as_mapping(reference_plan, "reference_plan").get("snapshot_hash"))
                if reference_plan is not None
                else ""
            ),
            "continuity": continuity.model_dump(mode="json"),
            "diagnostics": [item.model_dump(mode="json") for item in gate.diagnostics],
        }
    )
    # Stage 5a/5b 保存单 channel；另一个 channel 只在编译器内部存在。
    channel_bundle = bundle.model_copy(
        deep=True,
        update={
            "status": cast("Literal['ready', 'warning', 'blocked']", gate.status),
            "diagnostics": list(gate.diagnostics),
            "provenance": provenance,
            "video": None if stage_key == "5a-image-prompts" else bundle.video,
            "image": None if stage_key == "5b-video-prompts" else bundle.image,
            "generated": (
                {"image": bundle.image}
                if stage_key == "5a-image-prompts"
                else {"video": bundle.video}
            ),
            "final": (
                {"image": bundle.final.get("image", bundle.image)}
                if stage_key == "5a-image-prompts" and isinstance(bundle.final, Mapping)
                else {"video": bundle.final.get("video", bundle.video)}
                if stage_key == "5b-video-prompts" and isinstance(bundle.final, Mapping)
                else {"image": bundle.image}
                if stage_key == "5a-image-prompts"
                else {"video": bundle.video}
            ),
        },
    )
    final_channel = (
        channel_bundle.image if stage_key == "5a-image-prompts" else channel_bundle.video
    )
    if isinstance(channel_bundle.final, Mapping):
        final_channel = channel_bundle.final.get(
            "image" if stage_key == "5a-image-prompts" else "video",
            final_channel,
        )
    effective_reference_plan = _effective_reference_plan(reference_plan, final_channel)
    provenance["reference_plan"] = effective_reference_plan
    alignment_diagnostics: list[Diagnostic] = []
    if reference_plan is not None:
        alignment_diagnostics.extend(
            _reference_slot_override_diagnostics(
                reference_plan,
                channel_bundle.human_override,
                path="bundle",
            )
        )
    if effective_reference_plan is not None:
        provenance["reference_plan_hash"] = _text(
            _as_mapping(effective_reference_plan, "reference_plan").get("snapshot_hash")
        ) or prompt_input_hash(effective_reference_plan)
        alignment_diagnostics.extend(validate_reference_text("", effective_reference_plan))
    if alignment_diagnostics:
        diagnostics = _dedupe_diagnostics([*diagnostics, *alignment_diagnostics])
        gate = diagnostics_status(diagnostics)
        channel_bundle = channel_bundle.model_copy(
            deep=True,
            update={
                "status": cast("Literal['ready', 'warning', 'blocked']", gate.status),
                "diagnostics": list(gate.diagnostics),
            },
        )
        provenance["diagnostics"] = [item.model_dump(mode="json") for item in gate.diagnostics]
    provenance["human_override_hash"] = prompt_stage_override_hash(
        channel_bundle.human_override
        if channel_bundle.human_override is not None
        else {"cleared": True}
    )
    channel_bundle = channel_bundle.model_copy(
        deep=True,
        update={"provenance": provenance},
    )
    stable_reference = (
        stable_reference_plan_payload(effective_reference_plan)
        if effective_reference_plan is not None
        else None
    )
    provenance["effective_reference_plan"] = stable_reference
    base_hash, input_hash = calculate_prompt_bundle_hashes(
        channel_bundle,
        asset_versions=provenance.get("asset_versions", []),
        provider_capabilities=provenance.get("provider_capabilities", {}),
        effective_reference_plan=stable_reference,
    )
    provenance["base_compiler_hash"] = base_hash
    provenance["input_hash"] = input_hash
    provenance.pop("compiler_provenance", None)
    channel_bundle = channel_bundle.model_copy(
        deep=True,
        update={
            "base_compiler_hash": base_hash,
            "input_hash": input_hash,
            "provenance": provenance,
        },
    )
    return channel_bundle


def _compile_stage(
    storyboard: StoryboardOutput | Mapping[str, Any],
    *,
    stage_key: PromptStageKey,
    style: VisualStyleSpec | Mapping[str, Any],
    anchors_by_asset_id: Mapping[str, AssetVisualAnchor | Mapping[str, Any]],
    references_by_external_key: Mapping[str, ReferencePlan | Mapping[str, Any]],
    capabilities: Mapping[str, Any] | Any,
    workflow_run_id: str,
    workflow_stage_id: str,
    input_signature: str,
    provider_id: str,
    provider_model: str,
    overrides_by_external_key: Mapping[str, HumanOverrideSet | Mapping[str, Any]] | None,
    previous_end_frames: Mapping[str, Any] | None = None,
    previous_references_by_external_key: Mapping[str, Any] | None = None,
    previous_reference_info_by_external_key: Mapping[str, Any] | None = None,
) -> PromptStageOutput:
    content = _storyboard_content(storyboard)
    workflow_run_id = _required_text(workflow_run_id, "workflow_run_id")
    workflow_stage_id = _required_text(workflow_stage_id, "workflow_stage_id")
    input_signature = _required_text(input_signature, "input_signature")
    provider_id = _required_text(provider_id, "provider_id")
    provider_model = _required_text(provider_model, "provider_model")
    if not isinstance(anchors_by_asset_id, Mapping):
        raise PromptStageError("anchors_by_asset_id 必须是 Mapping")
    if not isinstance(references_by_external_key, Mapping):
        raise PromptStageError("references_by_external_key 必须是 Mapping")
    if overrides_by_external_key is not None and not isinstance(overrides_by_external_key, Mapping):
        raise PromptStageError("overrides_by_external_key 必须是 Mapping")
    previous_end_frames = previous_end_frames or {}
    previous_references_by_external_key = (
        previous_references_by_external_key or previous_reference_info_by_external_key or {}
    )

    try:
        normalized_caps = normalize_capabilities(_copy(capabilities)).model_copy(
            update={"provider_id": provider_id, "model": provider_model}
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise PromptStageError("Provider capabilities 无法归一化") from exc
    shots = sorted(content.shots, key=lambda shot: (shot.ordinal, shot.shot_no, shot.external_key))
    bundles: dict[str, CompiledPromptBundle] = {}
    all_diagnostics: list[Diagnostic] = []
    shot_keys: list[str] = []
    reference_hashes: dict[str, str] = {}
    override_hash = prompt_stage_override_hash(overrides_by_external_key)
    if not shots:
        all_diagnostics.append(
            _diagnostic(
                "blocking", "prompt_stage_no_shots", "Prompt stage 至少需要一个镜头", path="shots"
            )
        )
    duplicate_keys = sorted(
        key
        for key in {shot.external_key for shot in shots}
        if sum(item.external_key == key for item in shots) > 1
    )
    if duplicate_keys:
        all_diagnostics.append(
            _diagnostic(
                "blocking",
                "prompt_stage_duplicate_external_key",
                "Prompt stage 存在重复 external_key，不能安全建立 bundle 映射",
                path="shots",
                details={"external_keys": duplicate_keys},
            )
        )

    for index, shot in enumerate(shots):
        key = shot.external_key
        shot_keys.append(key)
        path = f"shots[{index}]"
        anchors, anchor_diagnostics = _anchors_for_shot(shot, anchors_by_asset_id, path=path)
        reference_plan, reference_diagnostics = _reference_plan(
            references_by_external_key, key, path=path
        )
        reference_hashes[key] = ""
        if reference_plan is not None:
            reference_hashes[key] = _text(
                _as_mapping(reference_plan, "reference_plan").get("snapshot_hash")
            ) or prompt_input_hash(reference_plan)

        continuity = ContinuityContext(
            transition_type=(
                "scene_change" if stage_key == "5a-image-prompts" else shot.transition_from_prev
            )
        )
        continuity_diagnostics: list[Diagnostic] = []
        if stage_key == "5b-video-prompts" and shot.transition_from_prev == "continuous":
            previous_frame = _map_lookup(previous_end_frames, key)
            previous_reference = _map_lookup(previous_references_by_external_key, key)
            if previous_reference is None and reference_plan is not None:
                reference_mapping = _as_mapping(reference_plan, "reference_plan")
                slots = reference_mapping.get("slots", [])
                if isinstance(slots, Sequence) and not isinstance(slots, str | bytes):
                    if any(
                        isinstance(slot, Mapping)
                        and (
                            _text(slot.get("purpose")) == "previous_end_frame"
                            or "previous" in _text(slot.get("role")).casefold()
                        )
                        for slot in slots
                    ):
                        previous_reference = {"reference_plan": _copy(reference_plan)}
            if isinstance(previous_frame, Mapping):
                previous_frame = previous_frame.get(
                    "previous_end_frame_path",
                    previous_frame.get("end_frame_path", previous_frame.get("path")),
                )
            previous_frame_text = _text(previous_frame)
            previous_shot_id = ""
            previous_end_state: Any = None
            previous_reference_mapping: Mapping[str, Any] = {}
            previous_reference_invalid = False
            if previous_reference is not None:
                try:
                    previous_reference_mapping = _as_mapping(
                        previous_reference, "previous_reference"
                    )
                except PromptStageError:
                    previous_reference_invalid = True
                    continuity_diagnostics.append(
                        _diagnostic(
                            "blocking",
                            "continuous_previous_reference_missing",
                            "continuous 镜头的上一镜引用信息不是有效映射",
                            path=f"{path}.continuity.previous_reference",
                            details={"external_key": key, "reason": "invalid_mapping"},
                        )
                    )
            if previous_reference_mapping:
                previous_shot_id = _text(
                    previous_reference_mapping.get("previous_shot_id")
                    or previous_reference_mapping.get("shot_id")
                )
                previous_end_state = _copy(
                    previous_reference_mapping.get("previous_end_state")
                    or previous_reference_mapping.get("end_state")
                )
            continuity = ContinuityContext(
                previous_shot_id=previous_shot_id or None,
                previous_end_frame_path=previous_frame_text or None,
                previous_end_state=previous_end_state,
                transition_type="continuous",
                axis=_copy(previous_reference_mapping.get("axis")),
                positioning=_copy(previous_reference_mapping.get("positioning")),
                props=_copy(previous_reference_mapping.get("props")),
                previous_continuity_state=_copy(
                    previous_reference_mapping.get("previous_continuity_state") or {}
                ),
            )
            if not previous_frame_text:
                continuity_diagnostics.append(
                    _diagnostic(
                        "blocking",
                        "continuous_previous_end_frame_missing",
                        "continuous 镜头缺少上一镜末帧路径",
                        path=f"{path}.continuity.previous_end_frame_path",
                        details={"external_key": key, "reason": "explicit_mapping_missing"},
                    )
                )
            if not previous_reference_mapping and not previous_reference_invalid:
                continuity_diagnostics.append(
                    _diagnostic(
                        "blocking",
                        "continuous_previous_reference_missing",
                        "continuous 镜头缺少显式上一镜引用信息",
                        path=f"{path}.continuity.previous_reference",
                        details={"external_key": key, "reason": "explicit_mapping_missing"},
                    )
                )

        is_image = stage_key == "5a-image-prompts"
        intent = _shot_intent(shot, image=is_image)
        compiler_intent = intent
        if is_image:
            # 当前 compiler 同时生成 image/video 两个内部 channel，并且要求视频
            # channel 有 action/end_state。这里使用稳定的内部占位值；随后只保留
            # image channel，故占位值不会进入图片 Prompt、semantic_plan 或 stage JSON。
            compiler_intent = intent.model_copy(
                update={
                    "action": "__image_stage_unused__",
                    "end_state": "__image_stage_unused__",
                }
            )
        if is_image and isinstance(_map_lookup(overrides_by_external_key or {}, key), Mapping):
            raw_override = _map_lookup(overrides_by_external_key or {}, key)
            assert isinstance(raw_override, Mapping)
            override = HumanOverrideSet(
                image_prompt=raw_override.get("image_prompt"),
                negative_prompt=raw_override.get("negative_prompt"),
                camera=_copy(raw_override.get("camera")),
                reference_slots=_copy(raw_override.get("reference_slots")),
                cleared=bool(raw_override.get("cleared", False)),
                override_version=_text(raw_override.get("override_version")) or "1",
            )
        elif is_image and isinstance(
            _map_lookup(overrides_by_external_key or {}, key), HumanOverrideSet
        ):
            raw_override = cast(HumanOverrideSet, _map_lookup(overrides_by_external_key or {}, key))
            override = HumanOverrideSet(
                image_prompt=raw_override.image_prompt,
                negative_prompt=raw_override.negative_prompt,
                camera=_copy(raw_override.camera),
                reference_slots=_copy(raw_override.reference_slots),
                cleared=raw_override.cleared,
                override_version=raw_override.override_version,
            )
        else:
            override = _override(
                _map_lookup(overrides_by_external_key or {}, key)
                if overrides_by_external_key is not None
                else None
            )
        try:
            override_diagnostics = _reference_slot_override_diagnostics(
                reference_plan,
                override,
                path=path,
            )
            compile_override = override
            if any(item.level == "blocking" for item in override_diagnostics):
                if isinstance(override, HumanOverrideSet):
                    compile_override = override.model_copy(update={"reference_slots": None})
                elif isinstance(override, Mapping):
                    compile_override = {
                        key: value
                        for key, value in override.items()
                        if key != "reference_slots"
                    }
            compiled = compile_prompt_bundle(
                style=_copy(style),
                shot=compiler_intent,
                anchors=_copy(anchors),
                continuity=continuity,
                reference_plan=_copy(reference_plan),
                capabilities=normalized_caps.model_copy(deep=True),
                overrides=compile_override,
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise PromptStageError(f"镜头 {key} Prompt 编译失败") from exc
        extra = [
            *anchor_diagnostics,
            *reference_diagnostics,
            *continuity_diagnostics,
            *override_diagnostics,
        ]
        bundle = _bundle_with_channel(
            compiled,
            stage_key=stage_key,
            shot=shot,
            reference_plan=reference_plan,
            continuity=continuity,
            extra_diagnostics=extra,
        )
        channel = bundle.image if is_image else bundle.video
        bundle_provenance = _copy(bundle.provenance)
        bundle_provenance.update(
            {
                "workflow_run_id": workflow_run_id,
                "workflow_stage_id": workflow_stage_id,
                "input_signature": input_signature,
                "upstream_input_signature": input_signature,
                "provider_id": provider_id,
                "provider_model": provider_model,
                "prompt_ir_hash": prompt_input_hash(channel) if channel is not None else "",
                "human_override_hash": override_hash,
            }
        )
        bundle = bundle.model_copy(deep=True, update={"provenance": bundle_provenance})
        bundles[key] = bundle
        all_diagnostics.extend(bundle.diagnostics)

    gate = diagnostics_status(_dedupe_diagnostics(all_diagnostics))
    stage_provenance = {
        "workflow_run_id": workflow_run_id,
        "workflow_stage_id": workflow_stage_id,
        "stage_key": stage_key,
        "input_signature": input_signature,
        "compiler_version": COMPILER_VERSION,
        "provider_id": provider_id,
        "provider_model": provider_model,
        "provider_capabilities": normalized_caps.model_dump(mode="json"),
        "shot_keys": shot_keys,
        "reference_plan_hashes": reference_hashes,
        "bundle_input_hashes": {key: bundle.input_hash for key, bundle in bundles.items()},
        "human_override_hash": override_hash,
        "diagnostics": [item.model_dump(mode="json") for item in gate.diagnostics],
    }
    artifact_meta = ArtifactMeta(
        schema_version=f"dynamic-{stage_key}-v1",
        normalizer_version=COMPILER_VERSION,
        input_signature=input_signature,
        content_signature=prompt_input_hash(stage_provenance, bundles),
        source_attempt=1,
        provider_id=provider_id,
        provider_model=provider_model,
    )
    return PromptStageOutput(
        stage_key=stage_key,
        artifact_meta=artifact_meta,
        bundles=bundles,
        target_duration_sec=content.target_duration_sec,
        status=cast("Literal['ready', 'warning', 'blocked']", gate.status),
        diagnostics=list(gate.diagnostics),
        provenance=stage_provenance,
    )


def compile_image_prompt_stage(
    storyboard: StoryboardOutput | Mapping[str, Any],
    *,
    style: VisualStyleSpec | Mapping[str, Any],
    anchors_by_asset_id: Mapping[str, AssetVisualAnchor | Mapping[str, Any]],
    references_by_external_key: Mapping[str, ReferencePlan | Mapping[str, Any]],
    capabilities: Mapping[str, Any] | Any,
    workflow_run_id: str,
    workflow_stage_id: str,
    input_signature: str,
    provider_id: str,
    provider_model: str,
    overrides_by_external_key: Mapping[str, HumanOverrideSet | Mapping[str, Any]] | None = None,
) -> PromptStageOutput:
    """编译 5a 图片静态关键帧。"""

    return _compile_stage(
        storyboard,
        stage_key="5a-image-prompts",
        style=style,
        anchors_by_asset_id=anchors_by_asset_id,
        references_by_external_key=references_by_external_key,
        capabilities=capabilities,
        workflow_run_id=workflow_run_id,
        workflow_stage_id=workflow_stage_id,
        input_signature=input_signature,
        provider_id=provider_id,
        provider_model=provider_model,
        overrides_by_external_key=overrides_by_external_key,
    )


def compile_video_prompt_stage(
    storyboard: StoryboardOutput | Mapping[str, Any],
    *,
    style: VisualStyleSpec | Mapping[str, Any],
    anchors_by_asset_id: Mapping[str, AssetVisualAnchor | Mapping[str, Any]],
    references_by_external_key: Mapping[str, ReferencePlan | Mapping[str, Any]],
    capabilities: Mapping[str, Any] | Any,
    workflow_run_id: str,
    workflow_stage_id: str,
    input_signature: str,
    provider_id: str,
    provider_model: str,
    overrides_by_external_key: Mapping[str, HumanOverrideSet | Mapping[str, Any]] | None = None,
    previous_end_frames: Mapping[str, Any] | None = None,
    previous_references_by_external_key: Mapping[str, Any] | None = None,
    previous_reference_info_by_external_key: Mapping[str, Any] | None = None,
) -> PromptStageOutput:
    """编译 5b 视频单动作、单运镜、结束状态和连续性。"""

    return _compile_stage(
        storyboard,
        stage_key="5b-video-prompts",
        style=style,
        anchors_by_asset_id=anchors_by_asset_id,
        references_by_external_key=references_by_external_key,
        capabilities=capabilities,
        workflow_run_id=workflow_run_id,
        workflow_stage_id=workflow_stage_id,
        input_signature=input_signature,
        provider_id=provider_id,
        provider_model=provider_model,
        overrides_by_external_key=overrides_by_external_key,
        previous_end_frames=previous_end_frames,
        previous_references_by_external_key=previous_references_by_external_key,
        previous_reference_info_by_external_key=previous_reference_info_by_external_key,
    )


def stage_output_to_media_plans(
    output: PromptStageOutput | Mapping[str, Any],
    *,
    provider_id: str,
    provider_model: str,
    keyframe_job_ids: Mapping[str, str] | None = None,
    previous_video_job_ids: Mapping[str, str] | None = None,
    previous_end_frames: Mapping[str, str] | None = None,
) -> DynamicMediaBridgePlan:
    """将单 channel stage output 转换成媒体计划，不为 blocked target 入队。"""

    try:
        stage = (
            output
            if isinstance(output, PromptStageOutput)
            else PromptStageOutput.model_validate(_copy(output))
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise PromptStageError("PromptStageOutput 无法归一化") from exc
    provider_id = _required_text(provider_id, "provider_id")
    provider_model = _required_text(provider_model, "provider_model")
    keyframe_job_ids = keyframe_job_ids or {}
    previous_video_job_ids = previous_video_job_ids or {}
    previous_end_frames = previous_end_frames or {}
    targets = []
    all_diagnostics = list(stage.diagnostics)
    previous_video_target = None
    for key in stage.provenance.get("shot_keys", list(stage.bundles)):
        bundle = stage.bundles.get(key)
        if bundle is None:
            continue
        metadata = bundle.provenance
        reference_plan = metadata.get("reference_plan")
        if stage.stage_key == "5a-image-prompts":
            target = build_keyframe_media_plan(
                bundle,
                workflow_run_id=stage.provenance["workflow_run_id"],
                workflow_stage_id=stage.provenance["workflow_stage_id"],
                shot_id=_text(metadata.get("shot_id")) or key,
                external_key=_text(metadata.get("external_key")) or key,
                input_signature=stage.provenance["input_signature"],
                provider_id=provider_id,
                provider_model=provider_model,
                reference_plan=reference_plan,
            )
        else:
            previous_video_job_id = _map_lookup(previous_video_job_ids, key)
            if previous_video_job_id is None and previous_video_target is not None:
                previous_video_job_id = previous_video_target.idempotency_key
            target = build_video_media_plan(
                bundle,
                workflow_run_id=stage.provenance["workflow_run_id"],
                workflow_stage_id=stage.provenance["workflow_stage_id"],
                shot_id=_text(metadata.get("shot_id")) or key,
                external_key=_text(metadata.get("external_key")) or key,
                input_signature=stage.provenance["input_signature"],
                provider_id=provider_id,
                provider_model=provider_model,
                reference_plan=reference_plan,
                keyframe_job_id=_map_lookup(keyframe_job_ids, key),
                previous_video_job_id=previous_video_job_id,
                previous_end_frame_path=(
                    _explicit_frame_path(_map_lookup(previous_end_frames, key))
                    or (
                        metadata.get("continuity", {}).get("previous_end_frame_path")
                        if isinstance(metadata.get("continuity"), Mapping)
                        else None
                    )
                ),
            )
        targets.append(target)
        all_diagnostics.extend(target.diagnostics)
        if target.kind == "video":
            previous_video_target = target
    gate = diagnostics_status(_dedupe_diagnostics(all_diagnostics))
    return DynamicMediaBridgePlan(
        workflow_run_id=stage.provenance["workflow_run_id"],
        stage_id=stage.provenance["workflow_stage_id"],
        input_signature=stage.provenance["input_signature"],
        provider_id=provider_id,
        model=provider_model,
        targets=targets,
        status=cast("Literal['ready', 'warning', 'blocked']", gate.status),
        diagnostics=list(gate.diagnostics),
    )


def stage_output_to_stage_json(output: PromptStageOutput | Mapping[str, Any]) -> dict[str, Any]:
    """返回可写入 workflow_stage draft/normalized 的 JSON，不执行持久化。"""

    try:
        stage = (
            output
            if isinstance(output, PromptStageOutput)
            else PromptStageOutput.model_validate(_copy(output))
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise PromptStageError("PromptStageOutput 无法归一化") from exc
    bundles = {key: bundle.model_dump(mode="json") for key, bundle in stage.bundles.items()}
    shot_map = {
        key: {
            "shot_id": _text(bundle.provenance.get("shot_id")) or key,
            "external_key": _text(bundle.provenance.get("external_key")) or key,
            "ordinal": bundle.provenance.get("ordinal"),
            "shot_no": bundle.provenance.get("shot_no"),
            "input_hash": bundle.input_hash,
            "status": bundle.status,
        }
        for key, bundle in stage.bundles.items()
    }
    payload = {
        "stage_key": stage.stage_key,
        "artifact_meta": stage.artifact_meta.model_dump(mode="json"),
        "target_duration_sec": stage.target_duration_sec,
        "status": stage.status,
        "bundles": bundles,
        "shot_map": shot_map,
        "provenance": _json_value(stage.provenance),
        "diagnostics": [item.model_dump(mode="json") for item in stage.diagnostics],
    }
    return cast(dict[str, Any], _json_value(payload))


__all__ = [
    "PromptStageError",
    "PromptStageOutput",
    "compile_image_prompt_stage",
    "compile_video_prompt_stage",
    "prompt_stage_override_hash",
    "stage_output_to_media_plans",
    "stage_output_to_stage_json",
]
