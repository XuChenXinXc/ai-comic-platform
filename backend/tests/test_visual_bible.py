from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import projects as projects_api
from app.db import Database
from app.dependencies import get_project_service
from app.dynamic_pipeline.visual_anchor import (
    build_visual_style_spec,
    compile_visual_bible,
    merge_visual_bible_negative,
    parse_visual_bible,
    render_visual_style_spec,
)
from app.services.errors import ServiceError
from app.services.file_service import FileService
from app.services.project_service import ProjectService
from app.services.prompt_service import (
    compile_asset_prompt,
    compile_keyframe_prompt,
    compile_video_prompt,
    style_anchor,
)


def test_水墨画风自动推导结构化默认值() -> None:
    style = build_visual_style_spec(style_prompt="东方粗笔重彩·写意水墨二维国漫风")

    assert style.line.type == "粗笔焦墨"
    assert style.coloring.method == "重彩点染+湿墨铺色"
    assert style.shading.method == "墨色明暗块"
    assert style.material.pbr is False
    assert style.material.photographic_reflection is False
    assert style.lighting.volumetric is False
    assert style.depth_of_field.enabled is False
    assert "PBR" in style.forbidden_styles
    assert "photorealistic" in style.forbidden_styles
    assert style.texture == "宣纸、水墨晕染、粗笔笔触"


def test_写实电影级三维动画推导三维CG指纹() -> None:
    style = build_visual_style_spec(style_prompt="写实电影级三维动画")

    # 线条/上色给出中性事实值，保证视觉圣经面板字段完整回显
    assert style.line.type == "无描线（三维模型轮廓）"
    assert style.coloring.method == "材质着色自然色彩"
    assert style.material.pbr is True
    assert style.material.photographic_reflection is True
    assert style.realism.character == 1.0
    assert "三维建模渲染" in style.render_fingerprint
    assert "二维平涂动漫" in style.forbidden_styles
    assert "真人实拍" in style.forbidden_styles


def test_写实电影级三维动画渲染描述压缩_每概念只声明一次() -> None:
    rendered = render_visual_style_spec(
        compile_visual_bible("写实电影级三维动画", None)
    )

    # 线条/上色以事实值各声明一次（面板回显需要），不重复渲染画法细节
    assert "线条：类型「无描线（三维模型轮廓）」" in rendered
    assert "上色：方式「材质着色自然色彩」" in rendered
    # PBR 只出现在材质方式一次，不再重复输出「启用PBR」
    assert "材质：方式「PBR 材质」、启用摄影级反射" in rendered
    assert "启用PBR" not in rendered
    assert rendered.count("PBR") == 1
    # 全局光照、无手绘描线各只出现一次
    assert rendered.count("三维全局光照") == 1
    assert rendered.count("无手绘描线") == 1
    # 渲染指纹不再重复罗列 PBR/光照渲染句
    assert "材质按 PBR 渲染" not in rendered
    assert "光影为三维全局光照" not in rendered


def test_写实真人画风推导摄影写实指纹() -> None:
    style = build_visual_style_spec(style_prompt="写实真人风格")

    assert style.line.type == "无描线（摄影写实轮廓）"
    assert style.realism.character == 1.0
    assert "真人实拍质感" in style.render_fingerprint
    assert "二维平涂动漫" in style.forbidden_styles
    assert "三维CG" not in style.forbidden_styles


