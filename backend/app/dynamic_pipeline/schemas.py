from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

StageKey = Literal[
    "0-source",
    "1-character-bios",
    "2-rhythm",
    "3-script",
    "4-storyboard",
    "5a-image-prompts",
    "5b-video-prompts",
    "6-delivery",
]

WorkflowStatus = Literal[
    "pending",
    "queued",
    "running",
    "blocked",
    "ready",
    "confirmed",
    "stale",
    "partial_failed",
    "failed",
    "interrupted",
    "cancelled",
]

StageStatus = WorkflowStatus
SourceKind = Literal["full_text", "synopsis"]
DeliverySubstage = Literal["image", "keyframe", "video", "audio", "subtitle", "composition"]
DiagnosticLevel = Literal["warning", "blocking"]

# Phase 2 阶段产物使用的稳定枚举和数值类型。
Importance = Literal["core", "supporting", "ensemble"]
ShootabilityStatus = Literal["shootable", "needs_adaptation", "unshootable"]
ConflictTurnKind = Literal["conflict", "turn", "payoff"]
RhythmBeatType = Literal[
    "hook",
    "setup",
    "escalation",
    "conflict",
    "turn",
    "payoff",
    "breath",
    "foreshadowing",
    "transition",
    "cliffhanger",
]
DynamicLevel = Annotated[float, Field(ge=0, le=1)]


class PipelineModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkflowRunCreate(PipelineModel):
    project_id: str | None = None
    title: str = Field(min_length=1)
    chapter_title: str = Field(min_length=1)
    source_text: str | None = None
    synopsis: str | None = None
    target_duration_sec: float = Field(default=30, gt=0, le=3600)
    aspect_ratio: str = Field(default="9:16", min_length=1)
    style_prompt: str = ""
    auto_run: bool = False
    generate_audio: bool = True
    generate_subtitle: bool = True
    generate_bgm: bool = True
    generate_composition: bool = True
    reuse_approved_assets: bool = False

    @model_validator(mode="after")
    def validate_source(self) -> WorkflowRunCreate:
        self.source_text = self.source_text.strip() if self.source_text is not None else None
        self.synopsis = self.synopsis.strip() if self.synopsis is not None else None
        self.title = self.title.strip()
        self.chapter_title = self.chapter_title.strip()
        self.aspect_ratio = self.aspect_ratio.strip()

        has_source_text = bool(self.source_text)
        has_synopsis = bool(self.synopsis)
        if has_source_text == has_synopsis:
            raise ValueError("source_text 与 synopsis 必须二选一且非空")
        if not self.title.strip() or not self.chapter_title.strip():
            raise ValueError("title 与 chapter_title 不能为空")
        if not self.aspect_ratio.strip():
            raise ValueError("aspect_ratio 不能为空")
        return self


class AssetMapping(PipelineModel):
    workflow_asset_id: str = Field(min_length=1)
    asset_id: str | None = None
    usage: str = Field(min_length=1)
    locked_revision: int | None = Field(default=None, ge=1)
    reuse_mode: Literal["new", "approved_existing"] = "new"


class WorkflowRunRead(PipelineModel):
    id: str
    project_id: str | None = None
    chapter_id: str | None = None
    title: str | None = None
    chapter_title: str | None = None
    kind: str = "dynamic_comic_0_6"
    schema_version: str = "1"
    status: WorkflowStatus
    current_stage: StageKey
    source_kind: SourceKind
    source_snapshot: dict[str, Any] = Field(default_factory=dict)
    config_snapshot: dict[str, Any] = Field(default_factory=dict)
    asset_mapping: list[AssetMapping] = Field(default_factory=list)
    input_signature: str = ""
    revision: int = Field(default=1, ge=1)
    error: str = ""
    created_at: str
    started_at: str | None = None
    updated_at: str
    finished_at: str | None = None


class Diagnostic(PipelineModel):
    level: DiagnosticLevel
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    path: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class StageGateResult(PipelineModel):
    status: Literal["ready", "warning", "blocked", "partial_failed"] = "blocked"
    passed: bool = False
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    checked_at: str | None = None
    can_confirm: bool = False


class ReferenceSlot(PipelineModel):
    index: int = Field(ge=1)
    role: str = Field(min_length=1)
    asset_id: str | None = None
    keyframe_id: str | None = None
    image_version_id: str | None = None
    required: bool = False
    purpose: str = Field(min_length=1)
    relative_path: str | None = None
    provider_url: str | None = None
    provider_url_status: str = ""


class DialogueCue(PipelineModel):
    dialogue_line_id: str = Field(min_length=1)
    speaker: str = ""
    text: str = Field(min_length=1)
    start_time: float = Field(ge=0)
    end_time: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_time_range(self) -> DialogueCue:
        if self.end_time <= self.start_time:
            raise ValueError("end_time 必须大于 start_time")
        return self


