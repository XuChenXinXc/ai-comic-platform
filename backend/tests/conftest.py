from __future__ import annotations

import sys

import pytest

from app.services import secret_service


class _InMemorySecretBackend:
    def encrypt(self, plaintext: str) -> str:
        return f"fake:{plaintext}"

    def decrypt(self, ciphertext: str) -> str:
        if not ciphertext.startswith("fake:"):
            raise secret_service.SecretServiceError("fake 后端无法解密")
        return ciphertext.removeprefix("fake:")


@pytest.fixture(autouse=True)
def _use_in_memory_secret_backend_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    """macOS 测试环境不写真实 Keychain，避免 CI/沙箱无法访问钥匙串。"""
    if sys.platform == "darwin":
        monkeypatch.setattr(secret_service, "_backend", _InMemorySecretBackend())
