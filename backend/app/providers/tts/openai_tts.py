"""OpenAI TTS Provider

支持 OpenAI TTS API 和兼容接口（如 deepseek、volcengine 等）。
"""

from typing import Any

import requests

from app.providers.base import ProviderError, validate_custom_headers
from app.schemas import ProviderConfig
from app.services.utils import sanitize_error_message


class OpenAITTSProvider:
    """OpenAI TTS API 兼容 Provider"""

    def __init__(self, config: ProviderConfig, api_key: str) -> None:
        self.config = config
        self.api_key = api_key
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
            payload["speed"] = float(params["speed"])

        try:
            response = requests.post(
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
                    error_msg = error_data.get("error", {}).get("message", str(error_data))
                    raise ProviderError(f"TTS 生成失败：{error_msg}")
                except (ValueError, KeyError):
                    raise ProviderError(f"TTS 返回非音频数据：{content_type}")

            return response.content

        except requests.HTTPError as exc:
            try:
                error_data = exc.response.json()
                error_msg = error_data.get("error", {}).get("message", str(error_data))
            except (ValueError, KeyError):
                error_msg = exc.response.text[:200]
            raise ProviderError(f"TTS API 请求失败：{error_msg}") from exc
        except requests.RequestException as exc:
            raise ProviderError(f"TTS 网络请求失败：{sanitize_error_message(exc)}") from exc

    def supports_voice(self, voice_id: str) -> bool:
        """检查是否支持指定音色"""
        # OpenAI 官方音色
        official_voices = {"alloy", "echo", "fable", "onyx", "nova", "shimmer"}
        return voice_id in official_voices
