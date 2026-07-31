from __future__ import annotations

import re
from typing import Any

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.db import Database, db
from app.models.tables import (
    asset,
    asset_image,
    job,
    keyframe_version,
    project,
    screenplay,
    shot,
    video_version,
)
from app.prompts import load_prompt
from app.schemas import (
    ShotExtractionOutput,
    ShotImagePromptOutput,
    ShotVideoPromptOutput,
)
from app.services.errors import bad_request, conflict, not_found
from app.services.file_service import file_service, project_media_url
from app.services.job_service import JobService
from app.services.prompt_service import (
    compile_keyframe_prompt,
    compile_video_prompt,
    resolve_keyframe_description,
    resolve_video_description,
    shot_template_context,
)
from app.services.provider_service import ProviderService
from app.services.signature_service import shot_signature
from app.services.utils import json_dumps, json_loads, new_id, row_dict, utc_now

_JSON_FIELDS = {
    "character_ids": "character_ids_json",
    "prop_ids": "prop_ids_json",
    "positions": "positions_json",
    "expressions": "expressions_json",
    "dialogue": "dialogue_json",
    "reference_bindings": "reference_bindings_json",
}

_SIGNATURE_FIELDS = (
    "source_type",
    "source_ref",
    "scene_asset_id",
    "character_ids",
    "prop_ids",
    "positions",
    "start_state",
    "action",
    "expressions",
    "dialogue",
    "narration",
    "shot_type",
    "camera_angle",
    "camera_move",
    "duration_sec",
    "end_state",
    "next_hook",
    "frame_description",
    "image_prompt",
    "video_prompt",
    "negative_prompt",
    "reference_bindings",
)

_ACTIVE_JOB_STATUSES = {"queued", "running", "waiting_provider", "downloading"}


