from __future__ import annotations

import base64
import binascii
import ctypes
import os
import sys
from abc import ABC, abstractmethod
from ctypes import wintypes


class SecretServiceError(RuntimeError):
    """本机密钥加解密失败。"""


class SecretBackend(ABC):
    """平台密钥存储后端。"""

    @abstractmethod
    def encrypt(self, plaintext: str) -> str:
        """加密明文，返回可在数据库保存的字符串。"""

    @abstractmethod
    def decrypt(self, ciphertext: str) -> str:
        """解密数据库中的密文/引用，返回明文。"""


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class WindowsDPAPIBackend(SecretBackend):
    """Windows DPAPI 实现。"""

    def __init__(self) -> None:
        if os.name != "nt":
            raise SecretServiceError("直接 API Key 仅支持 Windows DPAPI")
        self._crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def _last_error(self, message: str) -> SecretServiceError:
        error = ctypes.get_last_error()
        return SecretServiceError(f"{message}：{ctypes.WinError(error)}")

    @staticmethod
    def _input_blob(value: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
        buffer = ctypes.create_string_buffer(value)
        blob = _DataBlob(
            len(value),
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)),
        )
        return blob, buffer

    def encrypt(self, plaintext: str) -> str:
        self._crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.c_wchar_p,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        self._crypt32.CryptProtectData.restype = wintypes.BOOL
        self._kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self._kernel32.LocalFree.restype = ctypes.c_void_p

        input_blob, _input_buffer = self._input_blob(plaintext.encode("utf-8"))
        output_blob = _DataBlob()
        if not self._crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            None,
            None,
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        ):
            raise self._last_error("DPAPI 加密失败")
        try:
            encrypted = ctypes.string_at(output_blob.pbData, output_blob.cbData)
            return base64.b64encode(encrypted).decode("ascii")
        finally:
            self._kernel32.LocalFree(output_blob.pbData)

    def decrypt(self, ciphertext: str) -> str:
        try:
            encrypted = base64.b64decode(ciphertext.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error) as exc:
            raise SecretServiceError("DPAPI 密文格式无效") from exc

        self._crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.POINTER(ctypes.c_wchar_p),
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        self._crypt32.CryptUnprotectData.restype = wintypes.BOOL
        self._kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self._kernel32.LocalFree.restype = ctypes.c_void_p

        input_blob, _input_buffer = self._input_blob(encrypted)
        output_blob = _DataBlob()
        description = ctypes.c_wchar_p()
        if not self._crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            ctypes.byref(description),
            None,
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        ):
            raise self._last_error("DPAPI 解密失败")
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SecretServiceError("DPAPI 明文编码无效") from exc
        finally:
            self._kernel32.LocalFree(output_blob.pbData)
            if description:
                self._kernel32.LocalFree(description)


class MacOSKeychainBackend(SecretBackend):
    """macOS Keychain 实现。"""

    SERVICE_NAME = "ai-comic-platform"
    REFERENCE_PREFIX = "keychain:"

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise SecretServiceError("Keychain 仅支持 macOS")

    def encrypt(self, plaintext: str) -> str:
        import uuid

        import keyring

        account = f"api-key-{uuid.uuid4().hex}"
        try:
            keyring.set_password(self.SERVICE_NAME, account, plaintext)
        except keyring.errors.KeyringError as exc:
            raise SecretServiceError(f"macOS Keychain 保存失败：{exc}") from exc
        return f"{self.REFERENCE_PREFIX}{account}"

    def decrypt(self, ciphertext: str) -> str:
        import keyring

        if not ciphertext.startswith(self.REFERENCE_PREFIX):
            raise SecretServiceError(
                "无法解密该 API Key：由 Windows DPAPI 保存的密钥需在 macOS 重新填写"
            )
        account = ciphertext.removeprefix(self.REFERENCE_PREFIX)
        try:
            password = keyring.get_password(self.SERVICE_NAME, account)
        except keyring.errors.KeyringError as exc:
            raise SecretServiceError(f"macOS Keychain 读取失败：{exc}") from exc
        if password is None:
            raise SecretServiceError(f"macOS Keychain 中未找到密钥：{account}")
        return password


def _get_backend() -> SecretBackend:
    """按当前平台选择密钥存储后端。"""
    if os.name == "nt":
        return WindowsDPAPIBackend()
    if sys.platform == "darwin":
        return MacOSKeychainBackend()
    raise SecretServiceError(f"当前平台不支持直接 API Key：{sys.platform}")


_backend = _get_backend()


def encrypt_secret(value: str) -> str:
    if not value:
        return ""
    return _backend.encrypt(value)


def decrypt_secret(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    return _backend.decrypt(ciphertext)
