from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from app.config import load_project_environment
from app.providers.base import ProviderConfigurationError, ProviderError
from app.providers.image import HttpImageProvider
from app.providers.mock import MockImageProvider, MockVideoProvider
from app.providers.registry import provider_registry
from app.providers.text import OpenAICompatibleTextProvider
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
)

OUTPUT_SCHEMAS = (
    NovelAnalysisOutput,
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
        "recommended_size": "1024x1024",
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
