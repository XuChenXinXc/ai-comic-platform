from __future__ import annotations

import os
import re
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote
from uuid import uuid4

import requests
from sqlalchemy import select

from app.config import get_settings
from app.db import Database, db
from app.models.tables import platform_setting, project
from app.services.utils import sanitize_error_message


class FileServiceError(RuntimeError):
    pass


_INVALID_WINDOWS_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MAX_PROJECT_STORAGE_NAME_UTF16_UNITS = 80
_WINDOWS_MAX_PATH_UTF16_UNITS = 259
_MAX_WINDOWS_PROJECTS_DIR_UTF16_UNITS = 137
_MAX_MEDIA_CATEGORY_UTF16_UNITS = 6
_MAX_MEDIA_ITEM_ID_UTF16_UNITS = 38
_MAX_MEDIA_FILENAME_UTF16_UNITS = 32
_PROJECT_MEDIA_PATH_TAIL_UTF16_UNITS = (
    1
    + _MAX_MEDIA_CATEGORY_UTF16_UNITS
    + 1
    + _MAX_MEDIA_ITEM_ID_UTF16_UNITS
    + 1
    + _MAX_MEDIA_FILENAME_UTF16_UNITS
)
_CATEGORIZED_MEDIA_DIRECTORIES = (
    "角色",
    "物品",
    "场景",
    "分镜关键帧",
    "分镜视频",
)
_PROJECT_STORAGE_ROOT_KEY = "project_storage_root"
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _utf16_units(value: str) -> int:
    return sum(2 if ord(character) > 0xFFFF else 1 for character in value)


def _truncate_utf16(value: str, max_units: int) -> str:
    units = 0
    for index, character in enumerate(value):
        units += 2 if ord(character) > 0xFFFF else 1
        if units > max_units:
            return value[:index]
    return value


def sanitize_project_storage_name(
    name: str,
    *,
    max_utf16_units: int = _MAX_PROJECT_STORAGE_NAME_UTF16_UNITS,
) -> str:
    sanitized = _INVALID_WINDOWS_NAME.sub("_", name).strip().rstrip(". ")
    sanitized = _truncate_utf16(
        sanitized,
        max_utf16_units,
    ).rstrip(". ")
    return sanitized or "项目"


def project_media_url(project_id: str, relative_path: str | Path) -> str:
    normalized = str(relative_path).replace("\\", "/").lstrip("/")
    return quote(f"/media/{project_id}/{normalized}", safe="/")