class ShotService:
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

    @staticmethod
    def _scene_no(source_ref: str) -> int | None:
        match = re.match(r"scene:(\d+)(?:\||$)", source_ref)
        return int(match.group(1)) if match else None

    @classmethod
    def _decode(cls, record: dict[str, Any]) -> dict[str, Any]:
        result = dict(record)
        for alias, column in _JSON_FIELDS.items():
            value = json_loads(result[column], [])
            result[column] = value
            result[alias] = value
        result["scene_no"] = cls._scene_no(result["source_ref"])
        return result

    @staticmethod
    def _media_record(
        connection: Any,
        table: Any,
        shot_id: str,
        project_id: str,
    ) -> dict[str, Any] | None:
        record = row_dict(
            connection.execute(
                select(table).where(
                    table.c.shot_id == shot_id,
                    table.c.is_current.is_(True),
                )
            ).first()
        )
        if record and record.get("file_path"):
            record["file_url"] = project_media_url(project_id, record["file_path"])
            record["media_url"] = record["file_url"]
        if record:
            record["is_current"] = bool(record["is_current"])
        return record

    @staticmethod
    def _asset_names(connection: Any, current: dict[str, Any]) -> dict[str, str]:
        asset_ids = [
            item
            for item in [
                current.get("scene_asset_id"),
                *current.get("character_ids", []),
                *current.get("prop_ids", []),
            ]
            if item
        ]
        if not asset_ids:
            return {}
        return {
            row.id: row.name
            for row in connection.execute(
                select(asset.c.id, asset.c.name).where(asset.c.id.in_(asset_ids))
            )
        }

    @staticmethod
    def _reference_labels(
        connection: Any,
        bindings: list[dict[str, Any]],
    ) -> list[str]:
        labels = {
            "character_identity": "锁定角色身份与服装",
            "scene_layout": "锁定场景空间结构和光线",
            "prop_identity": "锁定物品外观、材质和结构",
            "previous_keyframe": "锁定上一镜构图与连续性",
        }
        result: list[str] = []
        for binding in bindings:
            role = str(binding.get("role", ""))
            name = ""
            if binding.get("asset_id"):
                name = (
                    connection.execute(
                        select(asset.c.name).where(asset.c.id == binding["asset_id"])
                    ).scalar()
                    or ""
                )
            elif binding.get("keyframe_id"):
                shot_no = connection.execute(
                    select(shot.c.shot_no)
                    .join(keyframe_version, keyframe_version.c.shot_id == shot.c.id)
                    .where(keyframe_version.c.id == binding["keyframe_id"])
                ).scalar()
                if shot_no:
                    name = f"上一镜分镜 {shot_no}"
            if name:
                result.append(f"{name}，{labels.get(role, '锁定当前参考信息')}")
        return result

    def _add_compiled_prompts(
        self,
        connection: Any,
        current_project: dict[str, Any],
        current: dict[str, Any],
    ) -> None:
        context = shot_template_context(current, self._asset_names(connection, current))
        labels = self._reference_labels(connection, current["reference_bindings"])
        image_prompt = resolve_keyframe_description(
            current["image_prompt"],
            current["frame_description"],
        )
        video_prompt = resolve_video_description(
            video_prompt=current["video_prompt"],
            start_state=current["start_state"],
            action=current["action"],
            end_state=current["end_state"],
            camera_move=current["camera_move"],
            duration_sec=current["duration_sec"],
        )
        current["compiled_image_prompt"] = compile_keyframe_prompt(
            style_prompt=current_project["style_prompt"],
            aspect_ratio=current_project["aspect_ratio"],
            image_prompt=image_prompt,
            anchors=[f"@Image{index}：{label}。" for index, label in enumerate(labels, start=1)],
            shot_context=context,
            negative_prompt=current["negative_prompt"],
        )
        current["compiled_video_prompt"] = compile_video_prompt(
            style_prompt=current_project["style_prompt"],
            aspect_ratio=current_project["aspect_ratio"],
            video_prompt=video_prompt,
            duration_sec=current["duration_sec"],
            anchors=[
                "@Image1：当前分镜关键帧，锁定构图、人物左右关系和起始站位。",
                *[f"@Image{index}：{label}。" for index, label in enumerate(labels, start=2)],
            ],
            shot_context=context,
            negative_prompt=current["negative_prompt"],
        )

    def list(self, project_id: str) -> list[dict[str, Any]]:
        current_project = self._project(project_id)
        statement = (
            select(shot)
            .where(shot.c.project_id == project_id)
            .order_by(shot.c.shot_order, shot.c.shot_no)
        )
        with self.database.engine.connect() as connection:
            records = []
            for row in connection.execute(statement):
                record = self._decode(dict(row._mapping))
                source_now = self._current_source_signature(
                    connection, current_project, record["source_type"]
                )
                record["is_stale"] = bool(
                    record["source_signature"] and record["source_signature"] != source_now
                )
                record["current_keyframe"] = self._media_record(
                    connection, keyframe_version, record["id"], project_id
                )
                record["current_video"] = self._media_record(
                    connection, video_version, record["id"], project_id
                )
                self._add_compiled_prompts(connection, current_project, record)
                records.append(record)
        return records

    def get(self, shot_id: str) -> dict[str, Any]:
        with self.database.engine.connect() as connection:
            record = row_dict(connection.execute(select(shot).where(shot.c.id == shot_id)).first())
            if record is None:
                raise not_found("分镜")
            result = self._decode(record)
            self._add_compiled_prompts(
                connection,
                self._project(result["project_id"]),
                result,
            )
            return result

    def _current_source_signature(
        self,
        connection: Any,
        project_record: dict[str, Any],
        source_type: str,
    ) -> str:
        if source_type == "novel":
            return project_record["source_signature"]
        return (
            connection.execute(
                select(screenplay.c.own_signature).where(
                    screenplay.c.project_id == project_record["id"]
                )
            ).scalar()
            or ""
        )

    def _source_signature(self, project_record: dict[str, Any], source_type: str) -> str:
        with self.database.engine.connect() as connection:
            signature = self._current_source_signature(connection, project_record, source_type)
        if source_type == "screenplay" and not signature:
            raise conflict("当前项目还没有可用剧本")
        return signature

    def _signature(self, values: dict[str, Any]) -> str:
        return shot_signature({key: values.get(key) for key in _SIGNATURE_FIELDS})

    def _validate_asset_ids(
        self,
        project_id: str,
        *,
        scene_asset_id: str | None,
        character_ids: list[str],
        prop_ids: list[str],
    ) -> None:
        requested: list[tuple[str, str]] = []
        if scene_asset_id:
            requested.append((scene_asset_id, "scene"))
        requested.extend((item, "character") for item in character_ids)
        requested.extend((item, "prop") for item in prop_ids)
        if not requested:
            return
        ids = [item[0] for item in requested]
        with self.database.engine.connect() as connection:
            records = connection.execute(
                select(asset.c.id, asset.c.asset_type, asset.c.project_id).where(
                    asset.c.id.in_(ids)
                )
            ).all()
        actual = {row.id: (row.asset_type, row.project_id) for row in records}
        for asset_id, expected_type in requested:
            if actual.get(asset_id) != (expected_type, project_id):
                raise bad_request(f"素材 {asset_id} 不存在、类型错误或不属于当前项目")

    def _validate_bindings(
        self,
        project_id: str,
        bindings: list[dict[str, Any]],
    ) -> None:
        allowed_asset_roles = {
            "character_identity": "character",
            "scene_layout": "scene",
            "prop_identity": "prop",
        }
        with self.database.engine.connect() as connection:
            for binding in bindings:
                role = binding.get("role")
                if role in allowed_asset_roles:
                    asset_id = binding.get("asset_id")
                    image_id = binding.get("asset_image_id")
                    if not asset_id or not image_id:
                        raise bad_request(f"{role} 绑定必须提供 asset_id 和 asset_image_id")
                    found = connection.execute(
                        select(asset_image.c.id)
                        .join(asset, asset.c.id == asset_image.c.asset_id)
                        .where(
                            asset_image.c.id == image_id,
                            asset_image.c.asset_id == asset_id,
                            asset.c.project_id == project_id,
                            asset.c.asset_type == allowed_asset_roles[role],
                        )
                    ).first()
                    if not found:
                        raise bad_request(f"参考素材图片 {image_id} 无效")
                elif role == "previous_keyframe":
                    keyframe_id = binding.get("keyframe_id")
                    if not keyframe_id:
                        raise bad_request("上一镜关键帧绑定必须提供 keyframe_id")
                    found = connection.execute(
                        select(keyframe_version.c.id)
                        .join(shot, shot.c.id == keyframe_version.c.shot_id)
                        .where(
                            keyframe_version.c.id == keyframe_id,
                            shot.c.project_id == project_id,
                        )
                    ).first()
                    if not found:
                        raise bad_request(f"上一镜关键帧 {keyframe_id} 无效")
                else:
                    raise bad_request(f"未知参考图职责：{role}")

    def _next_numbers(self, connection: Any, project_id: str) -> tuple[int, int]:
        row = connection.execute(
            select(
                func.max(shot.c.shot_no),
                func.max(shot.c.shot_order),
            ).where(shot.c.project_id == project_id)
        ).one()
        return (row[0] or 0) + 1, (row[1] or 0) + 1

    def create(self, project_id: str, values: dict[str, Any]) -> dict[str, Any]:
        current_project = self._project(project_id)
        if values.get("duration_sec") is None:
            raise bad_request("必须提供分镜时长")
        source_type = "screenplay"
        with self.database.engine.connect() as connection:
            source_signature = self._current_source_signature(
                connection,
                current_project,
                source_type,
            )
        self._validate_asset_ids(
            project_id,
            scene_asset_id=values.get("scene_asset_id"),
            character_ids=values.get("character_ids", []),
            prop_ids=values.get("prop_ids", []),
        )
        self._validate_bindings(project_id, values.get("reference_bindings", []))
        now = utc_now()
        semantic = {key: values.get(key) for key in _SIGNATURE_FIELDS if key in values}
        semantic["source_type"] = source_type
        semantic["duration_sec"] = values["duration_sec"]
        if not values.get("reference_bindings"):
            auto_bindings, _ = self._current_reference_bindings(
                {
                    "project_id": project_id,
                    "scene_asset_id": semantic.get("scene_asset_id"),
                    "character_ids": semantic.get("character_ids", []),
                    "prop_ids": semantic.get("prop_ids", []),
                }
            )
            semantic["reference_bindings"] = auto_bindings
        with self.database.engine.begin() as connection:
            next_no, next_order = self._next_numbers(connection, project_id)
            record: dict[str, Any] = {
                "id": new_id("shot"),
                "project_id": project_id,
                "shot_no": values.get("shot_no") or next_no,
                "shot_order": values.get("shot_order") or next_order,
                "source_signature": source_signature,
                "own_signature": self._signature(semantic),
                "revision": 1,
                "created_at": now,
                "updated_at": now,
            }
            for key in _SIGNATURE_FIELDS:
                value = semantic.get(key)
                column = _JSON_FIELDS.get(key, key)
                if column in shot.c:
                    record[column] = json_dumps(value or []) if column.endswith("_json") else value
            try:
                connection.execute(insert(shot).values(**record))
            except IntegrityError as exc:
                raise conflict("分镜编号已存在") from exc
        return self.get(record["id"])

    def sync_asset_image_binding(self, asset_id: str, asset_image_id: str) -> None:
        """素材图切换为当前版本后，回写所有引用该素材的分镜。"""
        with self.database.engine.begin() as connection:
            asset_record = connection.execute(
                select(asset.c.project_id, asset.c.asset_type).where(asset.c.id == asset_id)
            ).first()
            if asset_record is None:
                return
            role = {
                "character": "character_identity",
                "scene": "scene_layout",
                "prop": "prop_identity",
            }.get(asset_record.asset_type)
            if role is None:
                return
            rows = connection.execute(
                select(shot).where(shot.c.project_id == asset_record.project_id)
            ).all()
            replacement = {
                "role": role,
                "asset_id": asset_id,
                "asset_image_id": asset_image_id,
            }
            for row in rows:
                current = self._decode(dict(row._mapping))
                referenced = asset_id in {
                    current.get("scene_asset_id"),
                    *current.get("character_ids", []),
                    *current.get("prop_ids", []),
                }
                if not referenced:
                    continue
                bindings = current["reference_bindings"]
                next_bindings: list[dict[str, Any]] = []
                replaced = False
                for binding in bindings:
                    if isinstance(binding, dict) and binding.get("asset_id") == asset_id:
                        if not replaced:
                            next_bindings.append(replacement)
                            replaced = True
                    else:
                        next_bindings.append(binding)
                if not replaced:
                    next_bindings.append(replacement)
                if next_bindings == bindings:
                    continue
                semantic = {
                    key: next_bindings if key == "reference_bindings" else current.get(key)
                    for key in _SIGNATURE_FIELDS
                }
                connection.execute(
                    update(shot)
                    .where(shot.c.id == current["id"])
                    .values(
                        reference_bindings_json=json_dumps(next_bindings),
                        own_signature=self._signature(semantic),
                        revision=current["revision"] + 1,
                        updated_at=utc_now(),
                    )
                )

    def update(
        self,
        shot_id: str,
        values: dict[str, Any],
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        current = self.get(shot_id)
        if expected_revision is not None and current["revision"] != expected_revision:
            raise conflict("分镜已被其他请求更新，请重试")
        self._validate_asset_ids(
            current["project_id"],
            scene_asset_id=values.get("scene_asset_id", current["scene_asset_id"]),
            character_ids=values.get("character_ids", current["character_ids"]),
            prop_ids=values.get("prop_ids", current["prop_ids"]),
        )
        self._validate_bindings(
            current["project_id"],
            values.get("reference_bindings", current["reference_bindings"]),
        )
        changes: dict[str, Any] = {}
        for key, value in values.items():
            column = _JSON_FIELDS.get(key, key)
            changes[column] = json_dumps(value) if column.endswith("_json") else value
        if values:
            semantic = {key: values.get(key, current.get(key)) for key in _SIGNATURE_FIELDS}
            changes["own_signature"] = self._signature(semantic)
            changes["revision"] = current["revision"] + 1
            if "status" not in changes:
                changes["status"] = "ready"
            changes["updated_at"] = utc_now()
            with self.database.engine.begin() as connection:
                statement = update(shot).where(shot.c.id == shot_id)
                if expected_revision is not None:
                    statement = statement.where(shot.c.revision == expected_revision)
                result = connection.execute(statement.values(**changes))
                if expected_revision is not None and result.rowcount != 1:
                    raise conflict("分镜已被其他请求更新，请重试")
        return self.get(shot_id)

    @staticmethod
    def _temporary_number_start(connection: Any, project_id: str) -> int:
        row = connection.execute(
            select(
                func.max(shot.c.shot_no),
                func.max(shot.c.shot_order),
            ).where(shot.c.project_id == project_id)
        ).one()
        return max(row[0] or 0, row[1] or 0) + 1

    def _renumber(self, connection: Any, project_id: str, ids: list[str]) -> None:
        temporary_start = self._temporary_number_start(connection, project_id)
        for offset, shot_id in enumerate(ids):
            connection.execute(
                update(shot)
                .where(shot.c.id == shot_id)
                .values(
                    shot_no=temporary_start + offset,
                    shot_order=temporary_start + offset,
                )
            )
        for index, shot_id in enumerate(ids, start=1):
            connection.execute(
                update(shot)
                .where(shot.c.id == shot_id)
                .values(shot_no=index, shot_order=index, updated_at=utc_now())
            )

    @staticmethod
    def _related_media_ids(
        connection: Any,
        shot_ids: set[str],
    ) -> tuple[set[str], set[str]]:
        keyframe_ids = set(
            connection.execute(
                select(keyframe_version.c.id).where(keyframe_version.c.shot_id.in_(shot_ids))
            ).scalars()
        )
        video_ids = set(
            connection.execute(
                select(video_version.c.id).where(video_version.c.shot_id.in_(shot_ids))
            ).scalars()
        )
        return keyframe_ids, video_ids

    @classmethod
    def _ensure_no_active_jobs(
        cls,
        connection: Any,
        shot_ids: set[str],
    ) -> tuple[set[str], set[str]]:
        keyframe_ids, video_ids = cls._related_media_ids(connection, shot_ids)
        active_job = connection.execute(
            select(job.c.id).where(
                job.c.status.in_(_ACTIVE_JOB_STATUSES),
                (
                    (job.c.target_type == "shot") & job.c.target_id.in_(shot_ids)
                    | (job.c.target_type == "keyframe") & job.c.target_id.in_(keyframe_ids)
                    | (job.c.target_type == "video") & job.c.target_id.in_(video_ids)
                ),
            )
        ).first()
        if active_job:
            raise conflict("分镜仍有进行中的任务，不能删除、拆分或合并")
        return keyframe_ids, video_ids

    @classmethod
    def _remove_previous_keyframe_bindings(
        cls,
        connection: Any,
        *,
        project_id: str,
        keyframe_ids: set[str],
    ) -> None:
        if not keyframe_ids:
            return
        rows = connection.execute(select(shot).where(shot.c.project_id == project_id)).all()
        for row in rows:
            decoded = cls._decode(dict(row._mapping))
            bindings = decoded["reference_bindings"]
            remaining = [
                binding
                for binding in bindings
                if not (
                    isinstance(binding, dict)
                    and binding.get("role") == "previous_keyframe"
                    and binding.get("keyframe_id") in keyframe_ids
                )
            ]
            if remaining != bindings:
                connection.execute(
                    update(shot)
                    .where(shot.c.id == row.id)
                    .values(
                        reference_bindings_json=json_dumps(remaining),
                        own_signature=shot_signature(
                            {
                                key: (
                                    remaining if key == "reference_bindings" else decoded.get(key)
                                )
                                for key in _SIGNATURE_FIELDS
                            }
                        ),
                        revision=decoded["revision"] + 1,
                        updated_at=utc_now(),
                    )
                )

    def delete(self, shot_id: str) -> None:
        current = self.get(shot_id)
        with self.database.engine.begin() as connection:
            keyframe_ids, _ = self._ensure_no_active_jobs(connection, {shot_id})
            media_paths = self._media_paths(connection, [shot_id])
            self._remove_previous_keyframe_bindings(
                connection,
                project_id=current["project_id"],
                keyframe_ids=keyframe_ids,
            )
            connection.execute(delete(shot).where(shot.c.id == shot_id))
            remaining = list(
                connection.execute(
                    select(shot.c.id)
                    .where(shot.c.project_id == current["project_id"])
                    .order_by(shot.c.shot_order)
                ).scalars()
            )
            self._renumber(connection, current["project_id"], remaining)
        for path in media_paths:
            file_service.delete_file(current["project_id"], path)

    @staticmethod
    def _media_paths(connection: Any, shot_ids: list[str]) -> list[str]:
        if not shot_ids:
            return []
        paths: list[str] = []
        for table in (keyframe_version, video_version):
            paths.extend(
                connection.execute(
                    select(table.c.file_path).where(
                        table.c.shot_id.in_(shot_ids),
                        table.c.file_path.is_not(None),
                    )
                ).scalars()
            )
        return paths

    def reorder(self, project_id: str, ids: list[str]) -> list[dict[str, Any]]:
        self._project(project_id)
        with self.database.engine.begin() as connection:
            actual = list(
                connection.execute(
                    select(shot.c.id).where(shot.c.project_id == project_id)
                ).scalars()
            )
            if len(ids) != len(actual) or set(ids) != set(actual):
                raise bad_request("ids 必须包含当前项目的全部分镜且不能包含其他分镜")
            self._renumber(connection, project_id, ids)
        return self.list(project_id)

    @staticmethod
    def _split_action(action: str) -> tuple[str, str]:
        parts = [part.strip() for part in re.split(r"(?<=[，。；;])", action) if part.strip()]
        if len(parts) >= 2:
            middle = max(1, len(parts) // 2)
            return "".join(parts[:middle]), "".join(parts[middle:])
        return f"{action}（起始阶段）", f"{action}（完成阶段）"

    def split(self, shot_id: str, parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        current = self.get(shot_id)
        with self.database.engine.begin() as connection:
            self._ensure_no_active_jobs(connection, {shot_id})
        if not parts:
            first_action, second_action = self._split_action(current["action"])
            first_duration = max(current["duration_sec"] / 2, 0.1)
            parts = [
                {
                    "action": first_action,
                    "start_state": current["start_state"],
                    "end_state": "主要动作的中间状态",
                    "duration_sec": first_duration,
                },
                {
                    "action": second_action,
                    "start_state": "承接上一镜结束状态",
                    "end_state": current["end_state"],
                    "duration_sec": max(current["duration_sec"] - first_duration, 0.1),
                },
            ]
        self.update(shot_id, parts[0])
        created_ids = [shot_id]
        insert_after = shot_id
        for part in parts[1:]:
            created = self._copy_with_overrides(current, insert_after, part)
            created_ids.append(created["id"])
            insert_after = created["id"]
        return [self.get(item) for item in created_ids]

    def _copy_with_overrides(
        self,
        current: dict[str, Any],
        after_shot_id: str,
        overrides: dict[str, Any],
    ) -> dict[str, Any]:
        project_id = current["project_id"]
        with self.database.engine.begin() as connection:
            ordered = list(
                connection.execute(
                    select(shot.c.id)
                    .where(shot.c.project_id == project_id)
                    .order_by(shot.c.shot_order)
                ).scalars()
            )
            try:
                index = ordered.index(after_shot_id) + 1
            except ValueError as exc:
                raise bad_request("插入位置不属于当前项目") from exc
            new_id_value = new_id("shot")
            ordered.insert(index, new_id_value)
            semantic = {key: overrides.get(key, current.get(key)) for key in _SIGNATURE_FIELDS}
            now = utc_now()
            record = {
                "id": new_id_value,
                "project_id": project_id,
                "shot_no": 100000 + len(ordered),
                "shot_order": 100000 + len(ordered),
                "source_signature": current["source_signature"],
                "own_signature": self._signature(semantic),
                "revision": 1,
                "created_at": now,
                "updated_at": now,
            }
            for key in _SIGNATURE_FIELDS:
                column = _JSON_FIELDS.get(key, key)
                if column in shot.c:
                    value = semantic.get(key)
                    record[column] = json_dumps(value or []) if column.endswith("_json") else value
            connection.execute(insert(shot).values(**record))
            self._renumber(connection, project_id, ordered)
        return self.get(new_id_value)

    def copy(self, shot_id: str, after_shot_id: str | None = None) -> dict[str, Any]:
        current = self.get(shot_id)
        return self._copy_with_overrides(current, after_shot_id or shot_id, {})

    def merge(self, target_id: str, source_ids: list[str]) -> dict[str, Any]:
        source_ids = [item for item in source_ids if item != target_id]
        if not source_ids:
            return self.get(target_id)
        target = self.get(target_id)
        records = [target, *(self.get(item) for item in source_ids)]
        if any(item["project_id"] != target["project_id"] for item in records):
            raise bad_request("只能合并同一项目的分镜")
        affected_ids = {item["id"] for item in records}
        records.sort(key=lambda item: item["shot_order"])
        merged = {
            "start_state": records[0]["start_state"],
            "action": "\n".join(item["action"] for item in records if item["action"]),
            "dialogue": [dialogue for item in records for dialogue in item["dialogue"]],
            "narration": "\n".join(item["narration"] for item in records if item["narration"]),
            "duration_sec": sum(item["duration_sec"] for item in records),
            "end_state": records[-1]["end_state"],
            "next_hook": records[-1]["next_hook"],
            "frame_description": "\n".join(
                item["frame_description"] for item in records if item["frame_description"]
            ),
            "status": "ready",
        }
        semantic = {key: merged.get(key, target.get(key)) for key in _SIGNATURE_FIELDS}
        changes: dict[str, Any] = {}
        for key, value in merged.items():
            column = _JSON_FIELDS.get(key, key)
            changes[column] = json_dumps(value) if column.endswith("_json") else value
        changes.update(
            own_signature=self._signature(semantic),
            revision=target["revision"] + 1,
            updated_at=utc_now(),
        )
        with self.database.engine.begin() as connection:
            self._ensure_no_active_jobs(connection, affected_ids)
            source_keyframe_ids, _ = self._related_media_ids(connection, set(source_ids))
            media_paths = self._media_paths(connection, source_ids)
            connection.execute(update(shot).where(shot.c.id == target_id).values(**changes))
            self._remove_previous_keyframe_bindings(
                connection,
                project_id=target["project_id"],
                keyframe_ids=source_keyframe_ids,
            )
            connection.execute(delete(shot).where(shot.c.id.in_(source_ids)))
            remaining = list(
                connection.execute(
                    select(shot.c.id)
                    .where(shot.c.project_id == target["project_id"])
                    .order_by(shot.c.shot_order)
                ).scalars()
            )
            self._renumber(connection, target["project_id"], remaining)
        for path in media_paths:
            file_service.delete_file(target["project_id"], path)
        return self.get(target_id)

    def merge_many(self, project_id: str, ids: list[str]) -> dict[str, Any]:
        if len(ids) < 2:
            raise bad_request("至少选择两个分镜")
        target = self.get(ids[0])
        if target["project_id"] != project_id:
            raise bad_request("分镜不属于当前项目")
        return self.merge(ids[0], ids[1:])

    def queue_extract(self, project_id: str, options: dict[str, Any]) -> dict[str, Any]:
        current_project = self._project(project_id)
        source_type = "screenplay"
        self._source_signature(current_project, source_type)
        with self.database.engine.connect() as connection:
            scene_count = connection.execute(
                select(func.count())
                .select_from(asset)
                .where(
                    asset.c.project_id == project_id,
                    asset.c.asset_type == "scene",
                )
            ).scalar_one()
        if scene_count == 0:
            raise conflict("请先提取场景素材，再提取分镜")
        return self.jobs.create(
            project_id=project_id,
            job_type="extract_shots",
            target_type="project",
            target_id=project_id,
            provider_kind="text",
            provider_id=options.get("provider_id") or current_project["default_text_provider_id"],
            input_data={
                "source_type": source_type,
                "scope": options.get("scope", "all"),
                "scene_no": options.get("scene_no"),
                "params": options.get("params", {}),
            },
        )

    def queue_prompt(
        self,
        shot_id: str,
        prompt_type: str,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        current = self.get(shot_id)
        current_project = self._project(current["project_id"])
        job_type = (
            "generate_shot_image_prompt" if prompt_type == "image" else "generate_shot_video_prompt"
        )
        return self.jobs.create(
            project_id=current["project_id"],
            job_type=job_type,
            target_type="shot",
            target_id=shot_id,
            provider_kind="text",
            provider_id=options.get("provider_id") or current_project["default_text_provider_id"],
            input_data={"params": options.get("params", {})},
        )

    def queue_prompt_batch(self, project_id: str, options: dict[str, Any]) -> dict[str, Any]:
        current_project = self._project(project_id)
        statement = select(shot.c.id, shot.c.image_prompt, shot.c.video_prompt).where(
            shot.c.project_id == project_id
        )
        if options.get("ids"):
            statement = statement.where(shot.c.id.in_(options["ids"]))
        with self.database.engine.connect() as connection:
            records = connection.execute(statement).all()
        items = []
        for record in records:
            for prompt_type in options["prompt_types"]:
                if options.get("only_missing") and record._mapping[f"{prompt_type}_prompt"]:
                    continue
                items.append(
                    {
                        "project_id": project_id,
                        "job_type": f"generate_shot_{prompt_type}_prompt",
                        "target_type": "shot",
                        "target_id": record.id,
                        "provider_kind": "text",
                        "provider_id": options.get("provider_id")
                        or current_project["default_text_provider_id"],
                        "input_data": {"params": options.get("params", {})},
                    }
                )
        return self.jobs.create_batch(items)

    async def handle_extract(self, job: dict[str, Any]) -> dict[str, Any]:
        current_project = self._project(job["project_id"])
        input_data = json_loads(job.get("input_json"), {})
        source_type = "screenplay"
        with self.database.engine.connect() as connection:
            content_raw = connection.execute(
                select(screenplay.c.content_json).where(
                    screenplay.c.project_id == current_project["id"]
                )
            ).scalar()
        if not content_raw:
            raise conflict("当前项目还没有可用剧本")
        source_content = content_raw
        asset_catalog = self._asset_catalog(current_project["id"])
        if not any(item["asset_type"] == "scene" for item in asset_catalog):
            raise conflict("请先提取场景素材，再提取分镜")
        provider, config = self.providers.resolve(kind="text", provider_id=job.get("provider_id"))
        output = await provider.generate_json(
            system_prompt=load_prompt("shot_extract"),
            user_prompt=(
                f"来源类型：{source_type}\n"
                f"范围：{input_data['scope']}\n"
                f"目标场次：{input_data.get('scene_no') or '全部'}\n"
                f"当前项目可用素材目录：\n{json_dumps(asset_catalog)}\n"
                f"来源内容：\n{source_content}"
            ),
            schema=ShotExtractionOutput,
            params={**config["params"], **input_data.get("params", {})},
        )
        return self._save_extracted(
            current_project,
            source_type,
            input_data,
            output.model_dump(mode="json")["shots"],
        )

    def _asset_catalog(self, project_id: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        with self.database.engine.connect() as connection:
            for row in connection.execute(
                select(
                    asset.c.id,
                    asset.c.asset_type,
                    asset.c.name,
                    asset.c.profile_json,
                ).where(asset.c.project_id == project_id)
            ):
                profile = json_loads(row.profile_json, {})
                result.append(
                    {
                        "id": row.id,
                        "asset_type": row.asset_type,
                        "name": row.name,
                        "concept": profile.get("concept", ""),
                        "aliases": profile.get("aliases", []),
                        "source_refs": profile.get("source_refs", []),
                    }
                )
        return result

    @staticmethod
    def _normalize_asset_label(value: str) -> str:
        return re.sub(r"[\W_]+", "", value.casefold())

    def _asset_lookup(self, project_id: str) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {
            "character": {"ids": set(), "primary": {}, "exact": {}, "normalized": {}},
            "scene": {"ids": set(), "primary": {}, "exact": {}, "normalized": {}},
            "prop": {"ids": set(), "primary": {}, "exact": {}, "normalized": {}},
        }
        for current in self._asset_catalog(project_id):
            lookup = result[current["asset_type"]]
            lookup["ids"].add(current["id"])
            lookup["primary"].setdefault(current["name"], set()).add(current["id"])
            aliases = current.get("aliases", [])
            labels = [
                current["name"],
                *(aliases if isinstance(aliases, list) else []),
            ]
            for label in labels:
                if not isinstance(label, str) or not label.strip():
                    continue
                lookup["exact"].setdefault(label, set()).add(current["id"])
                normalized = self._normalize_asset_label(label)
                if normalized:
                    lookup["normalized"].setdefault(normalized, set()).add(current["id"])
        return result

    def _resolve_asset_name(
        self,
        lookup: dict[str, Any],
        name: str,
    ) -> str | None:
        primary = lookup["primary"].get(name, set())
        if len(primary) == 1:
            return next(iter(primary))
        exact = lookup["exact"].get(name, set())
        if len(exact) == 1:
            return next(iter(exact))
        normalized_name = self._normalize_asset_label(name)
        normalized = lookup["normalized"].get(normalized_name, set())
        if len(normalized) == 1:
            return next(iter(normalized))
        return None

    def _resolve_asset_id(
        self,
        lookup: dict[str, Any],
        *,
        provided_id: str | None,
        name: str,
    ) -> str | None:
        resolved_name = self._resolve_asset_name(lookup, name)
        if provided_id:
            if provided_id not in lookup["ids"]:
                return None
            if name and resolved_name != provided_id:
                return None
            return provided_id
        return resolved_name

    def _resolve_asset_ids(
        self,
        lookup: dict[str, Any],
        *,
        provided_ids: list[str],
        names: list[str],
    ) -> tuple[list[str], list[str]]:
        unique_provided = list(dict.fromkeys(provided_ids))
        resolved = [item for item in unique_provided if item in lookup["ids"]]
        unmatched = [item for item in unique_provided if item not in lookup["ids"]]
        resolved_names: list[tuple[str, str]] = []
        for name in names:
            asset_id = self._resolve_asset_name(lookup, name)
            if asset_id:
                resolved_names.append((name, asset_id))
            elif name not in unmatched:
                unmatched.append(name)
        if provided_ids and names:
            provided_set = set(resolved)
            named_set = {asset_id for _, asset_id in resolved_names}
            if provided_set != named_set:
                for name, asset_id in resolved_names:
                    mismatch = f"{name}（ID 与名称不一致）"
                    if asset_id not in provided_set and mismatch not in unmatched:
                        unmatched.append(mismatch)
                for asset_id in resolved:
                    mismatch = f"{asset_id}（ID 与名称不一致）"
                    if asset_id not in named_set and mismatch not in unmatched:
                        unmatched.append(mismatch)
        elif names:
            resolved = list(dict.fromkeys(asset_id for _, asset_id in resolved_names))
        return resolved, unmatched

    def _save_extracted(
        self,
        current_project: dict[str, Any],
        source_type: str,
        input_data: dict[str, Any],
        extracted_shots: list[dict[str, Any]],
    ) -> dict[str, Any]:
        asset_lookup = self._asset_lookup(current_project["id"])
        source_signature = self._source_signature(current_project, source_type)
        normalized: list[dict[str, Any]] = []
        binding_errors: list[str] = []
        for extracted in extracted_shots:
            item = dict(extracted)
            scene_no = item.pop("scene_no", None)
            source_ref = item.pop("source_ref", "")
            if scene_no:
                source_ref = f"scene:{scene_no}|{source_ref}"
            scene_name = item.pop("scene_name", "")
            scene_asset_id = self._resolve_asset_id(
                asset_lookup["scene"],
                provided_id=item.pop("scene_asset_id", None),
                name=scene_name,
            )
            character_names = item.pop("character_names", [])
            provided_character_ids = item.pop("character_asset_ids", [])
            character_ids, unmatched_characters = self._resolve_asset_ids(
                asset_lookup["character"],
                provided_ids=provided_character_ids,
                names=character_names,
            )
            character_binding_errors = [
                value
                for value in unmatched_characters
                if value in provided_character_ids or "（ID 与名称不一致）" in value
            ]
            prop_names = item.pop("prop_names", [])
            prop_ids, unmatched_props = self._resolve_asset_ids(
                asset_lookup["prop"],
                provided_ids=item.pop("prop_asset_ids", []),
                names=prop_names,
            )
            shot_label = item.get("shot_no", len(normalized) + 1)
            if not scene_asset_id:
                binding_errors.append(
                    f"分镜 {shot_label} 未匹配到场景素材：{scene_name or '未提供场景名称'}"
                )
            if character_binding_errors:
                binding_errors.append(
                    f"分镜 {shot_label} 未匹配到角色素材：{', '.join(character_binding_errors)}"
                )
            if unmatched_props:
                binding_errors.append(
                    f"分镜 {shot_label} 未匹配到物品素材：{', '.join(unmatched_props)}"
                )
            semantic = {
                "source_type": source_type,
                "source_ref": source_ref,
                "scene_asset_id": scene_asset_id,
                "character_ids": character_ids,
                "prop_ids": prop_ids,
                **item,
                "reference_bindings": [],
                "status": "ready",
            }
            semantic["reference_bindings"], _ = self._current_reference_bindings(semantic)
            normalized.append(semantic)
        if binding_errors:
            raise bad_request(
                "分镜素材绑定失败："
                + "；".join(binding_errors)
                + "。请先补齐对应素材，或重新提取分镜。"
            )
        scope_scene = input_data.get("scene_no") if input_data.get("scope") == "scene" else None
        now = utc_now()
        obsolete_paths: list[str] = []
        with self.database.engine.begin() as connection:
            existing_rows = connection.execute(
                select(shot)
                .where(shot.c.project_id == current_project["id"])
                .order_by(shot.c.shot_order)
            ).all()
            if scope_scene is None:
                selected_existing = list(existing_rows)
            else:
                prefix = f"scene:{scope_scene}|"
                selected_existing = [
                    row for row in existing_rows if row.source_ref.startswith(prefix)
                ]
            selected_ids = {row.id for row in selected_existing}
            self._ensure_no_active_jobs(
                connection,
                selected_ids,
            )
            temporary_start = self._temporary_number_start(
                connection,
                current_project["id"],
            )
            saved_ids: list[str] = []
            for index, semantic in enumerate(normalized):
                record_id = (
                    selected_existing[index].id
                    if index < len(selected_existing)
                    else new_id("shot")
                )
                values: dict[str, Any] = {
                    "source_signature": source_signature,
                    "own_signature": self._signature(semantic),
                    "status": "ready",
                    "updated_at": now,
                }
                for key in _SIGNATURE_FIELDS:
                    column = _JSON_FIELDS.get(key, key)
                    if column in shot.c:
                        value = semantic.get(key)
                        values[column] = (
                            json_dumps(value or []) if column.endswith("_json") else value
                        )
                if index < len(selected_existing):
                    values["revision"] = selected_existing[index].revision + 1
                    connection.execute(update(shot).where(shot.c.id == record_id).values(**values))
                else:
                    connection.execute(
                        insert(shot).values(
                            id=record_id,
                            project_id=current_project["id"],
                            shot_no=temporary_start + index,
                            shot_order=temporary_start + index,
                            revision=1,
                            created_at=now,
                            **values,
                        )
                    )
                saved_ids.append(record_id)
            obsolete = selected_existing[len(normalized) :]
            if obsolete:
                obsolete_paths = self._media_paths(connection, [row.id for row in obsolete])
                obsolete_ids = {row.id for row in obsolete}
                obsolete_keyframe_ids = set(
                    connection.execute(
                        select(keyframe_version.c.id).where(
                            keyframe_version.c.shot_id.in_(obsolete_ids)
                        )
                    ).scalars()
                )
                self._remove_previous_keyframe_bindings(
                    connection,
                    project_id=current_project["id"],
                    keyframe_ids=obsolete_keyframe_ids,
                )
                connection.execute(delete(shot).where(shot.c.id.in_([row.id for row in obsolete])))
            ordered_ids: list[str] = []
            if scope_scene is None:
                ordered_ids = saved_ids
            else:
                selected_set = {row.id for row in selected_existing}
                inserted = False
                for row in existing_rows:
                    if row.id in selected_set:
                        if not inserted:
                            ordered_ids.extend(saved_ids)
                            inserted = True
                        continue
                    ordered_ids.append(row.id)
                if not inserted:
                    ordered_ids.extend(saved_ids)
            self._renumber(connection, current_project["id"], ordered_ids)
        for path in obsolete_paths:
            file_service.delete_file(current_project["id"], path)
        return {
            "shot_ids": saved_ids,
            "count": len(saved_ids),
            "warnings": [],
        }

    def _current_reference_bindings(
        self, current: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        bindings: list[dict[str, Any]] = []
        warnings: list[str] = []
        requested = [
            (current.get("scene_asset_id"), "scene_layout"),
            *((item, "character_identity") for item in current["character_ids"]),
            *((item, "prop_identity") for item in current["prop_ids"]),
        ]
        with self.database.engine.connect() as connection:
            for asset_id, role in requested:
                if not asset_id:
                    continue
                image_id = connection.execute(
                    select(asset_image.c.id).where(
                        asset_image.c.asset_id == asset_id,
                        asset_image.c.is_current.is_(True),
                        asset_image.c.status == "completed",
                    )
                ).scalar()
                if image_id:
                    bindings.append(
                        {
                            "role": role,
                            "asset_id": asset_id,
                            "asset_image_id": image_id,
                        }
                    )
                else:
                    warnings.append(f"素材 {asset_id} 缺少当前参考图，将使用纯文字降级")
        return bindings, warnings

    async def handle_image_prompt(self, job: dict[str, Any]) -> dict[str, Any]:
        current = self.get(job["target_id"])
        current_project = self._project(current["project_id"])
        input_data = json_loads(job.get("input_json"), {})
        provider, config = self.providers.resolve(kind="text", provider_id=job.get("provider_id"))
        bindings, warnings = self._current_reference_bindings(current)
        output = await provider.generate_json(
            system_prompt=load_prompt("shot_image_prompt"),
            user_prompt=(
                f"项目画风：{current_project['style_prompt']}\n"
                f"分镜：{json_dumps({key: current.get(key) for key in _SIGNATURE_FIELDS})}\n"
                f"当前参考绑定：{json_dumps(bindings)}"
            ),
            schema=ShotImagePromptOutput,
            params={**config["params"], **input_data.get("params", {})},
        )
        generated = output.model_dump(mode="json")
        updated = self.update(
            current["id"],
            {
                "image_prompt": generated["prompt"],
                "negative_prompt": generated["negative_prompt"],
                "reference_bindings": bindings,
                "status": "ready",
            },
        )
        return {
            "shot_id": updated["id"],
            "prompt": generated,
            "warnings": warnings,
        }

    async def handle_video_prompt(self, job: dict[str, Any]) -> dict[str, Any]:
        current = self.get(job["target_id"])
        current_project = self._project(current["project_id"])
        input_data = json_loads(job.get("input_json"), {})
        provider, config = self.providers.resolve(kind="text", provider_id=job.get("provider_id"))
        output = await provider.generate_json(
            system_prompt=load_prompt("shot_video_prompt"),
            user_prompt=(
                f"项目画风：{current_project['style_prompt']}\n"
                f"实际时长：{current['duration_sec']}\n"
                f"分镜：{json_dumps({key: current.get(key) for key in _SIGNATURE_FIELDS})}"
            ),
            schema=ShotVideoPromptOutput,
            params={**config["params"], **input_data.get("params", {})},
        )
        generated = output.model_dump(mode="json")
        updated = self.update(
            current["id"],
            {
                "video_prompt": generated["prompt"],
                "negative_prompt": generated["negative_prompt"],
                "status": "ready",
            },
        )
        return {"shot_id": updated["id"], "prompt": generated}
