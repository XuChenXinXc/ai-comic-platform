"""H3 Ref2VA 提交链接线测试：协议识别与对白结构化提取。"""

from __future__ import annotations

from app.services.media_service import MediaService


def test_uses_h3_video_protocol_by_explicit_protocol() -> None:
    assert MediaService._uses_h3_video_protocol({"params": {"protocol": "h3"}})
    assert not MediaService._uses_h3_video_protocol({"params": {"protocol": "seedance"}})
    assert not MediaService._uses_h3_video_protocol({"params": {}})


def test_uses_h3_video_protocol_by_hostname() -> None:
    assert MediaService._uses_h3_video_protocol(
        {"params": {}, "base_url": "https://h3.zhongbiao.cloud/v1"}
    )
    assert not MediaService._uses_h3_video_protocol(
        {"params": {}, "base_url": "https://api.example.com/v1"}
    )


def test_ref2va_dialogue_lines_prefers_structured_cues_with_times() -> None:
    input_data = {
        "plan": {
            "prompt_bundle": {
                "semantic_plan": {
                    "cues": {
                        "dialogue": [
                            {
                                "dialogue_line_id": "line-1",
                                "speaker": "苏月",
                                "text": "你还是要走吗？",
                                "start_time": 0.5,
                                "end_time": 3.0,
                            }
                        ]
                    }
                }
            }
        }
    }
    lines = MediaService._ref2va_dialogue_lines(input_data, "任何原文")
    assert lines == [("苏月", "你还是要走吗？", (0.5, 3.0))]


def test_ref2va_dialogue_lines_skips_empty_text_entries() -> None:
    input_data = {
        "plan": {
            "prompt_bundle": {
                "semantic_plan": {
                    "cues": {
                        "dialogue": [
                            {"speaker": "苏月", "text": "   "},
                            {
                                "speaker": "林风",
                                "text": "我会回来。",
                                "start_time": 2,
                                "end_time": 4,
                            },
                        ]
                    }
                }
            }
        }
    }
    assert MediaService._ref2va_dialogue_lines(input_data, "任何原文") == [
        ("林风", "我会回来。", (2.0, 4.0))
    ]


def test_ref2va_dialogue_lines_falls_back_to_section_text() -> None:
    source = "【对白与口型】\n第1句：苏月说：“你还是要走吗？”\n"
    assert MediaService._ref2va_dialogue_lines({"plan": None}, source) == [
        ("苏月", "你还是要走吗？")
    ]
