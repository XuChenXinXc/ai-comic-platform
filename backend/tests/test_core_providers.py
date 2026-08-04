from __future__ import annotations

import asyncio
import base64
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
import requests
from PIL import Image

from app.config import load_project_environment
from app.providers.base import ProviderConfigurationError, ProviderError
from app.providers.image import HttpImageProvider
from app.providers.mock import MockImageProvider, MockVideoProvider
from app.providers.registry import provider_registry
from app.providers.text import OpenAICompatibleTextProvider
from app.providers.video import HttpVideoProvider
from app.schemas import (
    AssetExtractionOutput,
    AssetPromptOutput,
    NovelAnalysisOutput,
    ProviderConfig,
    ProviderPollResult,
    ScreenplayOutput,
    ShotExtractionOutput,
    ShotImagePromptOutput,
    ShotVideoPromptOutput,
    VisualStyleOutput,
)

OUTPUT_SCHEMAS = (
    NovelAnalysisOutput,
    VisualStyleOutput,
    ScreenplayOutput,
    AssetExtractionOutput,
    AssetPromptOutput,
    ShotExtractionOutput,
    ShotImagePromptOutput,
    ShotVideoPromptOutput,
)


def _config(kind: str, adapter: str, **params: Any) -> ProviderConfig:
    return ProviderConfig(
        id=f"{adapter}-{kind}",
        kind=kind,
        name="测试 Provider",
        adapter=adapter,
        model="test-model",
        params=params,
    )


def test_provider_api_key_in_project_env_file_is_available_to_http_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_name = "TEXT_TEST_PROJECT_API_KEY"
    monkeypatch.delenv(env_name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(f"{env_name}=provider-secret\n", encoding="utf-8")

    load_project_environment(env_file)
    provider = OpenAICompatibleTextProvider(
        ProviderConfig(
            id="text-real",
            kind="text",
            adapter="openai_compatible",
            api_key_env=env_name,
        )
    )

    assert provider._api_key() == "provider-secret"


def test_provider_api_key_with_unrelated_environment_name_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "sensitive-process-value")
    provider = OpenAICompatibleTextProvider(
        ProviderConfig(
            id="text-real",
            kind="text",
            adapter="openai_compatible",
            api_key_env="PATH",
        )
    )

    with pytest.raises(ProviderConfigurationError, match="TEXT"):
        provider._api_key()


def test_provider_direct_api_key_is_used_in_authorization_header() -> None:
    provider = OpenAICompatibleTextProvider(
        ProviderConfig(
            id="text-real",
            kind="text",
            adapter="openai_compatible",
            api_key="direct-provider-secret",
        )
    )

    assert provider._headers({})["Authorization"] == "Bearer direct-provider-secret"


def test_http_provider_request_with_cross_origin_absolute_path_is_rejected() -> None:
    provider = OpenAICompatibleTextProvider(
        ProviderConfig(
            id="text-real",
            kind="text",
            adapter="openai_compatible",
            base_url="https://provider.example/v1",
        )
    )

    with pytest.raises(ProviderConfigurationError, match="同源"):
        provider._url("https://attacker.example/collect")


def test_http_provider_with_json_http_error_preserves_provider_message_without_unsafe_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAICompatibleTextProvider(
        ProviderConfig(
            id="text-real",
            kind="text",
            adapter="openai_compatible",
            base_url="https://provider.example/v1",
            max_retries=1,
        )
    )
    unavailable = requests.Response()
    unavailable.status_code = 400
    unavailable.url = "https://provider.example/v1/images/generations"
    unavailable._content = (
        b'{"error":{"message":"No available compatible accounts","type":"api_error"}}'
    )
    unavailable.headers["Content-Type"] = "application/json"
    calls = 0

    def fake_request(*_args: Any, **_kwargs: Any) -> requests.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return unavailable
        raise requests.ConnectionError("connection reset")

    monkeypatch.setattr(requests, "request", fake_request)

    with pytest.raises(
        ProviderError,
        match=r"HTTP 400.*No available compatible accounts",
    ):
        provider._request(
            "POST",
            "https://provider.example/v1/images/generations",
            options={},
        )

    assert calls == 1


