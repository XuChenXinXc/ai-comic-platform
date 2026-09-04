from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError

from app.config import BACKEND_DIR, PROJECT_ROOT, Settings, get_settings
from app.db import Database
from app.models.tables import platform_setting
from app.services.errors import ServiceError, bad_request
from app.services.utils import sanitize_error_message, utc_now

PROJECT_STORAGE_ROOT_KEY = "project_storage_root"
_MAX_WINDOWS_PROJECTS_DIR_UTF16_UNITS = 137
DirectorySelector = Callable[[], str | Path | None]


def select_windows_directory() -> Path | None:
    """使用 Windows 原生目录选择框选择项目存储根目录。"""
    if os.name != "nt":
        raise RuntimeError("当前系统不支持 Windows 目录选择框")

    import tkinter as tk
    from tkinter import filedialog

    root: tk.Tk | None = None
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(
            parent=root,
            title="选择项目存储根目录",
            mustexist=True,
        )
    except tk.TclError as exc:
        raise RuntimeError("无法打开 Windows 目录选择框") from exc
    finally:
        if root is not None:
            root.destroy()
    return Path(selected) if selected else None


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


class SettingService:
    def __init__(
        self,
        database: Database,
        *,
        settings: Settings | None = None,
        directory_selector: DirectorySelector = select_windows_directory,
    ) -> None:
        self.database = database
        self.settings = settings or get_settings()
        self.directory_selector = directory_selector

    def get_project_storage(self) -> dict[str, Any]:
        with self.database.engine.connect() as connection:
            stored_path = connection.scalar(
                select(platform_setting.c.value).where(
                    platform_setting.c.key == PROJECT_STORAGE_ROOT_KEY
                )
            )
        if stored_path:
            return {"path": str(Path(stored_path).resolve()), "source": "database"}
        if self.settings.project_storage_dir is not None:
            return {
                "path": str(self.settings.project_storage_dir.resolve()),
                "source": "environment",
            }
        return {
            "path": str((self.settings.workspace_dir / "projects").resolve()),
            "source": "default",
        }

    def select_project_storage(self) -> dict[str, Any]:
        try:
            selected = self.directory_selector()
        except Exception as exc:
            message = sanitize_error_message(exc, max_length=500)
            raise ServiceError(500, f"打开目录选择框失败：{message}") from exc
        if selected in (None, ""):
            return {"selected": False, **self.get_project_storage()}

        selected_path = self._validate_project_storage_path(Path(selected))
        self._verify_writable(selected_path)
        now = utc_now()
        statement = sqlite_insert(platform_setting).values(
            key=PROJECT_STORAGE_ROOT_KEY,
            value=str(selected_path),
            updated_at=now,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[platform_setting.c.key],
            set_={"value": str(selected_path), "updated_at": now},
        )
        try:
            with self.database.engine.begin() as connection:
                connection.execute(statement)
        except SQLAlchemyError as exc:
            raise ServiceError(500, "保存项目存储根目录失败，请重试") from exc
        return {
            "selected": True,
            "path": str(selected_path),
            "source": "database",
        }

    def _validate_project_storage_path(self, value: Path) -> Path:
        if not value.is_absolute():
            raise bad_request("项目存储根目录必须是绝对路径")
        path = value.resolve()
        if path.parent == path:
            raise bad_request("项目存储根目录不能是磁盘根目录")
        if len(str(path).encode("utf-16-le")) // 2 > _MAX_WINDOWS_PROJECTS_DIR_UTF16_UNITS:
            raise bad_request("项目存储根目录路径过深，请选择更短的目录")

        workspace_dir = self.settings.workspace_dir.resolve()
        for protected_dir in (PROJECT_ROOT.resolve(), workspace_dir):
            if _is_relative_to(protected_dir, path):
                raise bad_request("项目存储根目录不能是平台源码、工作区或其上级目录")
        for source_dir in (
            BACKEND_DIR.resolve(),
            (PROJECT_ROOT / "frontend").resolve(),
            (PROJECT_ROOT / ".git").resolve(),
        ):
            if _is_relative_to(path, source_dir):
                raise bad_request("项目存储根目录不能指向平台源码目录")
        if not path.is_dir():
            raise bad_request("项目存储根目录不存在或不是目录")
        return path

    @staticmethod
    def _verify_writable(path: Path) -> None:
        probe_dir: Path | None = None
        probe_file: Path | None = None
        try:
            probe_dir = Path(tempfile.mkdtemp(prefix=".storage-check-", dir=path))
            probe_file = probe_dir / "write-test.tmp"
            probe_file.write_bytes(b"ok")
        except OSError as exc:
            raise bad_request("项目存储根目录不可写，请检查目录权限") from exc
        finally:
            if probe_file is not None:
                probe_file.unlink(missing_ok=True)
            if probe_dir is not None and probe_dir.exists():
                probe_dir.rmdir()


