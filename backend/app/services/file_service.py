from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

import requests

from app.config import get_settings
from app.services.utils import sanitize_error_message


class FileServiceError(RuntimeError):
    pass


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
        raise FileServiceError(
            f"下载文件失败：{sanitize_error_message(exc)}"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)
    return target_path


def _copy_response(source: BinaryIO, target: BinaryIO) -> None:
    shutil.copyfileobj(source, target, length=1024 * 1024)


class FileService:
    def __init__(self, workspace_dir: str | Path | None = None) -> None:
        configured = Path(workspace_dir) if workspace_dir else get_settings().workspace_dir
        self.workspace_dir = configured.resolve()
        self.projects_dir = (self.workspace_dir / "projects").resolve()
        self.projects_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _inside(path: Path, root: Path) -> Path:
        resolved = path.resolve()
        try:
            resolved.relative_to(root.resolve())
        except ValueError as exc:
            raise FileServiceError("文件路径超出允许的项目目录") from exc
        return resolved

    def _project_target(self, project_id: str) -> Path:
        if not project_id:
            raise FileServiceError("项目 ID 无效")
        target = self._inside(self.projects_dir / project_id, self.projects_dir)
        if target == self.projects_dir:
            raise FileServiceError("项目 ID 不能指向项目根目录")
        return target

    def project_dir(self, project_id: str) -> Path:
        target = self._project_target(project_id)
        target.mkdir(parents=True, exist_ok=True)
        return target

    def resolve_project_path(self, project_id: str, relative_path: str | Path) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute():
            raise FileServiceError("数据库文件路径必须是项目相对路径")
        return self._inside(self.project_dir(project_id) / relative, self.project_dir(project_id))

    def relative_project_path(self, project_id: str, path: str | Path) -> str:
        resolved = self._inside(Path(path), self.project_dir(project_id))
        return resolved.relative_to(self.project_dir(project_id)).as_posix()

    def asset_image_path(
        self,
        project_id: str,
        asset_type: str,
        asset_id: str,
        version_no: int,
        suffix: str = ".png",
    ) -> Path:
        folders = {"character": "characters", "scene": "scenes", "prop": "props"}
        if asset_type not in folders:
            raise FileServiceError(f"未知素材类型：{asset_type}")
        relative = Path("assets") / folders[asset_type] / asset_id / f"v{version_no:03d}{suffix}"
        return self.resolve_project_path(project_id, relative)

    def keyframe_path(
        self,
        project_id: str,
        shot_id: str,
        version_no: int,
        suffix: str = ".png",
    ) -> Path:
        return self.resolve_project_path(
            project_id,
            Path("keyframes") / shot_id / f"v{version_no:03d}{suffix}",
        )

    def video_path(
        self,
        project_id: str,
        shot_id: str,
        version_no: int,
        suffix: str = ".mp4",
    ) -> Path:
        return self.resolve_project_path(
            project_id,
            Path("videos") / shot_id / f"v{version_no:03d}{suffix}",
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
            shutil.rmtree(target)


file_service = FileService()
