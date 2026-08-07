from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
from collections.abc import Mapping
from pathlib import Path
from threading import RLock
from typing import BinaryIO
from urllib.parse import quote
from uuid import uuid4

import requests
from sqlalchemy import select, update

from app.config import get_settings
from app.db import Database, db
from app.models.tables import (
    asset,
    asset_image,
    chapter,
    keyframe_version,
    platform_setting,
    project,
    shot,
    video_version,
)
from app.services.utils import sanitize_error_message

logger = logging.getLogger(__name__)


class FileServiceError(RuntimeError):
    pass


_INVALID_WINDOWS_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MAX_PROJECT_STORAGE_NAME_UTF16_UNITS = 80
_WINDOWS_MAX_PATH_UTF16_UNITS = 259
_MAX_WINDOWS_PROJECTS_DIR_UTF16_UNITS = 137
_MAX_MEDIA_CATEGORY_UTF16_UNITS = 6
_MAX_MEDIA_NAME_UTF16_UNITS = 80
_MAX_MEDIA_FILENAME_UTF16_UNITS = 96
_PROJECT_MEDIA_PATH_TAIL_UTF16_UNITS = (
    1 + _MAX_MEDIA_CATEGORY_UTF16_UNITS + 1 + _MAX_MEDIA_FILENAME_UTF16_UNITS
)
_CATEGORIZED_MEDIA_DIRECTORIES = (
    "角色",
    "物品",
    "场景",
    "分镜图",
    "分镜视频",
)
_PROJECT_STORAGE_ROOT_KEY = "project_storage_root"
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_MEDIA_FILE_LOCK = RLock()


def _utf16_units(value: str) -> int:
    return sum(2 if ord(character) > 0xFFFF else 1 for character in value)


def _truncate_utf16(value: str, max_units: int) -> str:
    units = 0
    for index, character in enumerate(value):
        units += 2 if ord(character) > 0xFFFF else 1
        if units > max_units:
            return value[:index]
    return value


def chapter_folder_name(chapter_no: int, title: str = "") -> str:
    """章节媒体目录名：第01章-标题。标题为空时只保留章号。"""
    clean = _INVALID_WINDOWS_NAME.sub("_", title or "").strip().rstrip(". ")
    clean = _truncate_utf16(clean, 60).rstrip(". ")
    base = f"第{chapter_no:02d}章"
    return f"{base}-{clean}" if clean else base


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


