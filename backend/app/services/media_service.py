import asyncio
import base64
import io
import subprocess
import zipfile
from pathlib import Path
from typing import Any, Literal

from PIL import Image, UnidentifiedImageError
from sqlalchemy import delete, func, insert, select, update

from app.config import settings
from app.db import Database, db
from app.models.tables import (
    api_provider,
    asset,
    asset_image,
    job,
    keyframe_version,
    project,
    shot,
    video_version,
)
from app.schemas import ProviderPollResult
from app.services.errors import bad_request, conflict, not_found
from app.services.file_service import FileServiceError, atomic_copy, file_service
from app.services.job_service import JobService
from app.services.prompt_service import (
    compile_asset_prompt,
    compile_keyframe_prompt,
    compile_video_prompt,
)
from app.services.provider_service import ProviderService
from app.services.signature_service import (
    asset_image_signature,
    asset_signature,
    calculate_signature,
    is_stale,
    keyframe_signature,
    video_signature,
)
from app.services.utils import (
    json_dumps,
    json_loads,
    new_id,
    row_dict,
    sanitize_error_message,
    utc_now,
)

MediaKind = Literal["asset_image", "keyframe", "video"]

_MEDIA_TABLES = {
    "asset_image": (asset_image, "asset_id"),
    "keyframe": (keyframe_version, "shot_id"),
    "video": (video_version, "shot_id"),
}
_DELETED_MEDIA_ERROR = "__deleted_by_user__"
_ACTIVE_MEDIA_STATUSES = {"queued", "running", "waiting_provider", "downloading"}


