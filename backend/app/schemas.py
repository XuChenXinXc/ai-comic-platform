from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class ScreenplayScene(OutputModel):
    scene_no: int
    scene_name: str
    time_weather: str
    characters: list[str]
    objective: str
    action: str
    dialogue: list[dict[str, Any]]
    narration: list[str]
    emotion: str
    transition_to_next: str


class ScreenplayOutput(OutputModel):
    title: str
    logline: str
    scenes: list[ScreenplayScene]


class CharacterAssetOutput(OutputModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    gender: str = ""
    age: str = ""
    identity: str = ""
    personality: str = ""
    appearance: str = ""
    hair: str = ""
    body: str = ""
    costume: str = ""
    costume_variants: list[str] = Field(default_factory=list)
    immutable_features: list[str] = Field(default_factory=list)
    acting_traits: list[str] = Field(default_factory=list)
    relations: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    importance: Literal["core", "normal"] = "normal"


class SceneAssetOutput(OutputModel):
    name: str
    indoor_outdoor: str = ""
    time_of_day: str = ""
    weather: str = ""
    architecture: str = ""
    layout: str = ""
    lighting: str = ""
    color_mood: str = ""
    furnishings: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    importance: Literal["core", "normal"] = "normal"


class PropAssetOutput(OutputModel):
    name: str
    description: str
    owner: str = ""
    material: str = ""
    color: str = ""
    special_features: list[str] = Field(default_factory=list)
    plot_function: str = ""
    source_refs: list[str] = Field(default_factory=list)
    importance: Literal["core", "normal"] = "normal"


class AssetExtractionOutput(OutputModel):
    characters: list[CharacterAssetOutput]
    scenes: list[SceneAssetOutput]
    props: list[PropAssetOutput]


class AssetPromptOutput(OutputModel):
    prompt: str
    negative_prompt: str
    consistency_keywords: list[str]
    composition: str
    recommended_size: str


class ShotOutput(OutputModel):
    shot_no: int
    scene_no: int | None = None
    source_ref: str = ""
    scene_name: str
    character_names: list[str]
    prop_names: list[str]
    positions: list[dict[str, Any]]
    start_state: str
    action: str
    expressions: list[dict[str, Any]]
    dialogue: list[dict[str, Any]]
    narration: str = ""
    shot_type: str
    camera_angle: str
    camera_move: str
    duration_sec: float
    end_state: str
    next_hook: str
    frame_description: str


class ShotExtractionOutput(OutputModel):
    shots: list[ShotOutput]


class ShotImagePromptOutput(OutputModel):
    prompt: str
    negative_prompt: str
    reference_requirements: list[dict[str, Any]]


class ShotVideoPromptOutput(OutputModel):
    prompt: str
    negative_prompt: str
    timeline: list[dict[str, Any]]
    continuity_notes: list[str]


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