def test_http_provider_retries_post_on_transient_status_and_preserves_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """瞬时性错误（503 过载）对 POST 也重试，且最终错误仍保留上游原始消息。"""
    provider = OpenAICompatibleTextProvider(
        ProviderConfig(
            id="text-real",
            kind="text",
            adapter="openai_compatible",
            base_url="https://provider.example/v1",
            max_retries=1,
        )
    )
    unavailable = requests.Response()
    unavailable.status_code = 503
    unavailable.url = "https://provider.example/v1/chat/completions"
    unavailable._content = (
        b'{"error":{"message":"Service is too busy. We advise users to temporarily '
        b'switch to alternative LLM API service providers."}}'
    )
    unavailable.headers["Content-Type"] = "application/json"
    calls = 0

    def fake_request(*_args: Any, **_kwargs: Any) -> requests.Response:
        nonlocal calls
        calls += 1
        if calls <= 2:
            return unavailable
        raise requests.ConnectionError("connection reset")

    monkeypatch.setattr(requests, "request", fake_request)

    with pytest.raises(
        ProviderError,
        match=r"HTTP 503.*Service is too busy",
    ):
        provider._request(
            "POST",
            "https://provider.example/v1/chat/completions",
            options={},
        )

    assert calls == 2


@pytest.mark.parametrize("header_name", ["Authorization", "X-Signature"])
def test_http_provider_legacy_sensitive_header_is_rejected_at_runtime(
    header_name: str,
) -> None:
    provider = OpenAICompatibleTextProvider(
        ProviderConfig(
            id="text-real",
            kind="text",
            adapter="openai_compatible",
            base_url="https://provider.example/v1",
        )
    )

    with pytest.raises(ProviderConfigurationError, match="敏感"):
        provider._headers({"headers": {header_name: "legacy-secret"}})


def test_http_provider_cross_origin_download_does_not_send_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE_TEST_API_KEY", "provider-secret")
    provider = HttpImageProvider(
        ProviderConfig(
            id="image-real",
            kind="image",
            adapter="http_image",
            base_url="https://provider.example/v1",
            api_key_env="IMAGE_TEST_API_KEY",
        )
    )
    captured: dict[str, Any] = {}

    def fake_download(
        url: str,
        target: Path,
        *,
        headers: dict[str, str],
        timeout: int,
    ) -> Path:
        captured.update(url=url, headers=headers, timeout=timeout)
        target.write_bytes(b"downloaded")
        return target

    monkeypatch.setattr("app.providers.base.download_url_atomic", fake_download)
    target = tmp_path / "result.png"

    asyncio.run(
        provider.download(
            ProviderPollResult(
                task_id="remote-task",
                status="completed",
                result_url="https://cdn.example/result.png",
            ),
            target,
        )
    )

    assert captured["url"] == "https://cdn.example/result.png"
    assert captured["headers"] == {}
    assert target.read_bytes() == b"downloaded"


@pytest.mark.parametrize("schema", OUTPUT_SCHEMAS)
def test_mock_text_with_each_output_schema_returns_valid_model(
    schema: type[Any],
) -> None:
    provider = provider_registry.get(_config("text", "mock"))

    output = asyncio.run(
        provider.generate_json(
            system_prompt="系统提示",
            user_prompt="一个关于选择的短故事",
            schema=schema,
            params={},
        )
    )

    assert isinstance(output, schema)


def test_mock_shot_extraction_without_matching_catalog_name_keeps_unmatched_ids() -> None:
    provider = provider_registry.get(_config("text", "mock"))
    catalog = [
        {"id": "scene-office", "asset_type": "scene", "name": "办公室"},
        {"id": "character-xiaoming", "asset_type": "character", "name": "小明"},
    ]

    output = asyncio.run(
        provider.generate_json(
            system_prompt="提取分镜",
            user_prompt=(
                f"当前项目可用素材目录：\n{json.dumps(catalog, ensure_ascii=False)}\n"
                "来源内容：\n测试剧本"
            ),
            schema=ShotExtractionOutput,
            params={},
        )
    )

    assert output.shots[0].scene_asset_id == "mock-scene-id"
    assert output.shots[0].character_asset_ids == ["mock-character-id"]


def test_mock_shot_extraction_with_catalog_aliases_uses_alias_matches() -> None:
    provider = provider_registry.get(_config("text", "mock"))
    catalog = [
        {"id": "scene-office", "asset_type": "scene", "name": "办公室"},
        {
            "id": "scene-main",
            "asset_type": "scene",
            "name": "主场景内景",
            "aliases": ["主要场景"],
        },
        {"id": "character-xiaoming", "asset_type": "character", "name": "小明"},
        {
            "id": "character-main",
            "asset_type": "character",
            "name": "故事主人公",
            "aliases": ["主角"],
        },
    ]

    output = asyncio.run(
        provider.generate_json(
            system_prompt="提取分镜",
            user_prompt=(
                f"当前项目可用素材目录：\n{json.dumps(catalog, ensure_ascii=False)}\n"
                "来源内容：\n测试剧本"
            ),
            schema=ShotExtractionOutput,
            params={},
        )
    )

    assert output.shots[0].scene_asset_id == "scene-main"
    assert output.shots[0].character_asset_ids == ["character-main"]


