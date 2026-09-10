"""导演设计层（DirectorPlan）核心纯函数的回归测试。"""

from __future__ import annotations

from app.services.director_plan import (
    AudienceFocus,
    DialogueBinding,
    DirectorPlan,
    DirectorTimelineSegment,
    MicroPerformance,
    director_bundle_from_llm,
    normalize_voice_source,
    render_director_video_prompt,
    validate_shot_consistency,
)
from app.services.prompt_service import compile_video_prompt


def _plan(**overrides) -> DirectorPlan:
    payload = {
        "shot_purpose": "建立环境并强调孤独感",
        "audience_focus": AudienceFocus(first="苏月的背影", second="空旷候车厅"),
        "composition": "negative-space",
        "foreground": "半虚的前排座椅",
        "midground": "苏月站在画面右三分之一",
        "background": "候车厅与自动门",
        "camera_motivation": "先固定观察，人物转身后再轻微重新取景",
        "performance": "苏月缓慢转身，双手插兜",
        "micro_performance": [
            MicroPerformance(trigger="自动门滑开", action="肩头轻偏", source="环境刺激")
        ],
        "environment_motion": "仅自动门缓缓滑开",
        "visual_tone": ["真实都市空气感", "低饱和自然光"],
    }
    payload.update(overrides)
    return DirectorPlan(**payload)


def _timeline() -> list[DirectorTimelineSegment]:
    return [
        DirectorTimelineSegment(start=0, end=3, action="苏月背对镜头站立"),
        DirectorTimelineSegment(start=3, end=6, action="转身望向自动门"),
    ]


def test_render_is_natural_director_style() -> None:
    plan = _plan()
    prompt = render_director_video_prompt(
        style_prompt="国漫3D写实",
        aspect_ratio="9:16",
        director_plan=plan,
        timeline=_timeline(),
        dialogue_bindings=[
            DialogueBinding(
                speaker="苏月", text="又要走了。", start_time=0.5, end_time=2.0
            )
        ],
        initial_state="苏月背对镜头站在候车厅",
        end_state="苏月侧身望门",
        shot_type="中景",
    )
    # 不再有 10 段【】标题
    assert "【" not in prompt
    # 叙事意图、注意力、表演与对白均自然表达
    assert "本镜的叙事意图：建立环境并强调孤独感" in prompt
    assert "观众的注意力先落在苏月的背影" in prompt
    assert "苏月缓慢转身，双手插兜" in prompt
    assert "第1句，苏月说：“又要走了。”" in prompt
    # 时间轴粗粒度化：无 0-3秒 机械分段
    assert "0-3秒" not in prompt
    assert "3-6秒" not in prompt
    assert "先苏月背对镜头站立，随后转身望向自动门" in prompt
    assert "接近结尾时，苏月侧身望门。" in prompt
    # 环境动态保留导演设计内容，不再有「保持静态」等模板句
    assert "仅自动门缓缓滑开" in prompt
    assert "保持静态" not in prompt
    # 不再出现旧版的「统一画风锁定」约束段与机械化词
    # （结构化画风指纹自带的「渲染方式锁定」是合法内容，不在禁止之列）
    assert "统一画风锁定" not in prompt
    assert "禁止项" not in prompt