def test_影视级半写实仿真人漫风推导完整正向结构() -> None:
    style = build_visual_style_spec(style_prompt="影视级半写实仿真人漫风")

    # 正向结构化字段全部产出，不再只有禁用条目
    assert style.line.type == "精细写实勾线"
    assert style.coloring.method == "写实肤色与自然色上色"
    assert style.shading.method == "写实体积明暗"
    assert style.shading.realistic_shading is True
    assert style.material.method == "半写实绘画质感"
    # 影视级变体：写实电影布光 + 浅景深，不输出禁用体积光/电影级辉光
    assert style.lighting.realism == "写实电影布光"
    assert style.lighting.volumetric is None
    assert style.lighting.cinematic_glow is None
    assert style.depth_of_field.enabled is True
    assert style.realism.character == 0.85
    assert style.realism.environment == 0.8
    assert "电影级光影过渡" in style.texture
    # 渲染方式指纹：半写实仿真人漫拆成稳定视觉参数，逐镜重复同一套指纹
    assert "半写实人漫造型" in style.render_fingerprint
    assert "不是3D游戏CG" in style.render_fingerprint
    assert "影视动画渲染" in style.render_fingerprint
    # 仍保留二维基底防漂移禁则
    assert style.material.pbr is False
    assert style.material.photographic_reflection is False
    assert "PBR" in style.forbidden_styles

    rendered = render_visual_style_spec(style)
    assert rendered.startswith("结构化画风规范：")
    assert "线条：类型「精细写实勾线」" in rendered
    assert "景深：启用" in rendered
    assert "启用写实明暗" in rendered
    assert "禁用写实明暗" not in rendered
    assert "禁用体积光" not in rendered
    assert "写实度：角色写实度「0.85」、环境写实度「0.8」" in rendered
    assert "渲染方式锁定：" in rendered
    assert "不是二维平涂动漫" in rendered


def test_普通半写实仿真人漫风推导保守正向结构() -> None:
    style = build_visual_style_spec(style_prompt="半写实仿真人漫风")

    assert style.line.type == "精细写实勾线"
    assert style.shading.realistic_shading is True
    assert style.lighting.realism == "自然写实布光"
    assert style.lighting.volumetric is False
    assert style.lighting.cinematic_glow is False
    assert style.depth_of_field.enabled is False
    assert style.realism.character == 0.8
    assert style.realism.environment == 0.7
    assert style.texture == "细腻皮肤质感、半写实绘画笔触"
    # 非影视级变体：手绘动画渲染，不带影视级措辞
    assert "手绘动画渲染" in style.render_fingerprint
    assert "影视动画渲染" not in style.render_fingerprint

    rendered = render_visual_style_spec(style)
    assert "景深：禁用" in rendered
    assert "禁用体积光" in rendered


def test_半写实但明确偏向三维或真人走三维指纹而非二维推导() -> None:
    spec = compile_visual_bible("半写实3D真人漫风", None)
    assert spec.material.pbr is True
    assert "渲染方式锁定" in render_visual_style_spec(spec)


def test_结构化覆盖优先于自动推导() -> None:
    style = build_visual_style_spec(
        style_prompt="水墨二维国漫",
        overrides={"material": {"pbr": True}, "line": {"type": "工笔细线"}},
    )

    assert style.material.pbr is True
    assert style.line.type == "工笔细线"


def test_禁止风格与结构化开关互斥时拒绝构造() -> None:
    with pytest.raises(ValidationError):
        build_visual_style_spec(
            style_prompt="水墨二维国漫",
            overrides={
                "material": {"pbr": True},
                "forbidden_styles": ["PBR", "3D"],
            },
        )


def test_project_service_visual_bible_round_trip(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'visual-bible.db').as_posix()}")
    database.create_all()
    service = ProjectService(database, FileService(tmp_path / "files"))
    structured = {"material": {"pbr": False}, "forbidden_styles": ["3D", "PBR"]}
    record = service.create(
        {
            "name": "视觉圣经项目",
            "visual_bible_json": json.dumps(structured, ensure_ascii=False),
        }
    )

    assert record["visual_bible"] == structured
    assert record.get("visual_bible_json") is not None

    service.update(
        record["id"],
        {"visual_bible_json": json.dumps({"line": {"type": "工笔细线"}}, ensure_ascii=False)},
    )
    refreshed = service.get(record["id"])
    assert refreshed["visual_bible"] == {"line": {"type": "工笔细线"}}
    database.dispose()