def test_mock_image_submit_and_download_returns_decodable_png(tmp_path: Path) -> None:
    provider = MockImageProvider(_config("image", "mock"))
    submission = asyncio.run(
        provider.submit(
            prompt="角色设定图",
            negative_prompt="模糊",
            reference_files=[],
            params={"width": 320, "height": 180},
        )
    )
    result = ProviderPollResult(
        task_id=submission.task_id,
        status=submission.status,
        **submission.result,
    )
    target = tmp_path / "image.png"

    asyncio.run(provider.download(result, target))

    with Image.open(target) as image:
        image.verify()
    assert target.stat().st_size > 0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="本机未安装 FFmpeg")
def test_mock_video_after_provider_restart_continues_task_and_returns_playable_mp4(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.png"
    Image.new("RGB", (320, 180), (30, 60, 90)).save(frame)
    config = _config(
        "video",
        "mock",
        task_store_dir=str(tmp_path / "provider-tasks"),
        delay_sec=0,
    )
    first_provider = MockVideoProvider(config)
    prepared = asyncio.run(first_provider.prepare_input_image(frame))
    submission = asyncio.run(
        first_provider.submit(
            prompt="镜头缓慢推进",
            input_image=prepared,
            duration_sec=0.5,
            params={},
        )
    )

    restarted_provider = MockVideoProvider(config)
    deadline = time.monotonic() + 15
    while True:
        result = asyncio.run(restarted_provider.poll(submission.task_id))
        if result.status != "waiting_provider":
            break
        if time.monotonic() >= deadline:
            pytest.fail("Mock 视频任务轮询超时")
        time.sleep(0.05)

    assert result.status == "completed"
    target = tmp_path / "video.mp4"
    asyncio.run(restarted_provider.download(result, target))
    assert target.stat().st_size > 0
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        probe = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(target),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert probe.returncode == 0, probe.stderr
        assert float(probe.stdout.strip()) > 0


def test_openai_compatible_with_invalid_first_response_repairs_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAICompatibleTextProvider(
        ProviderConfig(
            id="text-real",
            kind="text",
            adapter="openai_compatible",
            base_url="https://example.invalid/v1",
            model="test-model",
        )
    )
    valid_payload = {
        "prompt": "清晰主体",
        "negative_prompt": "模糊",
        "consistency_keywords": ["固定服装"],
        "composition": "居中",
    }
    responses = [
        {"choices": [{"message": {"content": "{}"}}]},
        {"choices": [{"message": {"content": json.dumps(valid_payload)}}]},
    ]

    async def fake_request(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return responses.pop(0)

    monkeypatch.setattr(provider, "_request_json", fake_request)
    result = asyncio.run(
        provider.generate_json(
            system_prompt="输出素材提示词",
            user_prompt="主角",
            schema=AssetPromptOutput,
            params={},
        )
    )

    assert result.model_dump() == valid_payload
    assert responses == []


def test_openai_compatible_text_request_forces_high_reasoning_after_param_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAICompatibleTextProvider(
        ProviderConfig(
            id="text-real",
            kind="text",
            adapter="openai_compatible",
            base_url="https://example.invalid/v1",
            model="test-model",
            params={"reasoning_effort": "low"},
        )
    )
    payloads: list[dict[str, Any]] = []

    async def fake_request(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        payloads.append(kwargs["json"])
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "prompt": "清晰主体",
                                "negative_prompt": "模糊",
                                "consistency_keywords": ["固定服装"],
                                "composition": "居中",
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr(provider, "_request_json", fake_request)
    asyncio.run(
        provider.generate_json(
            system_prompt="输出素材提示词",
            user_prompt="主角",
            schema=AssetPromptOutput,
            params={"reasoning_effort": "medium"},
        )
    )

    assert [payload["reasoning_effort"] for payload in payloads] == ["high"]


def test_openai_compatible_text_repair_request_keeps_high_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAICompatibleTextProvider(
        ProviderConfig(
            id="text-real",
            kind="text",
            adapter="openai_compatible",
            base_url="https://example.invalid/v1",
            model="test-model",
        )
    )
    payloads: list[dict[str, Any]] = []
    responses = [
        {"choices": [{"message": {"content": "{}"}}]},
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "prompt": "清晰主体",
                                "negative_prompt": "模糊",
                                "consistency_keywords": ["固定服装"],
                                "composition": "居中",
                            }
                        )
                    }
                }
            ]
        },
    ]

    async def fake_request(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        payloads.append(kwargs["json"])
        return responses.pop(0)

    monkeypatch.setattr(provider, "_request_json", fake_request)
    asyncio.run(
        provider.generate_json(
            system_prompt="输出素材提示词",
            user_prompt="主角",
            schema=AssetPromptOutput,
            params={"reasoning_effort": "low"},
        )
    )

    assert [payload["reasoning_effort"] for payload in payloads] == ["high", "high"]


def test_openai_compatible_visual_style_keeps_prompts_plain_and_parses_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAICompatibleTextProvider(
        ProviderConfig(
            id="text-real",
            kind="text",
            adapter="openai_compatible",
            base_url="https://example.invalid/v1",
            model="test-model",
        )
    )
    style_prompt = "冷峻悬疑二维国漫，利落轮廓，低饱和冷色，分层暗光"
    payloads: list[dict[str, Any]] = []

    async def fake_request(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        payloads.append(kwargs["json"])
        return {"choices": [{"message": {"content": style_prompt}}]}

    monkeypatch.setattr(provider, "_request_json", fake_request)
    result = asyncio.run(
        provider.generate_json(
            system_prompt="完整系统提示词",
            user_prompt="完整用户提示词\n\n小说正文",
            schema=VisualStyleOutput,
            params={"response_format": {"type": "json_object"}},
        )
    )

    assert result.style_prompt == style_prompt
    assert payloads == [
        {
            "model": "test-model",
            "messages": [
                {"role": "system", "content": "完整系统提示词"},
                {"role": "user", "content": "完整用户提示词\n\n小说正文"},
            ],
            "reasoning_effort": "high",
        }
    ]


def test_openai_compatible_visual_style_repairs_invalid_length_as_plain_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAICompatibleTextProvider(
        ProviderConfig(
            id="text-real",
            kind="text",
            adapter="openai_compatible",
            base_url="https://example.invalid/v1",
            model="test-model",
        )
    )
    style_prompt = "都市温情二维国漫，克制线条，暖灰色调，柔和光影"
    responses = ["画" * 33, style_prompt]
    payloads: list[dict[str, Any]] = []

    async def fake_request(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        payloads.append(kwargs["json"])
        return {"choices": [{"message": {"content": responses.pop(0)}}]}

    monkeypatch.setattr(provider, "_request_json", fake_request)
    result = asyncio.run(
        provider.generate_json(
            system_prompt="完整系统提示词",
            user_prompt="完整用户提示词\n\n小说正文",
            schema=VisualStyleOutput,
            params={"reasoning_effort": "low"},
        )
    )

    assert result.style_prompt == style_prompt
    assert [payload["reasoning_effort"] for payload in payloads] == ["high", "high"]
    assert all("response_format" not in payload for payload in payloads)
    assert payloads[1]["messages"][:2] == [
        {"role": "system", "content": "完整系统提示词"},
        {"role": "user", "content": "完整用户提示词\n\n小说正文"},
    ]
    assert payloads[1]["messages"][-1]["content"].startswith(
        "上一次输出无法通过校验。请只返回修复后的一句纯文本"
    )


def test_http_image_with_custom_paths_maps_async_submission_and_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = HttpImageProvider(
        ProviderConfig(
            id="image-real",
            kind="image",
            adapter="http_image",
            base_url="https://example.invalid",
            params={
                "mode": "async",
                "submit_path": "create",
                "poll_path": "tasks/{task_id}",
                "task_id_path": "data.id",
                "status_path": "data.state",
                "result_url_path": "data.output.url",
                "completed_values": ["done"],
            },
        )
    )
    responses = [
        {"data": {"id": "remote-1", "state": "queued"}},
        {
            "data": {
                "id": "remote-1",
                "state": "done",
                "output": {"url": "https://example.invalid/result.png"},
            }
        },
    ]

    async def fake_request(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return responses.pop(0)

    monkeypatch.setattr(provider, "_request_json", fake_request)
    submission = asyncio.run(
        provider.submit(
            prompt="关键帧",
            negative_prompt="模糊",
            reference_files=[],
            params={},
        )
    )
    result = asyncio.run(provider.poll(submission.task_id))

    assert submission.task_id == "remote-1"
    assert submission.status == "waiting_provider"
    assert result.status == "completed"
    assert result.result_url == "https://example.invalid/result.png"


def test_gpt_image_2_uses_apib_submission_and_poll_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = HttpImageProvider(
        ProviderConfig(
            id="image-gpt",
            kind="image",
            adapter="http_image",
            base_url="https://api.apib.ai/v1",
            model="gpt-image-2",
        )
    )
    calls: list[dict[str, Any]] = []
    responses = [
        {
            "code": 200,
            "data": [{"task_id": "task-1", "status": "submitted"}],
        },
        {
            "code": 200,
            "data": {
                "id": "task-1",
                "status": "completed",
                "result": {
                    "images": [
                        {
                            "url": ["https://cdn.example/result.png"],
                        }
                    ]
                },
            },
        },
    ]

    async def fake_request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"method": method, "path": path, **kwargs})
        return responses.pop(0)

    reference = tmp_path / "character.png"
    reference.write_bytes(b"character-reference")
    monkeypatch.setattr(provider, "_request_json", fake_request)
    submission = asyncio.run(
        provider.submit(
            prompt="角色基础设定图",
            negative_prompt="模糊",
            reference_files=[reference],
            params={"size": "3840x2560"},
        )
    )
    result = asyncio.run(provider.poll(submission.task_id))

    assert calls[0]["method"] == "POST"
    assert calls[0]["path"] == "images/generations"
    assert calls[0]["json"] == {
        "resolution": "4k",
        "quality": "high",
        "n": 1,
        "size": "3840x2560",
        "prompt": "角色基础设定图",
        "image_urls": [
            "data:image/png;base64," + base64.b64encode(b"character-reference").decode("ascii")
        ],
        "model": "gpt-image-2",
    }
    assert calls[1]["method"] == "GET"
    assert calls[1]["path"] == "tasks/task-1"
    assert submission.task_id == "task-1"
    assert submission.status == "waiting_provider"
    assert result.status == "completed"
    assert result.result_url == "https://cdn.example/result.png"


def test_gpt_image_2_x5_without_references_uses_generation_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = HttpImageProvider(
        ProviderConfig(
            id="image-gpt-x5",
            kind="image",
            adapter="http_image",
            base_url="https://api.x5m5x.com/v1",
            model="gpt-image-2",
        )
    )
    calls: list[dict[str, Any]] = []

    async def fake_request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"method": method, "path": path, **kwargs})
        return {"data": [{"url": "https://cdn.example/generated.png"}]}

    monkeypatch.setattr(provider, "_request_json", fake_request)
    submission = asyncio.run(
        provider.submit(
            prompt="角色基础设定图",
            negative_prompt="模糊",
            reference_files=[],
            params={"size": "3840x2560"},
        )
    )

    assert calls[0]["method"] == "POST"
    assert calls[0]["path"] == "images/generations"
    assert calls[0]["json"] == {
        "quality": "high",
        "response_format": "url",
        "size": "3840x2560",
        "prompt": "角色基础设定图",
        "model": "gpt-image-2",
    }
    assert submission.status == "completed"
    assert submission.result["result_url"] == "https://cdn.example/generated.png"


