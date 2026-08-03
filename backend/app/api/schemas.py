import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas import ScreenplayDialogue

AssetType = Literal["character", "scene", "prop"]
ContentStatus = Literal["draft", "ready", "confirmed", "failed"]
ProviderKind = Literal["text", "image", "video"]
ShotSourceType = Literal["novel", "screenplay"]
ProjectStorageSource = Literal["database", "environment", "default"]


def _sensitive_param_path(value: Any, path: str = "params") -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key))
            parts = {part for part in re.split(r"[^a-z0-9]+", normalized.lower()) if part}
            collapsed = re.sub(r"[^a-z0-9]", "", normalized.lower())
            current = f"{path}.{key}"
            if parts & {
                "auth",
                "authorization",
                "credential",
                "credentials",
                "token",
                "key",
                "secret",
                "password",
                "passwd",
                "cookie",
                "sig",
                "signature",
            } or collapsed in {
                "apikey",
                "xapikey",
                "accesstoken",
                "accesskey",
                "authtoken",
                "bearertoken",
                "clientsecret",
                "privatekey",
                "proxyauthorization",
                "secretkey",
                "setcookie",
            }:
                return current
            nested = _sensitive_param_path(item, current)
            if nested:
                return nested
    elif isinstance(value, list):
        for index, item in enumerate(value):
            nested = _sensitive_param_path(item, f"{path}[{index}]")
            if nested:
                return nested
    return None


def _validate_public_params(value: dict[str, Any] | None) -> dict[str, Any] | None:
    sensitive_path = _sensitive_param_path(value)
    if sensitive_path:
        raise ValueError(f"params 不能包含敏感凭据字段：{sensitive_path}")
    return value


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectCreate(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    source_text: str = ""
    style_prompt: str = ""
    aspect_ratio: str = "16:9"
    image_size: str = "1920x1080"
    video_size: str = "16:9"
    default_duration_sec: float = Field(default=6, gt=0)
    default_text_provider_id: str | None = None
    default_image_provider_id: str | None = None
    default_video_provider_id: str | None = None


class ProjectUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    source_text: str | None = None
    style_prompt: str | None = None
    aspect_ratio: str | None = None
    image_size: str | None = None
    video_size: str | None = None
    default_duration_sec: float | None = Field(default=None, gt=0)
    default_text_provider_id: str | None = None
    default_image_provider_id: str | None = None
    default_video_provider_id: str | None = None


class ProjectStorageSetting(ApiModel):
    path: str
    source: ProjectStorageSource


class ProjectStorageSelectionResult(ProjectStorageSetting):
    selected: bool


class GenerateRequest(ApiModel):
    provider_id: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)

    _validate_params = field_validator("params")(_validate_public_params)


class TextContentUpdate(ApiModel):
    content: dict[str, Any] | None = None
    content_json: dict[str, Any] | None = None
    status: ContentStatus = "ready"

    @model_validator(mode="after")
    def validate_content(self) -> "TextContentUpdate":
        if self.content is None and self.content_json is None:
            raise ValueError("必须提供 content 或 content_json")
        if self.content is not None and self.content_json is not None:
            raise ValueError("content 和 content_json 只能提供一个")
        self.content = self.content if self.content is not None else self.content_json
        return self


class ScreenplaySceneGenerateRequest(GenerateRequest):
    scene_no: int = Field(ge=1)
    instructions: str = ""


class AssetExtractRequest(GenerateRequest):
    asset_types: list[AssetType] = Field(default_factory=lambda: ["character", "scene", "prop"])

    @model_validator(mode="after")
    def validate_asset_types(self) -> "AssetExtractRequest":
        if not self.asset_types:
            raise ValueError("asset_types 不能为空")
        if len(set(self.asset_types)) != len(self.asset_types):
            raise ValueError("asset_types 不能重复")
        return self


class AssetCreate(ApiModel):
    asset_type: AssetType
    name: str = Field(min_length=1, max_length=200)
    profile: dict[str, Any] = Field(default_factory=dict)
    prompt: str = ""
    negative_prompt: str = ""
    consistency_keywords: list[str] = Field(default_factory=list)
    composition: str = ""
    importance: Literal["core", "normal"] = "normal"
    status: ContentStatus = "draft"


class AssetUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    profile: dict[str, Any] | None = None
    prompt: str | None = None
    negative_prompt: str | None = None
    consistency_keywords: list[str] | None = None
    composition: str | None = None
    importance: Literal["core", "normal"] | None = None
    status: ContentStatus | None = None


class AssetMergeRequest(ApiModel):
    ids: list[str] | None = None
    source_asset_ids: list[str] | None = None

    @model_validator(mode="after")
    def validate_unique_sources(self) -> "AssetMergeRequest":
        values = self.ids if self.ids is not None else self.source_asset_ids
        if not values:
            raise ValueError("ids 不能为空")
        if len(set(values)) != len(values):
            raise ValueError("ids 不能重复")
        self.ids = values
        return self


class AssetBatchRequest(GenerateRequest):
    ids: list[str] | None = None
    asset_type: AssetType | None = None
    only_missing: bool = False


class ShotExtractRequest(GenerateRequest):
    source_type: ShotSourceType = "screenplay"
    scope: Literal["all", "scene"] = "all"
    scene_no: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_scope(self) -> "ShotExtractRequest":
        if self.scope == "scene" and self.scene_no is None:
            raise ValueError("scope 为 scene 时必须提供 scene_no")
        if self.scope == "all" and self.scene_no is not None:
            raise ValueError("scope 为 all 时不能提供 scene_no")
        return self


def _normalize_shot_nested_aliases(value: Any, aliases: dict[str, str]) -> Any:
    if not isinstance(value, dict):
        return value
    normalized = dict(value)
    for alias, target in aliases.items():
        if normalized.get(target) in (None, "") and normalized.get(alias) not in (None, ""):
            normalized[target] = normalized[alias]
    return normalized


class ShotPositionInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    character: str = ""
    position: str = ""
    facing: str | None = None
    gaze: str | None = None
    action_direction: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_aliases(cls, value: Any) -> Any:
        return _normalize_shot_nested_aliases(
            value,
            {
                "character_id": "character",
                "name": "character",
                "speaker": "character",
                "character_name": "character",
                "side": "position",
                "location": "position",
                "placement": "position",
                "direction": "facing",
                "look": "gaze",
                "eye_line": "gaze",
                "movement_direction": "action_direction",
            },
        )


class ShotExpressionInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    character: str = ""
    expression: str = ""

    @model_validator(mode="before")
    @classmethod
    def normalize_aliases(cls, value: Any) -> Any:
        return _normalize_shot_nested_aliases(
            value,
            {
                "character_id": "character",
                "name": "character",
                "speaker": "character",
                "character_name": "character",
                "emotion": "expression",
                "mood": "expression",
            },
        )


class ShotDialogueInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    speaker: str = ""
    text: str = ""

    @model_validator(mode="before")
    @classmethod
    def normalize_aliases(cls, value: Any) -> Any:
        return _normalize_shot_nested_aliases(
            value,
            {
                "character": "speaker",
                "name": "speaker",
                "content": "text",
                "line": "text",
            },
        )


class ShotFields(ApiModel):
    source_type: ShotSourceType = "screenplay"
    source_ref: str = ""
    scene_asset_id: str | None = None
    character_ids: list[str] = Field(default_factory=list)
    prop_ids: list[str] = Field(default_factory=list)
    positions: list[ShotPositionInput] = Field(default_factory=list)
    start_state: str = ""
    action: str = ""
    expressions: list[ShotExpressionInput] = Field(default_factory=list)
    dialogue: list[ShotDialogueInput] = Field(default_factory=list)
    narration: str = ""
    shot_type: str = ""
    camera_angle: str = ""
    camera_move: str = ""
    duration_sec: float = Field(gt=0)
    end_state: str = ""
    next_hook: str = ""
    frame_description: str = ""
    image_prompt: str = ""
    image_prompt_override: str = ""
    video_prompt: str = ""
    video_prompt_override: str = ""
    negative_prompt: str = ""
    reference_bindings: list[dict[str, Any]] = Field(default_factory=list)
    status: ContentStatus = "draft"


class ShotCreate(ShotFields):
    shot_no: int | None = Field(default=None, ge=1)
    shot_order: int | None = Field(default=None, ge=1)


