"""关键帧提示词去重压缩回归测试：通用限制只出现一次，避免稀释主体描述。"""

from app.services.prompt_service import compile_keyframe_prompt


def test_compile_keyframe_prompt_deduplicates_board_and_text_restrictions() -> None:
    prompt = compile_keyframe_prompt(
        style_prompt="二维国漫",
        aspect_ratio="16:9",
        image_prompt="林风站在大殿中央。",
        anchors=[],
        shot_context={
            "characters": "林风",
            "dialogue": "等等我。",
            "voice_over": "画外旁白",
            "shot_type": "中景",
            "camera_angle": "平视",
        },
        negative_prompt="",
    )

    assert prompt.count("不制作多格漫画") == 1
    assert prompt.count("不制作设定展板") == 1
    assert prompt.count("不制作动作分解图") == 1
    assert prompt.count("不在同一画面重复展示同一角色") == 1
    assert prompt.count("画面中不出现字幕") == 1
    # 文字禁令只在【成图限制】段出现一次，对白/画外音段不再重复
    assert "不得生成字幕或画面文字" not in prompt
