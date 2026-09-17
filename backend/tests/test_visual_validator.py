from __future__ import annotations

from app.dynamic_pipeline.visual_anchor import build_visual_style_spec
from app.dynamic_pipeline.visual_validator import (
    ANIMATION_CADENCE_LOCK_TEXT_2D_CINEMATIC,
    ANIMATION_CADENCE_LOCK_TEXT_3D,
    MOTION_STYLE_LOCK_TEXT,
    MOTION_STYLE_LOCK_TEXT_PHOTOREAL,
    animation_cadence_lock_for,
    dynamic_negative_rules_for,
    motion_style_lock_for,
    style_medium_of,
    validate_visual_prompt,
    validate_visual_prompt_lenient,
)


def _diagnostics(prompt: str, **kwargs) -> list[dict]:
    return validate_visual_prompt(prompt, **kwargs)


def test_中文写实化禁词命中为_blocking() -> None:
    diagnostics = _diagnostics("人物皮肤使用真实反射，电影光影包裹全身")
    codes = {item["code"] for item in diagnostics}
    assert "visual_conflict_forbidden_term" in codes
    forbidden = next(
        item for item in diagnostics if item["code"] == "visual_conflict_forbidden_term"
    )
    assert forbidden["level"] == "blocking"
    assert "真实反射" in forbidden["details"]["terms"]


def test_英文写实化禁词按整词命中() -> None:
    diagnostics = _diagnostics("cinematic lighting, volumetric lighting, PBR material")
    forbidden = next(
        item for item in diagnostics if item["code"] == "visual_conflict_forbidden_term"
    )
    terms = forbidden["details"]["terms"]
    assert "cinematic lighting" in terms
    assert "volumetric lighting" in terms
    assert "PBR" in terms


def test_否定语境不误报() -> None:
    assert _diagnostics("光影：无体积光，无电影辉光；材质表达：PBR 禁用") == []
    assert _diagnostics("不使用真实反射，不产生透明折射") == []


def test_禁用辉光画风下正文发光词触发冲突提示() -> None:
    """画风声明禁用电影级辉光时，正文「光晕」会被模型理解成 bloom/glow。"""
    style = {"style_prompt": "半写实3D国漫风，禁用电影级辉光"}
    diags = _diagnostics("接近结尾时，他在门内光晕中停住。", style=style)
    codes = {item["code"] for item in diags}
    assert "visual_conflict_glow" in codes
    glow = next(item for item in diags if item["code"] == "visual_conflict_glow")
    assert glow["level"] == "warning"
    assert "光晕" in glow["details"]["terms"]
    # 否定语境（无光晕/禁用辉光）不误报
    assert _diagnostics("画面无光晕，无辉光。", style=style) == []
    # 未声明禁用辉光的画风不触发
    assert _diagnostics("接近结尾时，他在门内光晕中停住。") == []


def test_项目画风原文作为豁免来源() -> None:
    # 画风原文含电影感，下游提示词里出现同一概念不算越权
    assert _diagnostics("延续电影感布光风格", style={"style_prompt": "电影感二维国漫"}) == []
    # 禁止风格列表中的词（PBR/3D）来自 VisualBible 自身，同样豁免
    style = {
        "style_prompt": "水墨二维国漫",
        "forbidden_styles": ["3D", "photorealistic", "PBR"],
    }
    assert _diagnostics("禁止风格：3D、photorealistic、PBR", style=style) == []


def test_同级语义冲突对命中为_warning() -> None:
    diagnostics = _diagnostics("粗笔焦墨与精细轮廓并存，轮廓线纤细")
    conflicts = [item for item in diagnostics if item["code"] == "visual_conflict_semantic"]
    assert conflicts
    assert conflicts[0]["level"] == "warning"
    assert {"粗笔", "精细轮廓"} & set(conflicts[0]["details"]["terms"])


def test_风格与内容冲突_画风二维内容真实体积感() -> None:
    diagnostics = _diagnostics(
        "人物带有真实空间体积感",
        style={"style_prompt": "二维国漫"},
    )
    conflicts = [item for item in diagnostics if item["code"] == "visual_conflict_semantic"]
    assert any(item["details"]["terms"] == ["二维", "真实空间体积感"] for item in conflicts)


def test_协议段不参与扫描() -> None:
    prompt = (
        "Subject: 林舟站在站台边\n"
        f"Motion style lock: {MOTION_STYLE_LOCK_TEXT}\n"
        "Negative: 3D建模渲染，写实渲染，真实物理\n"
    )
    assert _diagnostics(prompt) == []


def test_lenient_模式全部降级为_warning() -> None:
    diagnostics = validate_visual_prompt_lenient("体积光 + PBR 材质")
    assert diagnostics
    assert all(item["level"] == "warning" for item in diagnostics)


def test_干净提示词无诊断() -> None:
    assert _diagnostics("清晨，林舟从画面右侧走向写字楼入口，镜头缓慢横移") == []


