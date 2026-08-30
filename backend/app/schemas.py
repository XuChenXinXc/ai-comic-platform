from __future__ import annotations

import json
import math
import re
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
MIN_SHOT_DURATION_SEC = 5
MAX_SHOT_DURATION_SEC = 15


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
    # 本场预估成片时长（秒）：由剧本阶段按台词量与动作密度估算，
    # 分镜阶段据此换算每场镜头数上限，从源头控制分镜总量。
    target_duration_sec: int | None = Field(default=None, ge=15, le=600)

    _DURATION_ALIASES: ClassVar[tuple[str, ...]] = (
        "estimated_duration_sec",
        "estimated_seconds",
        "target_duration",
        "duration_sec",
    )

    @staticmethod
    def _parse_seconds(value: Any) -> int | None:
        """把任意来源的秒数收敛为 15-600 内的安全整数；无法解析时返回 None。"""
        if isinstance(value, bool):
            return None
        if isinstance(value, int | float):
            seconds = float(value)
        elif isinstance(value, str):
            match = re.search(r"\d+(?:\.\d+)?", value)
            if match is None:
                return None
            seconds = float(match.group())
        else:
            return None
        if seconds <= 0:
            return None
        parsed = int(round(seconds))
        return parsed if 15 <= parsed <= 600 else None

    @model_validator(mode="before")
    @classmethod
    def normalize_target_duration(cls, value: Any) -> Any:
        """吸收剧本模型的时长别名输出并统一收紧为合法值，坏值降级为 None。"""
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        current = normalized.get("target_duration_sec")
        if _has_content(current):
            resolved = cls._parse_seconds(current)
        else:
            resolved = None
            for alias in cls._DURATION_ALIASES:
                if alias in normalized and _has_content(normalized.get(alias)):
                    resolved = cls._parse_seconds(normalized[alias])
                    break
        if resolved is not None:
            normalized["target_duration_sec"] = resolved
        else:
            normalized.pop("target_duration_sec", None)
        for alias in cls._DURATION_ALIASES:
            normalized.pop(alias, None)
        return normalized


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

    @model_validator(mode="after")
    def validate_prop_importance(self) -> PropAssetOutput:
        if self.importance != "core":
            raise ValueError("物品素材 importance 必须为 core")
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