def test_gpt_image_2_x5_with_references_uses_multipart_image_edits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = HttpImageProvider(
        ProviderConfig(
            id="image-gpt-x5",
            kind="image",
            adapter="http_image",
            base_url="https://api.x5m5x.com/v1",
            model="gpt-image-2",
        )
    )
    first_reference = tmp_path / "character.png"
    second_reference = tmp_path / "scene.webp"
    first_reference.write_bytes(b"character-reference")
    second_reference.write_bytes(b"scene-reference")
    captured: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> requests.Response:
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = kwargs["headers"]
        captured["data"] = kwargs["data"]
        captured["file_fields"] = [field for field, _ in kwargs["files"]]
        captured["file_names"] = [value[0] for _, value in kwargs["files"]]
        captured["file_contents"] = [
            value[1].read() if hasattr(value[1], "read") else value[1]
            for _, value in kwargs["files"]
        ]
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(
            {
                "data": [
                    {
                        "b64_json": base64.b64encode(b"generated-image").decode("ascii"),
                    }
                ]
            }
        ).encode("utf-8")
        return response

    monkeypatch.setattr(requests, "request", fake_request)
    submission = asyncio.run(
        provider.submit(
            prompt="林风站在山门石阶上",
            negative_prompt="模糊",
            reference_files=[first_reference, second_reference],
            params={"size": "3840x2160"},
        )
    )

    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.x5m5x.com/v1/images/edits"
    assert "Content-Type" not in captured["headers"]
    assert captured["data"] == {
        "quality": "high",
        "n": "1",
        "size": "3840x2160",
        "prompt": "林风站在山门石阶上",
        "model": "gpt-image-2",
    }
    assert captured["file_fields"] == ["image[]", "image[]"]
    assert captured["file_names"] == ["character.png", "scene.webp"]
    assert captured["file_contents"] == [b"character-reference", b"scene-reference"]
    assert submission.status == "completed"
    assert submission.result["content_base64"] == base64.b64encode(b"generated-image").decode(
        "ascii"
    )