def test_parse_visual_bible_兼容解析字段与原始JSON字符串() -> None:
    structured = {"line": {"type": "工笔细线"}}

    assert parse_visual_bible({"visual_bible": structured}) == structured
    assert parse_visual_bible(
        {"visual_bible_json": json.dumps(structured, ensure_ascii=False)}
    ) == structured
    assert parse_visual_bible({"visual_bible": None}) is None
    assert parse_visual_bible({"visual_bible_json": ""}) is None
    assert parse_visual_bible({"visual_bible_json": "not-json"}) is None
    assert parse_visual_bible({}) is None


def test_render_visual_style_spec_渲染已设置字段与禁用语义() -> None:
    derived = render_visual_style_spec(
        compile_visual_bible("东方粗笔重彩·写意水墨二维国漫风", None)
    )

    assert derived.startswith("结构化画风规范：")
    assert "线条：类型「粗笔焦墨」" in derived
    assert "景深：禁用" in derived
    assert "禁用PBR" in derived
    assert "禁用摄影级反射" in derived
    assert "画面质感「宣纸、水墨晕染、粗笔笔触」" in derived


def test_render_visual_style_spec_写实画风输出写实指纹() -> None:
    rendered = render_visual_style_spec(compile_visual_bible("写实电影级三维动画", None))
    assert "渲染方式锁定" in rendered
    assert render_visual_style_spec(compile_visual_bible("", None)) == ""
    # 带「国漫」但明确偏向 3D 写实：走三维指纹而非二维禁则
    rendered_mixed = render_visual_style_spec(compile_visual_bible("国漫3D写实", None))
    assert "渲染方式锁定" in rendered_mixed


def test_merge_visual_bible_negative_幂等合并禁用风格() -> None:
    spec = compile_visual_bible("水墨二维国漫", None)

    merged = merge_visual_bible_negative("多余手指", spec)
    assert "多余手指" in merged
    assert "PBR" in merged

    merged_twice = merge_visual_bible_negative(merged, spec)
    assert merged_twice == merged


def test_compile_asset_prompt_注入结构化约束与禁用风格() -> None:
    prompt = compile_asset_prompt(
        style_prompt="东方粗笔重彩·写意水墨二维国漫风",
        aspect_ratio="3:2",
        asset_type="character",
        asset_name="林小满",
        profile={"外观": "白色道袍"},
        prompt="少女持剑站立",
        negative_prompt="",
        consistency_keywords=["白色道袍"],
        composition="半身像",
    )

    assert "结构化画风规范：" in prompt
    assert "禁用PBR" in prompt
    assert "PBR" in prompt


def test_compile_asset_prompt_写实画风注入三维指纹而非二维约束() -> None:
    prompt = compile_asset_prompt(
        style_prompt="写实电影级三维动画",
        aspect_ratio="3:2",
        asset_type="character",
        asset_name="林小满",
        profile={},
        prompt="少女持剑站立",
        negative_prompt="",
        consistency_keywords=[],
        composition="半身像",
    )

    assert "渲染方式锁定" in prompt
    assert "材质：方式「PBR 材质」" in prompt
    assert "启用摄影级反射" in prompt
    assert "启用PBR" not in prompt
    assert "禁用PBR" not in prompt


def test_compile_keyframe_prompt_注入结构化约束() -> None:
    prompt = compile_keyframe_prompt(
        style_prompt="东方粗笔重彩·写意水墨二维国漫风",
        aspect_ratio="16:9",
        image_prompt="少女推门走入大殿",
        anchors=[],
        shot_context={
            "characters": "林小满",
            "scene_name": "大殿",
            "shot_type": "中景",
            "camera_angle": "平视",
        },
        negative_prompt="",
    )

    assert "结构化画风规范：" in prompt
    assert "景深：禁用" in prompt
    assert "negative prompt:" in prompt
    assert "PBR" in prompt


