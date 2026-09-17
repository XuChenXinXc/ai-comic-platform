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
    key_props: list[str] = Field(default_factory=list)
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
    scene_no: int = Field(ge=1)
    scene_name: NonEmptyText
    time_weather: NonEmptyText
    characters: list[NonEmptyText]
    props: list[NonEmptyText] = Field(default_factory=list)
    source_refs: list[NonEmptyText] = Field(default_factory=list)
    objective: NonEmptyText
    action: NonEmptyText
    dialogue: list[ScreenplayDialogue]
    narration: list[NonEmptyText]
    emotion: NonEmptyText
    transition_to_next: NonEmptyText
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

    @model_validator(mode="after")
    def validate_scene_contract(self) -> ScreenplayScene:
        # 重复项去重并保留首个，而不是整批抛错：模型偶发重复不应让整份剧本保存失败。
        self.characters = list(dict.fromkeys(self.characters))
        self.props = list(dict.fromkeys(self.props))
        self.source_refs = list(dict.fromkeys(self.source_refs))
        undeclared_speakers = sorted(
            {item.speaker for item in self.dialogue if item.speaker not in self.characters}
        )
        if undeclared_speakers:
            self.characters.extend(undeclared_speakers)
        if self.target_duration_sec is None:
            dialogue_chars = sum(len(item.text) for item in self.dialogue)
            narration_chars = sum(len(item) for item in self.narration)
            action_units = [
                item
                for item in re.split(r"[。！？；;\n]", self.action)
                if item.strip()
            ]
            estimated = math.ceil((dialogue_chars + narration_chars) / 2.5)
            estimated += min(30, len(action_units) * 3)
            self.target_duration_sec = max(15, min(600, estimated))
        return self


class ScreenplayOutput(OutputModel):
    title: NonEmptyText
    logline: NonEmptyText
    scenes: list[ScreenplayScene] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_scene_sequence(self) -> ScreenplayOutput:
        scene_numbers = [scene.scene_no for scene in self.scenes]
        expected = list(range(1, len(self.scenes) + 1))
        if scene_numbers != expected:
            raise ValueError(
                f"场次编号必须从 1 开始连续且按顺序排列，当前为 {scene_numbers}"
            )
        return self


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
    # 视觉语义重解释治理（Phase 2）：角色只输出身份事实，不再生成布光/氛围描述。
    # 以下两个字段仅为兼容存量档案保留；若历史数据存在内容，只允许光源方向/时间等
    # 事实性描述，不得写入布光、氛围、造型气质或妆容画法（表达方式由项目统一画风决定）。
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
    aliases: list[str] = Field(default_factory=list)
    concept: str = ""
    worldview: str = ""
    indoor_outdoor: str = ""
    place_type: str = ""
    # 场景族：同一实体地点按视觉空间拆出的多个场景共享同一个 scene_family
    # （如「古城墙」）；family_anchor 标记族内锚点场景（每族恰好一个）。
    # 族成员生成设定图时以锚点场景的当前场景图为参考，保证同一建筑群外观一致。
    scene_family: str = ""
    family_anchor: bool = False
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
    # 固有颜色事实（如「门为暗朱红」）：只记录墙体、家具与陈设的固有色，
    # 不写色彩气质、色彩氛围等自由艺术指导（表达方式由项目统一画风决定）。
    main_color: str = ""
    accent_color: str = ""
    # 以下字段为兼容存量档案保留，不再生成自由艺术指导内容；
    # 场景光线只允许受控事实：time_of_day（昼/夜）、weather（天气）、
    # light_direction（光源方向）。旧字段如出现内容，仅作事实性参考。
    color_quality: str = ""
    light_direction: str = ""
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
    aliases: list[str] = Field(default_factory=list)
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
    # 视觉语义重解释治理（Phase 2）：材质只记录结构事实（金属/玻璃/塑料/屏幕是否
    # 透明、是否发光、光滑或哑光、颜色、磨损），不写渲染表达（真实PBR反射、镜面高光、
    # 透明折射、逼真材质贴图等由项目统一画风决定）。
    material: str = ""
    secondary_material: str = ""
    color: str = ""
    accent_color: str = ""
    # 受控词表：光滑/哑光/粗糙/透明/发光/带磨损等事实，不写渲染画法。
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

    @model_validator(mode="after")
    def validate_catalog_contract(self) -> AssetExtractionOutput:
        for asset_type, items in (
            ("角色", self.characters),
            ("场景", self.scenes),
            ("物品", self.props),
        ):
            labels: dict[str, str] = {}
            for item in items:
                canonical = re.sub(r"[\W_]+", "", item.name.casefold())
                if canonical in labels:
                    raise ValueError(
                        f"{asset_type}素材名称重复：{item.name} 与 {labels[canonical]}"
                    )
                labels[canonical] = item.name
                for alias in item.aliases:
                    normalized = re.sub(r"[\W_]+", "", alias.casefold())
                    if not normalized:
                        continue
                    owner = labels.get(normalized)
                    if owner is not None and owner != item.name:
                        raise ValueError(
                            f"{asset_type}素材名称或别名冲突：{alias} 同时指向 "
                            f"{owner} 与 {item.name}"
                        )
                    labels[normalized] = item.name
        members_by_family: dict[str, int] = {}
        anchors_by_family: dict[str, int] = {}
        for scene in self.scenes:
            family = scene.scene_family.strip()
            if not family:
                continue
            members_by_family[family] = members_by_family.get(family, 0) + 1
            if scene.family_anchor:
                anchors_by_family[family] = anchors_by_family.get(family, 0) + 1
        duplicated_anchors = [
            family for family, count in anchors_by_family.items() if count > 1
        ]
        if duplicated_anchors:
            raise ValueError(
                "同一场景族只能有一个锚点："
                + "、".join(sorted(duplicated_anchors))
            )
        missing_anchors = [
            family
            for family, member_count in members_by_family.items()
            if member_count > 1 and anchors_by_family.get(family, 0) == 0
        ]
        if missing_anchors:
            raise ValueError(
                "同批次包含多个场景族成员时必须指定一个锚点："
                + "、".join(sorted(missing_anchors))
            )
        return self