class MediaService:
    def __init__(
        self,
        database: Database | None = None,
        jobs: JobService | None = None,
    ) -> None:
        self.database = database or db
        self.jobs = jobs or JobService(self.database)
        self.providers = ProviderService(self.database)

    def _project(self, project_id: str) -> dict[str, Any]:
        with self.database.engine.connect() as connection:
            record = row_dict(
                connection.execute(select(project).where(project.c.id == project_id)).first()
            )
        if record is None:
            raise not_found("项目")
        return record

    def _asset(self, asset_id: str) -> dict[str, Any]:
        with self.database.engine.connect() as connection:
            record = row_dict(
                connection.execute(select(asset).where(asset.c.id == asset_id)).first()
            )
        if record is None:
            raise not_found("素材")
        record["profile"] = json_loads(record["profile_json"], {})
        record["consistency_keywords"] = json_loads(record["consistency_json"], [])
        return record

    def _shot(self, shot_id: str) -> dict[str, Any]:
        with self.database.engine.connect() as connection:
            record = row_dict(connection.execute(select(shot).where(shot.c.id == shot_id)).first())
        if record is None:
            raise not_found("分镜")
        for column in (
            "character_ids_json",
            "prop_ids_json",
            "positions_json",
            "expressions_json",
            "dialogue_json",
            "reference_bindings_json",
        ):
            record[column.removesuffix("_json")] = json_loads(record[column], [])
        return record

    @staticmethod
    def _decode_version(record: dict[str, Any], project_id: str) -> dict[str, Any]:
        result = dict(record)
        result["is_current"] = bool(result["is_current"])
        if result.get("error") != _DELETED_MEDIA_ERROR:
            result["error"] = sanitize_error_message(result.get("error", ""))
        for column in ("reference_files_json", "reference_bindings_json"):
            if column in result:
                result[column] = json_loads(result[column], [])
                result[column.removesuffix("_json")] = result[column]
        if result.get("file_path"):
            result["file_url"] = f"/media/{project_id}/{result['file_path']}"
            result["media_url"] = result["file_url"]
        return result

    @staticmethod
    def _job_params(connection: Any, version_id: str, provider_id: str | None) -> dict[str, Any]:
        raw = connection.execute(
            select(job.c.input_json)
            .where(job.c.target_id == version_id)
            .order_by(job.c.created_at.desc())
            .limit(1)
        ).scalar()
        overrides = json_loads(raw, {}).get("params", {})
        if not provider_id:
            return overrides
        provider_params = connection.execute(
            select(api_provider.c.params_json).where(api_provider.c.id == provider_id)
        ).scalar()
        return {**json_loads(provider_params, {}), **overrides}

    def _asset_expected_signature(
        self, connection: Any, record: dict[str, Any], current: dict[str, Any]
    ) -> str:
        params = self._job_params(connection, record["id"], record["provider_id"])
        project_record = row_dict(
            connection.execute(select(project).where(project.c.id == current["project_id"])).first()
        )
        if project_record is None:
            raise not_found("项目")
        return asset_image_signature(
            self._effective_asset_signature(current, project_record),
            record["provider_id"] or "uploaded",
            record["model"],
            params,
            record["reference_files_json"],
        )

    def _keyframe_expected_signature(
        self, connection: Any, record: dict[str, Any], current: dict[str, Any]
    ) -> str:
        params = self._job_params(connection, record["id"], record["provider_id"])
        project_record = row_dict(
            connection.execute(select(project).where(project.c.id == current["project_id"])).first()
        )
        if project_record is None:
            raise not_found("项目")
        return keyframe_signature(
            self._effective_shot_signature(current, project_record),
            current["reference_bindings"],
            record["provider_id"] or "uploaded",
            record["model"],
            params,
        )

    def _video_expected_signature(
        self, connection: Any, record: dict[str, Any], current: dict[str, Any]
    ) -> str:
        params = self._job_params(connection, record["id"], record["provider_id"])
        project_record = row_dict(
            connection.execute(select(project).where(project.c.id == current["project_id"])).first()
        )
        if project_record is None:
            raise not_found("项目")
        return video_signature(
            record["keyframe_version_id"] or "",
            self._effective_video_prompt(current, project_record),
            current["duration_sec"],
            record["provider_id"] or "uploaded",
            record["model"],
            params,
        )

    @staticmethod
    def _effective_asset_signature(current: dict[str, Any], project_record: dict[str, Any]) -> str:
        return asset_signature(
            current["profile"],
            current["prompt"],
            current["negative_prompt"],
            project_record["style_prompt"],
        )

    @staticmethod
    def _effective_shot_signature(current: dict[str, Any], project_record: dict[str, Any]) -> str:
        return calculate_signature(
            current["own_signature"],
            project_record["style_prompt"],
            project_record["aspect_ratio"],
        )

    @staticmethod
    def _effective_video_prompt(current: dict[str, Any], project_record: dict[str, Any]) -> str:
        return calculate_signature(
            current["video_prompt"],
            project_record["style_prompt"],
            project_record["aspect_ratio"],
        )

    def list_asset_images(self, asset_id: str) -> list[dict[str, Any]]:
        current = self._asset(asset_id)
        with self.database.engine.connect() as connection:
            records = []
            for row in connection.execute(
                select(asset_image)
                .where(
                    asset_image.c.asset_id == asset_id,
                    asset_image.c.error != _DELETED_MEDIA_ERROR,
                )
                .order_by(asset_image.c.version_no.desc())
            ):
                record = self._decode_version(dict(row._mapping), current["project_id"])
                expected = self._asset_expected_signature(connection, record, current)
                record["is_stale"] = is_stale(record["input_signature"], expected)
                record["stale_reason"] = (
                    "素材描述、提示词或 Provider 参数已变化" if record["is_stale"] else ""
                )
                records.append(record)
        return records

    def list_keyframes(self, shot_id: str) -> list[dict[str, Any]]:
        current = self._shot(shot_id)
        with self.database.engine.connect() as connection:
            records = []
            for row in connection.execute(
                select(keyframe_version)
                .where(
                    keyframe_version.c.shot_id == shot_id,
                    keyframe_version.c.error != _DELETED_MEDIA_ERROR,
                )
                .order_by(keyframe_version.c.version_no.desc())
            ):
                record = self._decode_version(dict(row._mapping), current["project_id"])
                expected = self._keyframe_expected_signature(connection, record, current)
                record["is_stale"] = is_stale(record["input_signature"], expected)
                record["stale_reason"] = (
                    "分镜、提示词或参考图绑定已变化" if record["is_stale"] else ""
                )
                records.append(record)
        return records

    def list_videos(self, shot_id: str) -> list[dict[str, Any]]:
        current = self._shot(shot_id)
        with self.database.engine.connect() as connection:
            records = []
            for row in connection.execute(
                select(video_version)
                .where(
                    video_version.c.shot_id == shot_id,
                    video_version.c.error != _DELETED_MEDIA_ERROR,
                )
                .order_by(video_version.c.version_no.desc())
            ):
                record = self._decode_version(dict(row._mapping), current["project_id"])
                expected = self._video_expected_signature(connection, record, current)
                record["is_stale"] = is_stale(record["input_signature"], expected)
                record["stale_reason"] = (
                    "关键帧、视频提示词、时长或 Provider 参数已变化" if record["is_stale"] else ""
                )
                records.append(record)
        return records

    def _insert_version(
        self,
        *,
        table: Any,
        parent_column: str,
        parent_id: str,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        version_id = new_id(
            "asset_image"
            if table is asset_image
            else "keyframe"
            if table is keyframe_version
            else "video"
        )
        with self.database.engine.begin() as connection:
            version_no = (
                connection.execute(
                    select(func.max(table.c.version_no)).where(table.c[parent_column] == parent_id)
                ).scalar()
                or 0
            ) + 1
            record = {
                "id": version_id,
                parent_column: parent_id,
                "version_no": version_no,
                "created_at": utc_now(),
                **values,
            }
            connection.execute(insert(table).values(**record))
        return record

    def queue_asset_image(self, asset_id: str, options: dict[str, Any]) -> dict[str, Any]:
        current = self._asset(asset_id)
        current_project = self._project(current["project_id"])
        _, config = self.providers.resolve(
            kind="image",
            provider_id=options.get("provider_id") or current_project["default_image_provider_id"],
        )
        params = {**config["params"], **options.get("params", {})}
        references: list[str] = []
        prompt = compile_asset_prompt(
            style_prompt=current_project["style_prompt"],
            aspect_ratio=current_project["aspect_ratio"],
            prompt=current["prompt"],
            consistency_keywords=current["consistency_keywords"],
            composition=current["composition"],
        )
        version = self._insert_version(
            table=asset_image,
            parent_column="asset_id",
            parent_id=asset_id,
            values={
                "file_path": None,
                "prompt_snapshot": prompt,
                "negative_prompt_snapshot": current["negative_prompt"],
                "reference_files_json": json_dumps(references),
                "provider_id": config["id"],
                "model": config["model"],
                "provider_task_id": "",
                "status": "queued",
                "is_current": False,
                "input_signature": asset_image_signature(
                    self._effective_asset_signature(current, current_project),
                    config["id"],
                    config["model"],
                    params,
                    references,
                ),
                "source": "generated",
                "error": "",
            },
        )
        return self.jobs.create(
            project_id=current["project_id"],
            job_type="generate_asset_image",
            target_type="asset_image",
            target_id=version["id"],
            provider_kind="image",
            provider_id=config["id"],
            input_data={"asset_id": asset_id, "params": options.get("params", {})},
            batch_id=options.get("_batch_id"),
        )

    def queue_asset_image_batch(self, project_id: str, options: dict[str, Any]) -> dict[str, Any]:
        current_project = self._project(project_id)
        statement = select(asset.c.id).where(asset.c.project_id == project_id)
        if options.get("ids"):
            statement = statement.where(asset.c.id.in_(options["ids"]))
        if options.get("asset_type"):
            statement = statement.where(asset.c.asset_type == options["asset_type"])
        if options.get("only_missing"):
            statement = statement.where(
                ~select(asset_image.c.id)
                .where(
                    asset_image.c.asset_id == asset.c.id,
                    asset_image.c.is_current.is_(True),
                )
                .exists()
            )
        with self.database.engine.connect() as connection:
            ids = list(connection.execute(statement).scalars())
        batch_id = new_id("batch")
        jobs = []
        failures: list[dict[str, str]] = []
        for asset_id in ids:
            try:
                job_record = self.queue_asset_image(
                    asset_id,
                    {
                        "provider_id": options.get("provider_id")
                        or current_project["default_image_provider_id"],
                        "params": options.get("params", {}),
                        "_batch_id": batch_id,
                    },
                )
            except Exception as exc:
                failures.append(
                    {
                        "target_id": asset_id,
                        "error": sanitize_error_message(exc),
                    }
                )
                continue
            jobs.append(job_record)
        return {"batch_id": batch_id, "jobs": jobs, "preflight_failures": failures}

    def queue_keyframe(self, shot_id: str, options: dict[str, Any]) -> dict[str, Any]:
        current = self._shot(shot_id)
        current_project = self._project(current["project_id"])
        _, config = self.providers.resolve(
            kind="image",
            provider_id=options.get("provider_id") or current_project["default_image_provider_id"],
        )
        params = {**config["params"], **options.get("params", {})}
        bindings = current["reference_bindings"]
        version = self._insert_version(
            table=keyframe_version,
            parent_column="shot_id",
            parent_id=shot_id,
            values={
                "file_path": None,
                "prompt_snapshot": compile_keyframe_prompt(
                    style_prompt=current_project["style_prompt"],
                    aspect_ratio=current_project["aspect_ratio"],
                    image_prompt=current["image_prompt"],
                    anchors=[],
                ),
                "negative_prompt_snapshot": current["negative_prompt"],
                "reference_bindings_json": json_dumps(bindings),
                "provider_id": config["id"],
                "model": config["model"],
                "provider_task_id": "",
                "status": "queued",
                "is_current": False,
                "input_signature": keyframe_signature(
                    self._effective_shot_signature(current, current_project),
                    bindings,
                    config["id"],
                    config["model"],
                    params,
                ),
                "source": "generated",
                "error": "",
            },
        )
        return self.jobs.create(
            project_id=current["project_id"],
            job_type="generate_keyframe",
            target_type="keyframe",
            target_id=version["id"],
            provider_kind="image",
            provider_id=config["id"],
            input_data={"shot_id": shot_id, "params": options.get("params", {})},
            batch_id=options.get("_batch_id"),
        )

    def queue_keyframe_batch(self, project_id: str, options: dict[str, Any]) -> dict[str, Any]:
        return self._queue_shot_media_batch(project_id, options, media_kind="keyframe")

    def queue_video(self, shot_id: str, options: dict[str, Any]) -> dict[str, Any]:
        current = self._shot(shot_id)
        current_project = self._project(current["project_id"])
        with self.database.engine.connect() as connection:
            keyframe = row_dict(
                connection.execute(
                    select(keyframe_version).where(
                        keyframe_version.c.shot_id == shot_id,
                        keyframe_version.c.is_current.is_(True),
                        keyframe_version.c.status == "completed",
                    )
                ).first()
            )
        if keyframe is None or not keyframe.get("file_path"):
            raise conflict("分镜缺少已完成的当前关键帧")
        _, config = self.providers.resolve(
            kind="video",
            provider_id=options.get("provider_id") or current_project["default_video_provider_id"],
        )
        params = {**config["params"], **options.get("params", {})}
        prompt = compile_video_prompt(
            style_prompt=current_project["style_prompt"],
            aspect_ratio=current_project["aspect_ratio"],
            video_prompt=current["video_prompt"],
            duration_sec=current["duration_sec"],
        )
        version = self._insert_version(
            table=video_version,
            parent_column="shot_id",
            parent_id=shot_id,
            values={
                "keyframe_version_id": keyframe["id"],
                "file_path": None,
                "prompt_snapshot": prompt,
                "negative_prompt_snapshot": current["negative_prompt"],
                "requested_duration_sec": current["duration_sec"],
                "actual_duration_sec": None,
                "provider_id": config["id"],
                "model": config["model"],
                "provider_task_id": "",
                "status": "queued",
                "is_current": False,
                "input_signature": video_signature(
                    keyframe["id"],
                    self._effective_video_prompt(current, current_project),
                    current["duration_sec"],
                    config["id"],
                    config["model"],
                    params,
                ),
                "source": "generated",
                "cost_estimate": 0,
                "error": "",
            },
        )
        return self.jobs.create(
            project_id=current["project_id"],
            job_type="generate_video",
            target_type="video",
            target_id=version["id"],
            provider_kind="video",
            provider_id=config["id"],
            input_data={
                "shot_id": shot_id,
                "keyframe_version_id": keyframe["id"],
                "params": options.get("params", {}),
            },
            batch_id=options.get("_batch_id"),
        )

    def queue_video_batch(self, project_id: str, options: dict[str, Any]) -> dict[str, Any]:
        return self._queue_shot_media_batch(project_id, options, media_kind="video")

    def _queue_shot_media_batch(
        self,
        project_id: str,
        options: dict[str, Any],
        *,
        media_kind: Literal["keyframe", "video"],
    ) -> dict[str, Any]:
        self._project(project_id)
        statement = select(shot.c.id).where(shot.c.project_id == project_id)
        if options.get("ids"):
            statement = statement.where(shot.c.id.in_(options["ids"]))
        if options.get("scene_no"):
            statement = statement.where(shot.c.source_ref.like(f"scene:{options['scene_no']}|%"))
        table = keyframe_version if media_kind == "keyframe" else video_version
        if options.get("only_missing"):
            statement = statement.where(
                ~select(table.c.id)
                .where(
                    table.c.shot_id == shot.c.id,
                    table.c.is_current.is_(True),
                )
                .exists()
            )
        with self.database.engine.connect() as connection:
            ids = list(connection.execute(statement).scalars())
        batch_id = new_id("batch")
        records: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        queue_one = self.queue_keyframe if media_kind == "keyframe" else self.queue_video
        for shot_id in ids:
            try:
                records.append(queue_one(shot_id, {**options, "_batch_id": batch_id}))
            except Exception as exc:
                failures.append(
                    {
                        "target_id": shot_id,
                        "error": sanitize_error_message(exc),
                    }
                )
        return {
            "batch_id": batch_id,
            "jobs": records,
            "preflight_failures": failures,
        }

    @staticmethod
    def _image_suffix(filename: str) -> str:
        suffix = Path(filename).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise bad_request("图片仅支持 PNG、JPG、JPEG 或 WEBP")
        return suffix

    @staticmethod
    def _verify_image(data: bytes) -> None:
        try:
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
        except (UnidentifiedImageError, OSError) as exc:
            raise bad_request("上传内容不是可解码图片") from exc

    @staticmethod
    def _video_suffix(filename: str) -> str:
        suffix = Path(filename).suffix.lower()
        if suffix not in {".mp4", ".webm", ".mov"}:
            raise bad_request("视频仅支持 MP4、WEBM 或 MOV")
        return suffix

    @staticmethod
    def _verify_video(path: Path) -> float:
        try:
            probe = subprocess.run(
                [
                    settings.ffprobe_path,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration:stream=codec_type",
                    "-of",
                    "json",
                    str(path),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise bad_request("无法校验视频，请确认 FFmpeg 已安装且可用") from exc
        payload = json_loads(probe.stdout, {})
        streams = payload.get("streams", [])
        try:
            duration = float(payload.get("format", {}).get("duration", 0))
        except (TypeError, ValueError):
            duration = 0
        if (
            probe.returncode != 0
            or duration <= 0
            or not any(stream.get("codec_type") == "video" for stream in streams)
        ):
            raise bad_request("视频文件无法解码")
        return duration

    def upload_asset_image(
        self,
        asset_id: str,
        filename: str,
        content_type: str,
        data: bytes,
    ) -> dict[str, Any]:
        del content_type
        self._verify_image(data)
        current = self._asset(asset_id)
        suffix = self._image_suffix(filename)
        version = self._insert_version(
            table=asset_image,
            parent_column="asset_id",
            parent_id=asset_id,
            values={
                "file_path": None,
                "prompt_snapshot": current["prompt"],
                "negative_prompt_snapshot": current["negative_prompt"],
                "reference_files_json": "[]",
                "provider_id": None,
                "model": "",
                "provider_task_id": "",
                "status": "completed",
                "is_current": False,
                "input_signature": asset_image_signature(
                    self._effective_asset_signature(current, self._project(current["project_id"])),
                    "uploaded",
                    "",
                    {},
                    [],
                ),
                "source": "uploaded",
                "error": "",
            },
        )
        target = file_service.asset_image_path(
            current["project_id"],
            current["asset_type"],
            asset_id,
            version["version_no"],
            suffix,
        )
        return self._finish_upload(
            kind="asset_image",
            version_id=version["id"],
            project_id=current["project_id"],
            target=target,
            data=data,
        )

    def upload_keyframe(
        self,
        shot_id: str,
        filename: str,
        content_type: str,
        data: bytes,
    ) -> dict[str, Any]:
        del content_type
        self._verify_image(data)
        current = self._shot(shot_id)
        suffix = self._image_suffix(filename)
        version = self._insert_version(
            table=keyframe_version,
            parent_column="shot_id",
            parent_id=shot_id,
            values={
                "file_path": None,
                "prompt_snapshot": current["image_prompt"],
                "negative_prompt_snapshot": current["negative_prompt"],
                "reference_bindings_json": current["reference_bindings_json"],
                "provider_id": None,
                "model": "",
                "provider_task_id": "",
                "status": "completed",
                "is_current": False,
                "input_signature": keyframe_signature(
                    self._effective_shot_signature(current, self._project(current["project_id"])),
                    current["reference_bindings"],
                    "uploaded",
                    "",
                    {},
                ),
                "source": "uploaded",
                "error": "",
            },
        )
        target = file_service.keyframe_path(
            current["project_id"], shot_id, version["version_no"], suffix
        )
        return self._finish_upload(
            kind="keyframe",
            version_id=version["id"],
            project_id=current["project_id"],
            target=target,
            data=data,
        )

    def upload_video(
        self,
        shot_id: str,
        filename: str,
        content_type: str,
        data: bytes,
    ) -> dict[str, Any]:
        del content_type
        if not data:
            raise bad_request("上传视频不能为空")
        current = self._shot(shot_id)
        suffix = self._video_suffix(filename)
        with self.database.engine.connect() as connection:
            keyframe_id = connection.execute(
                select(keyframe_version.c.id).where(
                    keyframe_version.c.shot_id == shot_id,
                    keyframe_version.c.is_current.is_(True),
                )
            ).scalar()
        version = self._insert_version(
            table=video_version,
            parent_column="shot_id",
            parent_id=shot_id,
            values={
                "keyframe_version_id": keyframe_id,
                "file_path": None,
                "prompt_snapshot": current["video_prompt"],
                "negative_prompt_snapshot": current["negative_prompt"],
                "requested_duration_sec": current["duration_sec"],
                "actual_duration_sec": None,
                "provider_id": None,
                "model": "",
                "provider_task_id": "",
                "status": "completed",
                "is_current": False,
                "input_signature": video_signature(
                    keyframe_id or "",
                    self._effective_video_prompt(current, self._project(current["project_id"])),
                    current["duration_sec"],
                    "uploaded",
                    "",
                    {},
                ),
                "source": "uploaded",
                "cost_estimate": 0,
                "error": "",
            },
        )
        target = file_service.video_path(
            current["project_id"], shot_id, version["version_no"], suffix
        )
        return self._finish_upload(
            kind="video",
            version_id=version["id"],
            project_id=current["project_id"],
            target=target,
            data=data,
        )

    def _finish_upload(
        self,
        *,
        kind: MediaKind,
        version_id: str,
        project_id: str,
        target: Path,
        data: bytes,
    ) -> dict[str, Any]:
        table, parent_column = _MEDIA_TABLES[kind]
        relative = file_service.relative_project_path(project_id, target)
        try:
            file_service.write_bytes_atomic(project_id, relative, data)
            actual_duration_sec = self._verify_video(target) if kind == "video" else None
            with self.database.engine.begin() as connection:
                record = row_dict(
                    connection.execute(select(table).where(table.c.id == version_id)).first()
                )
                if record is None:
                    raise not_found("媒体版本")
                connection.execute(
                    update(table)
                    .where(table.c[parent_column] == record[parent_column])
                    .values(is_current=False)
                )
                connection.execute(
                    update(table)
                    .where(table.c.id == version_id)
                    .values(
                        file_path=relative,
                        is_current=True,
                        **(
                            {"actual_duration_sec": actual_duration_sec}
                            if table is video_version
                            else {}
                        ),
                    )
                )
        except Exception:
            file_service.delete_file(project_id, relative)
            with self.database.engine.begin() as connection:
                connection.execute(delete(table).where(table.c.id == version_id))
            raise
        return self._get_version(kind, version_id)

    def _get_version(self, kind: MediaKind, version_id: str) -> dict[str, Any]:
        table, parent_column = _MEDIA_TABLES[kind]
        with self.database.engine.connect() as connection:
            record = row_dict(
                connection.execute(select(table).where(table.c.id == version_id)).first()
            )
            if record is None or record["error"] == _DELETED_MEDIA_ERROR:
                raise not_found("媒体版本")
            if kind == "asset_image":
                project_id = connection.execute(
                    select(asset.c.project_id).where(asset.c.id == record[parent_column])
                ).scalar()
            else:
                project_id = connection.execute(
                    select(shot.c.project_id).where(shot.c.id == record[parent_column])
                ).scalar()
        if not project_id:
            raise not_found("媒体所属项目")
        return self._decode_version(record, project_id)

    def set_current(self, kind: MediaKind, version_id: str) -> dict[str, Any]:
        table, parent_column = _MEDIA_TABLES[kind]
        with self.database.engine.begin() as connection:
            record = row_dict(
                connection.execute(select(table).where(table.c.id == version_id)).first()
            )
            if record is None or record["error"] == _DELETED_MEDIA_ERROR:
                raise not_found("媒体版本")
            if record["status"] != "completed" or not record.get("file_path"):
                raise conflict("只有已完成且文件存在的版本可以设为当前版本")
            connection.execute(
                update(table)
                .where(table.c[parent_column] == record[parent_column])
                .values(is_current=False)
            )
            connection.execute(
                update(table).where(table.c.id == version_id).values(is_current=True)
            )
        return self._get_version(kind, version_id)

    def unset_current(self, kind: MediaKind, version_id: str) -> dict[str, Any]:
        table, _ = _MEDIA_TABLES[kind]
        with self.database.engine.begin() as connection:
            record = row_dict(
                connection.execute(select(table).where(table.c.id == version_id)).first()
            )
            if record is None or record["error"] == _DELETED_MEDIA_ERROR:
                raise not_found("媒体版本")
            connection.execute(
                update(table).where(table.c.id == version_id).values(is_current=False)
            )
        return self._get_version(kind, version_id)

    def delete_version(self, kind: MediaKind, version_id: str) -> None:
        table, parent_column = _MEDIA_TABLES[kind]
        with self.database.engine.begin() as connection:
            record = row_dict(
                connection.execute(select(table).where(table.c.id == version_id)).first()
            )
            if record is None or record["error"] == _DELETED_MEDIA_ERROR:
                raise not_found("媒体版本")
            if record["is_current"]:
                raise conflict("当前媒体版本不能直接删除，请先切换其他版本")
            if record["status"] in _ACTIVE_MEDIA_STATUSES:
                raise conflict("正在执行的媒体版本不能删除，请等待任务结束")
            if kind == "keyframe":
                self._ensure_keyframe_not_referenced(connection, version_id)
            if kind == "asset_image":
                project_id = connection.execute(
                    select(asset.c.project_id).where(asset.c.id == record[parent_column])
                ).scalar()
            else:
                project_id = connection.execute(
                    select(shot.c.project_id).where(shot.c.id == record[parent_column])
                ).scalar()
            connection.execute(
                update(table)
                .where(table.c.id == version_id)
                .values(
                    file_path=None,
                    status="failed",
                    is_current=False,
                    error=_DELETED_MEDIA_ERROR,
                )
            )
            if project_id and record.get("file_path"):
                file_service.delete_file(project_id, record["file_path"])

    @staticmethod
    def _ensure_keyframe_not_referenced(connection: Any, keyframe_id: str) -> None:
        video_rows = connection.execute(
            select(video_version.c.id, video_version.c.status).where(
                video_version.c.keyframe_version_id == keyframe_id,
                video_version.c.error != _DELETED_MEDIA_ERROR,
            )
        ).all()
        if any(row.status in _ACTIVE_MEDIA_STATUSES for row in video_rows):
            raise conflict("关键帧仍被生成中的视频引用，不能删除")

        video_ids = {row.id for row in video_rows}
        if video_ids:
            active_video_job = connection.execute(
                select(job.c.id)
                .where(
                    job.c.target_type == "video",
                    job.c.target_id.in_(video_ids),
                    job.c.status.in_(_ACTIVE_MEDIA_STATUSES),
                )
                .limit(1)
            ).first()
            if active_video_job:
                raise conflict("关键帧仍被活动视频任务引用，不能删除")

        for raw_bindings in connection.scalars(select(shot.c.reference_bindings_json)):
            bindings = json_loads(raw_bindings, [])
            if not isinstance(bindings, list):
                continue
            if any(
                isinstance(binding, dict)
                and binding.get("role") == "previous_keyframe"
                and binding.get("keyframe_id") == keyframe_id
                for binding in bindings
            ):
                raise conflict("关键帧仍被后续分镜引用，不能删除")

    def resolve_video_download(self, video_id: str) -> Path:
        record = self._get_version("video", video_id)
        if record["status"] != "completed" or not record.get("file_path"):
            raise conflict("视频尚未完成")
        current = self._shot(record["shot_id"])
        path = file_service.resolve_project_path(current["project_id"], record["file_path"])
        if not path.is_file():
            raise not_found("视频文件")
        return path

    def export_current_videos(self, project_id: str) -> Path:
        self._project(project_id)
        with self.database.engine.connect() as connection:
            records = connection.execute(
                select(
                    video_version.c.file_path,
                    shot.c.shot_no,
                    shot.c.id.label("shot_id"),
                )
                .join(shot, shot.c.id == video_version.c.shot_id)
                .where(
                    shot.c.project_id == project_id,
                    video_version.c.is_current.is_(True),
                    video_version.c.status == "completed",
                )
                .order_by(shot.c.shot_order)
            ).all()
        if not records:
            raise conflict("当前项目没有可导出的当前视频")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for record in records:
                path = file_service.resolve_project_path(project_id, record.file_path)
                if path.is_file():
                    archive.write(
                        path,
                        arcname=f"{record.shot_no:03d}_{record.shot_id}{path.suffix.lower()}",
                    )
        if not buffer.getvalue():
            raise conflict("当前视频文件均不存在，无法导出")
        relative = f"export/current-videos-{utc_now().replace(':', '-')}.zip"
        return file_service.write_bytes_atomic(project_id, relative, buffer.getvalue())

    async def handle_asset_image(self, job_record: dict[str, Any]) -> dict[str, Any]:
        return await self._handle_image_job(job_record, kind="asset_image")

    async def handle_keyframe(self, job_record: dict[str, Any]) -> dict[str, Any]:
        return await self._handle_image_job(job_record, kind="keyframe")

    async def _handle_image_job(
        self, job_record: dict[str, Any], *, kind: Literal["asset_image", "keyframe"]
    ) -> dict[str, Any]:
        table = asset_image if kind == "asset_image" else keyframe_version
        version_id = job_record["target_id"]
        version = self._get_version(kind, version_id)
        try:
            input_data = json_loads(job_record.get("input_json"), {})
            if kind == "asset_image":
                current = self._asset(version["asset_id"])
                project_id = current["project_id"]
                references = [
                    file_service.resolve_project_path(project_id, item)
                    for item in version["reference_files_json"]
                    if file_service.resolve_project_path(project_id, item).is_file()
                ]
                target = file_service.asset_image_path(
                    project_id,
                    current["asset_type"],
                    current["id"],
                    version["version_no"],
                )
            else:
                current = self._shot(version["shot_id"])
                project_id = current["project_id"]
                references = self._resolve_binding_files(
                    project_id,
                    version["reference_bindings_json"],
                )
                target = file_service.keyframe_path(
                    project_id,
                    current["id"],
                    version["version_no"],
                )
            provider, config = self.providers.resolve(
                kind="image",
                provider_id=version["provider_id"],
            )
            params = {**config["params"], **input_data.get("params", {})}
            self._set_media_status(table, version_id, "running")
            if self._has_saved_result(job_record):
                result = ProviderPollResult.model_validate(job_record["result_data"])
            elif job_record.get("provider_task_id") or version["provider_task_id"]:
                result = await self._poll_existing(
                    provider,
                    job_record,
                    table,
                    version_id,
                    job_record.get("provider_task_id") or version["provider_task_id"],
                    config["timeout_sec"],
                )
            else:
                submission = await provider.submit(
                    prompt=version["prompt_snapshot"],
                    negative_prompt=version["negative_prompt_snapshot"],
                    reference_files=references,
                    params=params,
                )
                result = await self._await_provider_result(
                    provider,
                    job_record,
                    table,
                    version_id,
                    submission,
                    config["timeout_sec"],
                )
            await self._download_provider_result(provider, result, target)
            self._delete_staged_provider_result(project_id, result)
            self._verify_image(target.read_bytes())
            relative = file_service.relative_project_path(project_id, target)
            self._complete_media(table, version_id, relative)
            return {"version_id": version_id, "file_path": relative}
        except Exception as exc:
            self._set_media_status(table, version_id, "failed", str(exc))
            raise

    def _resolve_binding_files(self, project_id: str, bindings: list[dict[str, Any]]) -> list[Path]:
        paths: list[Path] = []
        with self.database.engine.connect() as connection:
            for binding in bindings:
                if binding.get("asset_image_id"):
                    relative = connection.execute(
                        select(asset_image.c.file_path).where(
                            asset_image.c.id == binding["asset_image_id"]
                        )
                    ).scalar()
                elif binding.get("keyframe_id"):
                    relative = connection.execute(
                        select(keyframe_version.c.file_path).where(
                            keyframe_version.c.id == binding["keyframe_id"]
                        )
                    ).scalar()
                else:
                    relative = None
                if relative:
                    path = file_service.resolve_project_path(project_id, relative)
                    if path.is_file():
                        paths.append(path)
        return paths

    async def handle_video(self, job_record: dict[str, Any]) -> dict[str, Any]:
        version_id = job_record["target_id"]
        version = self._get_version("video", version_id)
        project_id = ""
        target: Path | None = None
        try:
            current = self._shot(version["shot_id"])
            project_id = current["project_id"]
            keyframe = self._get_version("keyframe", version["keyframe_version_id"])
            keyframe_path = file_service.resolve_project_path(project_id, keyframe["file_path"])
            input_data = json_loads(job_record.get("input_json"), {})
            provider, config = self.providers.resolve(
                kind="video",
                provider_id=version["provider_id"],
            )
            params = {**config["params"], **input_data.get("params", {})}
            target = file_service.video_path(
                project_id,
                current["id"],
                version["version_no"],
            )
            self._set_media_status(video_version, version_id, "running")
            if self._has_saved_result(job_record):
                result = ProviderPollResult.model_validate(job_record["result_data"])
            elif job_record.get("provider_task_id") or version["provider_task_id"]:
                result = await self._poll_existing(
                    provider,
                    job_record,
                    video_version,
                    version_id,
                    job_record.get("provider_task_id") or version["provider_task_id"],
                    config["timeout_sec"],
                )
            else:
                prepared = await provider.prepare_input_image(keyframe_path)
                submission = await provider.submit(
                    prompt=version["prompt_snapshot"],
                    input_image=prepared,
                    duration_sec=version["requested_duration_sec"],
                    params=params,
                )
                result = await self._await_provider_result(
                    provider,
                    job_record,
                    video_version,
                    version_id,
                    submission,
                    config["timeout_sec"],
                )
            self._set_media_status(video_version, version_id, "downloading")
            self.jobs.repository.set_status(
                job_record["id"], "downloading", message="正在下载视频结果"
            )
            await self._download_provider_result(provider, result, target)
            self._delete_staged_provider_result(project_id, result)
            if not target.is_file() or target.stat().st_size <= 0:
                raise FileServiceError("视频文件为空")
            actual_duration_sec = self._verify_video(target)
            relative = file_service.relative_project_path(project_id, target)
            self._complete_media(
                video_version,
                version_id,
                relative,
                actual_duration_sec=actual_duration_sec,
            )
            return {"version_id": version_id, "file_path": relative}
        except Exception as exc:
            if target is not None and target.is_file():
                relative = file_service.relative_project_path(project_id, target)
                file_service.delete_file(project_id, relative)
            self._set_media_status(video_version, version_id, "failed", str(exc))
            raise

    async def _await_provider_result(
        self,
        provider: Any,
        job_record: dict[str, Any],
        table: Any,
        version_id: str,
        submission: Any,
        timeout_sec: int,
    ) -> ProviderPollResult:
        task_id = submission.task_id
        if task_id:
            self._set_media_status(table, version_id, "waiting_provider", provider_task_id=task_id)
            self.jobs.repository.set_provider_task(
                job_record["id"], task_id, status="waiting_provider"
            )
        if submission.status == "completed":
            result = ProviderPollResult(
                task_id=task_id,
                status="completed",
                **submission.result,
            )
            return self._save_provider_result(job_record["id"], result)
        if not task_id:
            raise RuntimeError(submission.message or "Provider 未返回任务 ID")
        return await self._poll_existing(
            provider,
            job_record,
            table,
            version_id,
            task_id,
            timeout_sec,
        )

    async def _poll_existing(
        self,
        provider: Any,
        job_record: dict[str, Any],
        table: Any,
        version_id: str,
        task_id: str,
        timeout_sec: int,
    ) -> ProviderPollResult:
        self._set_media_status(table, version_id, "waiting_provider", provider_task_id=task_id)
        self.jobs.repository.set_provider_task(job_record["id"], task_id, status="waiting_provider")
        try:
            async with asyncio.timeout(timeout_sec):
                while True:
                    result = await provider.poll(task_id)
                    if result.status == "completed":
                        return self._save_provider_result(job_record["id"], result)
                    if result.status == "failed":
                        raise RuntimeError(result.error or "Provider 任务失败")
                    await asyncio.sleep(0.25)
        except TimeoutError as exc:
            raise RuntimeError("Provider 轮询超时，可稍后重试原任务") from exc

    def _save_provider_result(
        self,
        job_id: str,
        result: ProviderPollResult,
    ) -> ProviderPollResult:
        payload = result.model_dump(mode="json")
        data: dict[str, Any] = {}
        if result.data.get("local_file"):
            data["local_file"] = result.data["local_file"]
        if result.content_base64:
            encoded = result.content_base64
            if encoded.startswith("data:") and "," in encoded:
                encoded = encoded.split(",", 1)[1]
            try:
                content = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise FileServiceError("Provider 返回的 Base64 文件无效") from exc
            job_record = self.jobs.repository.require(job_id)
            relative = f"temp/provider-results/{job_id}.bin"
            staged = file_service.write_bytes_atomic(
                job_record["project_id"],
                relative,
                content,
            )
            data["local_file"] = str(staged)
            payload["content_base64"] = ""
        payload["data"] = data
        self.jobs.repository.save_result(job_id, payload)
        return ProviderPollResult.model_validate(payload)

    @staticmethod
    async def _download_provider_result(
        provider: Any,
        result: ProviderPollResult,
        target: Path,
    ) -> None:
        local_file = result.data.get("local_file")
        if local_file:
            await asyncio.to_thread(atomic_copy, local_file, target)
            return
        await provider.download(result, target)

    @staticmethod
    def _delete_staged_provider_result(
        project_id: str,
        result: ProviderPollResult,
    ) -> None:
        local_file = result.data.get("local_file")
        if not local_file:
            return
        try:
            relative = file_service.relative_project_path(project_id, local_file)
        except FileServiceError:
            return
        if relative.startswith("temp/provider-results/"):
            file_service.delete_file(project_id, relative)

    @staticmethod
    def _has_saved_result(job_record: dict[str, Any]) -> bool:
        result = job_record.get("result_data") or {}
        return any(
            result.get(key)
            for key in ("result_url", "content_base64", "local_file", "download_url")
        ) or bool(result.get("data", {}).get("local_file"))

    def _set_media_status(
        self,
        table: Any,
        version_id: str,
        status: str,
        error: str = "",
        *,
        provider_task_id: str | None = None,
    ) -> None:
        values: dict[str, Any] = {
            "status": status,
            "error": sanitize_error_message(error),
        }
        if provider_task_id is not None:
            values["provider_task_id"] = provider_task_id
        with self.database.engine.begin() as connection:
            connection.execute(update(table).where(table.c.id == version_id).values(**values))

    def _complete_media(
        self,
        table: Any,
        version_id: str,
        relative: str,
        *,
        actual_duration_sec: float | None = None,
    ) -> None:
        parent_column = "asset_id" if table is asset_image else "shot_id"
        values: dict[str, Any] = {
            "file_path": relative,
            "status": "completed",
            "is_current": True,
            "error": "",
        }
        if table is video_version:
            values["actual_duration_sec"] = actual_duration_sec
        with self.database.engine.begin() as connection:
            record = row_dict(
                connection.execute(select(table).where(table.c.id == version_id)).first()
            )
            if record is None:
                raise not_found("媒体版本")
            connection.execute(
                update(table)
                .where(table.c[parent_column] == record[parent_column])
                .values(is_current=False)
            )
            connection.execute(update(table).where(table.c.id == version_id).values(**values))
