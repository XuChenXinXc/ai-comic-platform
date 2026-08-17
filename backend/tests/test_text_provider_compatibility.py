import asyncio
import json
from typing import Any

import pytest

from app.providers.text import OpenAICompatibleTextProvider
from app.schemas import (
    AssetPromptOutput,
    BatchRefinedPromptsOutput,
    NovelAnalysisOutput,
    ProviderConfig,
    ScreenplayOutput,
)


def _provider() -> OpenAICompatibleTextProvider:
    return OpenAICompatibleTextProvider(
        ProviderConfig(
            id="text-real",
            kind="text",
            adapter="openai_compatible",
            base_url="https://example.invalid/v1",
            model="test-model",
        )
    )


def _response(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {"content": json.dumps(payload, ensure_ascii=False)},
                "finish_reason": "stop",
            }
        ]
    }


def _analysis_payload() -> dict[str, Any]:
    return {
        "summary": "林舟在雨中走进钟表店。",
        "genre": "情感悬疑",
        "time_period": "现代都市",
        "locations": ["青石巷"],
        "worldview": "时间与修复",
        "main_characters": ["林舟"],
        "character_relations": [],
        "core_conflict": "林舟面对父亲留下的真相。",
        "plot_structure": {"beginning": "相遇"},
        "emotion_curve": [],
        "foreshadowing": "钟表同时响起的诡异现象",
        "visualizable_content": ["钟表店"],
        "hard_to_visualize": [],
        "production_risks": [],
    }


def test_openai_compatible_normalizes_singleton_list_field_without_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider()
    calls: list[dict[str, Any]] = []

    async def fake_request(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs["json"])
        return _response(_analysis_payload())

    monkeypatch.setattr(provider, "_request_json", fake_request)

    result = asyncio.run(
        provider.generate_json(
            system_prompt="分析小说",
            user_prompt="本章正文",
            schema=NovelAnalysisOutput,
            params={},
        )
    )

    assert result.foreshadowing == ["钟表同时响起的诡异现象"]
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("schema", "payload", "field", "expected"),
    [
        (
            AssetPromptOutput,
            {
                "prompt": "角色正面设定图",
                "negative_prompt": "模糊",
                "consistency_keywords": "黑色短发",
                "composition": "居中",
            },
            "consistency_keywords",
            ["黑色短发"],
        ),
        (
            BatchRefinedPromptsOutput,
            {"prompts": "镜头缓慢推进，人物停下观察。"},
            "prompts",
            ["镜头缓慢推进，人物停下观察。"],
        ),
    ],
)
def test_openai_compatible_normalizes_singleton_list_fields_for_other_steps(
    monkeypatch: pytest.MonkeyPatch,
    schema: type[Any],
    payload: dict[str, Any],
    field: str,
    expected: list[str],
) -> None:
    provider = _provider()

    async def fake_request(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return _response(payload)

    monkeypatch.setattr(provider, "_request_json", fake_request)

    result = asyncio.run(
        provider.generate_json(
            system_prompt="生成结构化结果",
            user_prompt="测试输入",
            schema=schema,
            params={},
        )
    )

    assert getattr(result, field) == expected


def test_openai_compatible_normalizes_single_scene_object_for_screenplay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider()
    payload = {
        "title": "钟表店",
        "logline": "林舟在钟表店发现父亲留下的录音。",
        "scenes": {
            "scene_no": 1,
            "scene_name": "青石巷钟表店",
            "time_weather": "傍晚，雨",
            "characters": ["林舟"],
            "objective": "寻找录音",
            "action": "林舟推门进入钟表店。",
            "dialogue": {"character": "林舟", "line": "有人吗？"},
            "narration": "雨声掩住了门轴声。",
            "emotion": "疑惑",
            "transition_to_next": "镜头切向柜台上的录音机。",
        },
    }

    async def fake_request(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return _response(payload)

    monkeypatch.setattr(provider, "_request_json", fake_request)

    result = asyncio.run(
        provider.generate_json(
            system_prompt="生成剧本",
            user_prompt="测试输入",
            schema=ScreenplayOutput,
            params={},
        )
    )

    assert len(result.scenes) == 1
    assert result.scenes[0].dialogue[0].speaker == "林舟"
    assert result.scenes[0].dialogue[0].text == "有人吗？"


def test_openai_compatible_extracts_string_segments_from_content_parts() -> None:
    payload = {"prompt": "清晰主体", "negative_prompt": "模糊"}

    result = OpenAICompatibleTextProvider._extract_content(
        {
            "choices": [
                {
                    "message": {
                        "content": [json.dumps(payload, ensure_ascii=False)],
                    }
                }
            ]
        }
    )

    assert result == json.dumps(payload, ensure_ascii=False)
