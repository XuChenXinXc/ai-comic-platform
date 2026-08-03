from __future__ import annotations

import json
from typing import Annotated, Any, ClassVar, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def _has_content(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_has_content(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_content(item) for item in value)
    return value is not None


def _readable_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "、".join(filter(None, (_readable_text(item) for item in value)))
    if isinstance(value, dict):
        for key in ("summary", "description", "text", "value"):
            if _has_content(value.get(key)):
                return _readable_text(value[key])
        return "；".join(
            f"{key}：{text}" for key, item in value.items() if (text := _readable_text(item))
        )
    return str(value or "").strip()


def _normalize_aliases(value: Any, aliases: dict[str, str]) -> Any:
    if not isinstance(value, dict):
        return value
    normalized = dict(value)
    for alias, target in aliases.items():
        if alias in normalized:
            if not _has_content(normalized.get(target)):
                normalized[target] = normalized[alias]
            normalized.pop(alias, None)
    return normalized


def _expand_nested_field(
    value: dict[str, Any],
    field_name: str,
    nested_fields: tuple[str, ...],
) -> dict[str, Any]:
    nested = value.get(field_name)
    if not isinstance(nested, dict):
        return value
    normalized = dict(value)
    for nested_name in nested_fields:
        if not _has_content(normalized.get(nested_name)) and _has_content(nested.get(nested_name)):
            normalized[nested_name] = nested[nested_name]
    normalized[field_name] = _readable_text(nested)
    return normalized


def _flatten_text_fields(model: type[BaseModel], value: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(value)
    for field_name, model_field in model.model_fields.items():
        current = normalized.get(field_name)
        if model_field.annotation is str and current is not None and not isinstance(current, str):
            normalized[field_name] = _readable_text(current)
    return normalized


class OutputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NovelAnalysisOutput(OutputModel):
    summary: str
    genre: str
    time_period: str
    locations: list[str]
    worldview: str
    main_characters: list[str]
    character_relations: list[dict[str, Any]]
    core_conflict: str
    plot_structure: dict[str, Any]
    emotion_curve: list[dict[str, Any]]
    foreshadowing: list[str]
    visualizable_content: list[str]
    hard_to_visualize: list[str]
    production_risks: list[str]


class VisualStyleOutput(OutputModel):
    plain_text_output_field: ClassVar[str] = "style_prompt"

    style_prompt: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=18, max_length=32),
    ] = Field(
        description=(
            "18至32个汉字，可直接写入项目统一画风 STYLE_ANCHOR 输入框的中文画风描述，"
            "不含字段名、标题或解释"
        ),
    )


