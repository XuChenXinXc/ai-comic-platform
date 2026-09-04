from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from app.dynamic_pipeline.schemas import Diagnostic, PipelineModel, ReferenceSlot

PromptValue = str | list[Any] | dict[str, Any] | None


class VisualStyleSpec(PipelineModel):
    """可编译的结构化视觉规范，用户自由文本只作为补充信息。"""

    medium: str = ""
    line_style: str = ""
    color_palette: str | list[str] = ""
    lighting_style: str = ""
    material_style: str = ""
    realism_level: str | float = ""
    mood: str = ""
    text_policy: str | list[str] = ""
    global_negative_rules: list[str] = Field(default_factory=list)
    immutable_rules: list[str] = Field(default_factory=list)
    style_version: str = "1"
    style_prompt: str | None = None


class AssetVisualAnchor(PipelineModel):
    """单个 canonical asset 的视觉事实，不按名称自动合并。"""

    canonical_name: str = Field(min_length=1)
    asset_id: str | None = Field(default=None, min_length=1)
    asset_type: Literal["character", "scene", "prop"] | None = None
    asset_revision: int | str | None = None
    asset_version: int | str | None = None
    visual_facts: list[str] = Field(default_factory=list)
    immutable_features: list[str] = Field(default_factory=list)
    forbidden_changes: list[str] = Field(default_factory=list)
    makeup_keyword_string: str = ""
    face: str = ""
    hair: str = ""
    body: str = ""
    costume: str = ""
    accessories: list[str] = Field(default_factory=list)
    variants: list[str] = Field(default_factory=list)
    current_variant: str | None = Field(default=None, min_length=1)
    approved_references: list[Any] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)
    character_details: dict[str, Any] | None = None
    scene_details: dict[str, Any] | None = None
    prop_details: dict[str, Any] | None = None
    layout: PromptValue = None
    axis: PromptValue = None
    entrances: list[Any] | None = None
    fixed_furnishings: list[Any] | None = None
    lighting_direction: PromptValue = None
    weather: PromptValue = None
    time_of_day: PromptValue = None
    shape: PromptValue = None
    material: PromptValue = None
    color: PromptValue = None
    wear: PromptValue = None
    owner: PromptValue = None
    current_holder: PromptValue = None
    visibility: PromptValue = None


class ShotIntent(PipelineModel):
    shot_id: str | None = Field(default=None, min_length=1)
    external_key: str | None = Field(default=None, min_length=1)
    keyframe_time_sec: float = Field(default=0, ge=0)
    subject: PromptValue = None
    action: PromptValue = None
    scene: PromptValue = None
    camera: PromptValue = None
    lighting: PromptValue = None
    audio: PromptValue = None
    start_state: PromptValue = None
    end_state: PromptValue = None
    next_hook: PromptValue = None
    transition_from_prev: str = "scene_change"
    duration_sec: float = Field(default=6, gt=0)
    action_events: list[Any] = Field(default_factory=list)
    dialogue_cues: list[Any] = Field(default_factory=list)
    sound_cues: list[Any] = Field(default_factory=list)
    continuity_state: dict[str, Any] = Field(default_factory=dict)
    is_continuous: bool = False


class ContinuityContext(PipelineModel):
    previous_shot_id: str | None = Field(default=None, min_length=1)
    previous_end_frame_path: str | None = Field(default=None, min_length=1)
    previous_end_state: PromptValue = None
    transition_type: str = "scene_change"
    axis: PromptValue = None
    positioning: PromptValue = None
    props: PromptValue = None
    previous_continuity_state: dict[str, Any] = Field(default_factory=dict)


ReferencePlanSlot = ReferenceSlot


class ReferencePlan(PipelineModel):
    slots: list[ReferencePlanSlot] = Field(default_factory=list)
    actual_local_files: list[str] = Field(default_factory=list)
    actual_urls: list[str] = Field(default_factory=list)
    text_mapping: dict[str, Any] = Field(default_factory=dict)
    snapshot_hash: str = ""


class ProviderCapabilities(PipelineModel):
    provider_id: str = ""
    model: str = ""
    supports_multiple_images: bool = False
    max_reference_images: int = Field(default=1, ge=0)
    supported_durations: list[Annotated[float, Field(gt=0)]] = Field(default_factory=list)
    min_duration_sec: float | None = Field(default=None, ge=0)
    max_duration_sec: float | None = Field(default=None, gt=0)
    duration_step_sec: float | None = Field(default=None, gt=0)
    supports_end_frame: bool = False
    supports_audio: bool = False
    supports_dialogue: bool = False
    capability_version: str = "1"


class HumanOverrideSet(PipelineModel):
    image_prompt: str | None = None
    video_prompt: str | None = None
    negative_prompt: str | None = None
    camera: PromptValue = None
    action: PromptValue = None
    end_state: PromptValue = None
    reference_slots: list[ReferenceSlot] | None = None
    cleared: bool = False
    override_version: str = "1"


