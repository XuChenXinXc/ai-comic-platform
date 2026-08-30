from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from app.schemas import ProviderConfig

from .base import (
    ImageProvider,
    ProviderConfigurationError,
    TextProvider,
    TTSProvider,
    VideoProvider,
)
from .image import HttpImageProvider
from .mock import MockImageProvider, MockTextProvider, MockTTSProvider, MockVideoProvider
from .text import OpenAICompatibleTextProvider
from .tts import HttpTTSProvider, OpenAITTSProvider
from .video import HttpVideoProvider

Provider = TextProvider | ImageProvider | VideoProvider | TTSProvider
ProviderFactory = Callable[..., Provider]


class ProviderRegistry:
    def __init__(self) -> None:
        self._factories: dict[tuple[str, str], ProviderFactory] = {}

    def register(
        self,
        kind: str,
        adapter: str,
        factory: ProviderFactory,
        *,
        replace: bool = False,
    ) -> None:
        key = (kind, adapter)
        if key in self._factories and not replace:
            raise ProviderConfigurationError(f"Provider Adapter 已注册：{kind}/{adapter}")
        self._factories[key] = factory

    def create(self, config: ProviderConfig | Mapping[str, Any]) -> Provider:
        parsed = (
            config if isinstance(config, ProviderConfig) else ProviderConfig.model_validate(config)
        )
        factory = self._factories.get((parsed.kind, parsed.adapter))
        if factory is None:
            raise ProviderConfigurationError(
                f"未注册 Provider Adapter：{parsed.kind}/{parsed.adapter}"
            )
        if parsed.kind == "tts":
            return factory(parsed, parsed.api_key)
        return factory(parsed)

    def get(self, config: ProviderConfig | Mapping[str, Any]) -> Provider:
        return self.create(config)

    def adapters(self, kind: str | None = None) -> list[tuple[str, str]]:
        keys = self._factories
        if kind is not None:
            return sorted(key for key in keys if key[0] == kind)
        return sorted(keys)


provider_registry = ProviderRegistry()
provider_registry.register("text", "mock", MockTextProvider)
provider_registry.register("image", "mock", MockImageProvider)
provider_registry.register("video", "mock", MockVideoProvider)
provider_registry.register("tts", "mock", MockTTSProvider)
provider_registry.register("text", "openai_compatible", OpenAICompatibleTextProvider)
provider_registry.register("image", "http_image", HttpImageProvider)
provider_registry.register("video", "http_video", HttpVideoProvider)
provider_registry.register("tts", "openai_tts", OpenAITTSProvider)
provider_registry.register("tts", "http_tts", HttpTTSProvider)