class ScreenplayDialogue(OutputModel):
    speaker: NonEmptyText
    text: NonEmptyText

    @model_validator(mode="before")
    @classmethod
    def normalize_aliases(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if not normalized.get("speaker"):
            normalized["speaker"] = normalized.get("character") or normalized.get("name")
        if not normalized.get("text"):
            normalized["text"] = normalized.get("content") or normalized.get("line")
        for alias in ("character", "name", "content", "line"):
            normalized.pop(alias, None)
        return normalized


class ScreenplayScene(OutputModel):
    scene_no: int
    scene_name: str
    time_weather: str
    characters: list[str]
    objective: str
    action: str
    dialogue: list[ScreenplayDialogue]
    narration: list[str]
    emotion: str
    transition_to_next: str


class ScreenplayOutput(OutputModel):
    title: str
    logline: str
    scenes: list[ScreenplayScene]


class AssetVisualPromptFields(OutputModel):
    prompt: NonEmptyText
    negative_prompt: NonEmptyText
    consistency_keywords: list[NonEmptyText] = Field(min_length=1)
    composition: NonEmptyText


class CharacterAssetOutput(AssetVisualPromptFields):
    name: NonEmptyText
    aliases: list[str] = Field(default_factory=list)
    gender: str = ""
    age: str = ""
    identity: str = ""
    concept: str = ""
    worldview: str = ""
    personality: str = ""
    appearance: str = ""
    face: str = ""
    hair: str = ""
    eyes: str = ""
    normal_expression: str = ""
    body: str = ""
    costume: str = ""
    main_color: str = ""
    accent_color: str = ""
    costume_material: str = ""
    upper: str = ""
    lower: str = ""
    outer: str = ""
    shoes: str = ""
    accessories: str = ""
    carried_item: str = ""
    style_tone: str = ""
    lighting: str = ""
    atmosphere: str = ""
    expression_group: list[str] = Field(default_factory=list)
    costume_variants: list[str] = Field(default_factory=list)
    immutable_features: list[str] = Field(default_factory=list)
    acting_traits: list[str] = Field(default_factory=list)
    relations: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    importance: Literal["core", "normal"] = "normal"

    @model_validator(mode="before")
    @classmethod
    def normalize_profile(cls, value: Any) -> Any:
        normalized = _normalize_aliases(
            value,
            {
                "world": "worldview",
                "color": "main_color",
                "material": "costume_material",
                "top": "upper",
                "bottom": "lower",
                "outerwear": "outer",
                "footwear": "shoes",
                "core_prop": "carried_item",
            },
        )
        if not isinstance(normalized, dict):
            return normalized
        normalized = _expand_nested_field(
            normalized,
            "appearance",
            ("face", "hair", "eyes", "body"),
        )
        return _flatten_text_fields(cls, normalized)

    @model_validator(mode="after")
    def validate_visual_profile(self) -> CharacterAssetOutput:
        if not any(
            _has_content(getattr(self, field_name))
            for field_name in (
                "appearance",
                "face",
                "hair",
                "eyes",
                "body",
                "costume",
                "immutable_features",
            )
        ):
            raise ValueError("角色素材缺少核心视觉信息")
        return self


class SceneAssetOutput(AssetVisualPromptFields):
    name: NonEmptyText
    concept: str = ""
    worldview: str = ""
    indoor_outdoor: str = ""
    place_type: str = ""
    plot_function: str = ""
    time_of_day: str = ""
    weather: str = ""
    architecture: str = ""
    layout: str = ""
    space_keywords: list[str] = Field(default_factory=list)
    shape_scale: str = ""
    observer_reference: str = ""
    regions: list[str] = Field(default_factory=list)
    entrance_position: str = ""
    window_position: str = ""
    character_position: str = ""
    prop_position: str = ""
    movement_path: str = ""
    wall_material: str = ""
    floor_material: str = ""
    furniture_material: str = ""
    glass_metal_material: str = ""
    special_material: str = ""
    main_color: str = ""
    accent_color: str = ""
    color_quality: str = ""
    main_view: str = ""
    standard_view_1: str = ""
    standard_view_2: str = ""
    detail_zone: list[str] = Field(default_factory=list)
    main_light: str = ""
    fill_light: str = ""
    atmosphere: str = ""
    lighting: str = ""
    color_mood: str = ""
    furnishings: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    importance: Literal["core", "normal"] = "normal"

    @model_validator(mode="before")
    @classmethod
    def normalize_profile(cls, value: Any) -> Any:
        normalized = _normalize_aliases(
            value,
            {
                "world": "worldview",
                "scale": "shape_scale",
            },
        )
        if not isinstance(normalized, dict):
            return normalized
        normalized = _expand_nested_field(
            normalized,
            "layout",
            (
                "space_keywords",
                "regions",
                "entrance_position",
                "window_position",
                "character_position",
                "prop_position",
                "movement_path",
            ),
        )
        return _flatten_text_fields(cls, normalized)

    @model_validator(mode="after")
    def validate_visual_profile(self) -> SceneAssetOutput:
        if not any(
            _has_content(getattr(self, field_name))
            for field_name in (
                "layout",
                "architecture",
                "space_keywords",
                "regions",
                "furnishings",
                "main_view",
            )
        ):
            raise ValueError("场景素材缺少核心视觉信息")
        return self


class PropAssetOutput(AssetVisualPromptFields):
    name: NonEmptyText
    description: str
    concept: str = ""
    worldview: str = ""
    category: str = ""
    owner: str = ""
    silhouette: str = ""
    dimensions: str = ""
    weight: str = ""
    front_structure: str = ""
    side_structure: str = ""
    back_structure: str = ""
    other_structure: str = ""
    key_structure_position: str = ""
    material: str = ""
    secondary_material: str = ""
    color: str = ""
    accent_color: str = ""
    surface_texture: str = ""
    wear: str = ""
    detail_zone: list[str] = Field(default_factory=list)
    scale_reference: str = ""
    special_features: list[str] = Field(default_factory=list)
    plot_function: str = ""
    source_refs: list[str] = Field(default_factory=list)
    importance: Literal["core", "normal"] = "normal"

    @model_validator(mode="before")
    @classmethod
    def normalize_profile(cls, value: Any) -> Any:
        normalized = _normalize_aliases(
            value,
            {
                "world": "worldview",
                "user": "owner",
                "size": "dimensions",
                "main_color": "color",
                "usage_marks": "wear",
            },
        )
        if not isinstance(normalized, dict):
            return normalized
        normalized = _expand_nested_field(
            normalized,
            "description",
            (
                "silhouette",
                "dimensions",
                "front_structure",
                "side_structure",
                "back_structure",
                "other_structure",
                "material",
                "secondary_material",
                "color",
                "surface_texture",
                "wear",
            ),
        )
        return _flatten_text_fields(cls, normalized)

    @model_validator(mode="after")
    def validate_visual_profile(self) -> PropAssetOutput:
        if not any(
            _has_content(getattr(self, field_name))
            for field_name in (
                "description",
                "silhouette",
                "front_structure",
                "material",
                "special_features",
            )
        ):
            raise ValueError("物品素材缺少核心视觉信息")
        return self


class AssetExtractionOutput(OutputModel):
    characters: list[CharacterAssetOutput]
    scenes: list[SceneAssetOutput]
    props: list[PropAssetOutput]


class AssetPromptOutput(AssetVisualPromptFields):
    pass


class ShotPosition(OutputModel):
    character: NonEmptyText
    position: NonEmptyText
    facing: NonEmptyText | None = Field(default=None, exclude_if=lambda value: value is None)
    gaze: NonEmptyText | None = Field(default=None, exclude_if=lambda value: value is None)
    action_direction: NonEmptyText | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_aliases(cls, value: Any) -> Any:
        return _normalize_aliases(
            value,
            {
                "name": "character",
                "speaker": "character",
                "character_name": "character",
                "location": "position",
                "side": "position",
                "placement": "position",
                "direction": "facing",
                "look": "gaze",
                "eye_line": "gaze",
                "movement_direction": "action_direction",
            },
        )


class ShotExpression(OutputModel):
    character: NonEmptyText
    expression: NonEmptyText

    @model_validator(mode="before")
    @classmethod
    def normalize_aliases(cls, value: Any) -> Any:
        return _normalize_aliases(
            value,
            {
                "name": "character",
                "speaker": "character",
                "character_name": "character",
                "emotion": "expression",
                "mood": "expression",
            },
        )


class ShotOutput(OutputModel):
    shot_no: int
    scene_no: int | None = None
    source_ref: str = ""
    scene_name: NonEmptyText
    scene_asset_id: NonEmptyText
    character_names: list[NonEmptyText]
    character_asset_ids: list[NonEmptyText]
    prop_names: list[NonEmptyText]
    prop_asset_ids: list[NonEmptyText]
    positions: list[ShotPosition]
    start_state: str
    action: str
    expressions: list[ShotExpression]
    dialogue: list[ScreenplayDialogue]
    narration: str = ""
    shot_type: str
    camera_angle: str
    camera_move: str
    duration_sec: int = Field(gt=0)
    end_state: str
    next_hook: str
    frame_description: str
    image_prompt: NonEmptyText
    video_prompt: NonEmptyText
    negative_prompt: NonEmptyText

    @field_validator("character_asset_ids", "prop_asset_ids")
    @classmethod
    def validate_unique_asset_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("素材 ID 不能重复")
        return value


class ShotExtractionOutput(OutputModel):
    shots: list[ShotOutput]


class ShotImagePromptOutput(OutputModel):
    prompt: NonEmptyText
    negative_prompt: NonEmptyText
    reference_requirements: list[dict[str, Any]]


class ShotVideoPromptOutput(OutputModel):
    prompt: NonEmptyText
    negative_prompt: NonEmptyText
    timeline: list[dict[str, Any]]
    continuity_notes: list[str]


class ShotVideoPromptOptimizeOutput(OutputModel):
    plain_text_output_field: ClassVar[str] = "prompt"

    prompt: NonEmptyText


class AssetReferenceBinding(OutputModel):
    role: Literal["character_identity", "scene_layout", "prop_identity"]
    asset_id: str
    asset_image_id: str


class PreviousKeyframeBinding(OutputModel):
    role: Literal["previous_keyframe"]
    keyframe_id: str


ReferenceBinding = Annotated[
    AssetReferenceBinding | PreviousKeyframeBinding,
    Field(discriminator="role"),
]


class ProviderConfig(BaseModel):
    id: str
    kind: Literal["text", "image", "video"]
    name: str = ""
    adapter: str
    base_url: str = ""
    api_key_env: str = ""
    api_key: str = ""
    model: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    timeout_sec: int = Field(default=120, gt=0)
    max_retries: int = Field(default=2, ge=0)
    enabled: bool = True

    @model_validator(mode="before")
    @classmethod
    def parse_database_json(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            try:
                value = dict(value)
            except (TypeError, ValueError):
                return value
        normalized = dict(value)
        if "params" not in normalized and "params_json" in normalized:
            raw_params = normalized.get("params_json") or "{}"
            normalized["params"] = (
                json.loads(raw_params) if isinstance(raw_params, str) else raw_params
            )
        return normalized


class ProviderSubmission(BaseModel):
    task_id: str = ""
    status: str
    result: dict[str, Any] = Field(default_factory=dict)
    message: str = ""


class ProviderPollResult(BaseModel):
    task_id: str = ""
    status: str
    result_url: str = ""
    content_base64: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    error: str = ""

    @property
    def completed(self) -> bool:
        return self.status == "completed"


class JobCreate(BaseModel):
    id: str | None = None
    project_id: str
    job_type: str
    target_type: str = ""
    target_id: str | None = None
    batch_id: str | None = None
    provider_kind: str = ""
    provider_id: str | None = None
    input_data: dict[str, Any] = Field(default_factory=dict)
    progress_total: int = Field(default=1, ge=1)


class JobRead(BaseModel):
    id: str
    project_id: str
    job_type: str
    target_type: str
    target_id: str | None
    batch_id: str | None
    provider_kind: str
    provider_id: str | None
    provider_task_id: str
    status: str
    progress_current: int
    progress_total: int
    message: str
    input_data: dict[str, Any]
    result_data: dict[str, Any]
    error: str
    retry_count: int
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None


class BatchSummary(BaseModel):
    batch_id: str
    total: int
    queued: int = 0
    running: int = 0
    waiting_provider: int = 0
    downloading: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    interrupted: int = 0
