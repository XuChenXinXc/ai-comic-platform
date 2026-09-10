"""对白口型按视频 Provider 能力降级/音频驱动的回归测试。"""

from typing import Any

from app.services.director_plan import (
    DialogueBinding,
    DirectorTimelineSegment,
    render_director_video_prompt,
)
from app.services.natural_video_prompt import render_natural_video_prompt


def _natural_prompt(*, audio_driven: bool) -> str:
    return render_natural_video_prompt(
        style_prompt="二维国漫",
        aspect_ratio="16:9",
        duration_sec=6,
        action_flow="人物向前走。",
        shot_context={"scene_name": "大殿", "characters": ["林风"]},
        dialogue_entries=[{"speaker": "林风", "text": "等等我。"}],
        audio_driven=audio_driven,
    )


def test_video_prompt_downgrades_lip_sync_without_audio_input() -> None:
    prompt = _natural_prompt(audio_driven=False)
    # 无音频输入：不要求自然配合，要求嘴部动作克制，避免与后期配音错位
    assert "口型与配音自然配合" not in prompt
    assert "轻微启合" in prompt
    assert "后期配音与字幕呈现" in prompt


def test_video_prompt_allows_natural_lip_sync_with_audio_input() -> None:
    prompt = _natural_prompt(audio_driven=True)
    assert "口型与配音自然配合" in prompt
    assert "轻微启合" not in prompt


def test_director_plan_render_respects_audio_driven() -> None:
    common: dict[str, Any] = {
        "style_prompt": "二维国漫",
        "aspect_ratio": "16:9",
        "director_plan": {
            "shot_purpose": "强调孤独",
            "audience_focus": {"first": "林风背影"},
            "composition": "negative-space",
        },
        "timeline": [DirectorTimelineSegment(start=0, end=6, action="林风转身")],
        "dialogue_bindings": [
            DialogueBinding(speaker="林风", text="等等我。", start_time=0.5, end_time=2.0)
        ],
        "initial_state": "林风站在大殿",
        "end_state": "林风转身",
    }
    degraded = render_director_video_prompt(**common, audio_driven=False)
    driven = render_director_video_prompt(**common, audio_driven=True)
    assert "轻微启合" in degraded
    assert "口型与配音自然配合" not in degraded
    assert "口型与配音自然配合" in driven
