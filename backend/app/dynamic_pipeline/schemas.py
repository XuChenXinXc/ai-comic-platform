from __future__ import annotations

from typing import Any, Literal

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