def test_render_hard_constraints_are_minimal() -> None:
    plan = _plan()
    prompt = render_director_video_prompt(
        style_prompt="国漫3D写实",
        aspect_ratio="9:16",
        director_plan=plan,
        timeline=_timeline(),
        dialogue_bindings=[],
        initial_state="站立",
        end_state="站立",
    )
    assert "画面中不出现：人物身份或服装改变、场景结构被重构、" in prompt
    assert "新增无关人物、画面内文字、道具凭空消失" in prompt
    negative_line = prompt.split("\n\nNegative: ", 1)[1].split("\n\n", 1)[0]
    # 3D 画风：反动态写实化负向（PBR/真实反射/景深等）不再注入，
    # 避免与正向 PBR 指纹打架；反向条款由禁止风格列表提供。
    assert "写实化动态" not in negative_line
    assert "真实物理模拟" not in negative_line
    assert "PBR 材质" not in negative_line
    assert "二维平涂动漫" in negative_line
    assert "保持稳定" not in prompt.split("\n\nMotion style lock:", 1)[0]
    assert "缓慢跟随" not in prompt
    assert "【" not in prompt
    # 3D 画风：运动风格锁为三维渲染版，不含二维绘画措辞
    assert "Motion style lock:" in prompt
    motion_line = prompt.split("\n\nMotion style lock: ", 1)[1].split("\n\n", 1)[0]
    assert "三维渲染画面" in motion_line
    assert "二维绘画画面" not in prompt
    # 动画节奏锁在渲染器出口注入，位于运动风格锁之后；
    # 3D CG 不再强制一拍二持帧/涂抹帧：细微动作保持自然连续，避免走路顿、转头跳。
    assert "Animation cadence lock:" in prompt
    assert "自然连续" in prompt
    assert "一拍二" not in prompt
    assert "涂抹帧" not in prompt
    assert "手绘二维动画节奏" not in prompt
    assert prompt.index("Motion style lock:") < prompt.index("Animation cadence lock:")


def test_render_hard_constraints_2d_keeps_dynamic_negative_rules() -> None:
    """二维画风保持原行为：反动态写实化负向与二维运动锁均注入。"""
    plan = _plan()
    prompt = render_director_video_prompt(
        style_prompt="都市温情二维国漫",
        aspect_ratio="9:16",
        director_plan=plan,
        timeline=_timeline(),
        dialogue_bindings=[],
        initial_state="站立",
        end_state="站立",
    )
    negative_line = prompt.split("\n\nNegative: ", 1)[1].split("\n\n", 1)[0]
    assert "写实化动态" in negative_line
    assert "真实物理模拟" in negative_line
    assert "PBR 材质" in negative_line
    motion_line = prompt.split("\n\nMotion style lock: ", 1)[1].split("\n\n", 1)[0]
    assert "二维绘画画面" in motion_line
    assert "手绘二维动画节奏（一拍二）" in prompt


def test_render_elliptical_transition_uses_explicit_scene_switch() -> None:
    """换场景省略剪辑：渲染开头明确写出省略式切换，不再暗示同一时空连续承接。"""
    plan = _plan()
    prompt = render_director_video_prompt(
        style_prompt="半写实3D国漫风",
        aspect_ratio="16:9",
        director_plan=plan,
        timeline=_timeline(),
        dialogue_bindings=[],
        initial_state="徐尘心已经走到七楼办公区门禁前",
        end_state="徐尘心在工位椅侧站定",
        transition_from_prev="elliptical",
        prev_end_state="玻璃门被推开大半，人物剪影停在门内",
        scene_name="写字楼七楼开放办公区",
    )
    assert "省略了一段过程" in prompt
    assert "直接切至写字楼七楼开放办公区" in prompt
    assert "不连续展示中间的移动或转场" in prompt


def test_render_continuous_transition_keeps_direct_handoff() -> None:
    """连续镜头保留直接承接句，不误加省略式切换。"""
    plan = _plan()
    prompt = render_director_video_prompt(
        style_prompt="半写实3D国漫风",
        aspect_ratio="16:9",
        director_plan=plan,
        timeline=_timeline(),
        dialogue_bindings=[],
        initial_state="苏月仍在原地",
        end_state="苏月侧身望门",
        transition_from_prev="continuous",
        prev_end_state="苏月站在门边",
        scene_name="候车厅",
    )
    assert "这一镜直接接在上一镜之后" in prompt
    assert "省略了一段过程" not in prompt


def test_validate_prop_name_drift_is_blocking() -> None:
    """导演计划把「双肩包」写成「公文包」：阻断级道具漂移。"""
    plan = _plan(performance="徐尘心把公文包从肩头卸下放到地面")
    issues = validate_shot_consistency(
        shot={
            "start_state": "徐尘心背着双肩包",
            "action": "他把双肩包放好",
            "end_state": "徐尘心坐定，双肩包在椅侧地面",
            "prop_names": ["双肩包"],
        },
        director_plan=plan,
        timeline=_timeline(),
        dialogue_bindings=[],
        duration_sec=6.0,
    )
    drift = [issue for issue in issues if issue.code == "prop_name_drift"]
    assert drift and drift[0].severity == "blocking"