class MissingAsset(OutputModel):
    """分镜提取时发现素材库中不存在的角色/场景/道具，交由补素材流程处理。"""

    asset_type: Literal["scene", "character", "prop"]
    reason: str = ""
    source_ref: str = ""
    suggested_name: NonEmptyText
    suggested_aliases: list[str] = Field(default_factory=list)


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
    # 临时剧情覆盖契约：分镜提取阶段由模型声明本镜承接的剧本节拍，
    # 后端验证完整性后写入 source_ref；无需新增数据库列。
    coverage_refs: list[NonEmptyText] = Field(default_factory=list)
    scene_name: NonEmptyText
    # 允许为空：剧情发生地无法对应素材库中任何场景时留空，并通过
    # missing_assets 报告缺失，由后端补素材后重新绑定，禁止强行绑定近似素材。
    scene_asset_id: str = ""
    character_names: list[NonEmptyText]
    character_asset_ids: list[NonEmptyText]
    prop_names: list[NonEmptyText]
    prop_asset_ids: list[NonEmptyText]
    # 素材库中不存在的角色/场景/道具：不得静默错误绑定，必须列出供补素材流程使用。
    missing_assets: list[MissingAsset] = Field(default_factory=list)
    positions: list[ShotPosition]
    start_state: NonEmptyText
    action: NonEmptyText
    expressions: list[ShotExpression]
    dialogue: list[ScreenplayDialogue]
    narration: str = ""
    shot_type: NonEmptyText
    camera_angle: NonEmptyText
    # 机位固定/末镜等场景下模型常输出空值，允许为空以免整批提取失败。
    camera_move: str = ""
    duration_sec: int = Field(ge=MIN_SHOT_DURATION_SEC, le=MAX_SHOT_DURATION_SEC)
    end_state: str = ""
    next_hook: str = ""
    frame_description: NonEmptyText
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

    @field_validator("scene_asset_id", mode="before")
    @classmethod
    def normalize_empty_scene_asset_id(cls, value: Any) -> Any:
        """None/空白场景 ID 归一化为空字符串：表示该镜场景缺失，
        由 missing_assets 报告，而不是解析失败。"""
        if isinstance(value, str):
            return value.strip()
        if not value:
            return ""
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

    def timeline_contract_warning(self) -> str:
        """校验视频提示词只有一套从 0 秒连续覆盖到镜头时长的时间轴。"""
        matches = list(self._TIMELINE_PATTERN.finditer(self.video_prompt or ""))
        if not matches:
            return "video_prompt 缺少从 0 秒开始的连续时间轴"
        segments = [
            (float(match.group(1)), float(match.group(2))) for match in matches
        ]
        if abs(segments[0][0]) > 0.01:
            return "video_prompt 时间轴必须从 0 秒开始"
        for index, (start, end) in enumerate(segments):
            if end <= start:
                return "video_prompt 时间分段结束时间必须大于开始时间"
            if index > 0 and abs(start - segments[index - 1][1]) > 0.01:
                return "video_prompt 时间分段必须连续且不得重叠或留空档"
        if abs(segments[-1][1] - float(self.duration_sec)) > 0.01:
            return (
                "video_prompt 时间轴末段必须结束于 duration_sec "
                f"({self.duration_sec} 秒)"
            )
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
                self.timeline_contract_warning(),
            )
            if warning
        ]