class ShotUpdate(ApiModel):
    source_ref: str | None = None
    scene_asset_id: str | None = None
    character_ids: list[str] | None = None
    prop_ids: list[str] | None = None
    positions: list[ShotPositionInput] | None = None
    start_state: str | None = None
    action: str | None = None
    expressions: list[ShotExpressionInput] | None = None
    dialogue: list[ShotDialogueInput] | None = None
    narration: str | None = None
    shot_type: str | None = None
    camera_angle: str | None = None
    camera_move: str | None = None
    duration_sec: float | None = Field(default=None, gt=0)
    end_state: str | None = None
    next_hook: str | None = None
    frame_description: str | None = None
    image_prompt: str | None = None
    image_prompt_override: str | None = None
    video_prompt: str | None = None
    video_prompt_override: str | None = None
    negative_prompt: str | None = None
    reference_bindings: list[dict[str, Any]] | None = None
    status: ContentStatus | None = None


class ShotReorderRequest(ApiModel):
    ids: list[str] | None = None
    shot_ids: list[str] | None = None

    @model_validator(mode="after")
    def validate_unique_shots(self) -> "ShotReorderRequest":
        values = self.ids if self.ids is not None else self.shot_ids
        if not values:
            raise ValueError("ids 不能为空")
        if len(set(values)) != len(values):
            raise ValueError("ids 不能重复")
        self.ids = values
        return self


class ShotSplitPart(ApiModel):
    action: str = ""
    start_state: str = ""
    end_state: str = ""
    frame_description: str = ""
    dialogue: list[ScreenplayDialogue] | None = None
    narration: str | None = None
    duration_sec: float | None = Field(default=None, gt=0)


class ShotSplitRequest(ApiModel):
    parts: list[ShotSplitPart] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_parts(self) -> "ShotSplitRequest":
        if self.parts and len(self.parts) < 2:
            raise ValueError("parts 至少包含两段")
        return self


class ShotCopyRequest(ApiModel):
    after_shot_id: str | None = None


class ShotMergeRequest(ApiModel):
    ids: list[str] | None = None
    source_shot_ids: list[str] | None = None

    @model_validator(mode="after")
    def validate_unique_sources(self) -> "ShotMergeRequest":
        values = self.ids if self.ids is not None else self.source_shot_ids
        if not values:
            raise ValueError("ids 不能为空")
        if len(set(values)) != len(values):
            raise ValueError("ids 不能重复")
        self.ids = values
        return self


class ShotPromptBatchRequest(GenerateRequest):
    ids: list[str] | None = None
    prompt_types: list[Literal["image", "video"]] = Field(
        default_factory=lambda: ["image", "video"]
    )
    only_missing: bool = False

    @model_validator(mode="after")
    def validate_prompt_types(self) -> "ShotPromptBatchRequest":
        if not self.prompt_types:
            raise ValueError("prompt_types 不能为空")
        if len(set(self.prompt_types)) != len(self.prompt_types):
            raise ValueError("prompt_types 不能重复")
        return self


class MediaBatchRequest(GenerateRequest):
    ids: list[str] | None = None
    scene_no: int | None = Field(default=None, ge=1)
    only_missing: bool = False


class ProviderCreate(ApiModel):
    kind: ProviderKind
    name: str = Field(min_length=1, max_length=200)
    adapter: str = Field(min_length=1, max_length=100)
    base_url: str = ""
    api_key_env: str = ""
    api_key: str = Field(default="", max_length=2000)
    model: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    timeout_sec: int = Field(default=120, ge=1)
    max_retries: int = Field(default=2, ge=0)
    enabled: bool = True
    is_default: bool = False

    _validate_params = field_validator("params")(_validate_public_params)


class ProviderUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    adapter: str | None = Field(default=None, min_length=1, max_length=100)
    base_url: str | None = None
    api_key_env: str | None = None
    api_key: str | None = Field(default=None, max_length=2000)
    model: str | None = None
    params: dict[str, Any] | None = None
    timeout_sec: int | None = Field(default=None, ge=1)
    max_retries: int | None = Field(default=None, ge=0)
    enabled: bool | None = None
    is_default: bool | None = None

    _validate_params = field_validator("params")(_validate_public_params)
