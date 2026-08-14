from __future__ import annotations

import re
from typing import Any

from sqlalchemy import and_, delete, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.db import Database, db
from app.models.tables import asset, asset_image, chapter, job, novel_analysis, project, shot
from app.prompts import load_prompt
from app.schemas import AssetExtractionOutput, AssetPromptOutput
from app.services.errors import bad_request, conflict, not_found
from app.services.file_service import file_service, project_media_url
from app.services.job_service import JobService
from app.services.prompt_service import compile_asset_prompt
from app.services.provider_service import ProviderService
from app.services.signature_service import (
    SHOT_SIGNATURE_FIELDS,
    asset_signature,
    shot_signature,
)
from app.services.utils import (
    json_dumps,
    json_loads,
    new_id,
    row_dict,
    utc_now,
)

_EXTRACTION_KEYS = {
    "character": "characters",
    "scene": "scenes",
    "prop": "props",
}

_ACTIVE_JOB_STATUSES = {"queued", "running", "waiting_provider", "downloading"}
_NAME_VARIANT_TOKENS = (
    "已熄灭",
    "熄灭",
    "已灭",
    "蒙尘",
    "残破",
    "破损",
    "陈旧",
    "黑色",
    "暗色",
    "金色",
    "青色",
    "银白",
    "白色",
    "红色",
    "夜色",
    "夜景",
    "雪夜",
    "风雪",
    "清晨",
    "黄昏",
    "深夜",
    "室内",
    "室外",
    "外部",
    "内部",
)
_SHOT_JSON_FIELDS = {
    "character_ids": "character_ids_json",
    "prop_ids": "prop_ids_json",
    "positions": "positions_json",
    "expressions": "expressions_json",
    "dialogue": "dialogue_json",
    "reference_bindings": "reference_bindings_json",
}