def test_未知媒介不注入运动与节奏锁() -> None:
    spec = build_visual_style_spec(style_prompt="三渲二卡通渲染")

    assert style_medium_of(spec) == "unknown"
    assert motion_style_lock_for(spec) == ""
    assert animation_cadence_lock_for(spec) == ""
    assert dynamic_negative_rules_for(spec) == ()


def test_半写实电影感负向剔除摄影级景深并使用电影感节奏锁() -> None:
    spec = build_visual_style_spec(style_prompt="影视级半写实仿真人漫风")

    rules = dynamic_negative_rules_for(spec)
    assert "摄影级景深" not in rules
    assert "PBR 材质" in rules
    # 半写实电影感二维保留关键姿势顿挫版（无手绘/速度线词），二维防漂移运动锁保留
    assert animation_cadence_lock_for(spec) == ANIMATION_CADENCE_LOCK_TEXT_2D_CINEMATIC
    assert motion_style_lock_for(spec) == MOTION_STYLE_LOCK_TEXT


def test_三维CG媒介使用自然连续节奏锁且不强制一拍二() -> None:
    spec = build_visual_style_spec(style_prompt="半写实3D国漫风，PBR材质")

    assert style_medium_of(spec) == "3d"
    lock = animation_cadence_lock_for(spec)
    assert lock == ANIMATION_CADENCE_LOCK_TEXT_3D
    assert "自然连续" in lock
    assert "一拍二" not in lock
    assert "涂抹帧" not in lock


def test_真人写实媒介选择写实锁且不注入反写实化负向() -> None:
    spec = build_visual_style_spec(style_prompt="写实真人风格")

    assert style_medium_of(spec) == "photoreal"
    assert motion_style_lock_for(spec) == MOTION_STYLE_LOCK_TEXT_PHOTOREAL
    assert dynamic_negative_rules_for(spec) == ()


def test_三维画风启用摄影级反射不误报() -> None:
    spec = build_visual_style_spec(style_prompt="3D国漫渲染")
    style = {"style_prompt": "3D国漫渲染", **spec.model_dump()}
    prompt = "结构化画风规范：材质：方式「PBR 材质」、启用摄影级反射；剧情：主角站在大厅"
    assert validate_visual_prompt_lenient(prompt, style=style) == []


def test_二维画风禁用摄影级反射不误报() -> None:
    spec = build_visual_style_spec(style_prompt="二维写意水墨国漫")
    style = {"style_prompt": "二维写意水墨国漫", **spec.model_dump()}
    prompt = "结构化画风规范：材质：方式「绘画概括」、禁用摄影级反射；剧情：主角站在大厅"
    assert validate_visual_prompt_lenient(prompt, style=style) == []


def test_二维画风正文违规使用摄影级反射正确报错() -> None:
    spec = build_visual_style_spec(style_prompt="二维写意水墨国漫")
    style = {"style_prompt": "二维写意水墨国漫", **spec.model_dump()}
    prompt = "结构化画风规范：材质：方式「绘画概括」、禁用摄影级反射；剧情：地面呈现摄影级反射"
    diags = validate_visual_prompt_lenient(prompt, style=style)
    assert len(diags) == 1
    assert "摄影级反射" in diags[0]["message"]


def test_景深与写实摄影参数命中为_blocking() -> None:
    """景深/大光圈/焦段/摄影机机型属越权写实摄影参数，必须能拦住。"""
    diagnostics = _diagnostics(
        "近景使用 50mm 定焦，f1.8 大光圈，浅景深虚化背景，Sony Venice 电影摄影机拍摄"
    )
    forbidden = next(
        item for item in diagnostics if item["code"] == "visual_conflict_forbidden_term"
    )
    assert forbidden["level"] == "blocking"
    terms = {str(term).casefold() for term in forbidden["details"]["terms"]}
    assert {"浅景深", "f1.8", "50mm", "sony venice"} <= terms
    # 「浅景深」已覆盖其中的「景深」，不重复报告
    assert "景深" not in terms


def test_景深否定语境与画风声明不误报() -> None:
    # 否定语境：无景深模糊 / 无背景虚化
    assert _diagnostics("全画面无景深模糊，无背景虚化。") == []
    # 画风指纹自身的声明：景深逻辑 / 景深：启用|禁用
    assert _diagnostics("渲染方式恒定：线稿强度、阴影软硬与景深逻辑保持不变。") == []
    assert _diagnostics("结构化画风规范：景深：禁用；渲染方式锁定：二维国风线稿。") == []


def test_二维画风与浅景深为同级语义冲突() -> None:
    diagnostics = _diagnostics("浅景深虚化背景", style={"style_prompt": "二维水墨国漫"})
    conflicts = [item for item in diagnostics if item["code"] == "visual_conflict_semantic"]
    assert any(item["details"]["terms"] == ["二维", "浅景深"] for item in conflicts)

