"""将 5a/5b Prompt Bundle 转换为纯媒体计划。

本模块只构造确定性的 Pydantic 计划和数据库 payload，不访问数据库、不调用
Provider，也不负责真正入队。动态工作流可以在事务边界内消费这些 payload，旧的
手工媒体路径不需要依赖本模块。
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.dynamic_pipeline.diagnostics import diagnostic, diagnostics_status
from app.dynamic_pipeline.prompt_ir import (
    CompiledPromptBundle,
    ImagePromptBundle,
    ReferencePlan,
    VideoPromptBundle,
    prompt_input_hash,
)
from app.dynamic_pipeline.reference_plan import validate_reference_text
from app.dynamic_pipeline.schemas import Diagnostic
from app.services.signature_service import keyframe_signature, video_signature

MEDIA_BRIDGE_VERSION = "dynamic-media-bridge-v1"


class MediaBridgeError(ValueError):
    """Prompt Bundle 不能安全转换为媒体计划时抛出的异常。"""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MediaReferenceInputs(_StrictModel):
    """ReferencePlan 实际保留下来的两组 Provider 输入。"""

    local: list[str] = Field(default_factory=list)
    url: list[str] = Field(default_factory=list)


class MediaTargetPlan(_StrictModel):
    """一个关键帧或视频目标的完整、可重放计划。"""

    workflow_run_id: str = Field(min_length=1)
    stage_id: str = Field(min_length=1)
    shot_id: str = Field(min_length=1)
    external_key: str = Field(min_length=1)
    kind: Literal["image", "video"]
    prompt_bundle: ImagePromptBundle | VideoPromptBundle
    prompt_ir_json: dict[str, Any] = Field(default_factory=dict)
    provenance_json: dict[str, Any] = Field(default_factory=dict)
    input_signature: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)
    provider_id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    requested_duration_sec: float | None = Field(default=None, gt=0)
    generation_duration_sec: float | None = Field(default=None, gt=0)
    reference_inputs: MediaReferenceInputs = Field(default_factory=MediaReferenceInputs)
    status: Literal["ready", "warning", "blocked"] = "ready"
    diagnostics: list[Diagnostic] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_channel_and_duration(self) -> MediaTargetPlan:
        if self.kind == "image":
            if not isinstance(self.prompt_bundle, ImagePromptBundle):
                raise ValueError("image media target 必须使用 ImagePromptBundle")
            if self.prompt_bundle.keyframe_time_sec != 0:
                raise ValueError("关键帧时间必须固定为 0")
            if self.requested_duration_sec is not None or self.generation_duration_sec is not None:
                raise ValueError("image media target 不应携带视频时长")
        else:
            if not isinstance(self.prompt_bundle, VideoPromptBundle):
                raise ValueError("video media target 必须使用 VideoPromptBundle")
            if self.requested_duration_sec is None or self.generation_duration_sec is None:
                raise ValueError("video media target 必须同时保存请求时长和生成时长")
        if any(not isinstance(item, str) or not item.strip() for item in self.depends_on):
            raise ValueError("depends_on 只能包含非空任务 ID")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("depends_on 不能包含重复任务 ID")
        return self

    @property
    def workflow_stage_id(self) -> str:
        """兼容调用方使用 workflow_stage_id 的命名。"""

        return self.stage_id

    @property
    def provider_model(self) -> str:
        return self.model

    @property
    def reference_inputs_local(self) -> list[str]:
        return list(self.reference_inputs.local)

    @property
    def reference_inputs_url(self) -> list[str]:
        return list(self.reference_inputs.url)

    @property
    def requested_duration(self) -> float | None:
        return self.requested_duration_sec

    @property
    def generation_duration(self) -> float | None:
        return self.generation_duration_sec


class DynamicMediaBridgePlan(_StrictModel):
    """一个工作流阶段的全部媒体目标和聚合门禁结果。"""

    workflow_run_id: str = Field(min_length=1)
    stage_id: str = Field(min_length=1)
    input_signature: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    targets: list[MediaTargetPlan] = Field(default_factory=list)
    status: Literal["ready", "warning", "blocked"] = "ready"
    diagnostics: list[Diagnostic] = Field(default_factory=list)

    @property
    def workflow_stage_id(self) -> str:
        return self.stage_id

    @property
    def provider_model(self) -> str:
        return self.model

    @property
    def media_targets(self) -> list[MediaTargetPlan]:
        return list(self.targets)


@dataclass(frozen=True)
class _BundleContext:
    prompt_bundle: ImagePromptBundle | VideoPromptBundle
    status: str
    diagnostics: tuple[Diagnostic, ...]
    provenance: dict[str, Any]
    compiler_version: str
    compiler_hash: str
    human_override: Any


@dataclass(frozen=True)
class _ReferenceContext:
    plan: ReferencePlan | None
    snapshot: dict[str, Any] | None
    snapshot_hash: str
    local: tuple[str, ...]
    url: tuple[str, ...]
    slots: tuple[dict[str, Any], ...]
    status: str
    diagnostics: tuple[Diagnostic, ...]


@dataclass(frozen=True)
class _DynamicInput:
    sort_key: tuple[str, ...]
    source_key: Any
    bundle: Any
    shot_id: str
    external_key: str
    keyframe_job_id: str | None
    previous_video_job_id: str | None
    metadata: dict[str, Any]


def _copy_json(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return copy.deepcopy(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): _copy_json(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_copy_json(item) for item in value]
    if isinstance(value, set | frozenset):
        return [_copy_json(item) for item in sorted(value, key=repr)]
    return copy.deepcopy(value)


def _json_text(value: Any) -> str:
    return json.dumps(
        _copy_json(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _required_text(value: Any, name: str) -> str:
    result = _text(value)
    if not result:
        raise MediaBridgeError(f"{name} 不能为空")
    return result


def _diagnostic_model(value: Any, label: str) -> Diagnostic:
    if isinstance(value, Diagnostic):
        return value.model_copy(deep=True)
    try:
        return Diagnostic.model_validate(copy.deepcopy(value))
    except (TypeError, ValueError, ValidationError) as exc:
        raise MediaBridgeError(f"{label} 不是有效诊断") from exc


def _diagnostic_key(value: Diagnostic) -> str:
    return _json_text(value.model_dump(mode="json"))


def _dedupe_diagnostics(values: list[Diagnostic]) -> list[Diagnostic]:
    result: list[Diagnostic] = []
    seen: set[str] = set()
    for item in values:
        key = _diagnostic_key(item)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _status_and_diagnostics(
    values: list[Diagnostic],
    *,
    bundle_status: str,
    reference_status: str,
) -> tuple[str, list[Diagnostic]]:
    diagnostics = _dedupe_diagnostics(values)
    if bundle_status == "blocked" and not any(item.level == "blocking" for item in diagnostics):
        diagnostics.append(
            diagnostic(
                "blocking",
                "compiled_bundle_blocked",
                "Prompt Bundle 已被编译门禁阻断，不能入队",
            )
        )
    elif bundle_status == "warning" and not diagnostics:
        diagnostics.append(
            diagnostic(
                "warning",
                "compiled_bundle_warning",
                "Prompt Bundle 带有编译警告",
            )
        )
    if reference_status == "blocked" and not any(
        item.level == "blocking" and item.code.startswith("reference_") for item in diagnostics
    ):
        diagnostics.append(
            diagnostic(
                "blocking",
                "reference_plan_blocked",
                "ReferencePlan 已被阻断，不能入队媒体任务",
                path="reference_plan",
            )
        )
    elif reference_status == "warning" and not any(
        item.level == "warning" and item.path == "reference_plan" for item in diagnostics
    ):
        diagnostics.append(
            diagnostic(
                "warning",
                "reference_plan_warning",
                "ReferencePlan 带有警告",
                path="reference_plan",
            )
        )
    gate = diagnostics_status(_dedupe_diagnostics(diagnostics))
    return str(gate.status), list(gate.diagnostics)


def _compiled_bundle(value: Any) -> CompiledPromptBundle | None:
    if isinstance(value, CompiledPromptBundle):
        return value.model_copy(deep=True)
    if not isinstance(value, Mapping):
        return None
    if not any(
        key in value
        for key in (
            "image",
            "video",
            "status",
            "diagnostics",
            "provenance",
            "compiler_version",
            "final",
            "generated",
        )
    ):
        return None
    try:
        return CompiledPromptBundle.model_validate(copy.deepcopy(dict(value)))
    except (TypeError, ValueError, ValidationError) as exc:
        raise MediaBridgeError("compiled Prompt Bundle 不符合 Prompt IR 合同") from exc


def _bundle_context(value: Any, kind: Literal["image", "video"]) -> _BundleContext:
    model_type: type[ImagePromptBundle] | type[VideoPromptBundle]
    if kind == "image":
        model_type = ImagePromptBundle
    else:
        model_type = VideoPromptBundle

    compiled = _compiled_bundle(value)
    if compiled is None:
        if isinstance(value, model_type):
            prompt_bundle = value.model_copy(deep=True)
        else:
            try:
                prompt_bundle = model_type.model_validate(copy.deepcopy(value))
            except (TypeError, ValueError, ValidationError) as exc:
                raise MediaBridgeError(f"{kind} Prompt Bundle 无法归一化") from exc
        return _BundleContext(
            prompt_bundle=prompt_bundle,
            status="ready",
            diagnostics=(),
            provenance={},
            compiler_version="",
            compiler_hash="",
            human_override=None,
        )

    source_bundle = compiled.image if kind == "image" else compiled.video
    if source_bundle is None:
        raise MediaBridgeError(f"compiled Prompt Bundle 缺少 {kind} bundle")

    # final 是页面最终发送稿；没有覆盖时它与 generated 等价。
    candidate: Any = source_bundle
    if isinstance(compiled.final, Mapping):
        candidate = compiled.final.get(kind, source_bundle)
    try:
        prompt_bundle = model_type.model_validate(copy.deepcopy(candidate))
    except (TypeError, ValueError, ValidationError) as exc:
        raise MediaBridgeError(f"compiled Prompt Bundle 的最终 {kind} bundle 无法归一化") from exc

    provenance = _copy_json(compiled.provenance)
    if not isinstance(provenance, dict):
        provenance = {}
    compiler_hash = _text(compiled.base_compiler_hash) or _text(
        provenance.get("base_compiler_hash") or provenance.get("input_hash")
    )
    return _BundleContext(
        prompt_bundle=prompt_bundle,
        status=compiled.status,
        diagnostics=tuple(item.model_copy(deep=True) for item in compiled.diagnostics),
        provenance=provenance,
        compiler_version=compiled.compiler_version,
        compiler_hash=compiler_hash,
        human_override=compiled.human_override.model_dump(mode="json")
        if compiled.human_override is not None
        else None,
    )


def _reference_context(value: ReferencePlan | Mapping[str, Any] | None) -> _ReferenceContext:
    if value is None:
        return _ReferenceContext(
            plan=None,
            snapshot=None,
            snapshot_hash="",
            local=(),
            url=(),
            slots=(),
            status="ready",
            diagnostics=(),
        )

    raw = _copy_json(value)
    if not isinstance(raw, Mapping):
        raise MediaBridgeError("reference_plan 必须是 ReferencePlan 或 Mapping")
    try:
        if isinstance(value, ReferencePlan):
            plan = value.model_copy(deep=True)
        else:
            plan = ReferencePlan.model_validate(
                {
                    key: copy.deepcopy(raw.get(key, default))
                    for key, default in (
                        ("slots", []),
                        ("actual_local_files", []),
                        ("actual_urls", []),
                        ("text_mapping", {}),
                        ("snapshot_hash", ""),
                    )
                }
            )
    except (TypeError, ValueError, ValidationError) as exc:
        raise MediaBridgeError("reference_plan 无法归一化") from exc

    raw_diagnostics = raw.get("diagnostics", [])
    if raw_diagnostics is None:
        raw_diagnostics = []
    if not isinstance(raw_diagnostics, list | tuple):
        raise MediaBridgeError("reference_plan.diagnostics 必须是数组")
    diagnostics = tuple(
        _diagnostic_model(item, f"reference_plan.diagnostics[{index}]")
        for index, item in enumerate(raw_diagnostics)
    )
    status = _text(raw.get("status")) or "ready"
    if status not in {"ready", "warning", "blocked"}:
        raise MediaBridgeError("reference_plan.status 无效")
    snapshot = _copy_json(raw)
    if not isinstance(snapshot, dict):
        snapshot = plan.model_dump(mode="json")
    snapshot_hash = _text(plan.snapshot_hash) or prompt_input_hash(snapshot)
    return _ReferenceContext(
        plan=plan,
        snapshot=snapshot,
        snapshot_hash=snapshot_hash,
        local=tuple(plan.actual_local_files),
        url=tuple(plan.actual_urls),
        slots=tuple(slot.model_dump(mode="json") for slot in plan.slots),
        status=status,
        diagnostics=diagnostics,
    )


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return any(_has_value(item) for item in value.values())
    if isinstance(value, list | tuple | set | frozenset):
        return any(_has_value(item) for item in value)
    return True


def _image_motion_diagnostics(bundle: ImagePromptBundle) -> list[Diagnostic]:
    semantic = bundle.semantic_plan
    if not isinstance(semantic, Mapping):
        semantic = {}
    has_motion = bool(semantic.get("timeline")) or _has_value(semantic.get("action"))
    has_completion = _has_value(semantic.get("end_state"))
    has_motion = has_motion or bool(
        re.search(r"(?im)^\s*(timeline|action|end\s+state)\s*:", bundle.prompt)
    )
    if not has_motion and not has_completion:
        return []
    return [
        diagnostic(
            "blocking",
            "image_contains_motion",
            "图片提示词包含视频时间轴、动作或动作完成状态",
            path="image.prompt",
        )
    ]


def _reference_diagnostics(
    bundle: ImagePromptBundle | VideoPromptBundle,
    reference: _ReferenceContext,
) -> list[Diagnostic]:
    if reference.plan is None:
        return []
    return list(validate_reference_text(bundle.prompt, reference.plan))


def _capability_snapshot(
    provenance: Mapping[str, Any],
    provider_id: str,
    provider_model: str,
) -> dict[str, Any]:
    raw = provenance.get("provider_capabilities")
    if raw is None:
        raw = provenance.get("provider_capability_snapshot", {})
    snapshot = _copy_json(raw)
    if not isinstance(snapshot, dict):
        snapshot = {}
    # 这是已选择的 Provider 标识，不是重新适配能力或修改 Prompt Bundle。
    snapshot["provider_id"] = provider_id
    snapshot["model"] = provider_model
    return snapshot


def _provenance(
    *,
    context: _BundleContext,
    reference: _ReferenceContext,
    diagnostics: list[Diagnostic],
    workflow_run_id: str,
    workflow_stage_id: str,
    shot_id: str,
    external_key: str,
    kind: Literal["image", "video"],
    provider_id: str,
    provider_model: str,
    prompt_ir_hash: str,
    input_signature: str,
    upstream_input_signature: str,
    idempotency_key: str,
    depends_on: list[str],
    requested_duration_sec: float | None,
    generation_duration_sec: float | None,
) -> dict[str, Any]:
    capability = _capability_snapshot(context.provenance, provider_id, provider_model)
    style_version = context.provenance.get("style_version")
    asset_versions = context.provenance.get(
        "asset_versions",
        context.provenance.get("material_versions", context.provenance.get("media_versions", [])),
    )
    result = {
        "bridge_version": MEDIA_BRIDGE_VERSION,
        "workflow_run_id": workflow_run_id,
        "workflow_stage_id": workflow_stage_id,
        "stage_id": workflow_stage_id,
        "shot_id": shot_id,
        "external_key": external_key,
        "kind": kind,
        "compiler_version": context.compiler_version,
        "compiler_hash": context.compiler_hash,
        "base_compiler_hash": context.compiler_hash,
        "prompt_ir_hash": prompt_ir_hash,
        "style_version": _copy_json(style_version),
        "asset_versions": _copy_json(asset_versions),
        "provider_id": provider_id,
        "model": provider_model,
        "provider_capabilities": capability,
        "capability_snapshot": copy.deepcopy(capability),
        "reference_plan": _copy_json(reference.snapshot),
        "reference_snapshot": _copy_json(reference.snapshot),
        "reference_plan_hash": reference.snapshot_hash,
        "diagnostics": [item.model_dump(mode="json") for item in diagnostics],
        "input_signature": input_signature,
        "upstream_input_signature": upstream_input_signature,
        "idempotency_key": idempotency_key,
        "depends_on": list(depends_on),
        "human_override": _copy_json(context.human_override),
        "requested_duration_sec": requested_duration_sec,
        "generation_duration_sec": generation_duration_sec,
        "reference_inputs": {
            "local": list(reference.local),
            "url": list(reference.url),
        },
        "compiler_provenance": _copy_json(context.provenance),
    }
    return result


def _target(
    *,
    context: _BundleContext,
    reference: _ReferenceContext,
    diagnostics: list[Diagnostic],
    workflow_run_id: str,
    workflow_stage_id: str,
    shot_id: str,
    external_key: str,
    kind: Literal["image", "video"],
    provider_id: str,
    provider_model: str,
    input_signature: str,
    media_signature: str,
    idempotency_key: str,
    depends_on: list[str],
    requested_duration_sec: float | None,
    generation_duration_sec: float | None,
) -> MediaTargetPlan:
    prompt_ir_json = context.prompt_bundle.model_dump(mode="json")
    prompt_ir_hash = prompt_input_hash(prompt_ir_json)
    provenance = _provenance(
        context=context,
        reference=reference,
        diagnostics=diagnostics,
        workflow_run_id=workflow_run_id,
        workflow_stage_id=workflow_stage_id,
        shot_id=shot_id,
        external_key=external_key,
        kind=kind,
        provider_id=provider_id,
        provider_model=provider_model,
        prompt_ir_hash=prompt_ir_hash,
        input_signature=media_signature,
        upstream_input_signature=input_signature,
        idempotency_key=idempotency_key,
        depends_on=depends_on,
        requested_duration_sec=requested_duration_sec,
        generation_duration_sec=generation_duration_sec,
    )
    status, ordered_diagnostics = _status_and_diagnostics(
        diagnostics,
        bundle_status=context.status,
        reference_status=reference.status,
    )
    # provenance 中的诊断必须与目标门禁看到的诊断完全一致。
    provenance["diagnostics"] = [item.model_dump(mode="json") for item in ordered_diagnostics]
    provenance["status"] = status
    try:
        return MediaTargetPlan(
            workflow_run_id=workflow_run_id,
            stage_id=workflow_stage_id,
            shot_id=shot_id,
            external_key=external_key,
            kind=kind,
            prompt_bundle=context.prompt_bundle,
            prompt_ir_json=prompt_ir_json,
            provenance_json=provenance,
            input_signature=media_signature,
            idempotency_key=idempotency_key,
            depends_on=list(depends_on),
            provider_id=provider_id,
            model=provider_model,
            requested_duration_sec=requested_duration_sec,
            generation_duration_sec=generation_duration_sec,
            reference_inputs=MediaReferenceInputs(
                local=list(reference.local),
                url=list(reference.url),
            ),
            status=cast("Literal['ready', 'warning', 'blocked']", status),
            diagnostics=ordered_diagnostics,
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise MediaBridgeError("媒体目标计划无法通过严格 schema 校验") from exc


def build_keyframe_media_plan(
    bundle: Any,
    *,
    workflow_run_id: str,
    workflow_stage_id: str,
    shot_id: str,
    external_key: str,
    input_signature: str,
    provider_id: str,
    provider_model: str,
    reference_plan: ReferencePlan | Mapping[str, Any] | None = None,
) -> MediaTargetPlan:
    """构造固定 0 秒关键帧计划，不访问 Provider 或数据库。"""

    workflow_run_id = _required_text(workflow_run_id, "workflow_run_id")
    workflow_stage_id = _required_text(workflow_stage_id, "workflow_stage_id")
    shot_id = _required_text(shot_id, "shot_id")
    external_key = _required_text(external_key, "external_key")
    input_signature = _required_text(input_signature, "input_signature")
    provider_id = _required_text(provider_id, "provider_id")
    provider_model = _required_text(provider_model, "provider_model")
    context = _bundle_context(bundle, "image")
    if not isinstance(context.prompt_bundle, ImagePromptBundle):
        raise MediaBridgeError("关键帧目标必须使用 ImagePromptBundle")
    reference = _reference_context(reference_plan)
    diagnostics = [*context.diagnostics, *reference.diagnostics]
    diagnostics.extend(_reference_diagnostics(context.prompt_bundle, reference))
    diagnostics.extend(_image_motion_diagnostics(context.prompt_bundle))

    params = {
        "prompt_ir": context.prompt_bundle.model_dump(mode="json"),
        "prompt_ir_hash": prompt_input_hash(context.prompt_bundle),
        "keyframe_time_sec": 0,
        "reference_snapshot": reference.snapshot,
        "reference_inputs": {"local": list(reference.local), "url": list(reference.url)},
        "provider_capabilities": _capability_snapshot(
            context.provenance, provider_id, provider_model
        ),
        "upstream_input_signature": input_signature,
    }
    media_signature = keyframe_signature(
        input_signature,
        list(reference.slots),
        provider_id,
        provider_model,
        params,
    )
    idempotency_key = "dynamic-keyframe-v1:" + prompt_input_hash(
        workflow_run_id,
        workflow_stage_id,
        shot_id,
        external_key,
        input_signature,
        context.prompt_bundle,
        reference.snapshot,
        list(reference.local),
        list(reference.url),
        _capability_snapshot(context.provenance, provider_id, provider_model),
        [],
        media_signature,
    )
    return _target(
        context=context,
        reference=reference,
        diagnostics=diagnostics,
        workflow_run_id=workflow_run_id,
        workflow_stage_id=workflow_stage_id,
        shot_id=shot_id,
        external_key=external_key,
        kind="image",
        provider_id=provider_id,
        provider_model=provider_model,
        input_signature=input_signature,
        media_signature=media_signature,
        idempotency_key=idempotency_key,
        depends_on=[],
        requested_duration_sec=None,
        generation_duration_sec=None,
    )


def _video_is_continuous(
    bundle: VideoPromptBundle,
    diagnostics: list[Diagnostic],
) -> bool:
    if any(item.code == "continuous_previous_end_frame_missing" for item in diagnostics):
        return True
    semantic = bundle.semantic_plan if isinstance(bundle.semantic_plan, Mapping) else {}
    if semantic.get("is_continuous") is True:
        return True
    continuity = semantic.get("continuity")
    if isinstance(continuity, Mapping):
        transition = _text(
            continuity.get("transition_type")
            or continuity.get("transition_from_prev")
            or continuity.get("transition")
        ).casefold()
        if transition == "continuous":
            return True
    return bool(re.search(r"transition\s*=\s*continuous", bundle.prompt, re.IGNORECASE))


def build_video_media_plan(
    bundle: Any,
    *,
    workflow_run_id: str,
    workflow_stage_id: str,
    shot_id: str,
    external_key: str,
    input_signature: str,
    provider_id: str,
    provider_model: str,
    reference_plan: ReferencePlan | Mapping[str, Any] | None = None,
    keyframe_job_id: str | None = None,
    previous_video_job_id: str | None = None,
    previous_end_frame_path: str | None = None,
) -> MediaTargetPlan:
    """构造视频计划，并显式表达关键帧及连续视频的 DAG 依赖。"""

    workflow_run_id = _required_text(workflow_run_id, "workflow_run_id")
    workflow_stage_id = _required_text(workflow_stage_id, "workflow_stage_id")
    shot_id = _required_text(shot_id, "shot_id")
    external_key = _required_text(external_key, "external_key")
    input_signature = _required_text(input_signature, "input_signature")
    provider_id = _required_text(provider_id, "provider_id")
    provider_model = _required_text(provider_model, "provider_model")
    context = _bundle_context(bundle, "video")
    if not isinstance(context.prompt_bundle, VideoPromptBundle):
        raise MediaBridgeError("视频目标必须使用 VideoPromptBundle")
    reference = _reference_context(reference_plan)
    keyframe_job_id = _text(keyframe_job_id) or None
    previous_video_job_id = _text(previous_video_job_id) or None
    previous_end_frame_path = _text(previous_end_frame_path) or None
    diagnostics = [*context.diagnostics, *reference.diagnostics]
    diagnostics.extend(_reference_diagnostics(context.prompt_bundle, reference))

    is_continuous = _video_is_continuous(context.prompt_bundle, diagnostics)
    depends_on: list[str] = []
    if keyframe_job_id:
        depends_on.append(keyframe_job_id)
    else:
        diagnostics.append(
            diagnostic(
                "blocking",
                "keyframe_job_missing",
                "视频目标缺少对应的关键帧 Job 依赖",
                path="depends_on",
            )
        )
    # 首个 continuous 镜头没有上一镜时按普通首镜处理；只有桥接层确实
    # 找到了上一镜 Job 或末帧快照，才要求两者成对存在。
    has_previous_context = bool(previous_video_job_id or previous_end_frame_path)
    if is_continuous and has_previous_context:
        if previous_video_job_id:
            if previous_video_job_id not in depends_on:
                depends_on.append(previous_video_job_id)
        else:
            diagnostics.append(
                diagnostic(
                    "blocking",
                    "continuous_previous_video_job_missing",
                    "continuous 视频目标缺少上一镜视频 Job 依赖",
                    path="depends_on",
                )
            )
        if not previous_end_frame_path:
            diagnostics.append(
                diagnostic(
                    "blocking",
                    "continuous_previous_end_frame_missing",
                    "continuous 视频目标缺少上一镜末帧路径",
                    path="previous_end_frame_path",
                )
            )

    requested_duration = context.prompt_bundle.edit_duration_sec
    generation_duration = context.prompt_bundle.generation_duration_sec
    # 编译器已经完成 Provider duration adaptation；桥接层只校验并保存，不再改值。
    if context.prompt_bundle.timeline:
        timeline_end = context.prompt_bundle.timeline[-1].end
        if abs(timeline_end - generation_duration) > 1e-9:
            diagnostics.append(
                diagnostic(
                    "blocking",
                    "timeline_discontinuous",
                    "视频时间轴末端与编译后的 generation_duration_sec 不一致",
                    path="video.timeline",
                    details={
                        "timeline_end_sec": timeline_end,
                        "generation_duration_sec": generation_duration,
                    },
                )
            )

    params = {
        "prompt_ir": context.prompt_bundle.model_dump(mode="json"),
        "prompt_ir_hash": prompt_input_hash(context.prompt_bundle),
        "requested_duration_sec": requested_duration,
        "generation_duration_sec": generation_duration,
        "reference_snapshot": reference.snapshot,
        "reference_inputs": {"local": list(reference.local), "url": list(reference.url)},
        "provider_capabilities": _capability_snapshot(
            context.provenance, provider_id, provider_model
        ),
        "depends_on": list(depends_on),
        "previous_end_frame_path": previous_end_frame_path,
        "upstream_input_signature": input_signature,
    }
    media_signature = video_signature(
        keyframe_job_id or "",
        context.prompt_bundle.prompt,
        generation_duration,
        provider_id,
        provider_model,
        params,
        list(reference.slots),
    )
    idempotency_key = "dynamic-video-v1:" + prompt_input_hash(
        workflow_run_id,
        workflow_stage_id,
        shot_id,
        external_key,
        input_signature,
        context.prompt_bundle,
        reference.snapshot,
        list(reference.local),
        list(reference.url),
        _capability_snapshot(context.provenance, provider_id, provider_model),
        list(depends_on),
        previous_end_frame_path,
        media_signature,
    )
    return _target(
        context=context,
        reference=reference,
        diagnostics=diagnostics,
        workflow_run_id=workflow_run_id,
        workflow_stage_id=workflow_stage_id,
        shot_id=shot_id,
        external_key=external_key,
        kind="video",
        provider_id=provider_id,
        provider_model=provider_model,
        input_signature=input_signature,
        media_signature=media_signature,
        idempotency_key=idempotency_key,
        depends_on=depends_on,
        requested_duration_sec=requested_duration,
        generation_duration_sec=generation_duration,
    )


def _metadata_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    metadata = value.get("metadata")
    result = dict(metadata) if isinstance(metadata, Mapping) else {}
    result.update(
        {
            key: item
            for key, item in value.items()
            if key
            in {
                "shot_id",
                "external_key",
                "keyframe_job_id",
                "previous_video_job_id",
                "workflow_run_id",
                "workflow_stage_id",
            }
        }
    )
    return _copy_json(result)


def _dynamic_input(key: Any, value: Any) -> _DynamicInput:
    metadata: dict[str, Any] = {}
    bundle = value
    if isinstance(value, Mapping) and "bundle" in value:
        bundle = value["bundle"]
        metadata = _metadata_mapping(value)
    elif isinstance(value, Mapping) and "compiled_bundle" in value:
        bundle = value["compiled_bundle"]
        metadata = _metadata_mapping(value)
    elif isinstance(value, tuple | list) and len(value) == 2 and isinstance(value[1], Mapping):
        bundle = value[0]
        metadata = _copy_json(value[1])

    if isinstance(value, Mapping):
        metadata = {
            **metadata,
            **{
                name: _copy_json(value[name])
                for name in ("reference_plan", "reference_snapshot", "previous_end_frame_path")
                if name in value and name not in metadata
            },
        }
        if "image" in value or "video" in value:
            bundle = {
                key: copy.deepcopy(value[key])
                for key in (
                    "schema_version",
                    "compiler_version",
                    "input_hash",
                    "status",
                    "image",
                    "video",
                    "diagnostics",
                    "provenance",
                    "generated",
                    "human_override",
                    "final",
                    "base_compiler_hash",
                    "override_status",
                )
                if key in value
            }

    key_values: tuple[str, ...]
    if isinstance(key, tuple | list):
        key_values = tuple(_text(item) for item in key)
    else:
        key_values = (_text(key),)
    key_values = tuple(item for item in key_values if item)
    if not key_values:
        raise MediaBridgeError("compiled_bundles 的 key 不能为空")

    compiled = _compiled_bundle(bundle)
    if compiled is not None:
        provenance = compiled.provenance
        metadata = {
            **metadata,
            **{
                key: provenance[key]
                for key in ("shot_id", "external_key", "keyframe_job_id", "previous_video_job_id")
                if key in provenance and key not in metadata
            },
        }

    shot_id = _text(metadata.get("shot_id")) or (key_values[0] if key_values else "")
    external_key = _text(metadata.get("external_key")) or (
        key_values[1] if len(key_values) > 1 else shot_id
    )
    shot_id = _required_text(shot_id, "shot_id")
    external_key = _required_text(external_key, "external_key")
    sort_key = (*key_values, shot_id, external_key)
    return _DynamicInput(
        sort_key=sort_key,
        source_key=key,
        bundle=bundle,
        shot_id=shot_id,
        external_key=external_key,
        keyframe_job_id=_text(metadata.get("keyframe_job_id")) or None,
        previous_video_job_id=_text(metadata.get("previous_video_job_id")) or None,
        metadata=metadata,
    )


def _previous_end_frame(
    previous_end_frames: Mapping[Any, Any],
    current: _DynamicInput,
    previous: _DynamicInput | None,
) -> str | None:
    candidates: list[Any] = [current.external_key, current.shot_id, current.source_key]
    if previous is not None:
        candidates.extend((previous.external_key, previous.shot_id, previous.source_key))
    for candidate in candidates:
        try:
            value = previous_end_frames.get(candidate)
        except (TypeError, AttributeError):
            value = None
        if isinstance(value, Mapping):
            value = value.get(
                "previous_end_frame_path",
                value.get("end_frame_path", value.get("path", value.get("local_file"))),
            )
        text = _text(value)
        if text:
            return text
    return None


def build_dynamic_media_bridge_plan(
    compiled_bundles: Mapping[Any, Any],
    *,
    workflow_run_id: str,
    workflow_stage_id: str,
    input_signature: str,
    provider_id: str,
    provider_model: str,
    previous_end_frames: Mapping[Any, Any] | None = None,
) -> DynamicMediaBridgePlan:
    """按稳定 key 顺序构造阶段内 image/video 目标，不提交 Provider。"""

    if not isinstance(compiled_bundles, Mapping):
        raise MediaBridgeError("compiled_bundles 必须是按 shot/external key 索引的 Mapping")
    workflow_run_id = _required_text(workflow_run_id, "workflow_run_id")
    workflow_stage_id = _required_text(workflow_stage_id, "workflow_stage_id")
    input_signature = _required_text(input_signature, "input_signature")
    provider_id = _required_text(provider_id, "provider_id")
    provider_model = _required_text(provider_model, "provider_model")
    previous_end_frames = previous_end_frames or {}

    items = sorted(
        (_dynamic_input(key, value) for key, value in compiled_bundles.items()),
        key=lambda item: item.sort_key,
    )
    targets: list[MediaTargetPlan] = []
    all_diagnostics: list[Diagnostic] = []
    previous_item: _DynamicInput | None = None
    previous_video: MediaTargetPlan | None = None

    for item in items:
        image = build_keyframe_media_plan(
            item.bundle,
            workflow_run_id=workflow_run_id,
            workflow_stage_id=workflow_stage_id,
            shot_id=item.shot_id,
            external_key=item.external_key,
            input_signature=input_signature,
            provider_id=provider_id,
            provider_model=provider_model,
            reference_plan=item.metadata.get("reference_plan"),
        )
        targets.append(image)
        all_diagnostics.extend(image.diagnostics)

        keyframe_job_id = item.keyframe_job_id or image.idempotency_key
        previous_video_job_id = item.previous_video_job_id
        if previous_video_job_id is None and previous_video is not None:
            previous_video_job_id = previous_video.idempotency_key
        end_frame = _text(item.metadata.get("previous_end_frame_path")) or _previous_end_frame(
            previous_end_frames, item, previous_item
        )
        video = build_video_media_plan(
            item.bundle,
            workflow_run_id=workflow_run_id,
            workflow_stage_id=workflow_stage_id,
            shot_id=item.shot_id,
            external_key=item.external_key,
            input_signature=input_signature,
            provider_id=provider_id,
            provider_model=provider_model,
            reference_plan=item.metadata.get("reference_plan"),
            keyframe_job_id=keyframe_job_id,
            previous_video_job_id=previous_video_job_id,
            previous_end_frame_path=end_frame,
        )
        targets.append(video)
        all_diagnostics.extend(video.diagnostics)
        previous_item = item
        previous_video = video

    gate = diagnostics_status(_dedupe_diagnostics(all_diagnostics))
    try:
        return DynamicMediaBridgePlan(
            workflow_run_id=workflow_run_id,
            stage_id=workflow_stage_id,
            input_signature=input_signature,
            provider_id=provider_id,
            model=provider_model,
            targets=targets,
            status=cast("Literal['ready', 'warning', 'blocked']", gate.status),
            diagnostics=list(gate.diagnostics),
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise MediaBridgeError("动态媒体桥接计划无法通过严格 schema 校验") from exc


def _as_plan(value: MediaTargetPlan | Mapping[str, Any]) -> MediaTargetPlan:
    if isinstance(value, MediaTargetPlan):
        return value.model_copy(deep=True)
    try:
        return MediaTargetPlan.model_validate(copy.deepcopy(value))
    except (TypeError, ValueError, ValidationError) as exc:
        raise MediaBridgeError("媒体目标计划无效") from exc


def _common_metadata(plan: MediaTargetPlan) -> dict[str, Any]:
    return {
        "media_bridge_version": MEDIA_BRIDGE_VERSION,
        "workflow_run_id": plan.workflow_run_id,
        "workflow_stage_id": plan.stage_id,
        "stage_id": plan.stage_id,
        "shot_id": plan.shot_id,
        "external_key": plan.external_key,
        "kind": plan.kind,
        "status": plan.status,
        "diagnostics": [item.model_dump(mode="json") for item in plan.diagnostics],
        "input_signature": plan.input_signature,
        "idempotency_key": plan.idempotency_key,
        "depends_on": list(plan.depends_on),
        "provider_id": plan.provider_id,
        "model": plan.model,
        "provider_model": plan.model,
        "requested_duration_sec": plan.requested_duration_sec,
        "generation_duration_sec": plan.generation_duration_sec,
        "reference_inputs": {
            "local": list(plan.reference_inputs.local),
            "url": list(plan.reference_inputs.url),
        },
        "prompt_bundle": plan.prompt_bundle.model_dump(mode="json"),
        "prompt_ir_json": _copy_json(plan.prompt_ir_json),
        "provenance_json": _copy_json(plan.provenance_json),
    }


def to_shot_pipeline_metadata(plan: MediaTargetPlan | Mapping[str, Any]) -> dict[str, Any]:
    """返回可序列化到 Shot.pipeline_metadata_json 的单目标 metadata。"""

    return _common_metadata(_as_plan(plan))


def to_media_version_payload(plan: MediaTargetPlan | Mapping[str, Any]) -> dict[str, Any]:
    """返回 keyframe_version/video_version 使用的纯 payload，不执行 insert。"""

    target = _as_plan(plan)
    bundle = target.prompt_bundle
    prompt_ir_text = _json_text(target.prompt_ir_json)
    provenance_text = _json_text(target.provenance_json)
    reference_slots = target.prompt_ir_json.get("reference_slots", [])
    queue_status = "blocked" if target.status == "blocked" else "queued"
    payload: dict[str, Any] = {
        "file_path": None,
        "prompt_snapshot": bundle.prompt,
        "negative_prompt_snapshot": bundle.negative_prompt,
        "prompt_ir_json": prompt_ir_text,
        "provenance_json": provenance_text,
        "provider_id": target.provider_id,
        "model": target.model,
        "provider_task_id": "",
        "status": queue_status,
        "is_current": False,
        "input_signature": target.input_signature,
        "source": "generated",
        "error": "",
        "reference_files_json": _json_text(target.reference_inputs.local),
        "reference_bindings_json": _json_text(reference_slots),
        "reference_inputs_local": list(target.reference_inputs.local),
        "reference_inputs_url": list(target.reference_inputs.url),
        "bridge_status": target.status,
        "can_enqueue": target.status != "blocked",
    }
    if target.kind == "video":
        assert isinstance(bundle, VideoPromptBundle)
        payload.update(
            {
                "keyframe_job_id": target.depends_on[0] if target.depends_on else None,
                "requested_duration_sec": target.requested_duration_sec,
                "generation_duration_sec": target.generation_duration_sec,
                "actual_duration_sec": None,
            }
        )
    return payload


def to_job_payload(plan: MediaTargetPlan | Mapping[str, Any]) -> dict[str, Any]:
    """返回 JobCreate 兼容的动态字段和 Provider 输入快照。"""

    target = _as_plan(plan)
    bundle = target.prompt_bundle
    is_image = target.kind == "image"
    job_type = "generate_keyframe" if is_image else "generate_video"
    target_type = "shot" if is_image else "keyframe"
    generation_duration = target.generation_duration_sec
    input_data: dict[str, Any] = {
        "workflow_run_id": target.workflow_run_id,
        "workflow_stage_id": target.stage_id,
        "shot_id": target.shot_id,
        "external_key": target.external_key,
        "kind": target.kind,
        "prompt": bundle.prompt,
        "negative_prompt": bundle.negative_prompt,
        "prompt_ir_json": _copy_json(target.prompt_ir_json),
        "provenance_json": _copy_json(target.provenance_json),
        "prompt_ir": _copy_json(target.prompt_ir_json),
        "provenance": _copy_json(target.provenance_json),
        "reference_inputs": {
            "local": list(target.reference_inputs.local),
            "url": list(target.reference_inputs.url),
        },
        "reference_inputs_local": list(target.reference_inputs.local),
        "reference_inputs_url": list(target.reference_inputs.url),
        "reference_files": list(target.reference_inputs.local),
        "reference_urls": list(target.reference_inputs.url),
        "depends_on": list(target.depends_on),
        "provider_id": target.provider_id,
        "model": target.model,
        "requested_duration_sec": target.requested_duration_sec,
        "generation_duration_sec": generation_duration,
        "duration_sec": generation_duration,
        "plan_status": target.status,
    }
    return {
        "job_type": job_type,
        "target_type": target_type,
        "target_id": target.shot_id,
        "workflow_run_id": target.workflow_run_id,
        "workflow_stage_id": target.stage_id,
        "idempotency_key": target.idempotency_key,
        "provider_kind": target.kind,
        "provider_id": target.provider_id,
        "input_signature": target.input_signature,
        "depends_on": list(target.depends_on),
        "input_data": input_data,
        "status": target.status,
        "queueable": target.status != "blocked",
        "progress_total": 1,
    }


__all__ = [
    "DynamicMediaBridgePlan",
    "MEDIA_BRIDGE_VERSION",
    "MediaBridgeError",
    "MediaReferenceInputs",
    "MediaTargetPlan",
    "build_dynamic_media_bridge_plan",
    "build_keyframe_media_plan",
    "build_video_media_plan",
    "to_job_payload",
    "to_media_version_payload",
    "to_shot_pipeline_metadata",
]
