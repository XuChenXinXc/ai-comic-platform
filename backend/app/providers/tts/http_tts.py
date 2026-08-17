"""HTTP TTS Provider

支持通用的 HTTP TTS API 接口。
"""

from typing import Any

import requests

from app.providers.base import ProviderError, validate_custom_headers
from app.schemas import ProviderConfig
from app.services.utils import sanitize_error_message


class HttpTTSProvider:
    """通用 HTTP TTS Provider"""

    def __init__(self, config: ProviderConfig, api_key: str) -> None:
        self.config = config
        self.api_key = api_key
        if not config.base_url:
            raise ProviderError("HTTP TTS Provider 必须配置 base_url")
        self.base_url = config.base_url.rstrip("/")
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
        调用通用 HTTP TTS API 生成语音

        默认请求格式（可通过 params 自定义）：
        POST {base_url}/tts
        Headers:
            Authorization: Bearer {api_key}
            Content-Type: application/json
        Body:
            {
                "text": "要合成的文本",
                "voice": "音色ID",
                ...其他参数
            }

        返回: 音频字节流（MP3/WAV 等）
        """
        try:
            # 构建请求 URL
            endpoint = params.pop("endpoint", "/tts")
            url = f"{self.base_url}{endpoint}"

            # 构建请求头
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                **self.custom_headers,
            }

            # 构建请求体
            body = {
                "text": text,
                "voice": voice_id,
                **params,
            }

            # 发送请求
            response = requests.post(
                url,
                json=body,
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()

            # 检查响应类型
            content_type = response.headers.get("Content-Type", "")
            if not any(
                audio_type in content_type.lower()
                for audio_type in ["audio", "mpeg", "mp3", "wav", "ogg", "flac", "aac", "m4a"]
            ):
                try:
                    error_data = response.json()
                    error_msg = error_data.get("error", error_data.get("message", str(error_data)))
                    raise ProviderError(f"TTS API 返回错误：{error_msg}")
                except (ValueError, KeyError):
                    raise ProviderError(f"TTS 返回非音频数据：{content_type}") from None

            return response.content

        except requests.HTTPError as exc:
            try:
                error_data = exc.response.json()
                error_msg = error_data.get("error", {}).get("message", str(error_data))
            except (ValueError, KeyError):
                error_msg = exc.response.text[:200]
            raise ProviderError(f"HTTP TTS API 请求失败：{error_msg}") from exc
        except requests.RequestException as exc:
            raise ProviderError(f"HTTP TTS 网络请求失败：{sanitize_error_message(exc)}") from exc

    def supports_voice(self, voice_id: str) -> bool:
        """HTTP TTS Provider 假设支持所有音色（由实际 API 决定）"""
        return True