ShotTransitionFromPrev = Literal[
    "continuous",
    "elliptical",
    "same_scene_cut",
    "scene_change",
]


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
    duration_sec: int = Field(ge=MIN_SHOT_DURATION_SEC, le=MAX_SHOT_DURATION_SEC)
    end_state: str
    next_hook: str
    frame_description: str
    image_prompt: NonEmptyText
    video_prompt: NonEmptyText
    negative_prompt: NonEmptyText
    # 与上一镜的衔接类型：由剧情动作是否连续决定，不能用场景素材是否相同来推断。
    # 只有 continuous 才允许把上一镜末帧作为参考图下发。
    transition_from_prev: ShotTransitionFromPrev = "scene_change"

    @field_validator("character_asset_ids", "prop_asset_ids")
    @classmethod
    def validate_unique_asset_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("素材 ID 不能重复")
        return value

    # 以下两项为「质量警告」而非硬校验：提取整批生成，若抛异常会导致
    # 一镜不合规、整批重来。改为在保存阶段收集警告，由用户决定是否修复。
    POSTURE_VERBS: ClassVar[tuple[str, ...]] = (
        "站起",
        "起身",
        "坐下",
        "转身",
        "走出",
        "离开",
        "站立",
    )
    UNFINISHED_MARKERS: ClassVar[tuple[str, ...]] = ("准备", "即将", "尚未", "仍")
    _DIALOGUE_SPEECH_MARKERS: ClassVar[tuple[str, ...]] = (
        "说",
        "讲",
        "问",
        "答",
        "开口",
        "道",
        "念",
        "喊",
        "叫",
        "读",
        "唱",
        "低语",
        "自语",
        "回应",
        "回答",
        "反问",
        "念叨",
        "讲述",
    )
    _NON_SPEECH_ACTION_MARKERS: ClassVar[tuple[str, ...]] = (
        "走",
        "跑",
        "站",
        "坐",
        "躺",
        "蹲",
        "起身",
        "站起",
        "坐下",
        "转身",
        "伸手",
        "抬起",
        "放下",
        "拿起",
        "捡起",
        "拾起",
        "握住",
        "攥住",
        "推开",
        "拉门",
        "环顾",
        "打量",
        "看向",
        "望向",
        "低头",
        "抬头",
        "点头",
        "摇头",
        "挥手",
        "前进",
        "离开",
        "进入",
        "翻找",
        "拿出",
        "抽出",
        "摸向",
        "移向",
        "放回",
    )
    _TIMELINE_PATTERN: ClassVar[re.Pattern[str]] = re.compile(
        r"(?:^|[。；;，,\n])\s*(\d+(?:\.\d+)?)\s*[-–—~－至]\s*(\d+(?:\.\d+)?)\s*秒"
    )

    @classmethod
    def _is_dialogue_focused(cls, action: str) -> bool:
        """纯对白镜头（没有明确动作动词）按 2.5 字/秒计算，否则按 2 字/秒+20% 余量。"""
        action_text = str(action or "").strip()
        if not action_text:
            return True
        return not any(marker in action_text for marker in cls._NON_SPEECH_ACTION_MARKERS)

    @classmethod
    def dialogue_rate_for(cls, action: Any) -> float:
        """返回当前镜头用于对白承载校验的有效语速。"""
        return 2.5 if cls._is_dialogue_focused(str(action or "")) else 2 * 0.8

    @classmethod
    def dialogue_capacity_for(cls, duration_sec: float, action: Any) -> int:
        """返回指定时长和动作条件下可承载的对白字数。"""
        return int(float(duration_sec) * cls.dialogue_rate_for(action))

    @classmethod
    def dialogue_char_count(cls, dialogue: Any) -> int:
        if not isinstance(dialogue, list):
            return 0
        total = 0
        for item in dialogue:
            if isinstance(item, dict):
                text = item.get("text")
            else:
                text = getattr(item, "text", "")
            total += len(str(text or ""))
        return total

    @classmethod
    def dialogue_duration_warning_for(
        cls,
        dialogue: Any,
        duration_sec: float,
        action: str,
    ) -> str:
        """返回对白承载警告；超过单镜上限时明确要求拆镜。"""
        total_chars = cls.dialogue_char_count(dialogue)
        if total_chars <= 0:
            return ""
        usable_rate = cls.dialogue_rate_for(action)
        duration = float(duration_sec)
        threshold = cls.dialogue_capacity_for(duration, action)
        if total_chars <= threshold:
            return ""
        rate_label = (
            "2.5字/秒（纯对白）"
            if cls._is_dialogue_focused(str(action or ""))
            else "2字/秒+20%动作余量"
        )
        max_threshold = cls.dialogue_capacity_for(MAX_SHOT_DURATION_SEC, action)
        recommended_sec = math.ceil(total_chars / usable_rate)
        if recommended_sec <= MAX_SHOT_DURATION_SEC:
            return (
                f"{duration:g}秒内台词共{total_chars}字，超出建议阈值{threshold}字"
                f"（按{rate_label}计算）。建议延长至{recommended_sec}秒"
                f"（不超过{MAX_SHOT_DURATION_SEC}秒），或拆分台词。"
            )
        required_shots = math.ceil(total_chars / max_threshold)
        return (
            f"{duration:g}秒内台词共{total_chars}字，超过单镜最长{MAX_SHOT_DURATION_SEC}秒"
            f"可承载的{max_threshold}字（按{rate_label}计算），"
            f"必须拆分为至少{required_shots}个分镜。"
        )

    @classmethod
    def timeline_conflict_warning(cls, video_prompt: str) -> str:
        """检测时间轴是否出现多套划分或分段重叠，返回警告文本。"""
        matches = list(cls._TIMELINE_PATTERN.finditer(video_prompt or ""))
        if len(matches) < 2:
            return ""
        segments: list[tuple[float, float]] = []
        for match in matches:
            try:
                start = float(match.group(1))
                end = float(match.group(2))
            except ValueError:
                continue
            segments.append((start, end))
        for index in range(1, len(segments)):
            if segments[index][0] < segments[index - 1][1] - 0.01:
                return "video_prompt 出现多套时间轴或时间分段重叠，请统一为单套覆盖全程的连续时间轴"
        return ""

    def end_state_warning(self) -> str:
        """end_state 的姿态变化在 video_prompt 时间轴里是否有对应调度。

        对应 shot_extract.md 第 1 条。只做关键词级检查（无法做语义级），
        挡住最典型的「时间轴只写到蹲着查看、end_state 却写站起身」。
        无问题返回空字符串。
        """
        end_state = self.end_state or ""
        hit = [verb for verb in self.POSTURE_VERBS if verb in end_state]
        if not hit:
            return ""
        if any(verb in (self.video_prompt or "") for verb in self.POSTURE_VERBS):
            return ""
        if any(marker in end_state for marker in self.UNFINISHED_MARKERS):
            return ""
        return (
            f"end_state 含姿态动词「{hit[0]}」，但 video_prompt 时间轴没有对应调度。"
            f"建议在时间轴补一段该动作（并相应增加 duration_sec），"
            f"或把 end_state 改为「仍…，准备{hit[0]}」的未完成表述。"
        )

    def dialogue_duration_warning(self) -> str:
        """台词总字数是否超出时长承载能力。

        纯对白镜头按 2.5 字/秒；有动作镜头按 2 字/秒并留 20% 动作余量。
        无问题返回空字符串。
        """
        return self.dialogue_duration_warning_for(
            self.dialogue,
            self.duration_sec,
            self.action,
        )

    def quality_warnings(self) -> list[str]:
        """收集本镜所有质量警告，供提取保存阶段回报给用户。"""
        return [
            warning
            for warning in (
                self.end_state_warning(),
                self.dialogue_duration_warning(),
                self.timeline_conflict_warning(self.video_prompt),
            )
            if warning
        ]


