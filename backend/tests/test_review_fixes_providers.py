"""Review 修复回归：TTS Provider、轮询清洗、local_file 校验、控制字段与 ImgBB 覆盖。

覆盖项：
- B-H/TTS：TTS Provider 构造时校验 base_url；http_tts endpoint 用
  resolve_provider_url 拼接；params 不被就地修改；同步 POST 走线程。
- B-L5：JSON 协议路径的轮询 task_id 也走字符白名单过滤。
- B-L4：Provider 返回的 local_file 必须位于平台工作区内。
- B-M8：图片 per-request params 过滤协议控制字段。
- B-L10：ImgBB Key 走运行时覆盖变量，不改写 settings 单例。
- B-L11：providers 包导出 TTSProvider 协议。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest
import requests

import app.providers.base as provider_base
from app.config import settings
from app.providers import TTSProvider
from app.providers.base import (
    ProviderConfigurationError,
    ProviderError,
    set_imgbb_api_key_override,
)
from app.providers.image import HttpImageProvider
from app.providers.tts import HttpTTSProvider, OpenAITTSProvider
from app.providers.video import HttpVideoProvider
from app.schemas import ProviderConfig, ProviderPollResult
from app.services.file_service import file_service


def _tts_config(base_url: str = "", **params: Any) -> ProviderConfig:
    return ProviderConfig(
        id="tts-test",
        kind="tts",
        adapter="http_tts",
        base_url=base_url,
        model="tts-1",
        params=params,
    )


def _audio_response() -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response._content = b"fake-mp3-bytes"
    response.headers["Content-Type"] = "audio/mpeg"
    return response


@pytest.mark.parametrize(
    "base_url",
    ["https://user:pass@tts.example/v1", "https://tts.example/v1?api_key=secret"],
)
def test_tts_providers_reject_unsafe_base_url_at_construction(base_url: str) -> None:
    """B-H/TTS：构造时校验 base_url，拒绝 userinfo 与敏感查询参数。"""
    with pytest.raises(ProviderConfigurationError):
        OpenAITTSProvider(_tts_config(base_url), "api-key")
    with pytest.raises(ProviderConfigurationError):
        HttpTTSProvider(_tts_config(base_url), "api-key")


def test_http_tts_requires_base_url() -> None:
    with pytest.raises(ProviderError, match="base_url"):
        HttpTTSProvider(_tts_config(""), "api-key")


def test_http_tts_synthesize_resolves_endpoint_and_does_not_mutate_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-H/TTS：endpoint 用 resolve_provider_url 拼接；params 不被就地修改。"""
    provider = HttpTTSProvider(_tts_config("https://tts.example/v1"), "api-key")
    captured: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> requests.Response:
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return _audio_response()

    monkeypatch.setattr(requests, "post", fake_post)
    params = {"endpoint": "custom/speak", "speed": 1.2}

    content = asyncio.run(
        provider.synthesize(text="你好", voice_id="alloy", params=params)
    )

    assert content == b"fake-mp3-bytes"
    # endpoint 与 base_url 同源拼接，而不是字符串硬拼。
    assert captured["url"] == "https://tts.example/v1/custom/speak"
    # endpoint 是路由控制键，不进入请求体；业务参数透传。
    assert captured["json"] == {"text": "你好", "voice": "alloy", "speed": 1.2}
    # 调用方字典未被修改（不再使用 pop）。
    assert params == {"endpoint": "custom/speak", "speed": 1.2}


def test_http_tts_synthesize_uses_default_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = HttpTTSProvider(_tts_config("https://tts.example/v1"), "api-key")
    captured: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> requests.Response:
        captured["url"] = url
        return _audio_response()

    monkeypatch.setattr(requests, "post", fake_post)
    asyncio.run(provider.synthesize(text="你好", voice_id="alloy", params={}))

    assert captured["url"] == "https://tts.example/v1/tts"