def test_compile_video_prompt_注入结构化约束() -> None:
    prompt = compile_video_prompt(
        style_prompt="东方粗笔重彩·写意水墨二维国漫风",
        aspect_ratio="16:9",
        video_prompt="少女推门走入大殿，停下环顾四周",
        duration_sec=6.0,
        anchors=["@Image1：当前分镜关键帧，锁定构图。"],
        shot_context={
            "scene_name": "大殿",
            "characters": ["林小满"],
            "props": [],
            "shot_type": "中景",
            "camera_angle": "平视",
            "opening_frame": "少女站在门前",
            "ending_frame": "少女站定环顾",
            "transition_from_prev": "scene_change",
            "dialogue_entries": [],
        },
        negative_prompt="",
    )

    assert "结构化画风规范：" in prompt
    assert "禁用PBR" in prompt
    assert "PBR" in prompt


def test_visual_bible_preview_api_返回推导结果(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'preview.db').as_posix()}")
    database.create_all()
    service = ProjectService(database, FileService(tmp_path / "files"))
    record = service.create(
        {"name": "推导预览项目", "style_prompt": "东方粗笔重彩·写意水墨二维国漫风"}
    )

    app = FastAPI()

    @app.exception_handler(ServiceError)
    async def handle_service_error(_: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    app.include_router(projects_api.router, prefix="/api")
    app.dependency_overrides[get_project_service] = lambda: service

    with TestClient(app) as client:
        response = client.get(f"/api/projects/{record['id']}/visual-bible-preview")
    assert response.status_code == 200
    body = response.json()
    assert body["style_prompt"] == "东方粗笔重彩·写意水墨二维国漫风"
    assert body["visual_bible"] is None
    assert body["derived"]["line"]["type"] == "粗笔焦墨"
    assert "PBR" in body["derived"]["forbidden_styles"]
    assert body["rendered"].startswith("结构化画风规范：")
    database.dispose()


def test_style_anchor_半写实注入渲染方式恒定与画风指纹() -> None:
    anchor = style_anchor("影视级半写实仿真人漫风", "16:9")

    assert "渲染方式恒定" in anchor
    assert "光色变化不得改变渲染方式" in anchor
    assert "渲染方式锁定：" in anchor
    assert "不是二维平涂动漫" in anchor
    # 抽象要求被具体渲染方式恒定条款替换
    assert "统一视觉规范" not in anchor


def test_compile_keyframe_prompt_职责隔离与行为链边界() -> None:
    prompt = compile_keyframe_prompt(
        style_prompt="影视级半写实仿真人漫风",
        aspect_ratio="16:9",
        image_prompt="她愣了半秒，随后把键盘往里一推，再拿起手机扫一眼群消息",
        anchors=["@Image1：苏棠家，锁定场景空间结构和光线。"],
        shot_context={
            "characters": "苏棠",
            "scene_name": "苏棠家",
            "shot_type": "中景",
            "camera_angle": "平视",
            "opening_frame": "苏棠坐在书桌前，双手刚离开键盘",
        },
        negative_prompt="",
    )

    assert "渲染方式锁定：" in prompt
    assert "参考图职责隔离" in prompt
    assert "本镜画面完全由【首帧关键构图】确定" in prompt
    assert "现在什么姿势" in prompt
    assert "色温变化不等于画风变化" in prompt
    assert "严格按照【参考】段画风指纹渲染" in prompt


def test_二维赛璐璐画风不反向禁用cel_shading() -> None:
    style = build_visual_style_spec(style_prompt="二维赛璐璐平涂")

    assert style.material.method == "绘画概括"
    assert "cel-shading" not in [item.casefold() for item in style.forbidden_styles]


def test_未命中关键词的画风不推导任何结构化画风() -> None:
    style = build_visual_style_spec(style_prompt="三渲二卡通渲染")

    assert style.render_fingerprint == ""
    assert style.material.method == ""
