from __future__ import annotations

import math
import re
from typing import Any

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.db import Database, db
from app.models.tables import (
    asset,
    asset_image,
    chapter,
    job,
    keyframe_version,
    project,
    screenplay,
    shot,
    video_version,
)
from app.prompts import load_prompt
from app.schemas import (
    BatchRefinedPromptsOutput,
    ShotContinuityPairOutput,
    ShotExtractionOutput,
    ShotImagePromptOutput,
    ShotOutput,
    ShotQualityFixOutput,
    ShotVideoPromptOptimizeOutput,
    ShotVideoPromptOutput,
)
from app.services.errors import ServiceError, bad_request, conflict, not_found
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
from app.services.utils import (
    json_dumps,
    json_loads,
    new_id,
    row_dict,
    sanitize_error_message,
    utc_now,
)

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
    "transition_from_prev",
    "frame_description",
    "image_prompt",
    "image_prompt_override",
    "video_prompt",
    "video_prompt_override",
    "negative_prompt",
    "reference_bindings",
)

_ACTIVE_JOB_STATUSES = {"queued", "running", "waiting_provider", "downloading"}
_PROMPT_GENERATED_FIELDS = {
    "image_prompt",
    "image_prompt_override",
    "video_prompt",
    "video_prompt_override",
    "negative_prompt",
}
_PROMPT_GUARD_FIELDS = tuple(
    field for field in _SIGNATURE_FIELDS if field not in _PROMPT_GENERATED_FIELDS
)


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

    def _chapter(self, chapter_id: str) -> dict[str, Any]:
        with self.database.engine.connect() as connection:
            record = row_dict(
                connection.execute(select(chapter).where(chapter.c.id == chapter_id)).first()
            )
        if record is None:
            raise not_found("章节")
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
        prev_shot: dict[str, Any] | None = None,
    ) -> None:
        prev_end_state = ""
        if prev_shot:
            # 文字前情只在动作确有承接时交代（continuous / elliptical）。
            # 独立机位或换场景时交代上一镜结束位置，会让模型把两个画面混在一起。
            if current.get("transition_from_prev") in {"continuous", "elliptical"}:
                prev_end_state = str(
                    prev_shot.get("end_state") or prev_shot.get("next_hook") or ""
                ).strip()
        context = shot_template_context(
            current, self._asset_names(connection, current), prev_end_state
        )
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
        current["final_image_prompt"] = (
            str(current["image_prompt_override"] or "").strip() or current["compiled_image_prompt"]
        )

        # 预览用的 @Image 编号必须与 media_service.queue_video 实际下发的顺序一致：
        # 关键帧固定 @Image1，上一镜末帧（仅 continuous 且已生成）次之，其余素材顺延
        has_prev_end_frame = False
        if prev_shot and current.get("transition_from_prev") == "continuous":
            has_prev_end_frame = bool(
                connection.execute(
                    select(video_version.c.end_frame_path)
                    .where(
                        video_version.c.shot_id == prev_shot["id"],
                        video_version.c.is_current.is_(True),
                        video_version.c.status == "completed",
                        video_version.c.end_frame_path.isnot(None),
                    )
                    .limit(1)
                ).scalar()
            )

        video_anchors = ["@Image1：当前分镜关键帧，锁定构图、人物左右关系和起始站位。"]
        if has_prev_end_frame:
            video_anchors.append("@Image2：上一镜结束帧，仅用于衔接连贯，不是本镜起始画面。")
        base_index = len(video_anchors) + 1
        video_anchors.extend(
            f"@Image{base_index + index}：{label}。" for index, label in enumerate(labels)
        )

        current["compiled_video_prompt"] = compile_video_prompt(
            style_prompt=current_project["style_prompt"],
            aspect_ratio=current_project["aspect_ratio"],
            video_prompt=video_prompt,
            duration_sec=current["duration_sec"],
            anchors=video_anchors,
            shot_context=context,
            negative_prompt=current["negative_prompt"],
        )
        current["final_video_prompt"] = (
            str(current["video_prompt_override"] or "").strip() or current["compiled_video_prompt"]
        )

    def list(self, chapter_id: str) -> list[dict[str, Any]]:
        current_chapter = self._chapter(chapter_id)
        project_id = current_chapter["project_id"]
        current_project = self._project(project_id)
        statement = (
            select(shot)
            .where(shot.c.chapter_id == chapter_id)
            .order_by(shot.c.shot_order, shot.c.shot_no)
        )
        with self.database.engine.connect() as connection:
            # 先收集所有 records，再按顺序传前一条给 _add_compiled_prompts
            # 使得 previous_state 能正确使用上一镜的 end_state
            raw_records = []
            for row in connection.execute(statement):
                record = self._decode(dict(row._mapping))
                source_now = self._current_source_signature(
                    connection, current_chapter, record["source_type"]
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
                raw_records.append(record)
            for i, record in enumerate(raw_records):
                prev = raw_records[i - 1] if i > 0 else None
                self._add_compiled_prompts(connection, current_project, record, prev)
        for record in raw_records:
            record["chapter_no"] = current_chapter["chapter_no"]
            record["chapter_title"] = current_chapter["title"]
        return raw_records

    def list_project(self, project_id: str) -> list[dict[str, Any]]:
        """全书分镜，按章节序 + 镜序排列。供跨章节的审校、精修与质量报告使用。

        优化：一次查询获取所有分镜，避免 N+1 查询问题。
        """
        current_project = self._project(project_id)
        with self.database.engine.connect() as connection:
            # 一次性查询所有章节信息
            chapter_map = {
                row.id: {"chapter_no": row.chapter_no, "title": row.title}
                for row in connection.execute(
                    select(chapter.c.id, chapter.c.chapter_no, chapter.c.title)
                    .where(chapter.c.project_id == project_id)
                    .order_by(chapter.c.chapter_no)
                )
            }

            if not chapter_map:
                return []

            # 一次性查询所有分镜（按章节序 + 镜序排列）
            statement = (
                select(shot, chapter.c.chapter_no)
                .join(chapter, shot.c.chapter_id == chapter.c.id)
                .where(chapter.c.project_id == project_id)
                .order_by(chapter.c.chapter_no, shot.c.shot_order, shot.c.shot_no)
            )

            raw_records = []
            for row in connection.execute(statement):
                record = self._decode(dict(row._mapping))
                chapter_id = record["chapter_id"]
                chapter_info = chapter_map[chapter_id]

                # 获取源签名（需要当前章节信息）
                current_chapter = self._chapter(chapter_id)
                source_now = self._current_source_signature(
                    connection, current_chapter, record["source_type"]
                )
                record["is_stale"] = bool(
                    record["source_signature"] and record["source_signature"] != source_now
                )

                # 获取当前关键帧和视频
                record["current_keyframe"] = self._media_record(
                    connection, keyframe_version, record["id"], project_id
                )
                record["current_video"] = self._media_record(
                    connection, video_version, record["id"], project_id
                )

                # 添加章节信息
                record["chapter_no"] = chapter_info["chapter_no"]
                record["chapter_title"] = chapter_info["title"]
                raw_records.append(record)

            # 计算编译后的提示词（需要前一镜信息）
            for i, record in enumerate(raw_records):
                prev = raw_records[i - 1] if i > 0 else None
                self._add_compiled_prompts(connection, current_project, record, prev)

        return raw_records


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

    @staticmethod
    def _adjacent_shot_data(
        connection: Any,
        chapter_id: str,
        shot_id: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """返回上一镜和下一镜的轻量数据，供提示词生成时注入衔接信息。"""
        ordered_ids = list(
            connection.execute(
                select(shot.c.id)
                .where(shot.c.chapter_id == chapter_id)
                .order_by(shot.c.shot_order, shot.c.shot_no)
            ).scalars()
        )
        try:
            idx = ordered_ids.index(shot_id)
        except ValueError:
            return None, None
        prev_data: dict[str, Any] | None = None
        next_data: dict[str, Any] | None = None
        if idx > 0:
            row = connection.execute(
                select(
                    shot.c.shot_no,
                    shot.c.end_state,
                    shot.c.next_hook,
                    shot.c.action,
                ).where(shot.c.id == ordered_ids[idx - 1])
            ).first()
            if row:
                prev_data = dict(row._mapping)
        if idx < len(ordered_ids) - 1:
            row = connection.execute(
                select(
                    shot.c.shot_no,
                    shot.c.start_state,
                    shot.c.action,
                ).where(shot.c.id == ordered_ids[idx + 1])
            ).first()
            if row:
                next_data = dict(row._mapping)
        return prev_data, next_data

    def _current_source_signature(
        self,
        connection: Any,
        chapter_record: dict[str, Any],
        source_type: str,
    ) -> str:
        if source_type == "novel":
            return chapter_record["source_signature"]
        return (
            connection.execute(
                select(screenplay.c.own_signature).where(
                    screenplay.c.chapter_id == chapter_record["id"]
                )
            ).scalar()
            or ""
        )

    def _source_signature(self, chapter_record: dict[str, Any], source_type: str) -> str:
        with self.database.engine.connect() as connection:
            signature = self._current_source_signature(connection, chapter_record, source_type)
        if source_type == "screenplay" and not signature:
            raise conflict("当前章节还没有可用剧本")
        return signature

    def _signature(self, values: dict[str, Any]) -> str:
        return shot_signature({key: values.get(key) for key in _SIGNATURE_FIELDS})

    @staticmethod
    def _prompt_guard_signature(current: dict[str, Any], prompt_type: str) -> str:
        values = {field: current.get(field) for field in _PROMPT_GUARD_FIELDS}
        values[f"{prompt_type}_prompt"] = current.get(f"{prompt_type}_prompt")
        values[f"{prompt_type}_prompt_override"] = current.get(f"{prompt_type}_prompt_override")
        return shot_signature(values)

    def _prompt_job_input(
        self,
        current: dict[str, Any],
        prompt_type: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "expected_revision": current["revision"],
            "expected_prompt_signature": self._prompt_guard_signature(current, prompt_type),
            "expected_negative_prompt": current["negative_prompt"],
            "expected_status": current["status"],
            "params": params,
        }

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

    def _next_numbers(self, connection: Any, chapter_id: str) -> tuple[int, int]:
        row = connection.execute(
            select(
                func.max(shot.c.shot_no),
                func.max(shot.c.shot_order),
            ).where(shot.c.chapter_id == chapter_id)
        ).one()
        return (row[0] or 0) + 1, (row[1] or 0) + 1

    def create(self, chapter_id: str, values: dict[str, Any]) -> dict[str, Any]:
        current_chapter = self._chapter(chapter_id)
        project_id = current_chapter["project_id"]
        if values.get("duration_sec") is None:
            raise bad_request("必须提供分镜时长")
        source_type = "screenplay"
        with self.database.engine.connect() as connection:
            source_signature = self._current_source_signature(
                connection,
                current_chapter,
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
        semantic.setdefault("image_prompt_override", "")
        semantic.setdefault("video_prompt_override", "")
        # 这些列 NOT NULL 且无 server_default，未传入时补空串，
        # 允许只提供时长和提示词的手动分镜直接创建。
        for key in (
            "source_ref",
            "start_state",
            "action",
            "narration",
            "shot_type",
            "camera_angle",
            "camera_move",
            "end_state",
            "next_hook",
            "frame_description",
            "image_prompt",
            "video_prompt",
            "negative_prompt",
        ):
            semantic.setdefault(key, "")
        # 该列 NOT NULL；调用方未指定时取安全侧默认（不引用上一镜末帧）
        if not semantic.get("transition_from_prev"):
            semantic["transition_from_prev"] = "scene_change"
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
            next_no, next_order = self._next_numbers(connection, chapter_id)
            record: dict[str, Any] = {
                "id": new_id("shot"),
                "project_id": project_id,
                "chapter_id": chapter_id,
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
    def _temporary_number_start(connection: Any, chapter_id: str) -> int:
        row = connection.execute(
            select(
                func.max(shot.c.shot_no),
                func.max(shot.c.shot_order),
            ).where(shot.c.chapter_id == chapter_id)
        ).one()
        return max(row[0] or 0, row[1] or 0) + 1

    def _renumber(self, connection: Any, chapter_id: str, ids: list[str]) -> None:
        temporary_start = self._temporary_number_start(connection, chapter_id)
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
    def _cancel_active_jobs(
        cls,
        connection: Any,
        shot_ids: set[str],
    ) -> tuple[set[str], set[str]]:
        """删除分镜时取消其进行中的任务，避免用户被“进行中”拦截。"""
        keyframe_ids, video_ids = cls._related_media_ids(connection, shot_ids)
        now = utc_now()
        connection.execute(
            update(job)
            .where(
                job.c.status.in_(_ACTIVE_JOB_STATUSES),
                (
                    (job.c.target_type == "shot") & job.c.target_id.in_(shot_ids)
                    | (job.c.target_type == "keyframe") & job.c.target_id.in_(keyframe_ids)
                    | (job.c.target_type == "video") & job.c.target_id.in_(video_ids)
                ),
            )
            .values(
                status="cancelled",
                message="关联分镜已删除，任务已取消",
                finished_at=now,
                updated_at=now,
            )
        )
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
            keyframe_ids, _ = self._cancel_active_jobs(connection, {shot_id})
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
                    .where(shot.c.chapter_id == current["chapter_id"])
                    .order_by(shot.c.shot_order)
                ).scalars()
            )
            self._renumber(connection, current["chapter_id"], remaining)
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
            if table is video_version:
                paths.extend(
                    connection.execute(
                        select(table.c.end_frame_path).where(
                            table.c.shot_id.in_(shot_ids),
                            table.c.end_frame_path.is_not(None),
                        )
                    ).scalars()
                )
        return paths

    def reorder(self, chapter_id: str, ids: list[str]) -> list[dict[str, Any]]:
        self._chapter(chapter_id)
        with self.database.engine.begin() as connection:
            actual = list(
                connection.execute(
                    select(shot.c.id).where(shot.c.chapter_id == chapter_id)
                ).scalars()
            )
            if len(ids) != len(actual) or set(ids) != set(actual):
                raise bad_request("ids 必须包含当前章节的全部分镜且不能包含其他分镜")
            self._renumber(connection, chapter_id, ids)
        return self.list(chapter_id)

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
                    "transition_from_prev": "continuous",
                },
            ]
        parts = [
            {
                **part,
                "image_prompt_override": "",
                "video_prompt_override": "",
            }
            for part in parts
        ]
        # 拆分后的后半镜默认与前半镜连续承接；用户可在编辑中再调整
        parts = [
            {
                **part,
                **(
                    {"transition_from_prev": "continuous"}
                    if index > 0 and "transition_from_prev" not in part
                    else {}
                ),
            }
            for index, part in enumerate(parts)
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
        chapter_id = current["chapter_id"]
        with self.database.engine.begin() as connection:
            ordered = list(
                connection.execute(
                    select(shot.c.id)
                    .where(shot.c.chapter_id == chapter_id)
                    .order_by(shot.c.shot_order)
                ).scalars()
            )
            try:
                index = ordered.index(after_shot_id) + 1
            except ValueError as exc:
                raise bad_request("插入位置不属于当前章节") from exc
            new_id_value = new_id("shot")
            ordered.insert(index, new_id_value)
            semantic = {
                key: overrides.get(
                    key,
                    "scene_change" if key == "transition_from_prev" else current.get(key),
                )
                for key in _SIGNATURE_FIELDS
            }
            now = utc_now()
            record = {
                "id": new_id_value,
                "project_id": project_id,
                "chapter_id": chapter_id,
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
            self._renumber(connection, chapter_id, ordered)
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
        if any(item["chapter_id"] != target["chapter_id"] for item in records):
            raise bad_request("只能合并同一章节的分镜")
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
            "image_prompt_override": "",
            "video_prompt_override": "",
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
                    .where(shot.c.chapter_id == target["chapter_id"])
                    .order_by(shot.c.shot_order)
                ).scalars()
            )
            self._renumber(connection, target["chapter_id"], remaining)
        for path in media_paths:
            file_service.delete_file(target["project_id"], path)
        return self.get(target_id)

    def merge_many(self, chapter_id: str, ids: list[str]) -> dict[str, Any]:
        if len(ids) < 2:
            raise bad_request("至少选择两个分镜")
        target = self.get(ids[0])
        if target["chapter_id"] != chapter_id:
            raise bad_request("分镜不属于当前章节")
        return self.merge(ids[0], ids[1:])

    def queue_extract(self, chapter_id: str, options: dict[str, Any]) -> dict[str, Any]:
        current_chapter = self._chapter(chapter_id)
        project_id = current_chapter["project_id"]
        current_project = self._project(project_id)
        source_type = "screenplay"
        self._source_signature(current_chapter, source_type)
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
            chapter_id=chapter_id,
            job_type="extract_shots",
            target_type="chapter",
            target_id=chapter_id,
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
            chapter_id=current["chapter_id"],
            job_type=job_type,
            target_type="shot",
            target_id=shot_id,
            provider_kind="text",
            provider_id=options.get("provider_id") or current_project["default_text_provider_id"],
            input_data=self._prompt_job_input(
                current,
                prompt_type,
                options.get("params", {}),
            ),
        )

    def queue_prompt_batch(self, chapter_id: str, options: dict[str, Any]) -> dict[str, Any]:
        current_chapter = self._chapter(chapter_id)
        project_id = current_chapter["project_id"]
        current_project = self._project(project_id)
        statement = select(shot).where(shot.c.chapter_id == chapter_id)
        if options.get("ids"):
            statement = statement.where(shot.c.id.in_(options["ids"]))
        with self.database.engine.connect() as connection:
            records = [self._decode(dict(row._mapping)) for row in connection.execute(statement)]
        items = []
        for record in records:
            for prompt_type in options["prompt_types"]:
                if options.get("only_missing") and (
                    str(record[f"{prompt_type}_prompt_override"] or "").strip()
                    or str(record[f"{prompt_type}_prompt"] or "").strip()
                ):
                    continue
                items.append(
                    {
                        "project_id": project_id,
                        "chapter_id": chapter_id,
                        "job_type": f"generate_shot_{prompt_type}_prompt",
                        "target_type": "shot",
                        "target_id": record["id"],
                        "provider_kind": "text",
                        "provider_id": options.get("provider_id")
                        or current_project["default_text_provider_id"],
                        "input_data": self._prompt_job_input(
                            record,
                            prompt_type,
                            options.get("params", {}),
                        ),
                    }
                )
        return self.jobs.create_batch(items)

    async def handle_extract(self, job: dict[str, Any]) -> dict[str, Any]:
        current_chapter = self._chapter(job["chapter_id"])
        current_project = self._project(current_chapter["project_id"])
        input_data = json_loads(job.get("input_json"), {})
        source_type = "screenplay"
        with self.database.engine.connect() as connection:
            content_raw = connection.execute(
                select(screenplay.c.content_json).where(
                    screenplay.c.chapter_id == current_chapter["id"]
                )
            ).scalar()
        if not content_raw:
            raise conflict("当前章节还没有可用剧本")
        screenplay_content = json_loads(content_raw, {})
        scenes = (
            screenplay_content.get("scenes")
            if isinstance(screenplay_content, dict)
            else None
        )
        asset_catalog = self._asset_catalog(current_project["id"])
        if not any(item["asset_type"] == "scene" for item in asset_catalog):
            raise conflict("请先提取场景素材，再提取分镜")
        provider, config = self.providers.resolve(kind="text", provider_id=job.get("provider_id"))
        all_shots: list[dict[str, Any]] = []
        all_warnings: list[dict[str, Any]] = []

        # 对话多的剧本一次全量提取容易截断/漏镜，按场次逐个调用，任一场失败则整批不落库。
        if isinstance(scenes, list) and scenes:
            if input_data.get("scope") == "scene":
                target_index = next(
                    (
                        index
                        for index, scene in enumerate(scenes)
                        if scene.get("scene_no") == input_data.get("scene_no")
                    ),
                    -1,
                )
                if target_index < 0:
                    raise not_found("剧本场次")
                scene_chunks = [scenes[target_index]]
                prev_scene = scenes[target_index - 1] if target_index > 0 else None
            else:
                scene_chunks = scenes
                prev_scene = None
            total_scenes = len(scene_chunks)
            for scene_index, scene in enumerate(scene_chunks, start=1):
                scene_no = int(scene.get("scene_no") or 0)
                continuity_context = ""
                if prev_scene is not None:
                    prev_transition = str(prev_scene.get("transition_to_next") or "").strip()
                    continuity_context = (
                        "\n\n上一场衔接信息（仅供本场首镜承接，不影响本场内容）："
                        f"上一场「{prev_scene.get('scene_name') or ''}」结束时："
                        f"{prev_transition or '无明确交代'}"
                    )
                scene_payload = json_dumps(
                    {
                        "title": screenplay_content.get("title", ""),
                        "logline": screenplay_content.get("logline", ""),
                        "scene": scene,
                    }
                )
                output = await provider.generate_json(
                    system_prompt=load_prompt("shot_extract"),
                    user_prompt=(
                        f"来源类型：{source_type}\n"
                        f"范围：{input_data.get('scope', 'all')}\n"
                        f"目标场次：{scene_no}\n"
                        f"当前项目可用素材目录：\n{json_dumps(asset_catalog)}\n"
                        f"来源内容：\n{scene_payload}"
                        f"{continuity_context}"
                    ),
                    schema=ShotExtractionOutput,
                    params={**config["params"], **input_data.get("params", {})},
                )
                shots, warnings = self._collect_extraction_result(output)
                if not shots:
                    raise conflict(
                        f"场次 {scene_no} 未提取到分镜，已中止整批提取，避免覆盖旧分镜"
                    )
                for shot in shots:
                    shot["scene_no"] = scene_no
                    if scene_no and not str(shot.get("source_ref") or "").strip():
                        shot["source_ref"] = f"scene:{scene_no}"
                all_shots.extend(shots)
                all_warnings.extend(warnings)
                prev_scene = scene
                update_progress = getattr(self.jobs, "update_progress", None)
                if update_progress is not None:
                    update_progress(
                        job["id"],
                        scene_index,
                        total_scenes,
                        message=f"提取场次 {scene_no}（{scene_index}/{total_scenes}）",
                    )
        else:
            # 旧格式/无 scenes 结构：整份内容单次提取，保持兼容
            output = await provider.generate_json(
                system_prompt=load_prompt("shot_extract"),
                user_prompt=(
                    f"来源类型：{source_type}\n"
                    f"范围：{input_data.get('scope', 'all')}\n"
                    f"目标场次：{input_data.get('scene_no') or '全部'}\n"
                    f"当前项目可用素材目录：\n{json_dumps(asset_catalog)}\n"
                    f"来源内容：\n{content_raw}"
                ),
                schema=ShotExtractionOutput,
                params={**config["params"], **input_data.get("params", {})},
            )
            all_shots, all_warnings = self._collect_extraction_result(output)
        return self._save_extracted(
            current_chapter,
            source_type,
            input_data,
            all_shots,
            all_warnings,
        )

    @staticmethod
    def _collect_extraction_result(
        output: Any,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """把 provider 返回的分镜结果统一成 dict 列表，并收集质量警告。

        output.shots 在真实链路是 ShotOutput 列表，测试替身里可能是 dict 列表，
        这里两种都兼容；无法解析的项保留原 dict 交给 _save_extracted 处理。
        """
        raw_shots = output.shots
        if isinstance(raw_shots, dict):
            raw_shots = raw_shots.get("shots", [])
        shots: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        for item in raw_shots:
            checked = item if isinstance(item, ShotOutput) else None
            if isinstance(item, dict):
                shots.append(dict(item))
                if checked is None:
                    try:
                        checked = ShotOutput.model_validate(item)
                    except Exception:
                        continue
            elif checked is not None:
                shots.append(checked.model_dump(mode="json"))
            else:
                continue
            for message in checked.quality_warnings():
                warnings.append({"shot_no": checked.shot_no, "message": message})
        return shots, warnings

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
        current_chapter: dict[str, Any],
        source_type: str,
        input_data: dict[str, Any],
        extracted_shots: list[dict[str, Any]],
        quality_warnings: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        project_id = current_chapter["project_id"]
        chapter_id = current_chapter["id"]
        # 素材查找按项目（全书共用），分镜落库按章节
        asset_lookup = self._asset_lookup(project_id)
        source_signature = self._source_signature(current_chapter, source_type)
        normalized: list[dict[str, Any]] = []
        binding_errors: list[str] = []
        for extracted in extracted_shots:
            item = dict(extracted)
            scene_no = item.pop("scene_no", None)
            source_ref = item.pop("source_ref", "")
            if scene_no:
                source_ref = re.sub(r"^scene:\d+\|?", "", source_ref)
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
                "image_prompt_override": "",
                "video_prompt_override": "",
                # LLM 未给出时兜底：该列 NOT NULL，且默认不引用上一镜末帧
                "transition_from_prev": item.get("transition_from_prev") or "scene_change",
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
                select(shot).where(shot.c.chapter_id == chapter_id).order_by(shot.c.shot_order)
            ).all()
            if scope_scene is None:
                selected_existing = list(existing_rows)
            else:
                prefix = f"scene:{scope_scene}|"
                selected_existing = [
                    row for row in existing_rows if row.source_ref.startswith(prefix)
                ]
            if scope_scene is None and not normalized and selected_existing:
                raise bad_request("提取结果为空，已保留现有分镜，未做覆盖删除")
            selected_ids = {row.id for row in selected_existing}
            self._ensure_no_active_jobs(
                connection,
                selected_ids,
            )
            temporary_start = self._temporary_number_start(connection, chapter_id)
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
                            project_id=project_id,
                            chapter_id=chapter_id,
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
                    project_id=project_id,
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
            self._renumber(connection, chapter_id, ordered_ids)
        for path in obsolete_paths:
            file_service.delete_file(project_id, path)
        return {
            "shot_ids": saved_ids,
            "count": len(saved_ids),
            "warnings": quality_warnings or [],
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

    def _prev_keyframe_binding(self, current: dict[str, Any]) -> dict[str, Any] | None:
        """查询上一镜的已完成关键帧，返回 previous_keyframe binding；无则返回 None。

        结果不写库，仅在生成关键帧提示词时临时注入，用于图生图锁定前镜构图连续性。
        """
        with self.database.engine.connect() as connection:
            ordered_ids = list(
                connection.execute(
                    select(shot.c.id)
                    .where(shot.c.chapter_id == current["chapter_id"])
                    .order_by(shot.c.shot_order, shot.c.shot_no)
                ).scalars()
            )
            try:
                idx = ordered_ids.index(current["id"])
            except ValueError:
                return None
            # 本章首镜没有可承接的前帧
            if idx == 0:
                return None
            prev_id = ordered_ids[idx - 1]
            keyframe_id = connection.execute(
                select(keyframe_version.c.id).where(
                    keyframe_version.c.shot_id == prev_id,
                    keyframe_version.c.is_current.is_(True),
                    keyframe_version.c.status == "completed",
                )
            ).scalar()
            if keyframe_id:
                return {"role": "previous_keyframe", "keyframe_id": keyframe_id}
        return None

    @staticmethod
    def _adjacent_context_text(
        prev_data: dict[str, Any] | None,
        next_data: dict[str, Any] | None,
    ) -> str:
        """构建前后镜衔接信息文本，注入提示词生成以改善剧情连续性。"""
        parts: list[str] = []
        if prev_data:
            end = str(prev_data.get("end_state") or prev_data.get("next_hook") or "").strip()
            if end:
                parts.append(f"上一镜（镜{prev_data.get('shot_no', '?')}）结束状态：{end}")
        if next_data:
            start = str(next_data.get("start_state") or "").strip()
            if start:
                parts.append(f"下一镜（镜{next_data.get('shot_no', '?')}）开始状态：{start}")
        return "\n".join(parts)

    async def handle_image_prompt(self, job: dict[str, Any]) -> dict[str, Any]:
        current = self.get(job["target_id"])
        current_project = self._project(current["project_id"])
        input_data = json_loads(job.get("input_json"), {})
        expected_revision = self._prompt_expected_revision(input_data, current)
        if not self._prompt_job_matches(
            input_data,
            current,
            "image",
            expected_revision,
        ):
            return self._skipped_prompt_result(current["id"])
        provider, config = self.providers.resolve(kind="text", provider_id=job.get("provider_id"))
        bindings, warnings = self._current_reference_bindings(current)
        # 方案C：自动将上一镜已完成关键帧注入绑定，图生图锁定构图连续性（不写库）
        prev_keyframe = self._prev_keyframe_binding(current)
        if prev_keyframe:
            bindings = [prev_keyframe, *bindings]
            warnings = ["已自动绑定上一镜关键帧以保持连续性", *warnings]
        # 方案B：注入前后镜衔接信息，让 LLM 感知剧情节点
        with self.database.engine.connect() as connection:
            prev_data, next_data = self._adjacent_shot_data(
                connection, current["chapter_id"], current["id"]
            )
        adjacent_context = self._adjacent_context_text(prev_data, next_data)
        output = await provider.generate_json(
            system_prompt=load_prompt("shot_image_prompt"),
            user_prompt=(
                f"项目画风：{current_project['style_prompt']}\n"
                f"分镜：{json_dumps({key: current.get(key) for key in _SIGNATURE_FIELDS})}\n"
                f"当前参考绑定：{json_dumps(bindings)}"
                + (
                    f"\n\n衔接信息（仅供连续性参考，不影响本镜内容）：\n{adjacent_context}"
                    if adjacent_context
                    else ""
                )
            ),
            schema=ShotImagePromptOutput,
            params={**config["params"], **input_data.get("params", {})},
        )
        generated = output.model_dump(mode="json")
        updated = self._save_generated_prompt(
            current["id"],
            "image",
            input_data,
            expected_revision,
            {
                "image_prompt": generated["prompt"],
                "image_prompt_override": "",
                "reference_bindings": bindings,
                "status": "ready",
            },
            generated["negative_prompt"],
        )
        if updated is None:
            return self._skipped_prompt_result(current["id"], generated)
        return {
            "shot_id": updated["id"],
            "prompt": generated,
            "warnings": warnings,
        }

    async def handle_video_prompt(self, job: dict[str, Any]) -> dict[str, Any]:
        current = self.get(job["target_id"])
        current_project = self._project(current["project_id"])
        input_data = json_loads(job.get("input_json"), {})
        expected_revision = self._prompt_expected_revision(input_data, current)
        if not self._prompt_job_matches(
            input_data,
            current,
            "video",
            expected_revision,
        ):
            return self._skipped_prompt_result(current["id"])
        provider, config = self.providers.resolve(kind="text", provider_id=job.get("provider_id"))
        # 方案B：注入前后镜衔接信息
        with self.database.engine.connect() as connection:
            prev_data, next_data = self._adjacent_shot_data(
                connection, current["chapter_id"], current["id"]
            )
        adjacent_context = self._adjacent_context_text(prev_data, next_data)
        output = await provider.generate_json(
            system_prompt=load_prompt("shot_video_prompt"),
            user_prompt=(
                f"项目画风：{current_project['style_prompt']}\n"
                f"实际时长：{current['duration_sec']}\n"
                f"分镜：{json_dumps({key: current.get(key) for key in _SIGNATURE_FIELDS})}"
                + (
                    f"\n\n衔接信息（仅供连续性参考，不影响本镜内容）：\n{adjacent_context}"
                    if adjacent_context
                    else ""
                )
            ),
            schema=ShotVideoPromptOutput,
            params={**config["params"], **input_data.get("params", {})},
        )
        generated = output.model_dump(mode="json")
        updated = self._save_generated_prompt(
            current["id"],
            "video",
            input_data,
            expected_revision,
            {
                "video_prompt": generated["prompt"],
                "video_prompt_override": "",
                "status": "ready",
            },
            generated["negative_prompt"],
        )
        if updated is None:
            return self._skipped_prompt_result(current["id"], generated)
        return {"shot_id": updated["id"], "prompt": generated}

    async def _optimize_video_prompt_once(
        self,
        current: dict[str, Any],
        current_project: dict[str, Any],
        original: str,
        *,
        provider_id: str | None,
        params: dict[str, Any] | None,
    ) -> str:
        """调用文字模型优化单条视频提示词（有限优化），返回优化后文本。"""
        with self.database.engine.connect() as connection:
            asset_names = self._asset_names(connection, current)
            # 传入前后镜衔接信息，避免优化时把承接句当作重复描述删除
            prev_data, next_data = self._adjacent_shot_data(
                connection, current["chapter_id"], current["id"]
            )
        context = shot_template_context(current, asset_names)
        adjacent_context = self._adjacent_context_text(prev_data, next_data)
        provider, config = self.providers.resolve(
            kind="text",
            provider_id=provider_id or current_project["default_text_provider_id"],
        )
        output = await provider.generate_json(
            system_prompt=load_prompt("shot_video_optimize"),
            user_prompt=(
                f"项目画风：{current_project['style_prompt']}\n"
                f"分镜时长：{current['duration_sec']:g} 秒\n"
                f"场景：{context.get('scene_name', '当前分镜场景')}\n"
                f"人物：{context.get('characters', '当前分镜角色')}\n"
                f"物品：{context.get('props', '当前分镜物品')}\n"
                + (
                    f"\n衔接信息（承接句必须保留，不得当作重复删除）：\n{adjacent_context}\n"
                    if adjacent_context
                    else ""
                )
                + "\n当前视频提示词（模板已编译）：\n"
                f"{original}"
            ),
            schema=ShotVideoPromptOptimizeOutput,
            params={**config["params"], **(params or {})},
        )
        optimized = str(output.prompt or "").strip()
        if not optimized:
            raise bad_request("文字模型返回了空提示词，请重试")
        return optimized

    async def optimize_video_prompt(
        self,
        shot_id: str,
        *,
        prompt: str,
        provider_id: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """调用文字模型优化当前分镜的视频提示词（有限优化，不改剧情内容）。

        返回优化后的提示词文本，由前端填入提示词编辑区；不落库，
        用户确认后可保存为 video_prompt_override。
        """
        current = self.get(shot_id)
        current_project = self._project(current["project_id"])
        original = str(prompt or "").strip()
        if not original:
            raise bad_request("当前分镜没有可优化的视频提示词")
        optimized = await self._optimize_video_prompt_once(
            current,
            current_project,
            original,
            provider_id=provider_id,
            params=params,
        )
        return {
            "shot_id": shot_id,
            "original": original,
            "prompt": optimized,
        }

    def queue_video_prompt_optimize_batch(
        self,
        project_id: str,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        """一键优化全部分镜视频提示词：入队批量任务，结果直接写入 override。"""
        current_project = self._project(project_id)
        statement = select(shot).where(shot.c.project_id == project_id)
        if options.get("ids"):
            statement = statement.where(shot.c.id.in_(options["ids"]))
        with self.database.engine.connect() as connection:
            records = [self._decode(dict(row._mapping)) for row in connection.execute(statement)]
        targets: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        for record in records:
            final = (
                str(record.get("video_prompt_override") or "").strip()
                or str(record.get("video_prompt") or "").strip()
            )
            if not final:
                skipped.append(
                    {
                        "target_id": record["id"],
                        "error": f"分镜 {record['shot_no']} 没有视频提示词，已跳过",
                    }
                )
                continue
            targets.append(record)
        if not targets:
            raise bad_request("没有可优化的分镜视频提示词")
        provider, config = self.providers.resolve(
            kind="text",
            provider_id=options.get("provider_id") or current_project["default_text_provider_id"],
        )
        job = self.jobs.create(
            project_id=project_id,
            job_type="optimize_shot_video_prompts",
            target_type="project",
            target_id=project_id,
            provider_kind="text",
            provider_id=config["id"],
            input_data={
                "params": options.get("params", {}),
                "ids": options.get("ids") or [],
            },
            progress_total=len(targets),
        )
        return {
            "batch_id": job.get("batch_id") or job["id"],
            "jobs": [job],
            "preflight_failures": skipped,
        }

    async def handle_optimize_video_prompts(self, job: dict[str, Any]) -> dict[str, Any]:
        """批量优化视频提示词任务：逐个优化并写入 video_prompt_override。"""
        project_id = str(job["target_id"])
        current_project = self._project(project_id)
        input_data = json_loads(job.get("input_json"), {})
        provider_id = job.get("provider_id")
        params = input_data.get("params", {})
        requested_ids = {str(item) for item in (input_data.get("ids") or [])}
        shots = self.list_project(project_id)
        targets = [
            item
            for item in shots
            if (not requested_ids or item["id"] in requested_ids)
            and (
                str(item.get("video_prompt_override") or "").strip()
                or str(item.get("video_prompt") or "").strip()
            )
        ]
        total = len(targets)
        succeeded = 0
        failed = 0
        skipped = 0
        failures: list[dict[str, Any]] = []
        for index, item in enumerate(targets, start=1):
            original = (
                str(item.get("video_prompt_override") or "").strip()
                or str(item.get("video_prompt") or "").strip()
            )
            try:
                optimized = await self._optimize_video_prompt_once(
                    item,
                    current_project,
                    original,
                    provider_id=provider_id,
                    params=params,
                )
                try:
                    self.update(
                        item["id"],
                        {"video_prompt_override": optimized},
                        expected_revision=item["revision"],
                    )
                    succeeded += 1
                except ServiceError as exc:
                    if exc.status_code not in {409, 404}:
                        raise
                    # 优化期间分镜被其他请求修改或删除：跳过，保留用户最新内容
                    skipped += 1
                    failures.append(
                        {
                            "shot_id": item["id"],
                            "shot_no": item["shot_no"],
                            "error": (
                                "分镜已被其他请求更新，本次跳过"
                                if exc.status_code == 409
                                else "分镜已被删除，本次跳过"
                            ),
                        }
                    )
            except Exception as exc:
                failed += 1
                failures.append(
                    {
                        "shot_id": item["id"],
                        "shot_no": item["shot_no"],
                        "error": sanitize_error_message(exc),
                    }
                )
            self.jobs.update_progress(
                job["id"],
                index,
                total,
                message=f"优化中 {index}/{total}",
            )
        return {
            "total": total,
            "succeeded": succeeded,
            "failed": failed,
            "skipped": skipped,
            "failures": failures,
        }

    def _prompt_job_matches(
        self,
        input_data: dict[str, Any],
        current: dict[str, Any],
        prompt_type: str,
        expected_revision: int | None,
    ) -> bool:
        if "expected_prompt_signature" in input_data:
            expected_signature = input_data["expected_prompt_signature"]
            return (
                isinstance(expected_signature, str)
                and bool(expected_signature)
                and self._prompt_guard_signature(current, prompt_type) == expected_signature
            )
        return expected_revision is not None and current["revision"] == expected_revision

    def _save_generated_prompt(
        self,
        shot_id: str,
        prompt_type: str,
        input_data: dict[str, Any],
        expected_revision: int | None,
        values: dict[str, Any],
        generated_negative_prompt: str,
    ) -> dict[str, Any] | None:
        for _attempt in range(2):
            latest = self.get(shot_id)
            if not self._prompt_job_matches(
                input_data,
                latest,
                prompt_type,
                expected_revision,
            ):
                return None
            changes = dict(values)
            expected_negative_prompt = input_data.get("expected_negative_prompt")
            if "expected_negative_prompt" not in input_data or (
                isinstance(expected_negative_prompt, str)
                and latest["negative_prompt"] == expected_negative_prompt
            ):
                changes["negative_prompt"] = generated_negative_prompt
            expected_status = input_data.get("expected_status")
            if (
                "status" in changes
                and "expected_status" in input_data
                and latest["status"] != expected_status
            ):
                changes.pop("status")
            try:
                return self.update(
                    shot_id,
                    changes,
                    expected_revision=latest["revision"],
                )
            except ServiceError as exc:
                if exc.status_code != 409:
                    raise
        return None

    def _shots_lightweight(self, project_id: str) -> list[dict[str, Any]]:
        """按顺序返回分镜轻量数据，只取连续性审校所需字段，避免全量编译提示词。

        字段集需与 shot_continuity.md 的审校维度保持一致：缺字段会让对应维度
        无法判断（模型看不到依据只能跳过），因此新增审校维度时必须同步这里。
        """
        with self.database.engine.connect() as connection:
            rows = connection.execute(
                select(
                    shot.c.id,
                    shot.c.shot_no,
                    shot.c.shot_order,
                    shot.c.chapter_id,
                    shot.c.source_type,
                    shot.c.source_ref,
                    shot.c.scene_asset_id,
                    shot.c.character_ids_json,
                    shot.c.prop_ids_json,
                    shot.c.positions_json,
                    shot.c.expressions_json,
                    shot.c.dialogue_json,
                    shot.c.narration,
                    shot.c.action,
                    shot.c.start_state,
                    shot.c.end_state,
                    shot.c.next_hook,
                    shot.c.transition_from_prev,
                    shot.c.shot_type,
                    shot.c.camera_angle,
                    shot.c.camera_move,
                    shot.c.duration_sec,
                    shot.c.video_prompt,
                    shot.c.video_prompt_override,
                    shot.c.revision,
                )
                .where(shot.c.project_id == project_id)
                .order_by(shot.c.shot_order, shot.c.shot_no)
            ).all()
            chapter_meta = {
                row.id: (row.chapter_no, row.title)
                for row in connection.execute(
                    select(chapter.c.id, chapter.c.chapter_no, chapter.c.title).where(
                        chapter.c.project_id == project_id
                    )
                )
            }
            asset_names = {
                row.id: row.name
                for row in connection.execute(
                    select(asset.c.id, asset.c.name).where(asset.c.project_id == project_id)
                )
            }
        result = []
        for row in rows:
            item = dict(row._mapping)
            item["character_ids"] = json_loads(item.pop("character_ids_json", "[]"), [])
            item["prop_ids"] = json_loads(item.pop("prop_ids_json", "[]"), [])
            item["positions"] = json_loads(item.pop("positions_json", "[]"), [])
            item["expressions"] = json_loads(item.pop("expressions_json", "[]"), [])
            item["dialogue"] = json_loads(item.pop("dialogue_json", "[]"), [])
            # 审校时模型需要看到可读名称而不是素材 ID
            item["prop_names"] = [asset_names[i] for i in item["prop_ids"] if i in asset_names]
            item["scene_name"] = asset_names.get(item.get("scene_asset_id") or "", "")
            item["project_id"] = project_id
            meta = chapter_meta.get(item.get("chapter_id") or "", (0, ""))
            item["chapter_no"], item["chapter_title"] = meta
            result.append(item)
        # 审校按「章节序 → 镜序」排列，使章节交界处的相邻镜也能被两两比对
        result.sort(key=lambda x: (x["chapter_no"], x["shot_order"], x["shot_no"]))
        return result

    @staticmethod
    def _continuity_shot_brief(current: dict[str, Any], *, is_previous: bool) -> str:
        """把单镜数据渲染成审校用的可读文本，字段与 shot_continuity.md 维度对应。"""
        lines = [
            f"景别/机位：{current.get('shot_type') or '未填'} / "
            f"{current.get('camera_angle') or '未填'}",
            f"场景：{current.get('scene_name') or '未绑定'}",
            f"时长：{current.get('duration_sec')} 秒",
            f"action: {current.get('action') or ''}",
        ]
        if is_previous:
            lines.append(f"end_state: {current.get('end_state') or ''}")
            lines.append(f"next_hook: {current.get('next_hook') or ''}")
        else:
            lines.append(f"start_state: {current.get('start_state') or ''}")
            # 后一镜才有衔接类型语义：它描述的是「与上一镜的关系」
            lines.append(
                f"transition_from_prev: {current.get('transition_from_prev') or 'scene_change'}"
            )
        positions = "；".join(
            f"{item.get('character', '角色')}：{item.get('position', '')}"
            f"{'，朝向' + item['facing'] if item.get('facing') else ''}"
            for item in current.get("positions", [])
            if isinstance(item, dict)
        )
        expressions = "；".join(
            f"{item.get('character', '角色')}：{item.get('expression', '')}"
            for item in current.get("expressions", [])
            if isinstance(item, dict)
        )
        dialogue = "；".join(
            f"{item.get('speaker', '角色')}「{item.get('text', '')}」"
            for item in current.get("dialogue", [])
            if isinstance(item, dict)
        )
        lines.append(f"positions: {positions or '（无）'}")
        lines.append(f"expressions: {expressions or '（无）'}")
        lines.append(f"dialogue: {dialogue or '（无画面内对白）'}")
        lines.append(f"narration: {current.get('narration') or '（无）'}")
        lines.append(f"持有道具: {'、'.join(current.get('prop_names', [])) or '（无绑定道具）'}")
        return "\n".join(lines)

    @staticmethod
    def _resolve_fix_target(
        prev_s: dict[str, Any],
        next_s: dict[str, Any],
        shot_no: Any,
        field: str,
    ) -> str | None:
        """把审校 fix 定位到具体分镜，避免跨章节同 shot_no 改错对象。

        end_state/next_hook 只属于上一镜，start_state/transition_from_prev 只属于下一镜；
        其余字段按 shot_no 在两镜中匹配，匹配不到就放弃（不落到其他章节）。
        """
        if field in ("end_state", "next_hook"):
            candidates = (prev_s,)
        elif field in ("start_state", "transition_from_prev"):
            candidates = (next_s,)
        else:
            candidates = (prev_s, next_s)
        for candidate in candidates:
            if candidate["shot_no"] == shot_no:
                return candidate["id"]
        return None

    def queue_continuity_check(self, project_id: str, options: dict[str, Any]) -> dict[str, Any]:
        """入队连续性审校任务；progress_total 按分镜对数估算。"""
        current_project = self._project(project_id)
        with self.database.engine.connect() as connection:
            shot_count = connection.execute(
                select(func.count()).select_from(shot).where(shot.c.project_id == project_id)
            ).scalar_one()
        return self.jobs.create(
            project_id=project_id,
            job_type="check_shot_continuity",
            target_type="project",
            target_id=project_id,
            provider_kind="text",
            provider_id=options.get("provider_id") or current_project["default_text_provider_id"],
            input_data={
                "ids": options.get("ids") or [],
                "apply_fixes": options.get("apply_fixes", True),
                "insert_transitions": options.get("insert_transitions", False),
                "params": options.get("params", {}),
            },
            progress_total=max(1, shot_count - 1),
        )

    async def handle_continuity_check(self, job: dict[str, Any]) -> dict[str, Any]:
        """逐对审校相邻分镜连续性，应用字段修正，可选逆序插入过渡镜。"""
        project_id = str(job["target_id"])
        current_project = self._project(project_id)
        input_data = json_loads(job.get("input_json"), {})
        requested_ids = {str(item) for item in (input_data.get("ids") or [])}
        apply_fixes = bool(input_data.get("apply_fixes", True))
        insert_transitions = bool(input_data.get("insert_transitions", False))
        params = input_data.get("params", {})
        all_shots = self._shots_lightweight(project_id)
        if len(all_shots) < 2:
            return {
                "total_pairs": 0,
                "issues_found": 0,
                "fixes_applied": 0,
                "transitions_inserted": 0,
                "results": [],
            }
        if requested_ids:
            pairs = [
                (all_shots[i], all_shots[i + 1])
                for i in range(len(all_shots) - 1)
                if all_shots[i]["id"] in requested_ids or all_shots[i + 1]["id"] in requested_ids
            ]
        else:
            pairs = list(zip(all_shots[:-1], all_shots[1:], strict=True))
        if not pairs:
            return {
                "total_pairs": 0,
                "issues_found": 0,
                "fixes_applied": 0,
                "transitions_inserted": 0,
                "results": [],
            }
        provider, config = self.providers.resolve(
            kind="text",
            provider_id=job.get("provider_id") or current_project["default_text_provider_id"],
        )
        # ── 第一轮：收集所有审校结果 ──────────────────────────────────────
        raw_results: list[dict[str, Any]] = []
        for index, (prev_s, next_s) in enumerate(pairs, start=1):
            try:
                same_scene_label = (
                    "是"
                    if prev_s.get("scene_asset_id") == next_s.get("scene_asset_id")
                    else "否（发生场景切换）"
                )
                output = await provider.generate_json(
                    system_prompt=load_prompt("shot_continuity"),
                    user_prompt=(
                        f"上一镜（第{prev_s.get('chapter_no')}章 镜{prev_s['shot_no']}）：\n"
                        f"{self._continuity_shot_brief(prev_s, is_previous=True)}\n\n"
                        f"下一镜（第{next_s.get('chapter_no')}章 镜{next_s['shot_no']}）：\n"
                        f"{self._continuity_shot_brief(next_s, is_previous=False)}\n\n"
                        f"两镜是否同场景：{same_scene_label}"
                        + (
                            f"\n\n【跨章节衔接】上一镜属于第{prev_s.get('chapter_no')}章"
                            f"「{prev_s.get('chapter_title') or ''}」，"
                            f"下一镜属于第{next_s.get('chapter_no')}章"
                            f"「{next_s.get('chapter_title') or ''}」。"
                            "章节之间的时间与地点跳跃属于正常叙事切换，不要报为断层；"
                            "只检查人物状态是否矛盾、关键道具是否无故消失。"
                            if prev_s.get("chapter_id") != next_s.get("chapter_id")
                            else ""
                        )
                    ),
                    schema=ShotContinuityPairOutput,
                    params={**config["params"], **params},
                )
                raw_results.append(output.model_dump(mode="json"))
            except Exception as exc:
                raw_results.append(
                    {
                        "prev_shot_no": prev_s["shot_no"],
                        "next_shot_no": next_s["shot_no"],
                        "is_continuous": True,
                        "issues": [],
                        "fixes": [],
                        "transition": None,
                        "error": sanitize_error_message(exc),
                    }
                )
            self.jobs.update_progress(
                job["id"], index, len(pairs), message=f"审校 {index}/{len(pairs)} 对"
            )
        # ── 第二轮：应用字段修正 ──────────────────────────────────────────
        total_fixes = 0
        if apply_fixes:
            for (prev_s, next_s), pair_result in zip(
                pairs,
                raw_results,
                strict=True,
            ):
                for fix in pair_result.get("fixes", []):
                    field = fix.get("field")
                    allowed_fields = (
                        "end_state",
                        "start_state",
                        "next_hook",
                        "action",
                        "narration",
                        "transition_from_prev",
                    )
                    if field not in allowed_fields:
                        continue
                    target_id = self._resolve_fix_target(
                        prev_s,
                        next_s,
                        fix.get("shot_no"),
                        field,
                    )
                    if target_id:
                        try:
                            self.update(target_id, {field: fix["suggested_value"]})
                            total_fixes += 1
                        except ServiceError:
                            pass  # 并发更新冲突时跳过
        # ── 第三轮：逆序插入过渡镜（逆序防止后续镜顺移导致插入位置错误）──
        total_transitions = 0
        if insert_transitions:
            pending = [
                (prev_s, next_s, raw_results[i]["transition"])
                for i, (prev_s, next_s) in enumerate(pairs)
                if raw_results[i].get("transition")
            ]
            for prev_s, next_s, t in reversed(pending):
                try:
                    self._copy_with_overrides(
                        prev_s,
                        prev_s["id"],
                        {
                            "action": t["action"],
                            "start_state": t["start_state"],
                            "end_state": t["end_state"],
                            "duration_sec": t["duration_sec"],
                            "image_prompt": "",
                            "video_prompt": "",
                            "negative_prompt": "",
                            "image_prompt_override": "",
                            "video_prompt_override": "",
                            "next_hook": next_s.get("start_state", ""),
                            "transition_from_prev": "continuous",
                            "frame_description": (
                                f"镜{prev_s['shot_no']}至镜{next_s['shot_no']}的过渡动作"
                            ),
                        },
                    )
                    total_transitions += 1
                except ServiceError:
                    pass
        return {
            "total_pairs": len(pairs),
            "issues_found": sum(len(r.get("issues", [])) for r in raw_results),
            "fixes_applied": total_fixes,
            "transitions_inserted": total_transitions,
            "results": raw_results,
        }

    _REFINE_SEPARATOR = "\n\n===分镜分隔===\n\n"

    def queue_refine_prompts(self, project_id: str, options: dict[str, Any]) -> dict[str, Any]:
        """入队全量提示词连续性精修任务，可同时处理图片与视频提示词。"""
        current_project = self._project(project_id)
        prompt_types = options.get("prompt_types") or ["video"]
        with self.database.engine.connect() as connection:
            shot_count = connection.execute(
                select(func.count()).select_from(shot).where(shot.c.project_id == project_id)
            ).scalar_one()
        if shot_count < 2:
            raise bad_request("至少需要两个分镜才能做连续性精修")
        chunk_size = options.get("chunk_size") or 6
        chunks_per_type = max(1, -(-shot_count // chunk_size))
        return self.jobs.create(
            project_id=project_id,
            job_type="refine_prompts_continuity",
            target_type="project",
            target_id=project_id,
            provider_kind="text",
            provider_id=options.get("provider_id") or current_project["default_text_provider_id"],
            input_data={
                "chunk_size": chunk_size,
                "prompt_types": prompt_types,
                "params": options.get("params", {}),
            },
            progress_total=chunks_per_type * len(prompt_types),
        )

    async def _refine_one_type(
        self,
        job: dict[str, Any],
        current_project: dict[str, Any],
        prompt_type: str,
        *,
        chunk_size: int,
        params: dict[str, Any],
        progress_offset: int,
        progress_total: int,
    ) -> dict[str, Any]:
        """精修单一类型（image / video）的全量提示词，返回该类型的统计。

        分块而非一次性全量：18 镜提示词约 15k tokens 输入 + 15k 输出，
        单请求容易截断导致整批失败。分块后每块独立成败，块间用重叠镜
        （前一块最后一镜）作只读上下文传递衔接信息。
        """
        source_field = f"final_{prompt_type}_prompt"
        target_field = f"{prompt_type}_prompt_override"
        type_label = "关键帧图片提示词" if prompt_type == "image" else "视频提示词"
        shots = self.list_project(current_project["id"])
        targets = [item for item in shots if str(item.get(source_field) or "").strip()]
        if len(targets) < 2:
            return {
                "prompt_type": prompt_type,
                "total": len(targets),
                "applied": 0,
                "skipped": len(targets),
                "changes": [],
                "failures": [{"error": f"至少需要两个已有{type_label}的分镜，本类型跳过"}],
            }
        provider, config = self.providers.resolve(
            kind="text",
            provider_id=job.get("provider_id") or current_project["default_text_provider_id"],
        )
        chunks: list[tuple[dict[str, Any] | None, list[dict[str, Any]]]] = []
        for start in range(0, len(targets), chunk_size):
            block = targets[start : start + chunk_size]
            context_shot = targets[start - 1] if start > 0 else None
            chunks.append((context_shot, block))
        applied = 0
        skipped = 0
        all_changes: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for index, (context_shot, block) in enumerate(chunks, start=1):
            numbered = self._REFINE_SEPARATOR.join(
                f"镜{item['shot_no']}：\n{item[source_field]}" for item in block
            )
            context_note = ""
            if context_shot:
                context_note = (
                    f"【只读上下文】上一块最后一镜（镜{context_shot['shot_no']}）的提示词，"
                    "仅用于理解衔接，不要返回它：\n"
                    f"{context_shot[source_field]}\n\n"
                )
            order_note = "、".join(f"镜{item['shot_no']}" for item in block)
            try:
                output = await provider.generate_json(
                    system_prompt=load_prompt("batch_refine_prompts"),
                    user_prompt=(
                        f"本次处理的提示词类型：{type_label}\n"
                        f"项目画风：{current_project['style_prompt']}\n"
                        f"本次需要返回 {len(block)} 个分镜的提示词，"
                        f"顺序为：{order_note}\n\n"
                        f"{context_note}"
                        f"【待精修分镜】\n{numbered}"
                    ),
                    schema=BatchRefinedPromptsOutput,
                    params={**config["params"], **params},
                )
                result = output.model_dump(mode="json")
                refined = result["prompts"]
                if len(refined) != len(block):
                    # 条数不符说明输出被截断或模型误解，整块跳过不写半截数据
                    failures.append(
                        {
                            "prompt_type": prompt_type,
                            "chunk": index,
                            "error": (
                                f"模型返回 {len(refined)} 条提示词，"
                                f"与请求的 {len(block)} 条不一致，本块跳过"
                            ),
                        }
                    )
                    skipped += len(block)
                else:
                    for item, text in zip(block, refined, strict=True):
                        try:
                            self.update(
                                item["id"],
                                {target_field: text},
                                expected_revision=item["revision"],
                            )
                            applied += 1
                        except ServiceError as exc:
                            if exc.status_code not in {409, 404}:
                                raise
                            skipped += 1
                            failures.append(
                                {
                                    "prompt_type": prompt_type,
                                    "shot_id": item["id"],
                                    "shot_no": item["shot_no"],
                                    "error": (
                                        "分镜已被其他请求更新，本次跳过"
                                        if exc.status_code == 409
                                        else "分镜已被删除，本次跳过"
                                    ),
                                }
                            )
                    for change in result.get("changes", []):
                        all_changes.append({**change, "prompt_type": prompt_type})
            except Exception as exc:
                skipped += len(block)
                failures.append(
                    {
                        "prompt_type": prompt_type,
                        "chunk": index,
                        "error": sanitize_error_message(exc),
                    }
                )
            self.jobs.update_progress(
                job["id"],
                progress_offset + index,
                progress_total,
                message=f"{type_label} 精修中 {index}/{len(chunks)} 块",
            )
        return {
            "prompt_type": prompt_type,
            "total": len(targets),
            "applied": applied,
            "skipped": skipped,
            "changes": all_changes,
            "failures": failures,
        }

    async def handle_refine_prompts(self, job: dict[str, Any]) -> dict[str, Any]:
        """全量提示词连续性精修：按类型依次分块处理，回填对应 override 字段。"""
        project_id = str(job["target_id"])
        current_project = self._project(project_id)
        input_data = json_loads(job.get("input_json"), {})
        chunk_size = max(2, int(input_data.get("chunk_size") or 6))
        prompt_types = [
            item
            for item in (input_data.get("prompt_types") or ["video"])
            if item in ("image", "video")
        ] or ["video"]
        params = input_data.get("params", {})
        with self.database.engine.connect() as connection:
            shot_count = connection.execute(
                select(func.count()).select_from(shot).where(shot.c.project_id == project_id)
            ).scalar_one()
        chunks_per_type = max(1, -(-shot_count // chunk_size))
        progress_total = chunks_per_type * len(prompt_types)
        per_type: list[dict[str, Any]] = []
        for order, prompt_type in enumerate(prompt_types):
            per_type.append(
                await self._refine_one_type(
                    job,
                    current_project,
                    prompt_type,
                    chunk_size=chunk_size,
                    params=params,
                    progress_offset=order * chunks_per_type,
                    progress_total=progress_total,
                )
            )
        return {
            "prompt_types": prompt_types,
            "applied": sum(item["applied"] for item in per_type),
            "skipped": sum(item["skipped"] for item in per_type),
            "changes": [change for item in per_type for change in item["changes"]],
            "failures": [failure for item in per_type for failure in item["failures"]],
            "per_type": per_type,
        }

    @staticmethod
    def _shot_quality_warnings(current: dict[str, Any]) -> list[str]:
        """对已入库的分镜复算质量警告，复用 ShotOutput 的判定逻辑。

        入库记录字段与 ShotOutput 不完全一致（如 dialogue 为 dict 列表），
        因此这里构造一个仅含判定所需字段的临时对象。
        """
        posture_verbs = ShotOutput.POSTURE_VERBS
        unfinished = ShotOutput.UNFINISHED_MARKERS
        warnings: list[str] = []
        end_state = str(current.get("end_state") or "")
        video_prompt = str(current.get("video_prompt_override") or "") or str(
            current.get("video_prompt") or ""
        )
        hit = [verb for verb in posture_verbs if verb in end_state]
        if (
            hit
            and not any(verb in video_prompt for verb in posture_verbs)
            and not any(marker in end_state for marker in unfinished)
        ):
            warnings.append(
                f"end_state 含姿态动词「{hit[0]}」，但 video_prompt 时间轴没有对应调度。"
                f"建议在时间轴补一段该动作（并相应增加 duration_sec），"
                f"或把 end_state 改为「仍…，准备{hit[0]}」的未完成表述。"
            )
        dialogue = [item for item in current.get("dialogue", []) if isinstance(item, dict)]
        duration = float(current.get("duration_sec") or 0)
        if dialogue and 0 < duration < 12:
            total = sum(len(str(item.get("text") or "")) for item in dialogue)
            dialogue_focused = ShotOutput._is_dialogue_focused(current.get("action"))
            usable_rate = 2.5 if dialogue_focused else 2 * 0.8
            threshold = int(duration * usable_rate)
            if total > threshold:
                recommended_sec = math.ceil(total / usable_rate)
                rate_label = (
                    "2.5字/秒（纯对白）"
                    if dialogue_focused
                    else "2字/秒+20%动作余量"
                )
                warnings.append(
                    f"{duration:g}秒内台词共{total}字，超出建议阈值{threshold}字"
                    f"（按{rate_label}计算）。"
                    f"建议延长时长至{recommended_sec}秒以上，或压缩台词。"
                )
        timeline_warning = ShotOutput.timeline_conflict_warning(video_prompt)
        if timeline_warning:
            warnings.append(timeline_warning)
        return warnings

    def quality_report(self, project_id: str) -> dict[str, Any]:
        """扫描全部分镜，返回存在质量问题的分镜清单，供前端展示与触发修复。"""
        self._project(project_id)
        items: list[dict[str, Any]] = []
        for current in self._shots_lightweight(project_id):
            warnings = self._shot_quality_warnings(current)
            if warnings:
                items.append(
                    {
                        "shot_id": current["id"],
                        "shot_no": current["shot_no"],
                        "chapter_no": current.get("chapter_no", 0),
                        "chapter_title": current.get("chapter_title", ""),
                        "duration_sec": current["duration_sec"],
                        "warnings": warnings,
                    }
                )
        return {"total": len(items), "items": items}

    def queue_quality_fix(self, project_id: str, options: dict[str, Any]) -> dict[str, Any]:
        """入队质量修复任务：只处理存在警告的分镜。"""
        current_project = self._project(project_id)
        report = self.quality_report(project_id)
        requested = {str(item) for item in (options.get("ids") or [])}
        targets = [
            item for item in report["items"] if not requested or item["shot_id"] in requested
        ]
        if not targets:
            raise bad_request("当前没有需要修复的分镜")
        return self.jobs.create(
            project_id=project_id,
            job_type="fix_shot_quality",
            target_type="project",
            target_id=project_id,
            provider_kind="text",
            provider_id=options.get("provider_id") or current_project["default_text_provider_id"],
            input_data={
                "ids": [item["shot_id"] for item in targets],
                "params": options.get("params", {}),
            },
            progress_total=len(targets),
        )

    async def handle_quality_fix(self, job: dict[str, Any]) -> dict[str, Any]:
        """逐个修复存在质量警告的分镜，只写回模型声明修改的字段。"""
        project_id = str(job["target_id"])
        current_project = self._project(project_id)
        input_data = json_loads(job.get("input_json"), {})
        requested = [str(item) for item in (input_data.get("ids") or [])]
        params = input_data.get("params", {})
        provider, config = self.providers.resolve(
            kind="text",
            provider_id=job.get("provider_id") or current_project["default_text_provider_id"],
        )
        fixed = 0
        skipped = 0
        failures: list[dict[str, Any]] = []
        changes: list[dict[str, Any]] = []
        total = len(requested)
        for index, shot_id in enumerate(requested, start=1):
            try:
                current = self.get(shot_id)
            except ServiceError:
                skipped += 1
                failures.append({"shot_id": shot_id, "error": "分镜已被删除，跳过"})
                self.jobs.update_progress(job["id"], index, total)
                continue
            warnings = self._shot_quality_warnings(current)
            if not warnings:
                # 期间已被手动改好
                skipped += 1
                self.jobs.update_progress(job["id"], index, total)
                continue
            try:
                effective_video_prompt = str(
                    current.get("video_prompt_override") or current.get("video_prompt") or ""
                )
                output = await provider.generate_json(
                    system_prompt=load_prompt("shot_quality_fix"),
                    user_prompt=(
                        f"分镜编号：镜{current['shot_no']}\n"
                        f"duration_sec：{current['duration_sec']}\n"
                        f"action：{current.get('action') or ''}\n"
                        f"start_state：{current.get('start_state') or ''}\n"
                        f"end_state：{current.get('end_state') or ''}\n"
                        f"video_prompt：{effective_video_prompt}\n"
                        f"dialogue：{json_dumps(current.get('dialogue', []))}\n\n"
                        "需要修复的问题：\n" + "\n".join(f"- {item}" for item in warnings)
                    ),
                    schema=ShotQualityFixOutput,
                    params={**config["params"], **params},
                )
                result = output.model_dump(mode="json", exclude_none=True)
                updates = {
                    name: result[name]
                    for name in result.get("changed_fields", [])
                    if name in result
                }
                if not updates:
                    skipped += 1
                    failures.append({"shot_id": shot_id, "error": "模型未返回任何修改"})
                else:
                    self.update(shot_id, updates, expected_revision=current["revision"])
                    fixed += 1
                    changes.append(
                        {
                            "shot_id": shot_id,
                            "shot_no": current["shot_no"],
                            "changed_fields": result.get("changed_fields", []),
                            "reason": result.get("reason", ""),
                        }
                    )
            except ServiceError as exc:
                if exc.status_code not in {404, 409}:
                    raise
                skipped += 1
                failures.append(
                    {
                        "shot_id": shot_id,
                        "shot_no": current["shot_no"],
                        "error": (
                            "分镜已被其他请求更新，本次跳过"
                            if exc.status_code == 409
                            else "分镜已被删除，跳过"
                        ),
                    }
                )
            except Exception as exc:
                failures.append(
                    {
                        "shot_id": shot_id,
                        "shot_no": current["shot_no"],
                        "error": sanitize_error_message(exc),
                    }
                )
            self.jobs.update_progress(job["id"], index, total, message=f"修复中 {index}/{total}")
        return {
            "total": total,
            "fixed": fixed,
            "skipped": skipped,
            "changes": changes,
            "failures": failures,
        }

    @staticmethod
    def _prompt_expected_revision(
        input_data: dict[str, Any],
        current: dict[str, Any],
    ) -> int | None:
        value = input_data.get("expected_revision")
        if type(value) is int and value >= 0:
            return value
        if "expected_revision" in input_data:
            return None
        return current["revision"]

    @staticmethod
    def _skipped_prompt_result(
        shot_id: str,
        generated: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "shot_id": shot_id,
            "skipped": True,
            "reason": "分镜已被其他请求更新，已保留最新内容",
            "prompt": generated or {},
        }