class ShotExtractionOutput(OutputModel):
    shots: list[ShotOutput] = Field(min_length=1)


class ShotImagePromptOutput(OutputModel):
    prompt: NonEmptyText
    negative_prompt: NonEmptyText


class ShotVideoPromptOutput(OutputModel):
    prompt: NonEmptyText
    negative_prompt: NonEmptyText


class ShotVideoPromptOptimizeOutput(OutputModel):
    plain_text_output_field: ClassVar[str] = "prompt"

    prompt: NonEmptyText


class KeyframeQaOutput(OutputModel):
    """关键帧视觉质检结果（视觉模型评估）。"""

    passed: bool
    score: int = Field(ge=0, le=100)
    face_drift: bool = False
    style_violation: bool = False
    issues: list[str] = Field(default_factory=list)


DirectorComposition = Literal[
    "centered",
    "left-weighted",
    "right-weighted",
    "environment-dominant",
    "negative-space",
    "foreground-obstructed",
    "layered-depth",
]


class DirectorAudienceFocusOutput(OutputModel):
    """观众第一 / 第二 / 第三视觉注意落点。"""

    first: NonEmptyText
    second: str = ""
    third: str = ""


class DirectorMicroPerformanceOutput(OutputModel):
    """单个自然微动作：触发时机 + 具体动作 + 来源。"""

    trigger: str = ""
    action: NonEmptyText
    source: str = ""


class DirectorDialogueBindingOutput(OutputModel):
    """结构化对白绑定；真人角色 requiresLipSync=true，设备/广播/电话等为 false。"""

    speaker: NonEmptyText
    text: NonEmptyText
    startTime: float = Field(ge=0)
    endTime: float = Field(gt=0)
    requiresLipSync: bool = True
    voiceSource: Literal["character", "device", "broadcast", "phone", "offscreen"] = "character"

    @model_validator(mode="after")
    def validate_time_range(self) -> DirectorDialogueBindingOutput:
        if self.endTime <= self.startTime:
            raise ValueError("endTime 必须大于 startTime")
        return self


class DirectorTimelineSegmentOutput(OutputModel):
    """唯一绝对时间轴的一段。"""

    start: float = Field(ge=0)
    end: float = Field(gt=0)
    action: NonEmptyText

    @model_validator(mode="after")
    def validate_time_range(self) -> DirectorTimelineSegmentOutput:
        if self.end <= self.start:
            raise ValueError("end 必须大于 start")
        return self


class ShotDirectorPlanOutput(OutputModel):
    """导演设计层输出：只决定「如何呈现」，不修改剧情 / 连续性 / 资产等既有事实。"""

    shotPurpose: NonEmptyText
    audienceFocus: DirectorAudienceFocusOutput
    composition: DirectorComposition
    foreground: str = ""
    midground: str = ""
    background: str = ""
    cameraMotivation: str = ""
    performance: str = ""
    microPerformance: list[DirectorMicroPerformanceOutput] = Field(
        default_factory=list, max_length=2
    )
    environmentMotion: str = ""
    visualTone: list[str] = Field(default_factory=list)
    timeline: list[DirectorTimelineSegmentOutput]
    dialogueBindings: list[DirectorDialogueBindingOutput] = Field(default_factory=list)


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