def test_gpt_image_2_x5_multipart_retry_resends_complete_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = HttpImageProvider(
        ProviderConfig(
            id="image-gpt-x5",
            kind="image",
            adapter="http_image",
            base_url="https://api.x5m5x.com/v1",
            model="gpt-image-2",
            max_retries=1,
        )
    )
    reference = tmp_path / "character.png"
    reference.write_bytes(b"complete-reference")
    sent_contents: list[bytes] = []

    def fake_request(_method: str, _url: str, **kwargs: Any) -> requests.Response:
        value = kwargs["files"][0][1][1]
        sent_contents.append(value.read() if hasattr(value, "read") else value)
        if len(sent_contents) == 1:
            raise requests.ConnectionError("connection reset")
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"data":[{"b64_json":"ZmFrZQ=="}]}'
        return response

    monkeypatch.setattr(requests, "request", fake_request)
    submission = asyncio.run(
        provider.submit(
            prompt="角色基础设定图",
            negative_prompt="",
            reference_files=[reference],
            params={"size": "3840x2560"},
        )
    )

    # POST 允许重试：第一次连接被重置后自动重试，第二次成功；
    # 重试时 multipart 参考文件重新读取，两次发送内容完整一致。
    assert sent_contents == [b"complete-reference", b"complete-reference"]
    assert submission.status == "completed"


