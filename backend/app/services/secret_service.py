from __future__ import annotations

import base64
import binascii
import logging
import os
import sys
import threading
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


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

    def delete(self, ciphertext: str) -> None:  # noqa: B027 — 默认空实现是有意设计
        """删除密文对应的存储条目；后端不支持删除时静默跳过。

        默认实现为空操作（DPAPI 密文不产生独立存储条目），
        因此不作为抽象方法强制子类实现。
        """


_CRYPTPROTECT_UI_FORBIDDEN = 0x1
# DPAPI 附加熵：同一 Windows 用户下的无关进程拿到密文也无法直接解密。
_DPAPI_ENTROPY = b"ai-comic-platform/api-key/v1"


class WindowsDPAPIBackend(SecretBackend):
    """Windows DPAPI 实现。

    ``ctypes.wintypes`` 只能在 Windows 导入（``VARIANT_BOOL._type_ = "v"`` 仅在
    Windows 编译），因此 ctypes 相关的导入与结构体定义全部推迟到实例化时，
    避免 macOS/Linux 上 import 本模块即失败。
    """

    def __init__(self) -> None:
        if os.name != "nt":
            raise SecretServiceError("直接 API Key 仅支持 Windows DPAPI")
        import ctypes
        from ctypes import wintypes

        class _DataBlob(ctypes.Structure):
            _fields_ = [
                ("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_byte)),
            ]

        self._ctypes = ctypes
        self._blob_type = _DataBlob
        self._crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        blob_pointer = ctypes.POINTER(_DataBlob)
        self._crypt32.CryptProtectData.argtypes = [
            blob_pointer,
            ctypes.c_wchar_p,
            blob_pointer,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            blob_pointer,
        ]
        self._crypt32.CryptProtectData.restype = wintypes.BOOL
        self._crypt32.CryptUnprotectData.argtypes = [
            blob_pointer,
            ctypes.POINTER(ctypes.c_wchar_p),
            blob_pointer,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            blob_pointer,
        ]
        self._crypt32.CryptUnprotectData.restype = wintypes.BOOL
        self._kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self._kernel32.LocalFree.restype = ctypes.c_void_p

    def _last_error(self, message: str) -> SecretServiceError:
        error = self._ctypes.get_last_error()
        return SecretServiceError(f"{message}：{self._ctypes.WinError(error)}")

    def _input_blob(self, value: bytes) -> tuple[Any, Any]:
        buffer = self._ctypes.create_string_buffer(value)
        blob = self._blob_type(
            len(value),
            self._ctypes.cast(buffer, self._ctypes.POINTER(self._ctypes.c_byte)),
        )
        return blob, buffer

    def encrypt(self, plaintext: str) -> str:
        ctypes = self._ctypes
        input_blob, _input_buffer = self._input_blob(plaintext.encode("utf-8"))
        entropy_blob, _entropy_buffer = self._input_blob(_DPAPI_ENTROPY)
        output_blob = self._blob_type()
        if not self._crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            None,
            ctypes.byref(entropy_blob),
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

    def _try_unprotect(self, encrypted: bytes, entropy: bytes | None) -> str | None:
        """按指定附加熵尝试解密；失败返回 ``None`` 交由调用方回退。"""
        ctypes = self._ctypes
        input_blob, _input_buffer = self._input_blob(encrypted)
        if entropy is None:
            entropy_reference = None
        else:
            entropy_blob, _entropy_buffer = self._input_blob(entropy)
            entropy_reference = ctypes.byref(entropy_blob)
        output_blob = self._blob_type()
        description = ctypes.c_wchar_p()
        if not self._crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            ctypes.byref(description),
            entropy_reference,
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        ):
            return None
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SecretServiceError("DPAPI 明文编码无效") from exc
        finally:
            self._kernel32.LocalFree(output_blob.pbData)
            if description:
                self._kernel32.LocalFree(description)

    def decrypt(self, ciphertext: str) -> str:
        try:
            encrypted = base64.b64decode(ciphertext.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error) as exc:
            raise SecretServiceError("DPAPI 密文格式无效") from exc
        # 新密文带附加熵；升级前保存的旧密文没有熵，必须仍能解开。
        for entropy in (_DPAPI_ENTROPY, None):
            plaintext = self._try_unprotect(encrypted, entropy)
            if plaintext is not None:
                if entropy is None:
                    logger.info("DPAPI legacy ciphertext detected")
                return plaintext
        raise self._last_error("DPAPI 解密失败")


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

    def delete(self, ciphertext: str) -> None:
        """B-L8：删除 Keychain 中的 account 条目，避免替换密钥后留下孤儿。

        仅处理 ``keychain:`` 引用；条目不存在视为已删除，失败只记录日志
        （清理失败不应阻塞新密钥的写入）。
        """
        if not ciphertext.startswith(self.REFERENCE_PREFIX):
            return
        account = ciphertext.removeprefix(self.REFERENCE_PREFIX)
        import keyring

        try:
            keyring.delete_password(self.SERVICE_NAME, account)
        except keyring.errors.KeyringError:
            logger.warning("macOS Keychain 条目删除失败（可能已不存在）：%s", account)


def _get_backend() -> SecretBackend:
    """按当前平台选择密钥存储后端。"""
    if os.name == "nt":
        return WindowsDPAPIBackend()
    if sys.platform == "darwin":
        return MacOSKeychainBackend()
    raise SecretServiceError(f"当前平台不支持直接 API Key：{sys.platform}")


_backend: SecretBackend | None = None
_backend_lock = threading.Lock()


def get_backend() -> SecretBackend:
    """惰性获取后端。

    不支持的平台只在真正使用「直接 API Key」时报错，而不是 import 本模块即失败，
    否则整个后端在 Linux 上都无法启动。
    """
    global _backend
    if _backend is None:
        with _backend_lock:
            if _backend is None:
                _backend = _get_backend()
    return _backend


def encrypt_secret(value: str, *, replaces: str = "") -> str:
    """加密明文；``replaces`` 为被替换的旧密文时同步清理其存储条目。

    B-L8：替换密钥时删除旧 account 条目（Keychain 后端），避免孤儿残留。
    旧条目清理失败只记录日志，不影响新密钥写入；空值与不支持删除的
    后端（DPAPI）为无操作。
    """
    if not value:
        delete_secret(replaces)
        return ""
    encrypted = get_backend().encrypt(value)
    if replaces:
        delete_secret(replaces)
    return encrypted


def decrypt_secret(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    return get_backend().decrypt(ciphertext)


def delete_secret(ciphertext: str) -> None:
    """删除密文对应的存储条目；空值或不支持的后端为无操作。"""
    if not ciphertext:
        return
    try:
        get_backend().delete(ciphertext)
    except SecretServiceError:
        # 非本后端格式的密文（如 macOS 上遇到 DPAPI 密文）无需清理，静默跳过。
        return