class SoundCue(PipelineModel):
    sound_id: str | None = None
    kind: str = Field(min_length=1)
    description: str = Field(min_length=1)
    start_time: float = Field(ge=0)
    end_time: float = Field(gt=0)
    volume: float = Field(default=1.0, ge=0, le=1)

    @model_validator(mode="after")
    def validate_time_range(self) -> SoundCue:
        if self.end_time <= self.start_time:
            raise ValueError("end_time 必须大于 start_time")
        return self


class WorkflowStageRead(PipelineModel):
    id: str
    workflow_run_id: str
    stage_key: StageKey
    ordinal: int = Field(ge=0)
    status: StageStatus
    input_signature: str = ""
    raw_output: dict[str, Any] = Field(default_factory=dict)
    normalized_output: dict[str, Any] = Field(default_factory=dict)
    draft_output: dict[str, Any] = Field(default_factory=dict)
    output_refs: list[dict[str, Any]] = Field(default_factory=list)
    gate_result: StageGateResult = Field(default_factory=lambda: StageGateResult(status="blocked"))
    attempt: int = Field(default=1, ge=1)
    error: str = ""
    created_at: str
    started_at: str | None = None
    updated_at: str
    finished_at: str | None = None
    confirmed_at: str | None = None
    confirmed_revision: int | None = Field(default=None, ge=1)


class WorkflowStageDraft(PipelineModel):
    stage_key: StageKey
    input_signature: str = ""
    raw_output: dict[str, Any] = Field(default_factory=dict)
    normalized_output: dict[str, Any] = Field(default_factory=dict)
    draft_output: dict[str, Any] = Field(default_factory=dict)
    output_refs: list[dict[str, Any]] = Field(default_factory=list)
    gate_result: StageGateResult | None = None
    diagnostics: list[Diagnostic] = Field(default_factory=list)


class ExpectedRevisionRequest(PipelineModel):
    expected_revision: int = Field(ge=1)


class StageDraftUpdate(PipelineModel):
    expected_revision: int = Field(ge=1)
    draft_output: dict[str, Any]
    gate_result: StageGateResult | None = None


class StageConfirmRequest(PipelineModel):
    expected_revision: int = Field(ge=1)


class StageActionRequest(PipelineModel):
    expected_revision: int = Field(ge=1)


class WorkflowRunActionRead(PipelineModel):
    run: WorkflowRunRead
    stages: list[WorkflowStageRead] = Field(default_factory=list)


class ArtifactMeta(PipelineModel):
    schema_version: str = Field(min_length=1)
    normalizer_version: str = Field(min_length=1)
    input_signature: str = Field(min_length=1)
    content_signature: str = Field(min_length=1)
    source_attempt: int = Field(default=1, ge=1)
    provider_id: str | None = Field(default=None, min_length=1)
    provider_model: str | None = Field(default=None, min_length=1)
    generated_at: str | None = Field(default=None, min_length=1)
    edited_at: str | None = Field(default=None, min_length=1)


class SourceMetadata(PipelineModel):
    source_kind: SourceKind
    title: str = Field(min_length=1)
    chapter_title: str = Field(min_length=1)
    source_signature: str = Field(min_length=1)
    target_duration_sec: float = Field(gt=0)
    aspect_ratio: str = Field(min_length=1)


class SourceCharacterRef(PipelineModel):
    character_key: str = Field(min_length=1)
    canonical_name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    importance: Importance
    story_function: str = Field(min_length=1)
    source_refs: list[str] = Field(default_factory=list)


class SourceSceneShootability(PipelineModel):
    status: ShootabilityStatus
    visible_action: str = Field(min_length=1)
    required_asset_keys: list[str] = Field(default_factory=list)
    production_risks: list[str] = Field(default_factory=list)
    adaptation_note: str | None = Field(default=None, min_length=1)


class SourceScene(PipelineModel):
    scene_id: str = Field(min_length=1)
    ordinal: int = Field(ge=1)
    location: str = Field(min_length=1)
    time_weather: str = Field(min_length=1)
    characters: list[SourceCharacterRef] = Field(default_factory=list)
    narrative_task: str = Field(min_length=1)
    plot_event: str = Field(min_length=1)
    conflict_or_turn: str = Field(min_length=1)
    visual_focus: str = Field(min_length=1)
    estimated_duration_sec: float = Field(ge=0)
    shootability: SourceSceneShootability
    source_refs: list[str] = Field(default_factory=list)


class ConflictTurn(PipelineModel):
    kind: ConflictTurnKind
    summary: str = Field(min_length=1)
    scene_id: str | None = Field(default=None, min_length=1)
    participants: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)