def test_gpt_image_2_x5_multipart_retry_can_be_disabled_by_params(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider 配置 params 显式传 retry_unsafe_methods=false 时，POST 不重试（兼容行为）。"""
    provider = HttpImageProvider(
        ProviderConfig(
            id="image-gpt-x5",
            kind="image",
            adapter="http_image",
            base_url="https://api.x5m5x.com/v1",
            model="gpt-image-2",
            max_retries=1,
            params={"retry_unsafe_methods": False},
        )
    )
    reference = tmp_path / "character.png"
    reference.write_bytes(b"complete-reference")
    sent_contents: list[bytes] = []
    failures = 0

    def fake_request(_method: str, _url: str, **kwargs: Any) -> requests.Response:
        nonlocal failures
        failures += 1
        value = kwargs["files"][0][1][1]
        sent_contents.append(value.read() if hasattr(value, "read") else value)
        raise requests.ConnectionError("connection reset")

    monkeypatch.setattr(requests, "request", fake_request)
    with pytest.raises(ProviderError, match="Provider 请求失败"):
        asyncio.run(
            provider.submit(
                prompt="角色基础设定图",
                negative_prompt="",
                reference_files=[reference],
                params={"size": "3840x2560"},
            )
        )

    assert failures == 1
    assert sent_contents == [b"complete-reference"]


@pytest.mark.parametrize(
    "model",
    [
        "grok-imagine-1.5-video-apimart",
        "grok-imagine-1.5-video-ext",
    ],
)
def test_grok_video_models_upload_local_input_image_to_apib(
    model: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = HttpVideoProvider(
        ProviderConfig(
            id="video-grok",
            kind="video",
            adapter="http_video",
            base_url="https://api.apib.ai/v1",
            model=model,
        )
    )
    reference = tmp_path / "keyframe.png"
    reference.write_bytes(b"keyframe")
    captured: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> requests.Response:
        captured.update(method=method, url=url, kwargs=kwargs)
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"url":"https://getapib.org/image/keyframe.png"}'
        return response

    monkeypatch.setattr(requests, "request", fake_request)
    prepared = asyncio.run(provider.prepare_input_image(reference))

    assert prepared == {
        "value": "https://getapib.org/image/keyframe.png",
        "mode": "upload",
        "raw": {"url": "https://getapib.org/image/keyframe.png"},
    }
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.apib.ai/v1/uploads/images"
    assert "file" in captured["kwargs"]["files"]


@pytest.mark.parametrize(
    "model",
    [
        "grok-imagine-1.5-video-apimart",
        "grok-imagine-1.5-video-ext",
    ],
)
def test_grok_video_models_use_apib_submission_and_poll_contract(
    model: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = HttpVideoProvider(
        ProviderConfig(
            id="video-grok",
            kind="video",
            adapter="http_video",
            base_url="https://api.apib.ai/v1",
            model=model,
        )
    )
    calls: list[dict[str, Any]] = []
    responses = [
        {
            "code": 200,
            "data": [{"task_id": "task-1", "status": "submitted"}],
        },
        {
            "code": 200,
            "data": {
                "id": "task-1",
                "status": "completed",
                "result": {
                    "videos": [
                        {
                            "url": ["https://cdn.example/result.mp4"],
                        }
                    ]
                },
            },
        },
    ]

    async def fake_request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"method": method, "path": path, **kwargs})
        return responses.pop(0)

    monkeypatch.setattr(provider, "_request_json", fake_request)
    submission = asyncio.run(
        provider.submit(
            prompt="让画面动起来",
            input_image={
                "values": [
                    "data:image/png;base64,AAAA",
                    "data:image/png;base64,BBBB",
                ]
            },
            duration_sec=3.0,
            params={},
        )
    )
    result = asyncio.run(provider.poll(submission.task_id))

    assert provider.supports_multiple_input_images is True
    assert calls[0]["method"] == "POST"
    assert calls[0]["path"] == "videos/generations"
    assert calls[0]["json"] == {
        "size": "16:9",
        "quality": "720p",
        "prompt": "让画面动起来",
        "image_urls": [
            "data:image/png;base64,AAAA",
            "data:image/png;base64,BBBB",
        ],
        "duration": 6,
        "model": model,
    }
    assert type(calls[0]["json"]["duration"]) is int
    assert calls[1]["method"] == "GET"
    assert calls[1]["path"] == "tasks/task-1"
    assert submission.status == "waiting_provider"
    assert result.status == "completed"
    assert result.result_url == "https://cdn.example/result.mp4"


@pytest.mark.parametrize(
    "model",
    [
        "doubao-seedance-2.0",
        "doubao-seedance-2.0-fast",
        "doubao-seedance-2.0-mini",
    ],
)
def test_doubao_video_models_pass_storyboard_duration_verbatim(
    model: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """doubao 系列：LLM 分镜填多少秒就传多少秒（4~15 秒整数），不取整不抬升。"""
    provider = HttpVideoProvider(
        ProviderConfig(
            id="video-doubao",
            kind="video",
            adapter="http_video",
            base_url="https://api.apib.ai/v1",
            model=model,
        )
    )
    calls: list[dict[str, Any]] = []
    responses = [
        {
            "code": 200,
            "data": [{"task_id": "task-1", "status": "submitted"}],
        },
        {
            "code": 200,
            "data": {
                "id": "task-1",
                "status": "completed",
                "result": {
                    "videos": [
                        {
                            "url": ["https://cdn.example/result.mp4"],
                        }
                    ]
                },
            },
        },
    ]

    async def fake_request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"method": method, "path": path, **kwargs})
        return responses.pop(0)

    monkeypatch.setattr(provider, "_request_json", fake_request)
    submission = asyncio.run(
        provider.submit(
            prompt="让画面动起来",
            input_image={
                "values": [
                    "data:image/png;base64,AAAA",
                    "data:image/png;base64,BBBB",
                ]
            },
            duration_sec=4.0,
            params={},
        )
    )
    result = asyncio.run(provider.poll(submission.task_id))

    assert provider.supports_multiple_input_images is True
    assert calls[0]["method"] == "POST"
    assert calls[0]["path"] == "videos/generations"
    assert calls[0]["json"] == {
        "size": "16:9",
        "resolution": "720p",
        "generate_audio": True,
        "prompt": "让画面动起来",
        "image_urls": [
            "data:image/png;base64,AAAA",
            "data:image/png;base64,BBBB",
        ],
        "duration": 4,
        "model": model,
    }
    assert type(calls[0]["json"]["duration"]) is int
    assert calls[1]["method"] == "GET"
    assert calls[1]["path"] == "tasks/task-1"
    assert result.status == "completed"
    assert result.result_url == "https://cdn.example/result.mp4"


@pytest.mark.parametrize(
    ("duration_sec", "expected_message"),
    [
        (3.0, "不能小于 4 秒"),
        (16.0, "不能大于 15 秒"),
        (4.5, "只接受整数秒"),
    ],
)
def test_doubao_video_duration_range_is_validated_before_submission(
    duration_sec: float,
    expected_message: str,
) -> None:
    provider = HttpVideoProvider(
        ProviderConfig(
            id="video-doubao",
            kind="video",
            adapter="http_video",
            base_url="https://api.apib.ai/v1",
            model="doubao-seedance-2.0-mini",
        )
    )
    with pytest.raises(ProviderError, match=expected_message):
        asyncio.run(
            provider.submit(
                prompt="让画面动起来",
                input_image={"value": "data:image/png;base64,AAAA"},
                duration_sec=duration_sec,
                params={},
            )
        )


@pytest.mark.parametrize(
    "duration_sec",
    [4.0, 15.0],
)
def test_doubao_video_accepts_boundary_durations(
    duration_sec: float,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4 秒与 15 秒是合法边界，原样透传。"""
    provider = HttpVideoProvider(
        ProviderConfig(
            id="video-doubao",
            kind="video",
            adapter="http_video",
            base_url="https://api.apib.ai/v1",
            model="doubao-seedance-2.0-mini",
        )
    )
    calls: list[dict[str, Any]] = []

    async def fake_request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"method": method, "path": path, **kwargs})
        return {
            "code": 200,
            "data": [{"task_id": "task-1", "status": "submitted"}],
        }

    monkeypatch.setattr(provider, "_request_json", fake_request)
    asyncio.run(
        provider.submit(
            prompt="让画面动起来",
            input_image={"value": "data:image/png;base64,AAAA"},
            duration_sec=duration_sec,
            params={},
        )
    )
    assert calls[0]["json"]["duration"] == int(duration_sec)