# ImgBB 是平台共享的辅助服务密钥，存到 platform_setting 里并用 secret_service 加密。
# 数据库读取优先级高于 .env，避免明文密钥落盘。
IMGBB_API_KEY_SETTING = "imgbb_api_key"


def get_imagbb_api_key_override(database: Database) -> str:
    """从平台设置中读取已加密的 ImgBB Key；解密失败或未配置时返回空字符串。"""
    from app.services.secret_service import SecretServiceError, decrypt_secret

    try:
        with database.engine.connect() as connection:
            ciphertext = connection.scalar(
                select(platform_setting.c.value).where(
                    platform_setting.c.key == IMGBB_API_KEY_SETTING
                )
            )
    except SQLAlchemyError:
        return ""
    if not ciphertext:
        return ""
    try:
        return decrypt_secret(ciphertext)
    except SecretServiceError:
        return ""


def set_imagbb_api_key(database: Database, plaintext: str) -> None:
    """加密后写入平台设置；空字符串视为清除。"""
    from app.services.secret_service import SecretServiceError, encrypt_secret

    if plaintext:
        try:
            ciphertext = encrypt_secret(plaintext)
        except SecretServiceError as exc:
            raise ServiceError(500, f"加密 ImgBB API Key 失败：{exc}") from exc
    else:
        ciphertext = ""
    now = utc_now()
    statement = sqlite_insert(platform_setting).values(
        key=IMGBB_API_KEY_SETTING,
        value=ciphertext,
        updated_at=now,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[platform_setting.c.key],
        set_={"value": ciphertext, "updated_at": now},
    )
    with database.engine.begin() as connection:
        connection.execute(statement)


# 运镜策略：平台级设置，控制「加不加运镜」的激进程度。
# 克制（默认）：定镜优先，漏加不提醒；标准：双方向提醒；丰富：倾向运镜。
CAMERA_POLICY_SETTING = "camera_policy"


def get_camera_policy(database: Database) -> str:
    """读取平台运镜策略；未配置或非法值一律回落「克制」。"""
    from app.services.camera_library import DEFAULT_CAMERA_POLICY, normalize_policy

    try:
        with database.engine.connect() as connection:
            stored = connection.scalar(
                select(platform_setting.c.value).where(
                    platform_setting.c.key == CAMERA_POLICY_SETTING
                )
            )
    except SQLAlchemyError:
        stored = None
    return normalize_policy(stored) if stored else DEFAULT_CAMERA_POLICY


def set_camera_policy(database: Database, policy: str) -> str:
    """写入平台运镜策略；非法值拒绝。"""
    from app.services.camera_library import SUPPORTED_POLICIES, normalize_policy

    if policy not in SUPPORTED_POLICIES:
        raise bad_request(f"运镜策略必须是 {'/'.join(SUPPORTED_POLICIES)} 之一")
    normalized = normalize_policy(policy)
    now = utc_now()
    statement = sqlite_insert(platform_setting).values(
        key=CAMERA_POLICY_SETTING,
        value=normalized,
        updated_at=now,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[platform_setting.c.key],
        set_={"value": normalized, "updated_at": now},
    )
    try:
        with database.engine.begin() as connection:
            connection.execute(statement)
    except SQLAlchemyError as exc:
        raise ServiceError(500, "保存运镜策略失败，请重试") from exc
    return normalized
