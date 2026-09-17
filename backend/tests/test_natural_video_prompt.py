"""自然语言视频提示词生成器与素材绑定校验的测试。

覆盖场景：

- 单角色 + 场景；单角色 + 场景 + 道具；
- 双角色；多角色左右位置变化但素材引用身份不变；
- 同一道具跨镜头保持同一素材身份（asset_id 不变，编号可变）；
- 场景切换；关键帧存在 / 不存在；
- 用户手动修改 Prompt 后再次优化（引用集合一致）；
- 素材顺序改变但 assetId 不变；@ImageN 编号不是固定语义顺序；
- 优化前后素材 identity 完全一致；校验失败时拒绝返回。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from app.db import Database
from app.schemas import ShotVideoPromptOptimizeOutput
from app.services.chapter_service import ChapterService
from app.services.errors import ServiceError
from app.services.file_service import FileService
from app.services.job_service import JobService
from app.services.natural_video_prompt import (
    _FORBIDDEN_TEMPLATE_WORDS,
    _dedupe_composition_clauses,
    _match_tokens_by_names,
    parse_reference_anchors,
    render_natural_video_prompt,
    validate_natural_style,
    validate_prompt_references,
)
from app.services.project_service import ProjectService
from app.services.prompt_service import compile_video_prompt
from app.services.shot_service import (
    ShotService,
    _collect_prompt_reference_tokens,
    _extract_protocol_tail,
    _validate_prompt_reference_identity,
)

_STYLE = "都市温情二维国漫"
_CONTEXT = {
    "transition_from_prev": "continuous",
    "prev_end_state": "徐尘心站在工位前，双肩包还挂在肩上。",
    "opening_frame": "徐尘心站在旋转门前。",
    "ending_frame": "徐尘心侧身迈入门内。",
    "characters": ["徐尘心"],
    "scene_name": "中关村办公区一楼大厅",
    "props": [],
    "positions": "徐尘心：画面左侧，身体朝向旋转门",
    "expressions": "徐尘心：平静专注",
    "shot_type": "中景",
    "camera_angle": "平视",
    "camera_move": "固定",
    "focus": "徐尘心走近旋转门推门进入",
    "composition": "旋转门位于画面右侧",
    "sound_effects": "旋转门转动的轻响",
}


def _render(
    *,
    anchors: list[Any] | None = None,
    video_prompt: str = "0-3秒：走到门前；3-5秒：抬手；5-7秒：推门。",
    style: str = _STYLE,
    **overrides: Any,
) -> str:
    context = {**_CONTEXT, **overrides}
    return compile_video_prompt(
        style_prompt=style,
        aspect_ratio="16:9",
        video_prompt=video_prompt,
        duration_sec=7,
        anchors=anchors if anchors is not None else [],
        shot_context=context,
        negative_prompt="人物变形、穿模",
    )


def test_single_character_scene_keeps_reference_identity() -> None:
    prompt = _render(
        anchors=[
            "@Image1：当前分镜关键帧，锁定构图、人物左右关系和起始站位。",
            "@Image2：徐尘心，锁定角色身份与服装。",
            "@Image3：中关村办公区，锁定场景空间结构和光线。",
        ]
    )

    assert "以 @Image1 作为这一镜的起始画面" in prompt
    assert "徐尘心的外貌、发型和当天服装沿用 @Image2 的参考素材" in prompt
    assert "场景结构、陈设与光线沿用 @Image3 的参考素材" in prompt
    tokens = validate_prompt_references(
        prompt, parse_reference_anchors(
            [
                "@Image1：当前分镜关键帧。",
                "@Image2：徐尘心，锁定角色身份与服装。",
                "@Image3：中关村办公区，锁定场景空间结构和光线。",
            ]
        )
    )
    assert tokens.ok


def test_single_character_scene_prop_embeds_prop_reference() -> None:
    prompt = _render(
        props=["工位仙人掌"],
        anchors=[
            "@Image1：当前分镜关键帧。",
            "@Image2：徐尘心，锁定角色身份与服装。",
            "@Image3：中关村办公区，锁定场景空间结构和光线。",
            "@Image4：工位仙人掌，锁定物品外观、材质和结构。",
        ],
    )

    assert "本镜道具：工位仙人掌" in prompt
    assert "工位仙人掌沿用 @Image4 的外观与材质" in prompt
    # 未绑定的引用不能凭空出现，也不能少
    found = set()
    for token in ("@Image1", "@Image2", "@Image3", "@Image4"):
        assert token in prompt
        found.add(token)
    assert "@Image5" not in prompt


def test_two_characters_bind_tokens_by_asset_name_not_order() -> None:
    """角色 token 绑定按素材名匹配，不依赖角色在数组/画面中的顺序。"""
    prompt = _render(
        characters=["徐尘心", "林舟"],
        positions="林舟：画面右侧，背对徐尘心；徐尘心：画面左侧，面向林舟",
        anchors=[
            "@Image1：当前分镜关键帧。",
            "@Image2：林舟，锁定角色身份与服装。",
            "@Image3：徐尘心，锁定角色身份与服装。",
        ],
    )

    # 编号顺序（林舟先）与 characters 顺序（徐尘心先）相反，仍按名字绑定
    assert "徐尘心的外貌、发型和当天服装沿用 @Image3 的参考素材" in prompt
    assert "林舟的外貌、发型和当天服装沿用 @Image2 的参考素材" in prompt


def test_swapped_positions_do_not_swap_reference_tokens() -> None:
    """人物左右位置互换不得交换素材引用。"""
    left_right = _render(
        characters=["徐尘心", "林舟"],
        positions="林舟：画面左侧；徐尘心：画面右侧",
        anchors=[
            "@Image1：当前分镜关键帧。",
            "@Image2：徐尘心，锁定角色身份与服装。",
            "@Image3：林舟，锁定角色身份与服装。",
        ],
    )
    right_left = _render(
        characters=["徐尘心", "林舟"],
        positions="徐尘心：画面左侧；林舟：画面右侧",
        anchors=[
            "@Image1：当前分镜关键帧。",
            "@Image2：徐尘心，锁定角色身份与服装。",
            "@Image3：林舟，锁定角色身份与服装。",
        ],
    )

    assert "徐尘心" in left_right and "徐尘心" in right_left
    assert left_right.index("@Image2") < left_right.index("林舟的外貌")
    assert right_left.index("@Image2") < right_left.index("林舟的外貌")
    assert "林舟的外貌、发型和当天服装沿用 @Image3" in left_right
    assert "林舟的外貌、发型和当天服装沿用 @Image3" in right_left


def test_same_prop_across_shots_keeps_asset_id_when_numbering_changes() -> None:
    """同一道具跨镜头：token 编号可以变，asset_id 必须保持同一素材身份。"""
    shot_a_tokens = parse_reference_anchors(
        [
            {"token": "@Image1", "role": "keyframe", "label": "当前分镜关键帧"},
            {
                "token": "@Image2",
                "role": "prop_identity",
                "label": "工位仙人掌",
                "asset_id": "prop-1",
            },
            {
                "token": "@Image3",
                "role": "scene_layout",
                "label": "中关村办公区",
                "asset_id": "scene-1",
            },
        ]
    )
    shot_b_tokens = parse_reference_anchors(
        [
            {"token": "@Image1", "role": "keyframe", "label": "当前分镜关键帧"},
            {
                "token": "@Image2",
                "role": "scene_layout",
                "label": "中关村办公区",
                "asset_id": "scene-1",
            },
            {
                "token": "@Image3",
                "role": "prop_identity",
                "label": "工位仙人掌",
                "asset_id": "prop-1",
            },
        ]
    )

    prompt_a = render_natural_video_prompt(
        style_prompt=_STYLE,
        aspect_ratio="16:9",
        duration_sec=5,
        action_flow="徐尘心走向工位。",
        shot_context={**_CONTEXT, "props": ["工位仙人掌"], "transition_from_prev": "scene_change"},
        dialogue_entries=[],
        tokens=shot_a_tokens,
    )
    prompt_b = render_natural_video_prompt(
        style_prompt=_STYLE,
        aspect_ratio="16:9",
        duration_sec=5,
        action_flow="徐尘心走向工位。",
        shot_context={**_CONTEXT, "props": ["工位仙人掌"], "transition_from_prev": "scene_change"},
        dialogue_entries=[],
        tokens=shot_b_tokens,
    )

    assert "工位仙人掌沿用 @Image2 的外观与材质" in prompt_a
    assert "工位仙人掌沿用 @Image3 的外观与材质" in prompt_b
    # 两镜中仙人掌的 asset_id 都是 prop-1（编号不同但素材身份不变）
    assert next(t for t in shot_a_tokens if t.role == "prop_identity").asset_id == "prop-1"
    assert next(t for t in shot_b_tokens if t.role == "prop_identity").asset_id == "prop-1"
    assert validate_prompt_references(prompt_a, shot_a_tokens).ok
    assert validate_prompt_references(prompt_b, shot_b_tokens).ok


def test_scene_change_transition_does_not_claim_continuity() -> None:
    prompt = _render(
        transition_from_prev="scene_change",
        prev_end_state="上一场景的收尾状态",
    )

    assert "不承接上一镜的构图" in prompt
    assert "直接接在上一镜之后" not in prompt


def test_continuous_transition_natural_carry_over() -> None:
    prompt = _render()

    assert "这一镜直接接在上一镜之后" in prompt
    assert "徐尘心站在工位前，双肩包还挂在肩上" in prompt


def test_elliptical_transition_mentions_elision() -> None:
    prompt = _render(transition_from_prev="elliptical")

    assert "省略了一段过程" in prompt
    assert "人物的服装与随身物品保持连续" in prompt


def test_missing_keyframe_token_is_detected() -> None:
    tokens = parse_reference_anchors(["@Image1：当前分镜关键帧。"])
    prompt = "徐尘心推开旋转门。"

    validation = validate_prompt_references(prompt, tokens, require_keyframe=True)
    assert not validation.ok
    assert any("关键帧" in issue for issue in validation.issues)


def test_unknown_reference_token_is_detected() -> None:
    tokens = parse_reference_anchors(["@Image1：当前分镜关键帧。"])
    prompt = "以 @Image1 作为起始画面。角色参考 @Image2。"

    validation = validate_prompt_references(prompt, tokens)
    assert not validation.ok
    assert any("@Image2" in issue for issue in validation.issues)


def test_duplicate_token_binding_is_detected() -> None:
    tokens = parse_reference_anchors(
        [
            "@Image1：徐尘心，锁定角色身份与服装。",
            "@Image1：中关村办公区，锁定场景空间结构和光线。",
        ]
    )
    prompt = "参考 @Image1。"

    validation = validate_prompt_references(prompt, tokens)
    assert not validation.ok
    assert any("多个素材" in issue for issue in validation.issues)


def test_reference_numbering_is_not_fixed_semantics() -> None:
    """自由制作连续承接：@Image1 是上一镜结束帧、@Image2 才是关键帧。"""
    prompt = _render(
        anchors=[
            "@Image1：上一镜结束帧，作为本镜第0秒起始帧（严格从该画面开始）。",
            "@Image2：当前分镜关键帧，作为本镜动作结束的目标帧。",
            "@Image3：徐尘心，锁定角色身份与服装。",
        ]
    )

    assert "画面从 @Image1（上一镜的结束画面）自然进入" in prompt
    assert "向 @Image2 展示的目标画面发展" in prompt
    assert "徐尘心的外貌、发型和当天服装沿用 @Image3" in prompt
    # 校验必须认 @Image1 为上一镜结束帧而不是关键帧
    tokens = parse_reference_anchors(
        [
            "@Image1：上一镜结束帧。",
            "@Image2：当前分镜关键帧。",
            "@Image3：徐尘心，锁定角色身份与服装。",
        ]
    )
    keyframe = next(t for t in tokens if t.role == "keyframe")
    assert keyframe.token == "@Image2"
    assert validate_prompt_references(prompt, tokens).ok


def test_mechanical_timeline_is_naturalized() -> None:
    prompt = _render()

    assert "0-3秒" not in prompt
    assert "3-5秒" not in prompt
    assert "5-7秒" not in prompt
    assert "开头走到门前，随后抬手，接近结尾时推门。" in prompt


def test_output_avoids_template_style_and_banned_words() -> None:
    prompt = _render(
        props=["双肩包"],
        anchors=[
            "@Image1：当前分镜关键帧。",
            "@Image2：徐尘心，锁定角色身份与服装。",
            "@Image3：中关村办公区，锁定场景空间结构和光线。",
            "@Image4：双肩包，锁定物品外观、材质和结构。",
        ],
    )

    assert "【" not in prompt
    # 协议尾部（Negative / Motion style lock / Animation cadence lock）是渲染器
    # 有意注入的约束文本，模板化措辞检查只针对正文部分（与视觉校验器豁免口径一致）。
    body = prompt.split("\n\nMotion style lock:", 1)[0]
    body = body.split("\n\nNegative:", 1)[0]
    for word in _FORBIDDEN_TEMPLATE_WORDS:
        assert word not in body
    assert "Motion style lock:" in prompt
    assert "Animation cadence lock:" in prompt
    assert "一拍二" in prompt
    # 不再出现「所有物体禁止移动」式场景冻结
    assert "不得在动画中移动、旋转、重构或重新生成" not in prompt
    assert "移动、旋转、重构" not in prompt


def test_3d_style_uses_3d_locks_and_skips_2d_negative_rules() -> None:
    """三维CG画风：运动锁/节奏锁为3D版，反写实化负向不注入，正向PBR不被反向禁用。"""
    prompt = _render(style="半写实3D国漫风，摄影级反射")

    assert "Motion style lock:" in prompt
    motion_line = prompt.split("\n\nMotion style lock: ", 1)[1].split("\n\n", 1)[0]
    assert "三维渲染画面" in motion_line
    assert "二维绘画画面" not in prompt
    assert "光线与阴影以二维绘画语言保持稳定" not in prompt
    # 3D 版节奏锁不再强制一拍二/涂抹帧：细微动作保持自然连续，避免走路顿、转头跳
    assert "自然连续" in prompt
    assert "一拍二" not in prompt
    assert "手绘二维动画节奏" not in prompt
    # 负向段不再出现与正向 PBR/反射冲突的反写实条款
    negative_line = prompt.split("\n\nNegative: ", 1)[1].split("\n\n", 1)[0]
    assert "PBR 材质" not in negative_line
    assert "真实反射" not in negative_line
    assert "摄影级景深" not in negative_line
    assert "二维平涂动漫" in negative_line


def test_environment_motion_written_only_when_requested() -> None:
    """环境微动按需启用：默认不写通用微动，避免背景全员微动造成 AI 插值感。"""
    outdoor = _render(scene_name="公司楼下街道")
    indoor = _render(scene_name="中关村办公区一楼大厅")

    # 默认不再无条件添加树叶草叶轻摆 / 窗帘纸张飘动
    assert "树叶" not in outdoor
    assert "草叶" not in outdoor
    assert "轻风" not in outdoor
    assert "窗帘" not in indoor
    assert "纸张" not in indoor

    with_motion = _render(
        scene_name="公司楼下街道",
        environment_motion="玉兰树叶在风中轻摆一次后恢复静止",
    )
    assert "玉兰树叶在风中轻摆一次后恢复静止" in with_motion

    empty_motion = _render(scene_name="公司楼下街道", environment_motion="")
    assert "树叶" not in empty_motion


def test_dialogue_entries_keep_verbatim_text_and_speaker() -> None:
    prompt = _render(
        dialogue_entries=[
            {"speaker": "徐尘心", "text": "让一下。"},
            {"speaker": "林舟", "text": "请进。"},
        ]
    )

    assert "第1句，徐尘心说：“让一下。”" in prompt
    assert "第2句，林舟说：“请进。”" in prompt


def test_timeline_marker_inside_clause_after_intro_is_naturalized() -> None:
    """真实提取结果：机位/场景铺垫句在前，时间标记出现在句子中间。"""
    video_prompt = (
        "固定机位平视中近景。清晨，写字楼旋转门前，徐尘心背着双肩电脑包站在画面中央；"
        "旋转门金属框与玻璃翼扇占据画面主体。徐尘心面部放松，目光平直落在门扇上。"
        "0-2秒：他走完最后两步，在旋转门前略顿停步，随即抬臂，手掌靠近玻璃门扇；"
        "2-5秒：手掌按实门扇向前发力，门扇绕轴转动打开，他顺势侧身迈步；"
        "5-7秒：他半侧身站在旋转门隔间内，门扇继续带着他朝门厅内侧旋转。"
    )
    prompt = _render(video_prompt=video_prompt, scene_name="中关村科技公司办公楼外")

    assert "0-2秒" not in prompt
    assert "2-5秒" not in prompt
    assert "5-7秒" not in prompt
    # 动作顺序保持：略顿 → 按实门扇 → 半侧身进入
    assert prompt.index("他走完最后两步") < prompt.index("手掌按实门扇")
    assert prompt.index("手掌按实门扇") < prompt.index("他半侧身站在旋转门隔间内")
    # 铺垫句里的场景描述保留，重复的机位句不再进入动作段
    assert "旋转门金属框与玻璃翼扇占据画面主体" in prompt


def test_opening_placeholder_is_not_repeated_as_start_sentence() -> None:
    """start_state 为「承接上一镜结束状态」占位时，不再输出「开始时，承接…」。"""
    prompt = _render(opening_frame="承接上一镜结束状态")

    assert "这一镜开始时，承接上一镜结束状态" not in prompt


def test_action_section_uses_animation_rhythm_not_live_action_language() -> None:
    """动作段用动画节奏语言（快出慢收/持帧/过冲），不再写实拍匀速语言。"""
    prompt = _render()

    assert "快出、慢收" in prompt
    assert "短暂持帧" in prompt
    assert "轻微过冲" in prompt
    # 实拍匀速语言不再出现
    assert "自然连贯" not in prompt
    assert "自然惯性" not in prompt


def test_animation_cadence_lock_follows_motion_style_lock() -> None:
    prompt = _render()

    assert "Animation cadence lock:" in prompt
    assert prompt.index("Motion style lock:") < prompt.index("Animation cadence lock:")


def test_office_building_outdoor_scene_keeps_scene_details() -> None:
    prompt = _render(scene_name="中关村科技公司办公楼外（玉兰树下）")

    # 默认无通用环境微动，但场景名本身保留
    assert "这一镜发生在中关村科技公司办公楼外（玉兰树下）" in prompt
    assert "窗帘、纸张" not in prompt


def test_style_prompt_with_trailing_period_has_no_double_suffix() -> None:
    prompt = _render(
        style="近未来科技职场题材，半写实 3D 国漫风，整体克制，带轻微黑色幽默与都市荒诞感。"
    )

    assert "荒诞感的风格。" in prompt
    assert "。的风格" not in prompt


def test_video_prompt_locks_physical_prop_contact_and_stable_lighting() -> None:
    """防电灯频闪与道具悬空飞行回归测试。"""
    prompt = _render(
        props=["手电筒", "文件袋"],
        video_prompt="他将文件袋递给对方。",
    )

    # 1. 道具段明确要求全程手部抓握接触，严禁脱手悬空飞行或隔空移物
    assert "必须全程由手部手指实体握持接触" in prompt
    assert "随手臂伸展与动作路径自然位移" in prompt
    assert "严禁道具脱手悬空飞行、严禁隔空移物" in prompt

    # 2. 光线段明确要求场景照明恒定稳定，无频闪无闪烁
    assert "场景照明与环境光线保持恒定稳定" in prompt
    assert "光源无频闪、无忽明忽暗、画面无闪烁跳变" in prompt

    # 3. 负向协议行明确加入防频闪与防悬空飞行的硬规则
    assert "道具脱手悬空飞行" in prompt
    assert "隔空移物" in prompt
    assert "灯光频闪" in prompt
    assert "画面闪烁" in prompt
    assert "光线忽明忽暗" in prompt



def test_optimize_identity_validation_accepts_same_reference_set() -> None:
    original = (
        "以 @Image1 作为起始画面。徐尘心沿用 @Image2 的参考素材。"
        "场景沿用 @Image3。0-2秒：推门；2-4秒：迈入。"
    )
    optimized = (
        "画面以 @Image1 的构图与站位为基准，徐尘心的外貌与服装沿用 @Image2，"
        "场景空间沿用 @Image3。他先推门，随后迈入门内。"
    )
    tokens = _collect_prompt_reference_tokens(original)
    assert _validate_prompt_reference_identity(original, optimized, tokens) == []


def test_optimize_identity_validation_rejects_missing_added_reordered() -> None:
    original = "参考 @Image1 与 @Image2 与 @Image3。"
    tokens = ["@Image1", "@Image2", "@Image3"]

    missing = _validate_prompt_reference_identity(original, "参考 @Image1 与 @Image2。", tokens)
    assert any("@Image3" in issue for issue in missing)

    added = _validate_prompt_reference_identity(original, "参考 @Image4 与 @Image1。", tokens)
    assert any("@Image4" in issue for issue in added)

    reordered = _validate_prompt_reference_identity(
        original, "参考 @Image2 与 @Image1 与 @Image3。", tokens
    )
    assert any("顺序" in issue for issue in reordered)


def test_validate_natural_style_detects_template_output() -> None:
    assert validate_natural_style("少年先向前走，随后停下观察。机位固定。") == []
    issues = validate_natural_style(
        "【核心动作】\n本镜只执行以下唯一一套时间轴，忽略其他时间描述；"
        "0-2秒：少年向前走；2-5秒：停下观察。"
    )
    assert any("【】" in issue for issue in issues)
    assert any("机械时间轴" in issue for issue in issues)
    assert any("本镜只执行" in issue for issue in issues)


def test_validate_natural_style_detects_precise_measurement() -> None:
    """毫米级动作描述（约三十度 / 半厘米）在优化验收时被拒绝。"""
    issues = validate_natural_style(
        "他颈转向玻璃方向约三十度，下巴朝玻璃探出又缩回半厘米。"
    )
    assert any("精确测量词" in issue for issue in issues)
    assert validate_natural_style("他颈转向玻璃方向，下巴极轻地向前探了一下。") == []


def test_director_plan_render_is_natural_not_template() -> None:
    """导演设计存在时，最终提示词也必须是自然语言而不是 10 段模板。"""
    from app.services.director_plan import (
        AudienceFocus,
        DialogueBinding,
        DirectorPlan,
        DirectorTimelineSegment,
        render_director_video_prompt,
    )

    prompt = render_director_video_prompt(
        style_prompt="国漫3D写实",
        aspect_ratio="9:16",
        director_plan=DirectorPlan(
            shot_purpose="强调孤独",
            audience_focus=AudienceFocus(first="苏月背影"),
            composition="negative-space",
            performance="缓慢转身",
            environment_motion="自动门滑开",
            visual_tone=["低饱和自然光"],
        ),
        timeline=[
            DirectorTimelineSegment(start=0, end=3, action="苏月背对镜头站立"),
            DirectorTimelineSegment(start=3, end=6, action="转身望向自动门"),
        ],
        dialogue_bindings=[
            DialogueBinding(
                speaker="苏月", text="又要走了。", start_time=0.5, end_time=2.0
            )
        ],
        initial_state="苏月背对镜头站在候车厅",
        end_state="苏月侧身望门",
        shot_type="中景",
        anchors=["@Image1：当前分镜关键帧。"],
    )

    assert "【" not in prompt
    assert validate_natural_style(prompt) == []
    assert "以 @Image1 作为这一镜的起始画面" in prompt
    assert "第1句，苏月说：“又要走了。”" in prompt
    assert "先苏月背对镜头站立，随后转身望向自动门" in prompt
    assert "Animation cadence lock:" in prompt
    assert validate_prompt_references(
        prompt, parse_reference_anchors(["@Image1：当前分镜关键帧。"])
    ).ok


class RecordingJobService(JobService):
    def __init__(self) -> None:
        super().__init__(Database("sqlite:///:memory:"))
        self.created: dict[str, Any] = {}
        self.progress: list[tuple[str, int, int, str]] = []

    def create(self, **kwargs: Any) -> dict[str, Any]:
        self.created = kwargs
        return {"id": "job-record", "status": "queued", "project_id": kwargs["project_id"]}

    def update_progress(
        self,
        job_id: str,
        current: int,
        total: int | None = None,
        message: str = "",
    ) -> None:
        self.progress.append((job_id, current, total or 0, message))


class StubTextProvider:
    def __init__(self, prompt: str) -> None:
        self.prompt = prompt
        self.calls: list[dict[str, Any]] = []

    async def generate_json(self, **values: Any) -> Any:
        self.calls.append(values)
        return ShotVideoPromptOptimizeOutput(prompt=self.prompt)


@pytest.fixture
def optimize_shot(tmp_path: Path) -> tuple[ShotService, dict[str, Any]]:
    database = Database(f"sqlite:///{(tmp_path / 'natural-optimize.db').as_posix()}")
    database.initialize(seed_mock_providers=True)
    project = ProjectService(database, FileService(tmp_path)).create({"name": "自然语言化"})
    chapter = ChapterService(database, FileService(tmp_path)).create(
        project["id"], {"title": "第一章", "source_text": "徐尘心走进办公楼。"}
    )
    service = ShotService(database, jobs=RecordingJobService())  # type: ignore[arg-type]
    shot = service.create(
        chapter["id"],
        {
            "shot_no": 1,
            "shot_order": 1,
            "source_type": "screenplay",
            "source_ref": "scene:1|中关村办公区",
            "narration": "",
            "shot_type": "中景",
            "camera_angle": "平视",
            "camera_move": "固定",
            "duration_sec": 5,
            "start_state": "徐尘心站在旋转门前。",
            "action": "徐尘心推门进入。",
            "end_state": "徐尘心迈入门内。",
            "next_hook": "",
            "frame_description": "徐尘心站在旋转门前。",
            "video_prompt": "0-2秒：徐尘心走到门前；2-5秒：推门进入。",
            "image_prompt": "徐尘心站在旋转门前。",
            "negative_prompt": "人物变形。",
            "status": "ready",
        },
    )
    try:
        yield service, shot
    finally:
        database.dispose()


def test_optimize_once_rejects_broken_reference_set(
    optimize_shot: tuple[ShotService, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """优化结果丢失/新增 @Image 引用时拒绝返回，不写入错误提示词。"""
    service, shot = optimize_shot
    current = service.get(shot["id"])
    project = service._project(current["project_id"])
    original = str(current["final_video_prompt"] or "")
    assert "@Image1" in original
    provider = StubTextProvider("优化后丢掉了关键帧引用的提示词。")

    monkeypatch.setattr(
        service.providers,
        "resolve",
        lambda **kwargs: (
            provider,
            {"id": "mock-text", "model": "mock-text", "params": {}, "timeout_sec": 120},
        ),
    )

    with pytest.raises(ServiceError) as exc_info:
        asyncio.run(
            service._optimize_video_prompt_once(
                current, project, original, provider_id=None, params=None
            )
        )

    assert "素材引用关系" in exc_info.value.detail


def test_handle_video_prompt_optimizes_and_preserves_user_edits(
    optimize_shot: tuple[ShotService, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """「重新生成提示词」= 基于当前提示词的自然语言优化，写入 override 并保留引用。"""
    service, shot = optimize_shot
    current = service.get(shot["id"])
    original = str(current["final_video_prompt"] or "")
    assert "@Image1" in original
    optimized_text = (
        "以 @Image1 作为起始画面。徐尘心先走到旋转门前，随后抬手推门，"
        "接近结尾时侧身迈入门内。机位固定。"
    )
    provider = StubTextProvider(optimized_text)

    monkeypatch.setattr(
        service.providers,
        "resolve",
        lambda **kwargs: (
            provider,
            {"id": "mock-text", "model": "mock-text", "params": {}, "timeout_sec": 120},
        ),
    )

    input_data = service._prompt_job_input(current, "video", {})
    job = {
        "target_id": shot["id"],
        "provider_id": None,
        "input_json": json.dumps(input_data),
    }
    result = asyncio.run(service.handle_video_prompt(job))

    assert result["shot_id"] == shot["id"]
    refreshed = service.get(shot["id"])
    # 优化结果保留原引用集合，且协议尾段（画风约束）逐字回补到 override。
    protocol_tail = _extract_protocol_tail(original)
    expected = optimized_text + (f"\n\n{protocol_tail}" if protocol_tail else "")
    assert refreshed["video_prompt_override"] == expected
    # 优化结果保留原引用集合
    assert _collect_prompt_reference_tokens(optimized_text) == _collect_prompt_reference_tokens(
        original
    )


def test_半写实电影感视频光影与负向不互斥() -> None:
    prompt = render_natural_video_prompt(
        style_prompt="影视级半写实仿真人漫风",
        aspect_ratio="16:9",
        duration_sec=6,
        action_flow="他推门进入。",
        shot_context={"scene_name": "办公楼大厅", "characters": ["徐尘心"]},
        dialogue_entries=[],
        tokens=(),
        negative_prompt="",
    )

    # 光影句用写实电影布光版本，不再与正向「景深：启用」互相否定
    assert "写实电影布光逻辑" in prompt
    assert "摄影级景深" not in prompt
    assert "景深：启用" in prompt
    # 二维防漂移运动锁保留，节奏锁为电影感版本
    assert "Motion style lock:" in prompt
    assert "Animation cadence lock:" in prompt


def test_构图去重切断括号说明时不残留孤立括号() -> None:
    """子句去重把「（说明，补充）」从中间切开时，孤立的右/左括号必须被清理。"""
    ref = "苏棠盘腿坐在茶几旁的地毯上，屏幕上刚弹出一枚红色提醒标记，画面不出现任何文字"
    # 右括号孤立：括号前半段与起始状态重合被删，后半段保留
    result = _dedupe_composition_clauses(
        "苏棠盘腿坐在地毯上，屏幕上刚弹出一枚红色提醒标记（画面不出现任何文字，提示内容与手机界面由后期叠加），她眼眶泛红",
        ref,
    )
    assert result.count("）") == result.count("（")
    assert "提示内容与手机界面由后期叠加" in result
    assert "她眼眶泛红" in result
    # 左括号孤立：括号后半段被删，前半段保留
    result2 = _dedupe_composition_clauses(
        "构图偏右（提示内容与手机界面由后期叠加，屏幕上刚弹出一枚红色提醒标记）",
        ref,
    )
    assert result2.count("）") == result2.count("（")
    assert "构图偏右" in result2


def test_素材名歧义时跳过绑定并记警告(caplog: pytest.LogCaptureFixture) -> None:
    """双向包含可能把「林」误配到多个角色，候选不唯一时不绑定。"""
    tokens = parse_reference_anchors(
        [
            "@Image1：林舟，锁定角色身份与服装。",
            "@Image2：林雪，锁定角色身份与服装。",
        ]
    )
    diagnostics: list[str] = []
    with caplog.at_level(logging.WARNING, logger="app.services.natural_video_prompt"):
        matched = _match_tokens_by_names(
            tokens, "character_identity", ["林"], diagnostics=diagnostics
        )

    assert matched == {}
    assert diagnostics and "多个相近参考锚点" in diagnostics[0]
    assert "林" in caplog.text


def test_素材名精确匹配优先于包含匹配() -> None:
    tokens = parse_reference_anchors(
        [
            "@Image1：徐尘，锁定角色身份与服装。",
            "@Image2：徐尘心，锁定角色身份与服装。",
        ]
    )

    matched = _match_tokens_by_names(tokens, "character_identity", ["徐尘心"])

    assert matched["徐尘心"].token == "@Image2"


def test_素材名唯一候选时允许包含匹配() -> None:
    tokens = parse_reference_anchors(["@Image1：徐尘心，锁定角色身份与服装。"])

    matched = _match_tokens_by_names(tokens, "character_identity", ["徐尘"])

    assert matched["徐尘"].token == "@Image1"


def test_无法解析的锚点行记警告(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.services.natural_video_prompt"):
        tokens = parse_reference_anchors(["这是一行没有编号的锚点", "@Image2：徐尘心。"])

    assert [token.token for token in tokens] == ["@Image2"]
    assert "无法解析" in caplog.text


def test_对白条目缺键时跳过而不报错() -> None:
    prompt = render_natural_video_prompt(
        style_prompt=_STYLE,
        aspect_ratio="16:9",
        duration_sec=6,
        action_flow="他推门进入。",
        shot_context={"scene_name": "办公楼大厅", "characters": ["徐尘心"]},
        dialogue_entries=[{"speaker": "徐尘心"}, {"text": "走吧。"}],
        tokens=(),
        negative_prompt="",
    )

    assert "本镜没有画内对白，人物全程不开口" in prompt
    assert "说：“”" not in prompt


def test_道具引用拼接不产生标点接缝() -> None:
    prompt = _render(
        props=["工位仙人掌"],
        anchors=[
            "@Image1：当前分镜关键帧。",
            "@Image3：中关村办公区，锁定场景空间结构和光线。",
            "@Image4：工位仙人掌，锁定物品外观、材质和结构。",
        ],
    )

    assert "。；" not in prompt
    assert "本镜道具：工位仙人掌；工位仙人掌沿用 @Image4 的外观与材质。" in prompt


def test_构图去重保留比例层级焦点维度() -> None:
    """比例/层级/焦点是必须保留的构图维度，即使与起始状态文字重合也不删除。"""
    ref = "苏棠盘腿坐在地毯上"
    result = _dedupe_composition_clauses(
        "画面比例为16:9，前中后景层级递进，焦点落在桌面的信纸上",
        ref,
    )

    assert "画面比例为16:9" in result
    assert "层级递进" in result
    assert "焦点落在桌面的信纸上" in result
