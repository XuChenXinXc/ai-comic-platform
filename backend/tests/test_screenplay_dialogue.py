from app.schemas import ScreenplayOutput


def test_screenplay_dialogue_with_line_alias_normalizes_to_text() -> None:
    screenplay = ScreenplayOutput.model_validate(
        {
            "title": "测试剧本",
            "logline": "测试梗概",
            "scenes": [
                {
                    "scene_no": 1,
                    "scene_name": "门口",
                    "time_weather": "夜晚",
                    "characters": ["小明"],
                    "objective": "见面",
                    "action": "小明推门。",
                    "dialogue": [{"speaker": "小明", "line": "你终于来了。"}],
                    "narration": [],
                    "emotion": "紧张",
                    "transition_to_next": "切入室内",
                }
            ],
        }
    )

    assert screenplay.model_dump(mode="json")["scenes"][0]["dialogue"] == [
        {"speaker": "小明", "text": "你终于来了。"}
    ]