def test_doubao_video_accepts_exactly_nine_reference_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = HttpVideoProvider(
        ProviderConfig(
            id="video-doubao",
            kind="video",
            adapter="http_video",
            base_url="https://api.apib.ai/v1",
            model="doubao-seedance-2.0-mini",
        )
    )
    calls: list[dict[str, Any]] = []

    async def fake_request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"method": method, "path": path, **kwargs})
        return {
            "code": 200,
            "data": [{"task_id": "task-1", "status": "submitted"}],
        }

    monkeypatch.setattr(provider, "_request_json", fake_request)
    asyncio.run(
        provider.submit(
            prompt="让画面动起来",
            input_image={
                "values": [f"data:image/png;base64,{index}" for index in range(9)]
            },
            duration_sec=5.0,
            params={},
        )
    )
    assert len(calls[0]["json"]["image_urls"]) == 9


def test_doubao_video_rejects_more_than_nine_reference_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = HttpVideoProvider(
        ProviderConfig(
            id="video-doubao",
            kind="video",
            adapter="http_video",
            base_url="https://api.apib.ai/v1",
            model="doubao-seedance-2.0-mini",
        )
    )

    async def fake_request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "code": 200,
            "data": [{"task_id": "task-1", "status": "submitted"}],
        }

    monkeypatch.setattr(provider, "_request_json", fake_request)
    with pytest.raises(ProviderError, match="参考图数量"):
        asyncio.run(
            provider.submit(
                prompt="让画面动起来",
                input_image={
                    "values": [
                        f"data:image/png;base64,{index}" for index in range(10)
                    ]
                },
                duration_sec=5.0,
                params={},
            )
        )