class AssetService:
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
    def _decode(record: dict[str, Any]) -> dict[str, Any]:
        result = dict(record)
        profile = json_loads(result["profile_json"], {})
        consistency = json_loads(result["consistency_json"], [])
        result["profile_json"] = profile
        result["profile"] = profile
        result["consistency_json"] = consistency
        result["consistency_keywords"] = consistency
        return result

    @staticmethod
    def _normalize_label(value: str) -> str:
        return re.sub(r"[\W_]+", "", value.casefold())

    @classmethod
    def _variant_name_core(cls, value: str) -> str:
        """去掉常见状态/颜色修饰词，得到用于匹配变体素材的名称核心。"""
        text = str(value or "").strip()
        text = text.split("的")[-1]
        for token in sorted(_NAME_VARIANT_TOKENS, key=len, reverse=True):
            text = text.replace(token, "")
        return cls._normalize_label(text)

    @classmethod
    def _asset_name_matches(cls, left: str, right: str) -> bool:
        """判断两个素材名是否指向同一对象，含别名、归属前缀与状态变体。"""
        left_text = str(left or "").strip()
        right_text = str(right or "").strip()
        if not left_text or not right_text:
            return False
        if left_text == right_text:
            return True
        if cls._normalize_label(left_text) == cls._normalize_label(right_text):
            return True
        # 「顾长渊的青玉命灯」与「青玉命灯」这类归属前缀
        if left_text.split("的")[-1] == right_text or right_text.split("的")[-1] == left_text:
            return True
        # 较长名称以较短名称为后缀，如「万灯阁第七层」与「第七层」
        shorter, longer = sorted((left_text, right_text), key=len)
        if len(shorter) >= 2 and longer.endswith(shorter):
            return True
        # 「万灯阁命灯」与「万灯阁黑色命灯」这类状态变体
        return cls._variant_name_core(left_text) == cls._variant_name_core(right_text)

    @classmethod
    def _find_matching_asset(
        cls,
        connection: Any,
        *,
        project_id: str,
        asset_type: str,
        name: str,
        aliases: list[str],
    ) -> tuple[Any, str]:
        """按名称、别名与状态变体查找同项目同类型已有素材，返回匹配行和提示。"""
        rows = connection.execute(
            select(asset).where(
                asset.c.project_id == project_id,
                asset.c.asset_type == asset_type,
            )
        ).all()
        exact = next((row for row in rows if row.name == name), None)
        if exact is not None:
            return exact, ""
        raw_aliases = aliases if isinstance(aliases, list) else []
        provided_aliases = [str(item).strip() for item in raw_aliases if str(item).strip()]
        scored: list[tuple[int, Any]] = []
        for row in rows:
            profile = json_loads(row.profile_json, {})
            existing_aliases = [
                str(item).strip()
                for item in profile.get("aliases", [])
                if str(item).strip()
            ]
            if name in existing_aliases or row.name in provided_aliases:
                scored.append((3, row))
                continue
            if cls._asset_name_matches(name, row.name):
                scored.append((1, row))
        if not scored:
            return None, ""
        best_score = max(score for score, _ in scored)
        candidates = [row for score, row in scored if score == best_score]
        matched = max(candidates, key=lambda row: len(row.name))
        if len(candidates) > 1:
            warning = f"素材「{name}」匹配到多个已有素材，已并入「{matched.name}」"
        else:
            warning = f"素材「{name}」与已有「{matched.name}」按别名或状态变体合并"
        return matched, warning

    @staticmethod
    def _compiled_prompt(
        current_project: dict[str, Any],
        current: dict[str, Any],
    ) -> str:
        return compile_asset_prompt(
            style_prompt=current_project["style_prompt"],
            aspect_ratio=current_project["aspect_ratio"],
            asset_type=current["asset_type"],
            asset_name=current["name"],
            profile=current["profile"],
            prompt=current["prompt"],
            negative_prompt=current["negative_prompt"],
            consistency_keywords=current["consistency_keywords"],
            composition=current["composition"],
        )

    def _current_image(
        self, connection: Any, asset_id: str, project_id: str
    ) -> dict[str, Any] | None:
        record = row_dict(
            connection.execute(
                select(asset_image).where(
                    asset_image.c.asset_id == asset_id,
                    asset_image.c.is_current.is_(True),
                )
            ).first()
        )
        if record and record.get("file_path"):
            record["file_url"] = project_media_url(project_id, record["file_path"])
            record["media_url"] = record["file_url"]
        if record:
            record["is_current"] = bool(record["is_current"])
        return record

    def list(self, project_id: str, asset_type: str | None = None) -> list[dict[str, Any]]:
        current_project = self._project(project_id)
        statement = select(asset).where(asset.c.project_id == project_id)
        if asset_type:
            statement = statement.where(asset.c.asset_type == asset_type)
        statement = statement.order_by(asset.c.asset_type, asset.c.name)
        with self.database.engine.connect() as connection:
            records = []
            for row in connection.execute(statement):
                record = self._decode(dict(row._mapping))
                # 素材库属于整本小说，跨章节共用，不再用已废弃的 project.source_signature 判旧
                record["is_stale"] = False
                record["compiled_prompt"] = self._compiled_prompt(current_project, record)
                record["current_image"] = self._current_image(connection, record["id"], project_id)
                records.append(record)
        return records

    def get(self, asset_id: str) -> dict[str, Any]:
        with self.database.engine.connect() as connection:
            record = row_dict(
                connection.execute(select(asset).where(asset.c.id == asset_id)).first()
            )
        if record is None:
            raise not_found("素材")
        result = self._decode(record)
        current_project = self._project(result["project_id"])
        # 素材库属于整本小说，跨章节共用，不再用已废弃的 project.source_signature 判旧
        result["is_stale"] = False
        result["compiled_prompt"] = self._compiled_prompt(current_project, result)
        with self.database.engine.connect() as connection:
            result["current_image"] = self._current_image(
                connection, asset_id, result["project_id"]
            )
        return result

    def _signature(
        self,
        project_record: dict[str, Any],
        *,
        profile: dict[str, Any],
        prompt: str,
        negative_prompt: str,
        prompt_override: str = "",
    ) -> str:
        return asset_signature(
            profile,
            prompt,
            negative_prompt,
            project_record["style_prompt"],
            prompt_override,
        )

    def create(self, project_id: str, values: dict[str, Any]) -> dict[str, Any]:
        current_project = self._project(project_id)
        now = utc_now()
        record = {
            "id": new_id("asset"),
            "project_id": project_id,
            "asset_type": values["asset_type"],
            "name": values["name"].strip(),
            "profile_json": json_dumps(values.get("profile", {})),
            "prompt": values.get("prompt", ""),
            "negative_prompt": values.get("negative_prompt", ""),
            "consistency_json": json_dumps(values.get("consistency_keywords", [])),
            "composition": values.get("composition", ""),
            "importance": values.get("importance", "normal"),
            "status": values.get("status", "draft"),
            "source_signature": current_project["source_signature"],
            "own_signature": self._signature(
                current_project,
                profile=values.get("profile", {}),
                prompt=values.get("prompt", ""),
                negative_prompt=values.get("negative_prompt", ""),
            ),
            "revision": 1,
            "created_at": now,
            "updated_at": now,
        }
        try:
            with self.database.engine.begin() as connection:
                connection.execute(insert(asset).values(**record))
        except IntegrityError as exc:
            raise conflict("同类型同名素材已存在") from exc
        return self.get(record["id"])

    def update(self, asset_id: str, values: dict[str, Any]) -> dict[str, Any]:
        current = self.get(asset_id)
        current_project = self._project(current["project_id"])
        changes: dict[str, Any] = {}
        field_map = {
            "profile": "profile_json",
            "consistency_keywords": "consistency_json",
        }
        for key, value in values.items():
            target = field_map.get(key, key)
            changes[target] = (
                json_dumps(value) if target in {"profile_json", "consistency_json"} else value
            )
        semantic_fields = {
            "name",
            "profile_json",
            "prompt",
            "prompt_override",
            "negative_prompt",
            "consistency_json",
            "composition",
            "importance",
        }
        if semantic_fields.intersection(changes):
            profile = json_loads(changes.get("profile_json", json_dumps(current["profile"])), {})
            changes["own_signature"] = self._signature(
                current_project,
                profile=profile,
                prompt=changes.get("prompt", current["prompt"]),
                negative_prompt=changes.get("negative_prompt", current["negative_prompt"]),
                prompt_override=changes.get(
                    "prompt_override", current.get("prompt_override") or ""
                ),
            )
            changes["revision"] = current["revision"] + 1
            if "status" not in changes:
                changes["status"] = "ready"
        if not changes:
            return current
        changes["updated_at"] = utc_now()
        try:
            with self.database.engine.begin() as connection:
                connection.execute(update(asset).where(asset.c.id == asset_id).values(**changes))
        except IntegrityError as exc:
            raise conflict("同类型同名素材已存在") from exc
        return self.get(asset_id)

    @staticmethod
    def _ensure_no_active_jobs(
        connection: Any,
        asset_ids: set[str],
        image_ids: set[str],
    ) -> None:
        active_job = connection.execute(
            select(job.c.id).where(
                job.c.status.in_(_ACTIVE_JOB_STATUSES),
                or_(
                    and_(
                        job.c.target_type == "asset",
                        job.c.target_id.in_(asset_ids),
                    ),
                    and_(
                        job.c.target_type == "asset_image",
                        job.c.target_id.in_(image_ids),
                    ),
                ),
            )
        ).first()
        if active_job:
            raise conflict("素材仍有进行中的任务，不能删除或合并")

    @staticmethod
    def _update_shot_asset_references(
        connection: Any,
        *,
        project_id: str,
        source_ids: set[str],
        image_ids: set[str],
        target_id: str | None,
    ) -> None:
        rows = connection.execute(select(shot).where(shot.c.project_id == project_id)).all()
        for row in rows:
            decoded = dict(row._mapping)
            for alias, column in _SHOT_JSON_FIELDS.items():
                decoded[alias] = json_loads(decoded[column], [])
            characters = decoded["character_ids"]
            props = decoded["prop_ids"]
            bindings = decoded["reference_bindings"]
            characters = characters if isinstance(characters, list) else []
            props = props if isinstance(props, list) else []
            bindings = bindings if isinstance(bindings, list) else []
            changes: dict[str, Any] = {}
            if target_id is None:
                remapped_characters = [item for item in characters if item not in source_ids]
                remapped_props = [item for item in props if item not in source_ids]
                remapped_bindings = [
                    binding
                    for binding in bindings
                    if not (
                        isinstance(binding, dict)
                        and (
                            binding.get("asset_id") in source_ids
                            or binding.get("asset_image_id") in image_ids
                        )
                    )
                ]
                scene_asset_id = None if row.scene_asset_id in source_ids else row.scene_asset_id
            else:
                remapped_characters = list(
                    dict.fromkeys(target_id if item in source_ids else item for item in characters)
                )
                remapped_props = list(
                    dict.fromkeys(target_id if item in source_ids else item for item in props)
                )
                remapped_bindings = []
                for binding in bindings:
                    if not isinstance(binding, dict):
                        remapped_bindings.append(binding)
                        continue
                    remapped = dict(binding)
                    if (
                        remapped.get("asset_id") in source_ids
                        or remapped.get("asset_image_id") in image_ids
                    ):
                        remapped["asset_id"] = target_id
                    remapped_bindings.append(remapped)
                scene_asset_id = (
                    target_id if row.scene_asset_id in source_ids else row.scene_asset_id
                )
            if scene_asset_id != row.scene_asset_id:
                changes["scene_asset_id"] = scene_asset_id
            if remapped_characters != characters:
                changes["character_ids_json"] = json_dumps(remapped_characters)
            if remapped_props != props:
                changes["prop_ids_json"] = json_dumps(remapped_props)
            if remapped_bindings != bindings:
                changes["reference_bindings_json"] = json_dumps(remapped_bindings)
            if changes:
                decoded["scene_asset_id"] = scene_asset_id
                decoded["character_ids"] = remapped_characters
                decoded["prop_ids"] = remapped_props
                decoded["reference_bindings"] = remapped_bindings
                semantic = {
                    name: decoded[name]
                    for name in SHOT_SIGNATURE_FIELDS
                    if not name.endswith("_json") and name in decoded
                }
                changes["own_signature"] = shot_signature(semantic)
                changes["revision"] = decoded["revision"] + 1
                changes["updated_at"] = utc_now()
                connection.execute(update(shot).where(shot.c.id == row.id).values(**changes))

    def delete(self, asset_id: str) -> None:
        current = self.get(asset_id)
        with self.database.engine.begin() as connection:
            image_rows = connection.execute(
                select(asset_image.c.id, asset_image.c.file_path).where(
                    asset_image.c.asset_id == asset_id
                )
            ).all()
            image_ids = {row.id for row in image_rows}
            self._ensure_no_active_jobs(connection, {asset_id}, image_ids)
            self._update_shot_asset_references(
                connection,
                project_id=current["project_id"],
                source_ids={asset_id},
                image_ids=image_ids,
                target_id=None,
            )
            paths = [row.file_path for row in image_rows if row.file_path]
            connection.execute(delete(asset).where(asset.c.id == asset_id))
        for path in paths:
            file_service.delete_file(current["project_id"], path)

    @staticmethod
    def _merge_profile(target: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
        result = dict(target)
        for key, value in source.items():
            if not result.get(key):
                result[key] = value
            elif isinstance(result[key], list) and isinstance(value, list):
                result[key] = list(dict.fromkeys([*result[key], *value]))
        return result

    def merge(self, target_id: str, source_ids: list[str]) -> dict[str, Any]:
        source_ids = [item for item in source_ids if item != target_id]
        if not source_ids:
            return self.get(target_id)
        target = self.get(target_id)
        sources = [self.get(item) for item in source_ids]
        if any(
            item["project_id"] != target["project_id"] or item["asset_type"] != target["asset_type"]
            for item in sources
        ):
            raise bad_request("只能合并同一项目、同一类型的素材")
        current_project = self._project(target["project_id"])
        merged_profile = dict(target["profile"])
        consistency = list(target["consistency_keywords"])
        prompt_parts = [target["prompt"]]
        negative_parts = [target["negative_prompt"]]
        importance = target["importance"]
        for source in sources:
            merged_profile = self._merge_profile(merged_profile, source["profile"])
            consistency.extend(source["consistency_keywords"])
            prompt_parts.append(source["prompt"])
            negative_parts.append(source["negative_prompt"])
            if source["importance"] == "core":
                importance = "core"
        consistency = list(dict.fromkeys(item for item in consistency if item))
        prompt = "\n".join(dict.fromkeys(item for item in prompt_parts if item))
        negative_prompt = "\n".join(dict.fromkeys(item for item in negative_parts if item))
        now = utc_now()
        with self.database.engine.begin() as connection:
            all_asset_ids = {target_id, *source_ids}
            all_image_rows = connection.execute(
                select(asset_image.c.id).where(asset_image.c.asset_id.in_(all_asset_ids))
            ).all()
            all_image_ids = {row.id for row in all_image_rows}
            source_image_ids = {
                row.id
                for row in connection.execute(
                    select(asset_image.c.id).where(asset_image.c.asset_id.in_(source_ids))
                )
            }
            self._ensure_no_active_jobs(connection, all_asset_ids, all_image_ids)
            next_version = (
                connection.execute(
                    select(func.max(asset_image.c.version_no)).where(
                        asset_image.c.asset_id == target_id
                    )
                ).scalar()
                or 0
            )
            source_images = connection.execute(
                select(asset_image)
                .where(asset_image.c.asset_id.in_(source_ids))
                .order_by(asset_image.c.created_at)
            ).all()
            for image in source_images:
                next_version += 1
                connection.execute(
                    update(asset_image)
                    .where(asset_image.c.id == image._mapping["id"])
                    .values(
                        asset_id=target_id,
                        version_no=next_version,
                        is_current=False,
                    )
                )
            self._update_shot_asset_references(
                connection,
                project_id=target["project_id"],
                source_ids=set(source_ids),
                image_ids=source_image_ids,
                target_id=target_id,
            )
            connection.execute(
                update(asset)
                .where(asset.c.id == target_id)
                .values(
                    profile_json=json_dumps(merged_profile),
                    prompt=prompt,
                    negative_prompt=negative_prompt,
                    consistency_json=json_dumps(consistency),
                    importance=importance,
                    status="ready",
                    own_signature=self._signature(
                        current_project,
                        profile=merged_profile,
                        prompt=prompt,
                        negative_prompt=negative_prompt,
                    ),
                    revision=target["revision"] + 1,
                    updated_at=now,
                )
            )
            connection.execute(delete(asset).where(asset.c.id.in_(source_ids)))
        return self.get(target_id)

    def merge_many(self, project_id: str, ids: list[str]) -> dict[str, Any]:
        if len(ids) < 2:
            raise bad_request("至少选择两个素材")
        target = self.get(ids[0])
        if target["project_id"] != project_id:
            raise bad_request("素材不属于当前项目")
        return self.merge(ids[0], ids[1:])

    def _chapter(self, chapter_id: str) -> dict[str, Any]:
        with self.database.engine.connect() as connection:
            record = row_dict(
                connection.execute(select(chapter).where(chapter.c.id == chapter_id)).first()
            )
        if record is None:
            raise not_found("章节")
        return record

    def _existing_catalog(self, project_id: str) -> list[dict[str, Any]]:
        """全书已有素材摘要，供提取时去重：同一对象跨章节只保留一份素材。"""
        result: list[dict[str, Any]] = []
        with self.database.engine.connect() as connection:
            for row in connection.execute(
                select(asset.c.asset_type, asset.c.name, asset.c.profile_json).where(
                    asset.c.project_id == project_id
                )
            ):
                profile = json_loads(row.profile_json, {})
                aliases = profile.get("aliases", [])
                result.append(
                    {
                        "asset_type": row.asset_type,
                        "name": row.name,
                        "aliases": aliases if isinstance(aliases, list) else [],
                    }
                )
        return result

    def _missing_key_characters(
        self,
        connection: Any,
        *,
        chapter_id: str,
        project_id: str,
    ) -> list[str]:
        """用已生成的小说分析里的主角名单核对素材库，返回尚未提取的角色名。"""
        analysis_raw = connection.execute(
            select(novel_analysis.c.content_json).where(
                novel_analysis.c.chapter_id == chapter_id
            )
        ).scalar()
        if not analysis_raw:
            return []
        analysis = json_loads(analysis_raw, {})
        main_characters = analysis.get("main_characters", [])
        if not isinstance(main_characters, list) or not main_characters:
            return []
        rows = connection.execute(
            select(asset.c.name, asset.c.profile_json).where(
                asset.c.project_id == project_id,
                asset.c.asset_type == "character",
            )
        ).all()
        labels = set()
        for row in rows:
            labels.add(str(row.name).strip())
            profile = json_loads(row.profile_json, {})
            aliases = profile.get("aliases", [])
            if isinstance(aliases, list):
                labels.update(str(item).strip() for item in aliases if str(item).strip())
        missing = []
        for character in main_characters:
            name = str(character or "").strip()
            if not name:
                continue
            if not any(self._asset_name_matches(name, label) for label in labels):
                missing.append(name)
        return missing

    def queue_extract(self, chapter_id: str, options: dict[str, Any]) -> dict[str, Any]:
        current_chapter = self._chapter(chapter_id)
        project_id = current_chapter["project_id"]
        current_project = self._project(project_id)
        return self.jobs.create(
            project_id=project_id,
            chapter_id=chapter_id,
            job_type="extract_assets",
            target_type="chapter",
            target_id=chapter_id,
            provider_kind="text",
            provider_id=options.get("provider_id") or current_project["default_text_provider_id"],
            input_data={
                "asset_types": options.get("asset_types", ["character", "scene", "prop"]),
                "params": options.get("params", {}),
            },
        )

    def queue_prompt(self, asset_id: str, options: dict[str, Any]) -> dict[str, Any]:
        current = self.get(asset_id)
        current_project = self._project(current["project_id"])
        return self.jobs.create(
            project_id=current["project_id"],
            job_type="generate_asset_prompt",
            target_type="asset",
            target_id=asset_id,
            provider_kind="text",
            provider_id=options.get("provider_id") or current_project["default_text_provider_id"],
            input_data={"params": options.get("params", {})},
        )

    def queue_prompt_batch(self, project_id: str, options: dict[str, Any]) -> dict[str, Any]:
        current_project = self._project(project_id)
        statement = select(asset.c.id).where(asset.c.project_id == project_id)
        if options.get("ids"):
            statement = statement.where(asset.c.id.in_(options["ids"]))
        if options.get("asset_type"):
            statement = statement.where(asset.c.asset_type == options["asset_type"])
        if options.get("only_missing"):
            statement = statement.where(asset.c.prompt == "")
        with self.database.engine.connect() as connection:
            ids = list(connection.execute(statement).scalars())
        items = [
            {
                "project_id": project_id,
                "job_type": "generate_asset_prompt",
                "target_type": "asset",
                "target_id": asset_id,
                "provider_kind": "text",
                "provider_id": options.get("provider_id")
                or current_project["default_text_provider_id"],
                "input_data": {"params": options.get("params", {})},
            }
            for asset_id in ids
        ]
        return self.jobs.create_batch(items)

    async def handle_extract(self, job: dict[str, Any]) -> dict[str, Any]:
        chapter_id = job.get("chapter_id") or job.get("target_id")
        current_chapter = self._chapter(chapter_id)
        current_project = self._project(current_chapter["project_id"])
        input_data = json_loads(job.get("input_json"), {})
        selected_types = set(input_data["asset_types"])
        provider, config = self.providers.resolve(kind="text", provider_id=job.get("provider_id"))
        existing_catalog = self._existing_catalog(current_project["id"])
        output = await provider.generate_json(
            system_prompt=load_prompt("asset_extract"),
            user_prompt=(
                f"本章正文：\n{current_chapter['source_text']}\n\n"
                f"已有素材目录（已存在，本章若出现同一对象请勿重复提取）：\n"
                f"{json_dumps(existing_catalog)}"
            ),
            schema=AssetExtractionOutput,
            params={**config["params"], **input_data.get("params", {})},
        )
        payload = output.model_dump(mode="json")
        saved_ids: list[str] = []
        warnings: list[str] = []
        for asset_type, key in _EXTRACTION_KEYS.items():
            if asset_type not in selected_types:
                continue
            for extracted in payload[key]:
                saved_ids.append(
                    self._upsert_extracted(
                        current_project,
                        asset_type,
                        extracted,
                        warnings=warnings,
                    )
                )
        with self.database.engine.connect() as connection:
            for character in self._missing_key_characters(
                connection,
                chapter_id=chapter_id,
                project_id=current_project["id"],
            ):
                warnings.append(f"核心角色「{character}」尚未在素材库中")
        return {"asset_ids": saved_ids, "count": len(saved_ids), "warnings": warnings}

    def _upsert_extracted(
        self,
        current_project: dict[str, Any],
        asset_type: str,
        extracted: dict[str, Any],
        *,
        warnings: list[str] | None = None,
    ) -> str:
        name = extracted["name"].strip()
        profile = dict(extracted)
        profile.pop("name", None)
        importance = profile.pop("importance", "normal")
        prompt = str(profile.pop("prompt", "")).strip()
        # 负向提示词存储 LLM 原始内容；默认约束（多人/单件/无文字/非摄影等）
        # 在编译素材提示词时统一合并，避免存储阶段丢失或重复默认项。
        negative_prompt = str(profile.pop("negative_prompt", "")).strip()
        consistency_keywords = [
            str(item).strip()
            for item in profile.pop("consistency_keywords", [])
            if str(item).strip()
        ]
        composition = str(profile.pop("composition", "")).strip()
        now = utc_now()
        with self.database.engine.begin() as connection:
            matched, match_warning = self._find_matching_asset(
                connection,
                project_id=current_project["id"],
                asset_type=asset_type,
                name=name,
                aliases=profile.get("aliases", []),
            )
            existing = row_dict(matched) if matched is not None else None
            if match_warning and warnings is not None:
                warnings.append(match_warning)
            if existing:
                # 同名素材字段级合并：保留现有非空字段（用户手动编辑/已审阅内容），
                # 新提取结果只填充空缺字段，不再整体覆盖。
                existing_profile = json_loads(existing["profile_json"], {})
                merged_profile = self._merge_profile(existing_profile, profile)
                saved_prompt = existing["prompt"] or prompt
                saved_negative_prompt = existing["negative_prompt"] or negative_prompt
                existing_consistency = json_loads(existing["consistency_json"], [])
                if not isinstance(existing_consistency, list):
                    existing_consistency = []
                saved_consistency = list(
                    dict.fromkeys([*existing_consistency, *consistency_keywords])
                )
                saved_composition = existing["composition"] or composition
                connection.execute(
                    update(asset)
                    .where(asset.c.id == existing["id"])
                    .values(
                        profile_json=json_dumps(merged_profile),
                        prompt=saved_prompt,
                        negative_prompt=saved_negative_prompt,
                        consistency_json=json_dumps(saved_consistency),
                        composition=saved_composition,
                        importance=(
                            "core"
                            if existing["importance"] == "core" or importance == "core"
                            else "normal"
                        ),
                        status=existing["status"],
                        source_signature=current_project["source_signature"],
                        own_signature=self._signature(
                            current_project,
                            profile=merged_profile,
                            prompt=saved_prompt,
                            negative_prompt=saved_negative_prompt,
                        ),
                        revision=existing["revision"] + 1,
                        updated_at=now,
                    )
                )
                return existing["id"]
            asset_id = new_id("asset")
            connection.execute(
                insert(asset).values(
                    id=asset_id,
                    project_id=current_project["id"],
                    asset_type=asset_type,
                    name=name,
                    profile_json=json_dumps(profile),
                    prompt=prompt,
                    negative_prompt=negative_prompt,
                    consistency_json=json_dumps(consistency_keywords),
                    composition=composition,
                    importance=importance,
                    status="ready",
                    source_signature=current_project["source_signature"],
                    own_signature=self._signature(
                        current_project,
                        profile=profile,
                        prompt=prompt,
                        negative_prompt=negative_prompt,
                    ),
                    revision=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            return asset_id

    async def handle_prompt(self, job: dict[str, Any]) -> dict[str, Any]:
        current = self.get(job["target_id"])
        current_project = self._project(current["project_id"])
        input_data = json_loads(job.get("input_json"), {})
        provider, config = self.providers.resolve(kind="text", provider_id=job.get("provider_id"))
        output = await provider.generate_json(
            system_prompt=load_prompt("asset_prompt"),
            user_prompt=(
                f"项目统一画风：{current_project['style_prompt']}\n"
                f"素材类型：{current['asset_type']}\n"
                f"素材名称：{current['name']}\n"
                f"素材资料：{json_dumps(current['profile'])}"
            ),
            schema=AssetPromptOutput,
            params={**config["params"], **input_data.get("params", {})},
        )
        generated = output.model_dump(mode="json")
        updated = self.update(
            current["id"],
            {
                "prompt": generated["prompt"],
                "negative_prompt": generated["negative_prompt"],
                "consistency_keywords": generated["consistency_keywords"],
                "composition": generated["composition"],
                "status": "ready",
            },
        )
        return {"asset_id": updated["id"], "prompt": generated}