class ShotExtractionOutput(OutputModel):
    shots: list[ShotOutput]


class ShotImagePromptOutput(OutputModel):
    prompt: NonEmptyText
    negative_prompt: NonEmptyText


class ShotVideoPromptOutput(OutputModel):
    prompt: NonEmptyText
    negative_prompt: NonEmptyText


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
    kind: Literal["text", "image", "video", "tts"]
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
    chapter_id: str | None = None
    workflow_run_id: str | None = None
    workflow_stage_id: str | None = None
    idempotency_key: str | None = None
    job_type: str
    target_type: str = ""
    target_id: str | None = None
    batch_id: str | None = None
    provider_kind: str = ""
    provider_id: str | None = None
    input_data: dict[str, Any] = Field(default_factory=dict)
    input_signature: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    progress_total: int = Field(default=1, ge=1)


class JobRead(BaseModel):
    id: str
    project_id: str
    chapter_id: str | None = None
    workflow_run_id: str | None = None
    workflow_stage_id: str | None = None
    idempotency_key: str | None = None
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
    input_signature: str | None = None
    depends_on: list[str] = Field(default_factory=list)
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


# ── 连续性审校 ──────────────────────────────────────────────────────────────


class ShotContinuityFix(OutputModel):
    """单条字段修正建议。"""

    shot_no: int
    field: Literal[
        "end_state",
        "start_state",
        "next_hook",
        "action",
        "narration",
        "transition_from_prev",
    ]
    current_value: str
    suggested_value: NonEmptyText
    reason: NonEmptyText


class ShotContinuityTransition(OutputModel):
    """建议在两镜之间插入的过渡镜描述。"""

    insert_after_shot_no: int
    action: NonEmptyText
    start_state: NonEmptyText
    end_state: NonEmptyText
    duration_sec: int = Field(
        default=MIN_SHOT_DURATION_SEC,
        ge=MIN_SHOT_DURATION_SEC,
        le=MAX_SHOT_DURATION_SEC,
    )


class ShotContinuityPairOutput(OutputModel):
    """单对相邻分镜的连续性审校结果。"""

    prev_shot_no: int
    next_shot_no: int
    is_continuous: bool
    issues: list[str] = Field(default_factory=list)
    fixes: list[ShotContinuityFix] = Field(default_factory=list)
    transition: ShotContinuityTransition | None = None


class ShotContinuityCheckOutput(OutputModel):
    """连续性审校批量结果（按相邻对返回）。"""

    pairs: list[ShotContinuityPairOutput]


class RefinedPromptChange(OutputModel):
    """单镜提示词的改动说明，供用户复核。"""

    shot_no: int
    reason: NonEmptyText


class BatchRefinedPromptsOutput(OutputModel):
    """全量提示词连续性精修结果：prompts 顺序与输入分镜一一对应。"""

    prompts: list[NonEmptyText]
    changes: list[RefinedPromptChange] = Field(default_factory=list)


class ShotQualityFixOutput(OutputModel):
    """单镜质量修复结果：只返回被修改的字段，未修改的字段应缺省。"""

    duration_sec: int | None = Field(
        default=None,
        ge=MIN_SHOT_DURATION_SEC,
        le=MAX_SHOT_DURATION_SEC,
    )
    end_state: str | None = None
    video_prompt: str | None = None
    dialogue: list[ScreenplayDialogue] | None = None
    changed_fields: list[Literal["duration_sec", "end_state", "video_prompt", "dialogue"]] = Field(
        default_factory=list
    )
    reason: str = ""

    @model_validator(mode="after")
    def validate_has_change(self) -> ShotQualityFixOutput:
        if not self.changed_fields:
            raise ValueError("必须至少修改一个字段并在 changed_fields 中声明")
        for name in self.changed_fields:
            if getattr(self, name) is None:
                raise ValueError(f"changed_fields 声明修改了 {name}，但未提供该字段的新值")
        return self