def _media_filename(
    prefix: str,
    name: str,
    version_no: int,
    suffix: str,
    *,
    current: bool,
    disambiguator: str | None = None,
) -> str:
    if not suffix.startswith(".") or "/" in suffix or "\\" in suffix:
        raise FileServiceError("媒体文件扩展名无效")
    identity_suffix = (
        f"-{hashlib.sha256(disambiguator.encode('utf-8')).hexdigest()[:12]}"
        if disambiguator
        else ""
    )
    safe_name = sanitize_project_storage_name(
        name,
        max_utf16_units=_MAX_MEDIA_NAME_UTF16_UNITS - _utf16_units(identity_suffix),
    )
    version = "" if current else f"-v{version_no:03d}"
    return f"{prefix}-{safe_name}{identity_suffix}{version}{suffix}"


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

    def _asset_media_disambiguator(
        self,
        project_id: str,
        asset_type: str,
        asset_id: str,
        asset_name: str,
    ) -> str | None:
        if self.database is None:
            return None
        safe_name = sanitize_project_storage_name(
            asset_name,
            max_utf16_units=_MAX_MEDIA_NAME_UTF16_UNITS,
        ).casefold()
        with self.database.engine.connect() as connection:
            names = connection.scalars(
                select(asset.c.name).where(
                    asset.c.project_id == project_id,
                    asset.c.asset_type == asset_type,
                )
            ).all()
        collision_count = sum(
            sanitize_project_storage_name(
                name,
                max_utf16_units=_MAX_MEDIA_NAME_UTF16_UNITS,
            ).casefold()
            == safe_name
            for name in names
        )
        return asset_id if collision_count > 1 else None

    def asset_image_path(
        self,
        project_id: str,
        asset_type: str,
        asset_id: str,
        version_no: int,
        suffix: str = ".png",
        *,
        asset_name: str | None = None,
        current: bool = False,
        storage_name: str | None = None,
    ) -> Path:
        folders = {"character": "characters", "scene": "scenes", "prop": "props"}
        if asset_type not in folders:
            raise FileServiceError(f"未知素材类型：{asset_type}")
        layout, persisted_storage_name = self._project_storage(project_id)
        if layout == "categorized":
            named_folders = {"character": "角色", "scene": "场景", "prop": "物品"}
            folder = named_folders[asset_type]
            relative = Path(folder) / _media_filename(
                folder,
                asset_name or asset_id,
                version_no,
                suffix,
                current=current,
                disambiguator=self._asset_media_disambiguator(
                    project_id,
                    asset_type,
                    asset_id,
                    asset_name or asset_id,
                ),
            )
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
        shot_no: int | None = None,
        current: bool = False,
        storage_name: str | None = None,
        chapter_no: int | None = None,
        chapter_title: str = "",
    ) -> Path:
        layout, persisted_storage_name = self._project_storage(project_id)
        effective_storage_name = storage_name or persisted_storage_name
        if layout == "categorized":
            folder = "分镜图"
            name = f"{shot_no:03d}" if shot_no is not None else shot_id
            # 分镜媒体按章节分目录；素材图仍在项目根，全书共用
            prefix = (
                Path(chapter_folder_name(chapter_no, chapter_title))
                if chapter_no is not None
                else Path()
            )
            relative = (
                prefix
                / folder
                / _media_filename(
                    folder,
                    name,
                    version_no,
                    suffix,
                    current=current,
                )
            )
        else:
            folder = (
                f"{sanitize_project_storage_name(effective_storage_name)}-分镜关键帧"
                if effective_storage_name
                else "keyframes"
            )
            relative = Path(folder) / shot_id / f"v{version_no:03d}{suffix}"
        return self.resolve_project_path(
            project_id,
            relative,
        )

    def video_path(
        self,
        project_id: str,
        shot_id: str,
        version_no: int,
        suffix: str = ".mp4",
        *,
        shot_no: int | None = None,
        current: bool = False,
        storage_name: str | None = None,
        chapter_no: int | None = None,
        chapter_title: str = "",
    ) -> Path:
        layout, persisted_storage_name = self._project_storage(project_id)
        effective_storage_name = storage_name or persisted_storage_name
        if layout == "categorized":
            folder = "分镜视频"
            name = f"{shot_no:03d}" if shot_no is not None else shot_id
            # 分镜媒体按章节分目录；素材图仍在项目根，全书共用
            prefix = (
                Path(chapter_folder_name(chapter_no, chapter_title))
                if chapter_no is not None
                else Path()
            )
            relative = (
                prefix
                / folder
                / _media_filename(
                    folder,
                    name,
                    version_no,
                    suffix,
                    current=current,
                )
            )
        else:
            folder = (
                f"{sanitize_project_storage_name(effective_storage_name)}-分镜视频"
                if effective_storage_name
                else "videos"
            )
            relative = Path(folder) / shot_id / f"v{version_no:03d}{suffix}"
        return self.resolve_project_path(
            project_id,
            relative,
        )

    def _categorized_media_records(self, project_id: str) -> list[dict[str, object]]:
        if self.database is None:
            return []
        records: list[dict[str, object]] = []
        with self.database.engine.connect() as connection:
            for row in connection.execute(
                select(
                    asset_image.c.id,
                    asset_image.c.file_path,
                    asset_image.c.version_no,
                    asset_image.c.is_current,
                    asset.c.id.label("asset_id"),
                    asset.c.asset_type,
                    asset.c.name.label("asset_name"),
                )
                .select_from(asset_image.join(asset, asset_image.c.asset_id == asset.c.id))
                .where(
                    asset.c.project_id == project_id,
                    asset_image.c.file_path.is_not(None),
                )
            ):
                records.append(
                    {
                        "table": asset_image,
                        "kind": "asset_image",
                        **dict(row._mapping),
                    }
                )
            for table, kind in (
                (keyframe_version, "keyframe"),
                (video_version, "video"),
            ):
                for row in connection.execute(
                    select(
                        table.c.id,
                        table.c.file_path,
                        table.c.version_no,
                        table.c.is_current,
                        shot.c.id.label("shot_id"),
                        shot.c.shot_no,
                        chapter.c.chapter_no,
                        chapter.c.title.label("chapter_title"),
                    )
                    .select_from(
                        table.join(shot, table.c.shot_id == shot.c.id).join(
                            chapter, chapter.c.id == shot.c.chapter_id
                        )
                    )
                    .where(
                        shot.c.project_id == project_id,
                        table.c.file_path.is_not(None),
                    )
                ):
                    records.append(
                        {
                            "table": table,
                            "kind": kind,
                            **dict(row._mapping),
                        }
                    )
        return records

    def _categorized_media_target(
        self,
        project_id: str,
        record: dict[str, object],
    ) -> Path:
        file_path = str(record["file_path"])
        suffix = Path(file_path).suffix
        if not suffix:
            raise FileServiceError(f"媒体文件缺少扩展名：{file_path}")
        version_no = int(record["version_no"])
        current = bool(record["is_current"])
        if record["kind"] == "asset_image":
            return self.asset_image_path(
                project_id,
                str(record["asset_type"]),
                str(record["asset_id"]),
                version_no,
                suffix,
                asset_name=str(record["asset_name"]),
                current=current,
            )
        chapter_no = record.get("chapter_no")
        chapter_title = str(record.get("chapter_title") or "")
        if record["kind"] == "keyframe":
            return self.keyframe_path(
                project_id,
                str(record["shot_id"]),
                version_no,
                suffix,
                shot_no=int(record["shot_no"]),
                current=current,
                chapter_no=int(chapter_no) if chapter_no is not None else None,
                chapter_title=chapter_title,
            )
        return self.video_path(
            project_id,
            str(record["shot_id"]),
            version_no,
            suffix,
            shot_no=int(record["shot_no"]),
            current=current,
            chapter_no=int(chapter_no) if chapter_no is not None else None,
            chapter_title=chapter_title,
        )

    def migrate_categorized_media_layouts(self, *, strict: bool = True) -> int:
        if self.database is None:
            return 0
        with self.database.engine.connect() as connection:
            project_ids = connection.scalars(
                select(project.c.id).where(project.c.storage_layout == "categorized")
            ).all()
        moved_count = 0
        for project_id in project_ids:
            try:
                moved_count += self._migrate_categorized_project_media(str(project_id))
            except Exception:
                if strict:
                    raise
                logger.exception("项目媒体目录迁移失败，已跳过项目 %s", project_id)
        return moved_count

    def _migration_stage_path(
        self,
        project_id: str,
        record: dict[str, object],
        suffix: str,
    ) -> Path:
        table = record["table"]
        identity = hashlib.sha256(f"{table.name}:{record['id']}".encode()).hexdigest()
        return self.resolve_project_path(
            project_id,
            Path("temp") / "layout-migration" / f"{table.name}-{identity}{suffix}",
        )

    @staticmethod
    def _cleanup_empty_media_directories(project_dir: Path) -> None:
        for directory in (*_CATEGORIZED_MEDIA_DIRECTORIES, "分镜关键帧"):
            category_dir = project_dir / directory
            if not category_dir.is_dir() or is_reparse_point(category_dir):
                continue
            for child in category_dir.iterdir():
                if child.is_dir() and not is_reparse_point(child):
                    try:
                        child.rmdir()
                    except OSError:
                        pass
            if directory == "分镜关键帧":
                try:
                    category_dir.rmdir()
                except OSError:
                    pass

    def _migrate_categorized_project_media(self, project_id: str) -> int:
        records = self._categorized_media_records(project_id)
        project_dir = self.project_path(project_id)
        targets: dict[str, str] = {}
        moves: list[dict[str, object]] = []
        for record in records:
            source = self.resolve_project_path(project_id, str(record["file_path"]))
            target = self._categorized_media_target(project_id, record)
            target_key = str(target).casefold()
            existing_id = targets.get(target_key)
            if existing_id and existing_id != str(record["id"]):
                raise FileServiceError(f"项目媒体迁移目标重名：{target.name}")
            targets[target_key] = str(record["id"])
            if source == target:
                continue
            suffix = target.suffix
            moves.append(
                {
                    **record,
                    "source": source,
                    "target": target,
                    "stage": self._migration_stage_path(project_id, record, suffix),
                    "target_relative": self.relative_project_path(project_id, target),
                }
            )

        source_paths = {Path(move["source"]) for move in moves}
        for move in moves:
            source = Path(move["source"])
            target = Path(move["target"])
            stage = Path(move["stage"])
            if stage.exists() and not stage.is_file():
                raise FileServiceError(f"项目媒体迁移暂存路径无效：{stage.name}")
            if source.exists() and not source.is_file():
                raise FileServiceError(f"项目媒体迁移源路径无效：{move['file_path']}")
            if target.exists() and not target.is_file():
                raise FileServiceError(f"项目媒体迁移目标路径无效：{target.name}")
            if stage.is_file() and source.is_file():
                raise FileServiceError(f"项目媒体迁移暂存文件冲突：{stage.name}")
            if target.is_file() and source.is_file() and target not in source_paths:
                raise FileServiceError(f"项目媒体迁移目标已存在：{target.name}")
            if not source.is_file() and not stage.is_file() and not target.is_file():
                raise FileServiceError(f"项目媒体迁移失败，源文件不存在：{move['file_path']}")

        migration_dir = self.resolve_project_path(project_id, Path("temp") / "layout-migration")
        with _MEDIA_FILE_LOCK:
            try:
                for move in moves:
                    source = Path(move["source"])
                    stage = Path(move["stage"])
                    if source.is_file():
                        self.move_file(project_id, source, stage)
                for move in moves:
                    stage = Path(move["stage"])
                    target = Path(move["target"])
                    if stage.is_file():
                        if target.exists():
                            raise FileServiceError(f"项目媒体迁移目标已存在：{target.name}")
                        self.move_file(project_id, stage, target)
                    elif not target.is_file():
                        raise FileServiceError(f"项目媒体迁移暂存文件不存在：{stage.name}")
                with self.database.engine.begin() as connection:
                    for move in moves:
                        table = move["table"]
                        connection.execute(
                            update(table)
                            .where(table.c.id == str(move["id"]))
                            .values(file_path=str(move["target_relative"]))
                        )
            except Exception as exc:
                rollback_errors: list[str] = []
                for move in reversed(moves):
                    source = Path(move["source"])
                    stage = Path(move["stage"])
                    target = Path(move["target"])
                    if source.exists():
                        continue
                    recovery_source = target if target.is_file() else stage
                    if not recovery_source.is_file():
                        continue
                    try:
                        self.move_file(project_id, recovery_source, source)
                    except FileServiceError as rollback_exc:
                        rollback_errors.append(str(rollback_exc))
                if rollback_errors:
                    raise FileServiceError(
                        "项目媒体迁移失败，且文件回滚失败：" + "；".join(rollback_errors)
                    ) from exc
                if isinstance(exc, FileServiceError):
                    raise
                raise FileServiceError(f"项目媒体迁移失败：{exc}") from exc
            finally:
                if migration_dir.exists():
                    try:
                        migration_dir.rmdir()
                    except OSError:
                        pass

        self._cleanup_empty_media_directories(project_dir)
        for directory in _CATEGORIZED_MEDIA_DIRECTORIES:
            (project_dir / directory).mkdir(parents=True, exist_ok=True)
        return len(moves)

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

    def move_file(
        self,
        project_id: str,
        source: str | Path,
        target: str | Path,
    ) -> Path:
        project_dir = self.project_path(project_id)
        source_value = Path(source)
        target_value = Path(target)
        source_path = (
            self._inside(source_value, project_dir)
            if source_value.is_absolute()
            else self.resolve_project_path(project_id, source_value)
        )
        target_path = (
            self._inside(target_value, project_dir)
            if target_value.is_absolute()
            else self.resolve_project_path(project_id, target_value)
        )
        if source_path == target_path:
            return target_path
        with _MEDIA_FILE_LOCK:
            if not source_path.is_file():
                raise FileServiceError(f"待移动媒体文件不存在：{source_path.name}")
            if target_path.exists():
                raise FileServiceError(f"媒体目标文件已存在：{target_path.name}")
            target_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                source_path.rename(target_path)
            except OSError as exc:
                raise FileServiceError(f"移动媒体文件失败：{source_path.name}") from exc
        return target_path

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
