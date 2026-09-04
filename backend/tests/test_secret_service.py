from __future__ import annotations

import os
import sys
import types

import pytest

from app.services import secret_service
from app.services.secret_service import (
    MacOSKeychainBackend,
    SecretServiceError,
    decrypt_secret,
    delete_secret,
    encrypt_secret,
)


class _FakeBackend:
    def encrypt(self, plaintext: str) -> str:
        return f"enc:{plaintext}"

    def decrypt(self, ciphertext: str) -> str:
        return ciphertext.removeprefix("enc:")

    def delete(self, ciphertext: str) -> None:
        # 默认后端不支持删除：无操作（DPAPI 密文不产生独立存储条目）。
        return None


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


def _fake_keyring_module() -> tuple[types.ModuleType, dict[tuple[str, str], str]]:
    """构造带 errors 层级的假 keyring 模块与底层存储字典。"""
    stored: dict[tuple[str, str], str] = {}

    class _KeyringError(Exception):
        pass

    errors = types.ModuleType("keyring.errors")
    errors.KeyringError = _KeyringError  # type: ignore[attr-defined]
    module = types.ModuleType("keyring")
    module.errors = errors  # type: ignore[attr-defined]
    module.set_password = lambda service, account, password: stored.__setitem__(  # type: ignore[attr-defined]
        (service, account), password
    )
    module.get_password = lambda service, account: stored.get((service, account))  # type: ignore[attr-defined]

    def _delete_password(service: str, account: str) -> None:
        if (service, account) not in stored:
            raise _KeyringError("entry not found")

        stored.pop((service, account))

    module.delete_password = _delete_password  # type: ignore[attr-defined]
    return module, stored


@pytest.fixture
def fake_keychain(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[MacOSKeychainBackend, dict[tuple[str, str], str]]:
    """在任意平台模拟 macOS Keychain：假 platform + 假 keyring 模块。"""
    module, stored = _fake_keyring_module()
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "keyring", module)
    monkeypatch.setitem(sys.modules, "keyring.errors", module.errors)
    return MacOSKeychainBackend(), stored


def test_keychain_delete_removes_account_entry(
    fake_keychain: tuple[MacOSKeychainBackend, dict[tuple[str, str], str]],
) -> None:
    """B-L8：delete 删除 Keychain 中的 account 条目。"""
    backend, stored = fake_keychain
    reference = backend.encrypt("old-secret")

    backend.delete(reference)

    assert ("ai-comic-platform", reference.removeprefix("keychain:")) not in stored


def test_keychain_delete_missing_entry_is_tolerated(
    fake_keychain: tuple[MacOSKeychainBackend, dict[tuple[str, str], str]],
) -> None:
    """B-L8：条目不存在时 delete 不抛错（可能已清理）。"""
    backend, _stored = fake_keychain

    backend.delete("keychain:api-key-missing")


def test_keychain_delete_ignores_non_keychain_ciphertext(
    fake_keychain: tuple[MacOSKeychainBackend, dict[tuple[str, str], str]],
) -> None:
    """B-L8：非 keychain: 前缀密文（如 DPAPI）不做清理。"""
    backend, _stored = fake_keychain

    backend.delete("not-a-keychain-reference")


def test_encrypt_secret_with_replaces_deletes_old_keychain_entry(
    fake_keychain: tuple[MacOSKeychainBackend, dict[tuple[str, str], str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-L8：替换密钥时旧 account 条目被删除，不再留下孤儿。"""
    backend, stored = fake_keychain
    monkeypatch.setattr(secret_service, "_backend", backend)
    old_reference = encrypt_secret("old-secret")
    assert old_reference.startswith("keychain:")

    new_reference = encrypt_secret("new-secret", replaces=old_reference)

    assert new_reference.startswith("keychain:")
    assert new_reference != old_reference
    # 旧条目已删除；新条目存在且可解密。
    assert ("ai-comic-platform", old_reference.removeprefix("keychain:")) not in stored
    assert decrypt_secret(new_reference) == "new-secret"


def test_delete_secret_skips_foreign_backend_ciphertext(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """delete_secret 遇到非本后端格式密文时静默跳过，不抛错。"""
    monkeypatch.setattr(secret_service, "_backend", _FakeBackend())

    delete_secret("any-ciphertext")

    # 空值同样是无操作。
    delete_secret("")