def test_validate_measurement_words_warn() -> None:
    """毫米级测量词触发提示：约三十度 / 半厘米。"""
    plan = _plan(performance="他颈转向玻璃方向约三十度，下巴探出又缩回半厘米")
    issues = validate_shot_consistency(
        shot={"start_state": "站立", "action": "看", "end_state": "站立"},
        director_plan=plan,
        timeline=_timeline(),
        dialogue_bindings=[],
        duration_sec=6.0,
    )
    assert any(issue.code == "precise_measurement" for issue in issues)


def test_validate_lighting_shift_between_shots_warns() -> None:
    """同场连续镜头时段跳变（清晨→日间）触发光照一致性提示。"""
    plan = _plan(visual_tone=["日间室内", "台灯右侧来光"])
    issues = validate_shot_consistency(
        shot={
            "start_state": "徐尘心坐在工位椅上",
            "action": "侧头看音箱",
            "end_state": "徐尘心维持侧身",
            "transition_from_prev": "same_scene_cut",
        },
        director_plan=plan,
        timeline=_timeline(),
        dialogue_bindings=[],
        duration_sec=6.0,
        prev_visual_tone=["清晨", "过道侧窗方向晨光"],
    )
    assert any(issue.code == "lighting_shift_between_shots" for issue in issues)


def test_validate_timeline_too_dense_warns() -> None:
    """单镜动作节点过多（多段不足1.5秒）触发拆镜提示。"""
    dense = [
        DirectorTimelineSegment(start=0, end=1, action="a"),
        DirectorTimelineSegment(start=1, end=2, action="b"),
        DirectorTimelineSegment(start=2, end=3, action="c"),
        DirectorTimelineSegment(start=3, end=6, action="d"),
    ]
    issues = validate_shot_consistency(
        shot={},
        timeline=dense,
        dialogue_bindings=[],
        duration_sec=6.0,
    )
    assert any(issue.code == "timeline_too_dense" for issue in issues)


def test_validate_timeline_detects_gap() -> None:
    issues = validate_shot_consistency(
        shot={},
        timeline=[
            DirectorTimelineSegment(start=0, end=2, action="a"),
            DirectorTimelineSegment(start=3, end=6, action="b"),
        ],
        dialogue_bindings=[],
        duration_sec=6.0,
    )
    codes = {issue.code for issue in issues}
    assert "timeline_gap_or_overlap" in codes


def test_validate_dialogue_detects_duplicate() -> None:
    issues = validate_shot_consistency(
        shot={"dialogue": [{"text": "你好"}]},
        timeline=[DirectorTimelineSegment(start=0, end=5, action="x")],
        dialogue_bindings=[
            DialogueBinding(speaker="A", text="你好", start_time=0, end_time=1),
            DialogueBinding(speaker="A", text="你好", start_time=0, end_time=1),
        ],
        duration_sec=5.0,
    )
    assert any(issue.code == "dialogue_duplicated" for issue in issues)


def test_validate_dialogue_not_in_list() -> None:
    issues = validate_shot_consistency(
        shot={"dialogue": [{"text": "别的话"}]},
        timeline=[DirectorTimelineSegment(start=0, end=5, action="x")],
        dialogue_bindings=[
            DialogueBinding(speaker="A", text="凭空新增台词", start_time=0, end_time=1)
        ],
        duration_sec=5.0,
    )
    assert any(issue.code == "dialogue_not_in_list" for issue in issues)


def test_normalize_voice_source_device_is_not_lip_sync() -> None:
    assert normalize_voice_source("蓝牙音箱") == ("device", False)
    assert normalize_voice_source("广播") == ("broadcast", False)
    assert normalize_voice_source("苏月") == ("character", True)