def test_generic_http_video_provider_defaults_to_single_input_image() -> None:
    provider = HttpVideoProvider(
        ProviderConfig(
            id="video-generic",
            kind="video",
            adapter="http_video",
            base_url="https://provider.example/v1",
            model="generic-video-model",
        )
    )

    assert provider.supports_multiple_input_images is False


def test_http_image_with_too_many_references_rejects_before_submission(
    tmp_path: Path,
) -> None:
    provider = HttpImageProvider(
        ProviderConfig(
            id="image-limited",
            kind="image",
            adapter="http_image",
            base_url="https://example.invalid",
            params={"max_reference_files": 0},
        )
    )
    reference = tmp_path / "reference.png"
    reference.write_bytes(b"image")

    with pytest.raises(ProviderError, match="参考图数量"):
        asyncio.run(
            provider.submit(
                prompt="关键帧",
                negative_prompt="",
                reference_files=[reference],
                params={},
            )
        )


def test_grok_video_upload_retries_transient_status_with_complete_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """视频关键帧上传遇 503 重试时，两次发送的文件内容必须完整一致（不得发空文件）。"""
    provider = HttpVideoProvider(
        ProviderConfig(
            id="video-grok",
            kind="video",
            adapter="http_video",
            base_url="https://api.apib.ai/v1",
            model="grok-imagine-1.5-video-apimart",
            max_retries=1,
        )
    )
    reference = tmp_path / "keyframe.png"
    reference.write_bytes(b"complete-keyframe-content")
    sent_contents: list[bytes] = []

    def fake_request(_method: str, _url: str, **kwargs: Any) -> requests.Response:
        value = kwargs["files"]["file"][1]
        sent_contents.append(value.read() if hasattr(value, "read") else value)
        if len(sent_contents) == 1:
            response = requests.Response()
            response.status_code = 503
            response._content = b''
            return response
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"url":"https://getapib.org/image/keyframe.png"}'
        return response

    monkeypatch.setattr(requests, "request", fake_request)
    prepared = asyncio.run(provider.prepare_input_image(reference))

    assert len(sent_contents) == 2
    assert sent_contents == [b"complete-keyframe-content", b"complete-keyframe-content"]
    assert prepared["value"] == "https://getapib.org/image/keyframe.png"
