from collections.abc import Iterable
from typing import Any

STYLE_ANCHOR = "[STYLE_ANCHOR]"

_SAFETY_REPLACEMENTS = {
    "血肉模糊": "受伤后的非血腥画面",
    "肢解": "以遮挡和画外结果表现严重冲突",
    "斩首": "以剪影表现致命冲突",
    "爆头": "强烈能量冲击后倒下",
    "内脏": "受损痕迹",
    "色情": "克制的情感表达",
    "全裸": "穿着得体服装",
}


def join_non_empty(parts: Iterable[Any]) -> str:
    return "\n".join(str(part).strip() for part in parts if str(part or "").strip())


def style_anchor(style_prompt: str, aspect_ratio: str) -> str:
    return (
        f"{STYLE_ANCHOR} {style_prompt.strip()}；画面比例 {aspect_ratio}；"
        "人物身份稳定，构图清楚，无文字水印"
    )


def apply_content_safety(prompt: str) -> str:
    compiled = prompt
    for unsafe, replacement in _SAFETY_REPLACEMENTS.items():
        compiled = compiled.replace(unsafe, replacement)
    return compiled


def compile_asset_prompt(
    *,
    style_prompt: str,
    aspect_ratio: str,
    prompt: str,
    consistency_keywords: list[str],
    composition: str,
) -> str:
    return apply_content_safety(
        join_non_empty(
            [
                style_anchor(style_prompt, aspect_ratio),
                prompt,
                "一致性锚点：" + "、".join(consistency_keywords) if consistency_keywords else "",
                f"构图：{composition}" if composition else "",
            ]
        )
    )


def compile_keyframe_prompt(
    *,
    style_prompt: str,
    aspect_ratio: str,
    image_prompt: str,
    anchors: list[str],
    continuity_instruction: str = "",
) -> str:
    return apply_content_safety(
        join_non_empty(
            [
                style_anchor(style_prompt, aspect_ratio),
                image_prompt,
                *anchors,
                continuity_instruction,
            ]
        )
    )


def compile_video_prompt(
    *,
    style_prompt: str,
    aspect_ratio: str,
    video_prompt: str,
    duration_sec: float,
) -> str:
    return apply_content_safety(
        join_non_empty(
            [
                style_anchor(style_prompt, aspect_ratio),
                video_prompt,
                f"实际时长：{duration_sec:g} 秒；以当前关键帧为开场状态",
                "角色身份参考只用于保持一致；禁止随机新增人物、服装和物品漂移",
            ]
        )
    )
