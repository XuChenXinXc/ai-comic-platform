"""OpenAI TTS Provider

支持 OpenAI TTS API 和兼容接口（如 deepseek、volcengine 等）。
"""

import asyncio
from typing import Any

import requests

from app.providers.base import (
    ProviderError,
    post_with_redirect_guard,
    validate_custom_headers,
    validate_provider_base_url,
    validate_provider_base_url_network,
)
from app.schemas import ProviderConfig
from app.services.constants import MAX_AUDIO_SIZE
from app.services.utils import sanitize_error_message


def _error_message(payload: Any, fallback: str) -> str:
    """从 Provider 错误体提取文本；错误体可能是任意结构，逐层防御。"""
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("detail") or error)
        if isinstance(error, str) and error:
            return error
        message = payload.get("message") or payload.get("detail")
        if isinstance(message, str) and message:
            return message
        return str(payload)
    if isinstance(payload, str) and payload:
        return payload
    return fallback


class OpenAITTSProvider:
    """OpenAI TTS API 兼容 Provider"""

    def __init__(self, config: ProviderConfig, api_key: str) -> None:
        self.config = config
        self.api_key = api_key
        # B-H/TTS：构造时校验 base_url，与 HttpProviderBase 一致，
        # 拒绝 userinfo / 敏感查询参数等不安全配置，并拒绝内网地址。
        validate_provider_base_url(config.base_url)
        validate_provider_base_url_network(config.base_url)
        self.base_url = config.base_url.rstrip("/") if config.base_url else "https://api.openai.com/v1"
        self.model = config.model or "tts-1"
        self.timeout = config.timeout_sec or 120
        self.custom_headers = validate_custom_headers(config.params)

    async def synthesize(
        self,
        *,
        text: str,
        voice_id: str,
        params: dict[str, Any],
    ) -> bytes:
        """
        生成语音音频

        Args:
            text: 要合成的文本
            voice_id: 音色 ID（如 alloy, echo, fable, onyx, nova, shimmer）
            params: 额外参数（如 speed: 0.25-4.0）

        Returns:
            音频二进制数据（MP3 格式）
        """
        if not text.strip():
            raise ProviderError("文本内容为空")

        url = f"{self.base_url}/audio/speech"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **self.custom_headers,
        }

        payload = {
            "model": self.model,
            "input": text,
            "voice": voice_id or "alloy",
            "response_format": params.get("response_format", "mp3"),
        }

        # 可选参数
        if "speed" in params:
            try:
                payload["speed"] = float(params["speed"])
            except (TypeError, ValueError) as exc:
                raise ProviderError("TTS speed 参数必须是数字") from exc

        try:
            # B-H/TTS：synthesize 是 async 函数，同步请求会阻塞事件循环；移入
            # 线程执行并逐跳校验重定向目标（防 30x 跳转到内网），保持函数签名
            # 与返回契约不变。
            response = await asyncio.to_thread(
                post_with_redirect_guard,
                url,
                headers=headers,
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()

            # 返回音频二进制
            content_type = response.headers.get("content-type", "")
            if "audio" not in content_type.lower() and "octet-stream" not in content_type.lower():
                # 可能是 JSON 错误响应
                try:
                    error_data = response.json()
                except (ValueError, KeyError):
                    error_data = None
                raise ProviderError(
                    f"TTS 生成失败：{_error_message(error_data, f'非音频数据（{content_type}）')}"
                )

            content = response.content
            if len(content) > MAX_AUDIO_SIZE:
                raise ProviderError(
                    f"TTS 返回音频超过大小上限（{len(content)} > {MAX_AUDIO_SIZE} 字节）"
                )
            return content

        except requests.HTTPError as exc:
            try:
                error_payload = exc.response.json() if exc.response is not None else None
            except (ValueError, KeyError):
                error_payload = None
            fallback = (
                exc.response.text[:200]
                if exc.response is not None and exc.response.text
                else str(exc)
            )
            raise ProviderError(
                f"TTS API 请求失败：{_error_message(error_payload, fallback)}"
            ) from exc
        except requests.RequestException as exc:
            raise ProviderError(f"TTS 网络请求失败：{sanitize_error_message(exc)}") from exc

    def supports_voice(self, voice_id: str) -> bool:
        """是否支持指定音色。

        兼容接口（deepseek/volcengine 等）的音色由渠道决定，无法在本地枚举，
        因此只拒绝空音色；具体可用性由渠道返回错误说明。
        """
        return bool(str(voice_id or "").strip())