def test_director_bundle_from_llm_camel_case() -> None:
    bundle = director_bundle_from_llm(
        {
            "shotPurpose": "强调孤独",
            "audienceFocus": {"first": "苏月", "second": "候车厅"},
            "composition": "negative-space",
            "cameraMotivation": "固定观察",
            "performance": "缓慢转身",
            "environmentMotion": "自动门滑开",
            "visualTone": ["低饱和自然光"],
            "timeline": [{"start": 0, "end": 3, "action": "站立"}],
            "dialogueBindings": [
                {
                    "speaker": "苏月",
                    "text": "又要走了。",
                    "startTime": 0.5,
                    "endTime": 2.0,
                    "requiresLipSync": True,
                    "voiceSource": "character",
                }
            ],
        }
    )
    assert bundle.director_plan.composition == "negative-space"
    assert bundle.timeline[0].action == "站立"
    assert bundle.dialogue_bindings[0].requires_lip_sync is True


def test_compile_video_prompt_uses_director_plan_when_present() -> None:
    context = {
        "opening_frame": "苏月背对镜头站着",
        "ending_frame": "苏月侧身望门",
        "shot_type": "中景",
        "characters": ["苏月"],
        "props": [],
        "scene_name": "候车厅",
        "focus": "转身望门",
    }
    director_plan = {
        "shotPurpose": "强调孤独",
        "audienceFocus": {"first": "苏月背影"},
        "composition": "negative-space",
        "cameraMotivation": "固定观察",
        "performance": "缓慢转身",
        "environmentMotion": "自动门滑开",
        "visualTone": ["低饱和自然光"],
        "timeline": [
            {"start": 0, "end": 3, "action": "站立"},
            {"start": 3, "end": 6, "action": "转身"},
        ],
        "dialogueBindings": [
            {"speaker": "苏月", "text": "又要走了。", "startTime": 0.5, "endTime": 2.0}
        ],
    }
    prompt = compile_video_prompt(
        style_prompt="国漫3D写实",
        aspect_ratio="9:16",
        video_prompt="",
        duration_sec=6,
        anchors=[],
        shot_context=context,
        negative_prompt="",
        director_plan=director_plan,
    )
    assert "本镜的叙事意图：强调孤独" in prompt
    assert "先站立，随后转身" in prompt
    assert "【" not in prompt


def test_compile_video_prompt_falls_back_without_director_plan() -> None:
    context = {
        "opening_frame": "苏月站着",
        "ending_frame": "苏月坐下",
        "shot_type": "中景",
        "characters": ["苏月"],
        "props": [],
        "scene_name": "候车厅",
        "focus": "坐下",
    }
    prompt = compile_video_prompt(
        style_prompt="国漫3D写实",
        aspect_ratio="9:16",
        video_prompt="0-3秒：站立；3-6秒：坐下",
        duration_sec=6,
        anchors=[],
        shot_context=context,
        negative_prompt="",
    )
    assert "这一镜发生在候车厅" in prompt
    assert "【本镜叙事意图】" not in prompt


def test_compile_video_prompt_falls_back_on_malformed_director_plan() -> None:
    """存储中的导演计划结构非法（时间轴段 end<=start）时回退旧版渲染而非抛异常。"""
    context = {
        "opening_frame": "苏月站着",
        "ending_frame": "苏月坐下",
        "shot_type": "中景",
        "characters": ["苏月"],
        "props": [],
        "scene_name": "候车厅",
        "focus": "坐下",
    }
    malformed_plan = {
        "shotPurpose": "强调孤独",
        "audienceFocus": {"first": "苏月背影"},
        "composition": "negative-space",
        "timeline": [
            {"start": 3, "end": 3, "action": "站立"},
        ],
    }
    prompt = compile_video_prompt(
        style_prompt="国漫3D写实",
        aspect_ratio="9:16",
        video_prompt="0-3秒：站立；3-6秒：坐下",
        duration_sec=6,
        anchors=[],
        shot_context=context,
        negative_prompt="",
        director_plan=malformed_plan,
    )
    assert "这一镜发生在候车厅" in prompt
    assert "【本镜叙事意图】" not in prompt