def test_openai_tts_synthesize_runs_post_in_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-H/TTS：async synthesize 内的同步 requests.post 移入线程执行。"""
    provider = OpenAITTSProvider(
        _tts_config("https://tts.openai.example/v1"), "api-key"
    )
    captured: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> requests.Response:
        captured["thread"] = sys.modules["threading"].current_thread().name
        return _audio_response()

    monkeypatch.setattr(requests, "post", fake_post)
    # 在事件循环内调用：若仍在循环线程同步执行 requests.post，会阻塞且
    # 线程名是主线程；to_thread 后应运行在线程池工作线程。
    async def call() -> bytes:
        return await provider.synthesize(text="你好", voice_id="alloy", params={})

    content = asyncio.run(call())

    assert content == b"fake-mp3-bytes"
    assert not captured["thread"].startswith("MainThread")


def test_http_tts_synthesize_runs_post_in_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = HttpTTSProvider(_tts_config("https://tts.example/v1"), "api-key")
    captured: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> requests.Response:
        captured["thread"] = sys.modules["threading"].current_thread().name
        return _audio_response()

    monkeypatch.setattr(requests, "post", fake_post)

    async def call() -> bytes:
        return await provider.synthesize(text="你好", voice_id="alloy", params={})

    asyncio.run(call())
    assert not captured["thread"].startswith("MainThread")


def test_video_poll_sanitizes_task_id_for_json_protocol_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-L5：JSON 协议（非 multipart）轮询路径同样过滤 task_id 注入字符。"""
    provider = HttpVideoProvider(
        ProviderConfig(
            id="video-grok-poll",
            kind="video",
            adapter="http_video",
            base_url="https://api.apib.ai/v1",
            model="grok-imagine-1.5-video-apimart",
        )
    )
    requested_paths: list[str] = []

    async def fake_request_json(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        del method, kwargs
        requested_paths.append(path)
        return {"data": {"status": "queued"}}

    monkeypatch.setattr(provider, "_request_json", fake_request_json)
    result = asyncio.run(provider.poll("../evil?x=1#y"))

    # 白名单只保留 [A-Za-z0-9_-]："../evil?x=1#y" -> "evilx1y"
    assert requested_paths == ["tasks/evilx1y"]
    assert result.status == "waiting_provider"


def test_download_rejects_local_file_outside_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-L4：Provider 返回的 local_file 超出工作区时拒绝复制（纵深防御）。"""
    monkeypatch.setattr(file_service, "workspace_dir", tmp_path.resolve())
    provider = HttpImageProvider(
        ProviderConfig(
            id="image-download",
            kind="image",
            adapter="http_image",
            base_url="https://provider.example/v1",
        )
    )
    outside = tmp_path.parent / "outside-secret.bin"
    result = ProviderPollResult(
        task_id="task-1",
        status="completed",
        data={"local_file": str(outside)},
    )

    with pytest.raises(ProviderError, match="工作区"):
        asyncio.run(provider.download(result, tmp_path / "target.png"))


def test_download_accepts_local_file_inside_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-L4：工作区内的 local_file（如 mock Provider 产物）正常复制。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(file_service, "workspace_dir", workspace.resolve())
    provider = HttpImageProvider(
        ProviderConfig(
            id="image-download",
            kind="image",
            adapter="http_image",
            base_url="https://provider.example/v1",
        )
    )
    source = workspace / "mock-result.bin"
    source.write_bytes(b"mock-bytes")
    result = ProviderPollResult(
        task_id="task-1",
        status="completed",
        data={"local_file": str(source)},
    )

    target = asyncio.run(provider.download(result, tmp_path / "target.bin"))

    assert target.read_bytes() == b"mock-bytes"


def test_image_submit_filters_protocol_control_fields_from_request_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-M8：per-request params 中的协议控制字段不进入上游载荷。"""
    provider = HttpImageProvider(
        ProviderConfig(
            id="image-control",
            kind="image",
            adapter="http_image",
            base_url="https://provider.example/v1",
            model="test-model",
        )
    )
    captured: dict[str, Any] = {}

    async def fake_request_json(
        method: str, path: str, **kwargs: Any
    ) -> dict[str, Any]:
        captured["json"] = kwargs.get("json")
        return {"data": [{"url": "https://cdn.example/generated.png"}]}

    monkeypatch.setattr(provider, "_request_json", fake_request_json)
    asyncio.run(
        provider.submit(
            prompt="测试提示词",
            negative_prompt="",
            reference_files=[],
            params={
                "submit_path": "evil/path",
                "headers": {"X-Custom": "value"},
                "verify_ssl": False,
                "retry_unsafe_methods": True,
                "test_path": "test",
                "size": "16:9",
            },
        )
    )

    payload = captured["json"]
    # 业务字段透传。
    assert payload["prompt"] == "测试提示词"
    assert payload["size"] == "16:9"
    # 协议控制字段被过滤，不进入上游载荷。
    for forbidden in ("submit_path", "headers", "verify_ssl", "retry_unsafe_methods", "test_path"):
        assert forbidden not in payload


def test_imgbb_key_override_takes_priority_over_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-L10：运行时覆盖优先于 settings 环境变量；清空后回退。"""
    monkeypatch.setattr(settings, "imgbb_api_key", "env-key")
    try:
        set_imgbb_api_key_override("runtime-key")
        assert provider_base.get_imgbb_api_key() == "runtime-key"
        # settings 单例未被改写。
        assert settings.imgbb_api_key == "env-key"
        set_imgbb_api_key_override("")
        assert provider_base.get_imgbb_api_key() == "env-key"
    finally:
        set_imgbb_api_key_override("")


def test_tianyue_public_upload_uses_runtime_imgbb_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-L10：天悦公网参考图上传读取运行时覆盖变量。"""
    from PIL import Image

    provider = HttpVideoProvider(
        ProviderConfig(
            id="video-tianyue",
            kind="video",
            adapter="http_video",
            base_url="https://api.tianyue.xyz/v1",
            model="tianyue-video",
        )
    )
    reference = tmp_path / "keyframe.png"
    Image.new("RGB", (64, 64), (10, 10, 10)).save(reference)
    captured: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> requests.Response:
        captured["url"] = url
        captured["params"] = kwargs.get("params")
        response = requests.Response()
        response.status_code = 200
        response._content = (
            b'{"success":true,"data":{"url":"https://i.ibb.co/abc/image.png"}}'
        )
        return response

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(settings, "imgbb_api_key", "env-key")
    set_imgbb_api_key_override("runtime-imgbb-key")
    try:
        prepared = asyncio.run(provider.prepare_input_image(reference))
    finally:
        set_imgbb_api_key_override("")

    assert captured["url"] == "https://api.imgbb.com/1/upload"
    assert captured["params"]["key"] == "runtime-imgbb-key"
    assert prepared["value"] == "https://i.ibb.co/abc/image.png"


def test_tianyue_public_upload_without_any_key_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = HttpVideoProvider(
        ProviderConfig(
            id="video-tianyue-nokey",
            kind="video",
            adapter="http_video",
            base_url="https://api.tianyue.xyz/v1",
            model="tianyue-video",
        )
    )
    reference = tmp_path / "keyframe.png"
    from PIL import Image

    Image.new("RGB", (64, 64), (10, 10, 10)).save(reference)
    monkeypatch.setattr(settings, "imgbb_api_key", "")
    set_imgbb_api_key_override("")
    try:
        with pytest.raises(ProviderConfigurationError, match="ImgBB"):
            asyncio.run(provider.prepare_input_image(reference))
    finally:
        set_imgbb_api_key_override("")


def test_tts_provider_protocol_is_exported() -> None:
    """B-L11：providers 包导出 TTSProvider 协议。"""
    assert TTSProvider is not None
    # runtime_checkable 协议：Mock TTS Provider 应满足结构化检查。
    from app.providers.mock import MockTTSProvider

    assert isinstance(MockTTSProvider(_tts_config()), TTSProvider)


def test_edge_tts_module_keeps_used_symbols_only() -> None:
    """B-H/TTS：死函数 generate/list_voices_sync 已删除，幻影导入不复存在。"""
    from app.providers.tts import edge_tts

    assert not hasattr(edge_tts, "generate")
    assert not hasattr(edge_tts, "list_voices_sync")
    assert hasattr(edge_tts, "COMMON_VOICES")
    assert hasattr(edge_tts, "_generate_tts")
    # 避免误删后留下未使用导入。
    assert "asyncio" not in vars(edge_tts)
    assert "TYPE_CHECKING" not in vars(edge_tts)