class SourceAnalysisOutput(PipelineModel):
    artifact_meta: ArtifactMeta
    metadata: SourceMetadata
    episode_logline: str = Field(min_length=1)
    scenes: list[SourceScene] = Field(default_factory=list)
    character_roster: list[SourceCharacterRef] = Field(default_factory=list)
    conflict_turns: list[ConflictTurn] = Field(default_factory=list)
    visualizable_content: list[str] = Field(default_factory=list)
    hard_to_visualize: list[str] = Field(default_factory=list)
    rewrite_requirements: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    continuation_outline: list[str] = Field(default_factory=list)


class CharacterBasicInfo(PipelineModel):
    character_key: str = Field(min_length=1)
    canonical_name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    importance: Importance
    identity: str = ""
    age: str = ""
    gender: str = ""


class CharacterVisualAnchor(PipelineModel):
    visual_facts: list[str] = Field(default_factory=list)
    face: str = Field(min_length=1)
    hair: str = Field(min_length=1)
    body: str = Field(min_length=1)
    costume: str = Field(min_length=1)
    accessories: list[str] = Field(default_factory=list)
    immutable_features: list[str] = Field(default_factory=list)
    forbidden_changes: list[str] = Field(default_factory=list)
    makeup_keyword_string: str = Field(min_length=1)
    variants: list[str] = Field(default_factory=list)
    current_variant: str | None = Field(default=None, min_length=1)
    approved_reference_ids: list[str] = Field(default_factory=list)


class CharacterBio(PipelineModel):
    basic_info: CharacterBasicInfo
    background: str = Field(min_length=1)
    personality_behavior_logic: str = Field(min_length=1)
    dynamic_comic_setting: str = Field(min_length=1)
    plot_relation: str = Field(min_length=1)
    visual_anchor: CharacterVisualAnchor


class EnsembleAnchor(PipelineModel):
    ensemble_key: str = Field(min_length=1)
    canonical_name: str = Field(min_length=1)
    visual_facts: list[str] = Field(default_factory=list)
    usage_notes: str = ""


class CharacterBiosOutput(PipelineModel):
    artifact_meta: ArtifactMeta
    characters: list[CharacterBio] = Field(default_factory=list)
    ensemble_anchors: list[EnsembleAnchor] = Field(default_factory=list)
    unresolved_character_refs: list[str] = Field(default_factory=list)


class RhythmSegment(PipelineModel):
    segment_id: str = Field(min_length=1)
    ordinal: int = Field(ge=1)
    start_sec: float = Field(ge=0)
    end_sec: float = Field(gt=0)
    beat_type: RhythmBeatType
    narrative_task: str = Field(min_length=1)
    event_summary: str = Field(min_length=1)
    visual_action: str = Field(min_length=1)
    focus_characters: list[str] = Field(default_factory=list)
    dynamic_level: DynamicLevel
    strong_dynamic_event_id: str | None = Field(default=None, min_length=1)
    source_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_time_range(self) -> RhythmSegment:
        if self.end_sec <= self.start_sec:
            raise ValueError("end_sec 必须大于 start_sec")
        return self


class StrongDynamicEvent(PipelineModel):
    event_id: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    segment_id: str | None = Field(default=None, min_length=1)
    dynamic_level: DynamicLevel = 1.0
    visual_action: str = ""
    source_refs: list[str] = Field(default_factory=list)


class StrongDynamicBudget(PipelineModel):
    max_events: int = Field(default=0, ge=0)
    event_ids: list[str] = Field(default_factory=list)
    events: list[StrongDynamicEvent] = Field(default_factory=list)


class DialogueSuggestion(PipelineModel):
    segment_id: str | None = Field(default=None, min_length=1)
    speaker: str = ""
    text: str = Field(min_length=1)
    purpose: str = ""
    source_refs: list[str] = Field(default_factory=list)


class SoundSuggestion(PipelineModel):
    segment_id: str | None = Field(default=None, min_length=1)
    kind: str = Field(min_length=1)
    description: str = Field(min_length=1)
    intensity: DynamicLevel | None = None
    source_refs: list[str] = Field(default_factory=list)


RhythmBeat = StrongDynamicEvent | Annotated[str, Field(min_length=1)]


class RhythmOutput(PipelineModel):
    target_duration_sec: float = Field(gt=0)
    artifact_meta: ArtifactMeta
    timeline: list[RhythmSegment] = Field(default_factory=list)
    core_payoff: RhythmBeat
    small_beats: list[RhythmBeat] = Field(default_factory=list)
    strong_dynamic_budget: StrongDynamicBudget
    foreshadowing: list[str] = Field(default_factory=list)
    open_space: list[str] = Field(default_factory=list)
    dialogue_suggestions: list[DialogueSuggestion] = Field(default_factory=list)
    sound_suggestions: list[SoundSuggestion] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


StageArtifactContent = SourceAnalysisOutput | CharacterBiosOutput | RhythmOutput | dict[str, Any]


class StageArtifactEnvelope(PipelineModel):
    meta: ArtifactMeta
    content: StageArtifactContent


class StageArtifactDraft(StageArtifactEnvelope):
    stage_key: StageKey | None = None