def is_reparse_point(path: str | Path) -> bool:
    candidate = Path(path)
    try:
        metadata = os.lstat(candidate)
    except FileNotFoundError:
        return False
    return os.path.islink(candidate) or bool(
        getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _validate_non_empty(path: Path) -> None:
    if not path.is_file() or path.stat().st_size <= 0:
        raise FileServiceError("文件为空，拒绝写入正式目录")


def _temp_path(temp_dir: Path, target: Path) -> Path:
    temp_dir.mkdir(parents=True, exist_ok=True)
    suffix = "".join(target.suffixes)
    return temp_dir / f"{uuid4().hex}.part{suffix}"


def atomic_write_bytes(
    target: str | Path,
    data: bytes,
    *,
    temp_dir: str | Path | None = None,
) -> Path:
    if not data:
        raise FileServiceError("文件内容为空")
    target_path = Path(target)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_root = Path(temp_dir) if temp_dir else target_path.parent
    temporary = _temp_path(temp_root, target_path)
    try:
        temporary.write_bytes(data)
        _validate_non_empty(temporary)
        os.replace(temporary, target_path)
    finally:
        temporary.unlink(missing_ok=True)
    return target_path


def atomic_copy(
    source: str | Path,
    target: str | Path,
    *,
    temp_dir: str | Path | None = None,
) -> Path:
    source_path = Path(source)
    _validate_non_empty(source_path)
    target_path = Path(target)
    if source_path.resolve() == target_path.resolve():
        return target_path
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_root = Path(temp_dir) if temp_dir else target_path.parent
    temporary = _temp_path(temp_root, target_path)
    try:
        shutil.copyfile(source_path, temporary)
        _validate_non_empty(temporary)
        os.replace(temporary, target_path)
    finally:
        temporary.unlink(missing_ok=True)
    return target_path


def download_url_atomic(
    url: str,
    target: str | Path,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 120,
    temp_dir: str | Path | None = None,
) -> Path:
    target_path = Path(target)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_root = Path(temp_dir) if temp_dir else target_path.parent
    temporary = _temp_path(temp_root, target_path)
    try:
        with requests.get(
            url,
            headers=dict(headers or {}),
            timeout=timeout,
            stream=True,
        ) as response:
            response.raise_for_status()
            with temporary.open("wb") as output:
                _copy_response(response.raw, output)
        _validate_non_empty(temporary)
        os.replace(temporary, target_path)
    except requests.RequestException as exc:
        raise FileServiceError(f"下载文件失败：{sanitize_error_message(exc)}") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return target_path


def _copy_response(source: BinaryIO, target: BinaryIO) -> None:
    shutil.copyfileobj(source, target, length=1024 * 1024)


class FileService:
    def __init__(
        self,
        workspace_dir: str | Path | None = None,
        *,
        database: Database | None = None,
    ) -> None:
        if workspace_dir is None:
            settings = get_settings()
            self.workspace_dir = settings.workspace_dir.resolve()
            self.projects_dir = settings.projects_dir.resolve()
        else:
            self.workspace_dir = Path(workspace_dir).resolve()
            self.projects_dir = (self.workspace_dir / "projects").resolve()
        self.database = database

    @staticmethod
    def _inside(path: Path, root: Path) -> Path:
        resolved = path.resolve()
        try:
            resolved.relative_to(root.resolve())
        except ValueError as exc:
            raise FileServiceError("文件路径超出允许的项目目录") from exc
        return resolved

    @staticmethod
    def _direct_child(root: Path, name: str) -> Path:
        if not name:
            raise FileServiceError("项目存储目录名无效")
        resolved_root = root.resolve()
        candidate = resolved_root / name
        if is_reparse_point(candidate):
            raise FileServiceError("项目存储目录不能是符号链接或目录联接")
        target = candidate.resolve()
        if target.parent != resolved_root:
            raise FileServiceError("项目存储路径必须是存储根目录的直接子目录")
        return target

    def _project_record(self, project_id: str) -> dict[str, str] | None:
        if self.database is None:
            return None
        with self.database.engine.connect() as connection:
            row = connection.execute(
                select(
                    project.c.storage_root,
                    project.c.storage_name,
                    project.c.storage_layout,
                ).where(project.c.id == project_id)
            ).first()
        return dict(row._mapping) if row is not None else None

    def _project_target(self, project_id: str) -> Path:
        if not project_id:
            raise FileServiceError("项目 ID 无效")
        record = self._project_record(project_id)
        if record is None:
            return self._direct_child(self.projects_dir, project_id)

        storage_root = record.get("storage_root") or str(self.projects_dir)
        root = Path(storage_root)
        if not root.is_absolute():
            raise FileServiceError("项目存储根目录必须是绝对路径")
        layout = record.get("storage_layout") or "prefixed"
        if layout == "categorized":
            storage_name = record.get("storage_name") or ""
        elif layout == "prefixed":
            storage_name = project_id
        else:
            raise FileServiceError("项目存储布局无效")
        return self._direct_child(root, storage_name)

    def _project_storage(self, project_id: str) -> tuple[str, str | None]:
        record = self._project_record(project_id)
        if record is None:
            return "prefixed", None
        return (
            record.get("storage_layout") or "prefixed",
            record.get("storage_name") or None,
        )

    def current_projects_root(self) -> Path:
        if self.database is not None:
            with self.database.engine.connect() as connection:
                stored_root = connection.scalar(
                    select(platform_setting.c.value).where(
                        platform_setting.c.key == _PROJECT_STORAGE_ROOT_KEY
                    )
                )
            if stored_root:
                root = Path(stored_root)
                if not root.is_absolute():
                    raise FileServiceError("项目存储根目录必须是绝对路径")
                return root.resolve()
        return self.projects_dir

    def project_path(self, project_id: str) -> Path:
        return self._project_target(project_id)

    @staticmethod
    def _project_storage_name_max_units(root: Path) -> int:
        max_units = _MAX_PROJECT_STORAGE_NAME_UTF16_UNITS
        if os.name == "nt":
            if _utf16_units(str(root)) > _MAX_WINDOWS_PROJECTS_DIR_UTF16_UNITS:
                raise FileServiceError("项目存储路径过深，请缩短 APP_PROJECTS_DIR 后重试")
            max_units = min(
                max_units,
                _WINDOWS_MAX_PATH_UTF16_UNITS
                - _utf16_units(str(root))
                - 1
                - _PROJECT_MEDIA_PATH_TAIL_UTF16_UNITS,
            )
            if max_units < _utf16_units("项目"):
                raise FileServiceError("项目存储路径过深，请缩短 APP_PROJECTS_DIR 后重试")
        return max_units

    def project_storage_name(self, project_id: str, name: str) -> str:
        if not project_id:
            raise FileServiceError("项目 ID 无效")
        max_units = _MAX_PROJECT_STORAGE_NAME_UTF16_UNITS
        if os.name == "nt":
            project_path_units = _utf16_units(str(self.project_path(project_id)))
            max_units = min(
                max_units,
                _WINDOWS_MAX_PATH_UTF16_UNITS
                - project_path_units
                - _PROJECT_MEDIA_PATH_TAIL_UTF16_UNITS,
            )
            if max_units < _utf16_units("项目"):
                raise FileServiceError("项目存储路径过深，请缩短 APP_PROJECTS_DIR 后重试")
        return sanitize_project_storage_name(name, max_utf16_units=max_units)

    def project_dir(self, project_id: str) -> Path:
        target = self.project_path(project_id)
        target.mkdir(parents=True, exist_ok=True)
        return target

    def create_categorized_project(self, name: str) -> tuple[Path, str]:
        root = self.current_projects_root()
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise FileServiceError("创建项目存储根目录失败，请检查目录权限") from exc

        max_name_units = self._project_storage_name_max_units(root)
        index = 1
        while True:
            suffix = "" if index == 1 else f" ({index})"
            max_base_units = max_name_units - _utf16_units(suffix)
            if max_base_units < _utf16_units("项目"):
                raise FileServiceError("项目存储路径过深，无法创建同名项目目录")
            candidate = sanitize_project_storage_name(name, max_utf16_units=max_base_units) + suffix
            target = self._direct_child(root, candidate)
            try:
                target.mkdir(exist_ok=False)
            except FileExistsError:
                index += 1
                continue
            except OSError as exc:
                raise FileServiceError("创建项目目录失败，请检查目录权限") from exc
            try:
                for directory in _CATEGORIZED_MEDIA_DIRECTORIES:
                    (target / directory).mkdir()
            except OSError as exc:
                shutil.rmtree(target, ignore_errors=True)
                raise FileServiceError("创建项目分类目录失败，请检查目录权限") from exc
            return target, candidate

    def delete_reserved_project(self, root: str | Path, target: str | Path) -> None:
        resolved_root = Path(root).resolve()
        resolved_target = Path(target).resolve()
        if resolved_target.parent != resolved_root:
            raise FileServiceError("只能清理存储根目录下的直接项目目录")
        if resolved_target.exists():
            if is_reparse_point(resolved_target):
                raise FileServiceError("只能清理真实项目目录，不能清理符号链接或目录联接")
            try:
                shutil.rmtree(resolved_target)
            except OSError as exc:
                raise FileServiceError("清理项目目录失败，请检查目录权限") from exc

    def reserve_project_deletion(self, project_id: str) -> tuple[Path, Path | None]:
        target = self._project_target(project_id)
        if not target.exists():
            return target, None
        if is_reparse_point(target):
            raise FileServiceError("项目目录不能是符号链接或目录联接")
        tombstone = target.parent / f".{target.name}.deleting-{uuid4().hex}"
        self._direct_child(target.parent, tombstone.name)
        try:
            os.replace(target, tombstone)
        except OSError as exc:
            raise FileServiceError("预留项目目录失败，请检查目录权限") from exc
        return target, tombstone

    @staticmethod
    def restore_reserved_project(target: Path, tombstone: Path | None) -> None:
        if tombstone is None or not tombstone.exists():
            return
        if target.exists():
            raise FileServiceError("无法恢复项目目录：目标路径已存在")
        try:
            os.replace(tombstone, target)
        except OSError as exc:
            raise FileServiceError("恢复项目目录失败，请检查目录权限") from exc

    @staticmethod
    def finalize_reserved_project(tombstone: Path | None) -> None:
        if tombstone is None or not tombstone.exists():
            return
        if is_reparse_point(tombstone):
            raise FileServiceError("待删除项目目录不能是符号链接或目录联接")
        try:
            shutil.rmtree(tombstone)
        except OSError as exc:
            raise FileServiceError("删除项目目录失败，请检查目录权限") from exc

    def initialize_project(self, project_id: str, storage_name: str) -> Path:
        project_dir = self.project_dir(project_id)
        safe_name = sanitize_project_storage_name(storage_name)
        for suffix in ("角色", "物品", "场景", "分镜关键帧", "分镜视频"):
            (project_dir / f"{safe_name}-{suffix}").mkdir(parents=True, exist_ok=True)
        return project_dir

    def resolve_project_path(self, project_id: str, relative_path: str | Path) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise FileServiceError("数据库文件路径必须是项目相对路径")
        project_dir = self.project_path(project_id)
        return self._inside(project_dir / relative, project_dir)

    def relative_project_path(self, project_id: str, path: str | Path) -> str:
        project_dir = self.project_path(project_id)
        resolved = self._inside(Path(path), project_dir)
        return resolved.relative_to(project_dir).as_posix()

    def asset_image_path(
        self,
        project_id: str,
        asset_type: str,
        asset_id: str,
        version_no: int,
        suffix: str = ".png",
        *,
        storage_name: str | None = None,
    ) -> Path:
        folders = {"character": "characters", "scene": "scenes", "prop": "props"}
        if asset_type not in folders:
            raise FileServiceError(f"未知素材类型：{asset_type}")
        layout, persisted_storage_name = self._project_storage(project_id)
        if layout == "categorized":
            named_folders = {"character": "角色", "scene": "场景", "prop": "物品"}
            relative = Path(named_folders[asset_type]) / asset_id / f"v{version_no:03d}{suffix}"
        elif storage_name or persisted_storage_name:
            named_folders = {"character": "角色", "scene": "场景", "prop": "物品"}
            safe_name = sanitize_project_storage_name(storage_name or persisted_storage_name or "")
            folder = f"{safe_name}-{named_folders[asset_type]}"
            relative = Path(folder) / asset_id / f"v{version_no:03d}{suffix}"
        else:
            relative = (
                Path("assets") / folders[asset_type] / asset_id / f"v{version_no:03d}{suffix}"
            )
        return self.resolve_project_path(project_id, relative)

    def keyframe_path(
        self,
        project_id: str,
        shot_id: str,
        version_no: int,
        suffix: str = ".png",
        *,
        storage_name: str | None = None,
    ) -> Path:
        layout, persisted_storage_name = self._project_storage(project_id)
        effective_storage_name = storage_name or persisted_storage_name
        if layout == "categorized":
            folder = "分镜关键帧"
        else:
            folder = (
                f"{sanitize_project_storage_name(effective_storage_name)}-分镜关键帧"
                if effective_storage_name
                else "keyframes"
            )
        return self.resolve_project_path(
            project_id,
            Path(folder) / shot_id / f"v{version_no:03d}{suffix}",
        )

    def video_path(
        self,
        project_id: str,
        shot_id: str,
        version_no: int,
        suffix: str = ".mp4",
        *,
        storage_name: str | None = None,
    ) -> Path:
        layout, persisted_storage_name = self._project_storage(project_id)
        effective_storage_name = storage_name or persisted_storage_name
        if layout == "categorized":
            folder = "分镜视频"
        else:
            folder = (
                f"{sanitize_project_storage_name(effective_storage_name)}-分镜视频"
                if effective_storage_name
                else "videos"
            )
        return self.resolve_project_path(
            project_id,
            Path(folder) / shot_id / f"v{version_no:03d}{suffix}",
        )

    def upload_path(self, project_id: str, filename: str | None = None) -> Path:
        upload_dir = self.resolve_project_path(project_id, "uploads")
        upload_dir.mkdir(parents=True, exist_ok=True)
        if filename is None:
            return upload_dir
        safe_name = Path(filename).name
        if not safe_name:
            raise FileServiceError("上传文件名无效")
        return self._inside(upload_dir / safe_name, upload_dir)

    def write_bytes_atomic(
        self,
        project_id: str,
        relative_path: str | Path,
        data: bytes,
    ) -> Path:
        return atomic_write_bytes(
            self.resolve_project_path(project_id, relative_path),
            data,
            temp_dir=self.resolve_project_path(project_id, "temp"),
        )

    def copy_atomic(
        self,
        project_id: str,
        source: str | Path,
        relative_path: str | Path,
    ) -> Path:
        return atomic_copy(
            source,
            self.resolve_project_path(project_id, relative_path),
            temp_dir=self.resolve_project_path(project_id, "temp"),
        )

    def download_atomic(
        self,
        project_id: str,
        url: str,
        relative_path: str | Path,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = 120,
    ) -> Path:
        return download_url_atomic(
            url,
            self.resolve_project_path(project_id, relative_path),
            headers=headers,
            timeout=timeout,
            temp_dir=self.resolve_project_path(project_id, "temp"),
        )

    def delete_file(self, project_id: str, relative_path: str | Path) -> bool:
        target = self.resolve_project_path(project_id, relative_path)
        if not target.exists():
            return False
        if not target.is_file():
            raise FileServiceError("只能删除文件")
        target.unlink()
        return True

    def clear_temp(self, project_id: str) -> None:
        temporary = self.resolve_project_path(project_id, "temp")
        if temporary.exists():
            shutil.rmtree(temporary)
        temporary.mkdir(parents=True, exist_ok=True)

    def delete_project(self, project_id: str) -> None:
        target = self._project_target(project_id)
        if target.exists():
            if is_reparse_point(target):
                raise FileServiceError("项目目录不能是符号链接或目录联接")
            try:
                shutil.rmtree(target)
            except OSError as exc:
                raise FileServiceError("删除项目目录失败，请检查目录权限") from exc


file_service = FileService(database=db)
