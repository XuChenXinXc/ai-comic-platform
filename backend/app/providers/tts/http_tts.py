"""HTTP TTS Provider

支持通用的 HTTP TTS API 接口。
"""

import asyncio
from typing import Any

import requests

from app.providers.base import (
    ProviderError,
    resolve_provider_url,
    validate_custom_headers,
    validate_provider_base_url,
)
from app.providers.tts.openai_tts import _error_message
from app.schemas import ProviderConfig
from app.services.constants import MAX_AUDIO_SIZE
from app.services.utils import sanitize_error_message


class HttpTTSProvider:
    """通用 HTTP TTS Provider"""

    def __init__(self, config: ProviderConfig, api_key: str) -> None:
        self.config = config
        self.api_key = api_key
        if not config.base_url:
            raise ProviderError("HTTP TTS Provider 必须配置 base_url")
        # B-H/TTS：构造时校验 base_url（拒绝 userinfo / 敏感查询参数）。
        validate_provider_base_url(config.base_url)
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
            # 构建请求 URL：endpoint 通过 resolve_provider_url 拼接，
            # 校验与 base_url 同源，防止相对路径注入穿越。
            # 使用 params.get 而不是 pop：params 是调用方字典，不能就地修改。
            endpoint = str(params.get("endpoint", "/tts") or "/tts")
            url = resolve_provider_url(self.base_url, endpoint)

            # 构建请求头
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                **self.custom_headers,
            }

            # 构建请求体：endpoint 是路由控制键，不进入请求体（与原 pop 语义一致）。
            body = {
                "text": text,
                "voice": voice_id,
                **{key: value for key, value in params.items() if key != "endpoint"},
            }

            # 发送请求：synthesize 是 async 函数，同步 requests.post 会阻塞
            # 事件循环；移入线程执行，保持函数签名与返回契约不变。
            response = await asyncio.to_thread(
                requests.post,
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
                f"HTTP TTS API 请求失败：{_error_message(error_payload, fallback)}"
            ) from exc
        except requests.RequestException as exc:
            raise ProviderError(f"HTTP TTS 网络请求失败：{sanitize_error_message(exc)}") from exc

    def supports_voice(self, voice_id: str) -> bool:
        """HTTP TTS Provider 假设支持所有音色（由实际 API 决定）"""
        return True
