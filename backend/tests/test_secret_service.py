from __future__ import annotations

import os
import sys

import pytest

from app.services import secret_service
from app.services.secret_service import (
    MacOSKeychainBackend,
    SecretServiceError,
    decrypt_secret,
    encrypt_secret,
)


class _FakeBackend:
    def encrypt(self, plaintext: str) -> str:
        return f"enc:{plaintext}"

    def decrypt(self, ciphertext: str) -> str:
        return ciphertext.removeprefix("enc:")


def test_secret_empty_value_returns_empty_ciphertext_and_plaintext() -> None:
    assert encrypt_secret("") == ""
    assert decrypt_secret("") == ""


def test_encrypt_secret_delegates_to_selected_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secret_service, "_backend", _FakeBackend())

    assert encrypt_secret("direct-provider-secret") == "enc:direct-provider-secret"


def test_decrypt_secret_delegates_to_selected_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secret_service, "_backend", _FakeBackend())

    assert decrypt_secret("enc:direct-provider-secret") == "direct-provider-secret"


@pytest.mark.skipif(os.name != "nt", reason="DPAPI 仅在 Windows 可用")
def test_secret_round_trip_returns_original_value() -> None:
    encrypted = encrypt_secret("direct-provider-secret")

    assert encrypted
    assert encrypted != "direct-provider-secret"
    assert decrypt_secret(encrypted) == "direct-provider-secret"


@pytest.mark.skipif(os.name != "nt", reason="DPAPI 仅在 Windows 可用")
def test_secret_invalid_ciphertext_raises_configuration_error() -> None:
    with pytest.raises(SecretServiceError, match="DPAPI"):
        decrypt_secret("not-a-valid-dpapi-blob")


@pytest.mark.skipif(sys.platform != "darwin", reason="Keychain 仅在 macOS 可用")
def test_macos_keychain_encrypt_stores_plaintext_in_keyring_and_returns_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keyring

    stored: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        keyring,
        "set_password",
        lambda service, account, password: stored.__setitem__((service, account), password),
    )
    monkeypatch.setattr(
        keyring,
        "get_password",
        lambda service, account: stored.get((service, account)),
    )
    backend = MacOSKeychainBackend()

    reference = backend.encrypt("direct-provider-secret")

    assert reference.startswith("keychain:")
    assert "direct-provider-secret" not in reference
    assert stored[("ai-comic-platform", reference.removeprefix("keychain:"))] == (
        "direct-provider-secret"
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="Keychain 仅在 macOS 可用")
def test_macos_keychain_decrypt_returns_original_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keyring

    stored = {("ai-comic-platform", "api-key-abc"): "direct-provider-secret"}
    monkeypatch.setattr(
        keyring,
        "get_password",
        lambda service, account: stored.get((service, account)),
    )
    backend = MacOSKeychainBackend()

    assert backend.decrypt("keychain:api-key-abc") == "direct-provider-secret"


@pytest.mark.skipif(sys.platform != "darwin", reason="Keychain 仅在 macOS 可用")
def test_macos_keychain_decrypt_missing_reference_raises_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda service, account: None)
    backend = MacOSKeychainBackend()

    with pytest.raises(SecretServiceError, match="未找到"):
        backend.decrypt("keychain:api-key-missing")


@pytest.mark.skipif(sys.platform != "darwin", reason="Keychain 仅在 macOS 可用")
def test_macos_keychain_decrypt_windows_dpapi_ciphertext_raises_error() -> None:
    backend = MacOSKeychainBackend()

    with pytest.raises(SecretServiceError, match="Windows"):
        backend.decrypt("not-a-valid-dpapi-blob")