class TimelineSegment(PipelineModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    action: str = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def accept_canonical_time_names(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        normalized = dict(value)
        if "start" not in normalized and "start_sec" in normalized:
            normalized["start"] = normalized["start_sec"]
        if "end" not in normalized and "end_sec" in normalized:
            normalized["end"] = normalized["end_sec"]
        normalized.pop("start_sec", None)
        normalized.pop("end_sec", None)
        return normalized

    @model_validator(mode="after")
    def validate_time_range(self) -> TimelineSegment:
        if self.end <= self.start:
            raise ValueError("timeline end 必须大于 start")
        return self


PromptTimelineSegment = TimelineSegment


class ImagePromptBundle(PipelineModel):
    prompt: str = ""
    negative_prompt: str = ""
    keyframe_time_sec: Literal[0] = 0
    reference_slots: list[ReferenceSlot] = Field(default_factory=list)
    semantic_plan: dict[str, Any] = Field(default_factory=dict)
    diagnostics: list[Diagnostic] = Field(default_factory=list)


class VideoPromptBundle(PipelineModel):
    prompt: str = ""
    negative_prompt: str = ""
    edit_duration_sec: float = Field(default=6, gt=0)
    generation_duration_sec: float = Field(default=6, gt=0)
    timeline: list[TimelineSegment] = Field(default_factory=list)
    end_state: PromptValue = None
    reference_slots: list[ReferenceSlot] = Field(default_factory=list)
    semantic_plan: dict[str, Any] = Field(default_factory=dict)
    diagnostics: list[Diagnostic] = Field(default_factory=list)


PromptLayer = dict[str, Any] | ImagePromptBundle | VideoPromptBundle | None


class CompiledPromptBundle(PipelineModel):
    schema_version: str = "dynamic-comic-prompt-v1"
    compiler_version: str = "dynamic-prompt-2"
    input_hash: str = ""
    status: Literal["ready", "warning", "blocked"] = "ready"
    image: ImagePromptBundle | None = None
    video: VideoPromptBundle | None = None
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)
    generated: PromptLayer = Field(default_factory=dict)
    human_override: HumanOverrideSet | None = None
    final: PromptLayer = Field(default_factory=dict)
    base_compiler_hash: str = ""
    override_status: Literal["active", "stale", "cleared"] = "cleared"

    @model_validator(mode="after")
    def populate_deterministic_hashes(self) -> CompiledPromptBundle:
        asset_versions = self.provenance.get(
            "asset_versions",
            self.provenance.get("material_versions", self.provenance.get("media_versions", [])),
        )
        capability_snapshot = self.provenance.get(
            "provider_capabilities",
            self.provenance.get("provider_capability_snapshot", {}),
        )
        if not self.base_compiler_hash:
            self.base_compiler_hash, calculated_input_hash = calculate_prompt_bundle_hashes(
                self,
                asset_versions=asset_versions,
                provider_capabilities=capability_snapshot,
            )
        else:
            calculated_input_hash = ""
        if not self.input_hash:
            if not calculated_input_hash:
                _, calculated_input_hash = calculate_prompt_bundle_hashes(
                    self,
                    asset_versions=asset_versions,
                    provider_capabilities=capability_snapshot,
                )
            self.input_hash = calculated_input_hash
        if self.human_override and not self.human_override.cleared:
            if self.override_status == "cleared":
                self.override_status = "active"
        elif self.human_override and self.human_override.cleared:
            self.override_status = "cleared"
        return self


def _canonical_value(value: Any) -> Any:
    if isinstance(value, PipelineModel):
        return _canonical_value(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, list | tuple):
        return [_canonical_value(item) for item in value]
    if isinstance(value, set | frozenset):
        values = [_canonical_value(item) for item in value]
        return sorted(
            values,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Enum):
        return _canonical_value(value.value)
    return value


def canonical_prompt_json(value: Any) -> str:
    """将 Prompt IR 值编码为无时间、无随机因素的排序 JSON。"""

    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def prompt_input_hash(*parts: Any) -> str:
    """计算 Prompt IR 输入签名。调用方传入的所有版本和覆盖信息都会参与签名。"""

    return hashlib.sha256(canonical_prompt_json(parts).encode("utf-8")).hexdigest()


def calculate_prompt_bundle_hashes(
    bundle: CompiledPromptBundle,
    *,
    asset_versions: Any = None,
    provider_capabilities: Any = None,
    effective_reference_plan: Any = None,
) -> tuple[str, str]:
    """按真实单 channel/full bundle 结构计算统一的 base/input hash。"""

    provenance = bundle.provenance if isinstance(bundle.provenance, Mapping) else {}
    if asset_versions is None:
        asset_versions = provenance.get(
            "asset_versions",
            provenance.get("material_versions", provenance.get("media_versions", [])),
        )
    if provider_capabilities is None:
        provider_capabilities = provenance.get(
            "provider_capabilities",
            provenance.get("provider_capability_snapshot", {}),
        )
    if effective_reference_plan is None:
        effective_reference_plan = provenance.get(
            "effective_reference_plan",
            provenance.get("reference_plan"),
        )
    if bundle.image is not None and bundle.video is not None:
        channel = "both"
    elif bundle.image is not None:
        channel = "image"
    elif bundle.video is not None:
        channel = "video"
    else:
        channel = "none"
    payload = {
        "schema_version": bundle.schema_version,
        "compiler_version": bundle.compiler_version,
        "channel": channel,
        "generated": bundle.generated,
        "image": bundle.image,
        "video": bundle.video,
        "style_version": provenance.get("style_version", ""),
        "asset_versions": asset_versions,
        "provider_capabilities": provider_capabilities,
        "effective_reference_plan": effective_reference_plan,
    }
    base_hash = prompt_input_hash("dynamic-prompt-bundle-hash-v1", payload)
    override = (
        bundle.human_override.model_dump(mode="json")
        if bundle.human_override is not None
        else None
    )
    input_hash = prompt_input_hash(
        "dynamic-prompt-bundle-hash-v1",
        payload,
        {"human_override": override},
    )
    return base_hash, input_hash


def _validated_override(override: HumanOverrideSet | Mapping[str, Any] | None) -> HumanOverrideSet:
    if override is None:
        return HumanOverrideSet(cleared=True)
    if isinstance(override, HumanOverrideSet):
        return override
    return HumanOverrideSet.model_validate(override)


def _override_values(override: HumanOverrideSet) -> dict[str, Any]:
    values = override.model_dump(mode="python", exclude_none=True)
    values.pop("cleared", None)
    values.pop("override_version", None)
    return values


def _set_semantic_value(target: dict[str, Any], key: str, value: Any) -> None:
    if key in target:
        target[key] = copy.deepcopy(value)
        return
    semantic_plan = target.get("semantic_plan")
    if not isinstance(semantic_plan, Mapping):
        semantic_plan = {}
    else:
        semantic_plan = copy.deepcopy(dict(semantic_plan))
    semantic_plan[key] = copy.deepcopy(value)
    target["semantic_plan"] = semantic_plan


def _apply_override_to_mapping(
    generated: Mapping[str, Any],
    override: HumanOverrideSet,
    channel: Literal["image", "video"] | None = None,
) -> dict[str, Any]:
    result = copy.deepcopy(dict(generated))
    values = _override_values(override)

    if channel is None and ("image" in result or "video" in result):
        if "image" in result:
            result["image"] = _apply_override_to_value(result["image"], override, "image")
        if "video" in result:
            result["video"] = _apply_override_to_value(result["video"], override, "video")
        return result

    if channel == "image":
        if "image_prompt" in values:
            result["prompt"] = copy.deepcopy(values["image_prompt"])
    elif channel == "video":
        if "video_prompt" in values:
            result["prompt"] = copy.deepcopy(values["video_prompt"])
    else:
        if "image_prompt" in values and "image_prompt" in result:
            result["image_prompt"] = copy.deepcopy(values["image_prompt"])
        if "video_prompt" in values and "video_prompt" in result:
            result["video_prompt"] = copy.deepcopy(values["video_prompt"])
        if "prompt" in result:
            prompt_value = values.get("video_prompt", values.get("image_prompt"))
            if prompt_value is not None:
                result["prompt"] = copy.deepcopy(prompt_value)

    if "negative_prompt" in values:
        result["negative_prompt"] = copy.deepcopy(values["negative_prompt"])
    for key in ("camera", "action"):
        if key in values:
            _set_semantic_value(result, key, values[key])
    if "end_state" in values and channel != "image":
        _set_semantic_value(result, "end_state", values["end_state"])
    if "reference_slots" in values:
        result["reference_slots"] = copy.deepcopy(values["reference_slots"])
    return result


def _apply_override_to_value(
    generated: Any,
    override: HumanOverrideSet,
    channel: Literal["image", "video"] | None = None,
) -> Any:
    if isinstance(generated, PipelineModel):
        model_channel = channel
        if model_channel is None and isinstance(generated, ImagePromptBundle):
            model_channel = "image"
        elif model_channel is None and isinstance(generated, VideoPromptBundle):
            model_channel = "video"
        values = _apply_override_to_mapping(
            generated.model_dump(mode="python"),
            override,
            model_channel,
        )
        return type(generated).model_validate(values)
    if isinstance(generated, Mapping):
        return _apply_override_to_mapping(generated, override, channel)
    return copy.deepcopy(generated)


def apply_human_override(
    generated: PromptLayer,
    override: HumanOverrideSet | Mapping[str, Any] | None,
) -> PromptLayer:
    """将非空人工覆盖应用到 generated，保留 generated 本身不变。"""

    validated_override = _validated_override(override)
    if validated_override.cleared:
        return copy.deepcopy(generated)
    return _apply_override_to_value(generated, validated_override)
