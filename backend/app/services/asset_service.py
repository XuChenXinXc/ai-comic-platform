from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import and_, delete, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.db import Database, db
from app.dynamic_pipeline.visual_anchor import parse_visual_bible, visual_bible_brief
from app.models.tables import (
    asset,
    asset_image,
    chapter,
    job,
    novel_analysis,
    project,
    screenplay,
    shot,
)
from app.prompts import load_prompt
from app.schemas import AssetExtractionOutput, AssetPromptOutput
from app.services.errors import bad_request, conflict, not_found
from app.services.file_service import FileServiceError, file_service, project_media_url
from app.services.job_service import JobService
from app.services.prompt_service import compile_asset_prompt
from app.services.prop_utils import is_critical_prop, normalize_prop_name
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
from app.services.writing_service import (
    screenplay_spatial_issues,
    validate_screenplay_source_coverage,
)

_EXTRACTION_KEYS = {
    "character": "characters",
    "scene": "scenes",
    "prop": "props",
}

logger = logging.getLogger(__name__)

# 剧本对白里的「非人物」speaker：旁白、画外音、群像等。它们不应登记为角色素材
# 需求，否则审计会要求为旁白/群像补建角色素材，阻断提取或产出垃圾素材。
_NON_CHARACTER_SPEAKERS = {
    "旁白",
    "画外音",
    "os",
    "内心",
    "内心独白",
    "独白",
    "录音",
    "录音笔",
    "众人",
    "群众",
    "人群",
    "背景音",
    "画外",
}

# 首次素材提取（该章节所属项目素材库为空）时，模型返回空结果所允许的额外重试次数。
_EXTRACT_EMPTY_RETRIES = 1
# 首次提取空结果重试时追加到 user_prompt 末尾的强化措辞，不改变原始提示内容结构。
_EXTRACT_EMPTY_RETRY_PROMPT = (
    "\n\n注意：这是该章节的首次素材提取，素材库当前为空。请仔细阅读正文，"
    "至少提取正文中确实出现、需要跨分镜保持一致的素材；"
    "除非本章正文确实没有任何可提取对象，否则不得返回空数组。"
)

# 视觉语义重解释治理（Phase 2）：素材提取/提示词补全只记录事实，
# 不写任何画法词；画风由项目统一画风（project.style_prompt）唯一决定。
_ASSET_FACT_ONLY_CONSTRAINT = (
    "硬约束：提取结果只记录身份、空间与结构事实，不写任何绘画方式、线条、上色、"
    "光影、写实度或材质渲染词；画风由项目统一画风唯一决定。"
)

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
    def _binding_name_matches(
        cls,
        required_name: str,
        candidate_name: str,
        asset_type: str,
    ) -> bool:
        """绑定阶段只接受确定性名称/别名匹配；物品兼容量词、归属前缀与“的”字漂移。"""
        required = cls._normalize_label(required_name)
        candidate = cls._normalize_label(candidate_name)
        if not required or not candidate:
            return False
        if required == candidate:
            return True
        if asset_type == "prop":
            req_clean = cls._normalize_label(normalize_prop_name(required_name))
            cand_clean = cls._normalize_label(normalize_prop_name(candidate_name))
            if req_clean and cand_clean and req_clean == cand_clean:
                return True
            req_no_de = req_clean.replace("的", "")
            cand_no_de = cand_clean.replace("的", "")
            if req_no_de and cand_no_de and req_no_de == cand_no_de:
                return True
            if "的" in required_name and "的" not in candidate_name:
                if cls._normalize_label(required_name.split("的")[-1]) == cand_clean:
                    return True
            elif "的" in candidate_name and "的" not in required_name:
                if cls._normalize_label(candidate_name.split("的")[-1]) == req_clean:
                    return True
        return False

    @classmethod
    def _variant_name_core(cls, value: str, asset_type: str | None = None) -> str:
        """去掉常见状态/场景修饰词，得到用于匹配变体素材的名称核心。"""
        text = str(value or "").strip()
        # 注意：禁止随意按"的"切分，避免"张三的命灯"与"李四的命灯"误合并；
        # 角色素材严禁剥除颜色与修饰词，避免「红衣女子」与「黑衣女子」误合并
        if asset_type != "character":
            for token in sorted(_NAME_VARIANT_TOKENS, key=len, reverse=True):
                text = text.replace(token, "")
        return cls._normalize_label(text)

    @classmethod
    def _asset_name_matches(cls, left: str, right: str, asset_type: str | None = None) -> bool:
        """判断两个素材名是否指向同一对象，含别名、归属前缀与状态变体。"""
        left_text = str(left or "").strip()
        right_text = str(right or "").strip()
        if not left_text or not right_text:
            return False
        if left_text == right_text:
            return True
        if cls._normalize_label(left_text) == cls._normalize_label(right_text):
            return True
        # 「顾长渊的青玉命灯」与「青玉命灯」这类归属前缀（仅单侧包含归属前缀，另一侧为无归属纯本体）
        if "的" in left_text and "的" not in right_text:
            if left_text.split("的")[-1] == right_text:
                return True
        elif "的" in right_text and "的" not in left_text:
            if right_text.split("的")[-1] == left_text:
                return True
        # 较长名称以较短名称为后缀，如「万灯阁第七层」与「第七层」
        # （仅限非角色素材，避免「林海」误匹配「陈林海」）
        if asset_type != "character":
            shorter, longer = sorted((left_text, right_text), key=len)
            if len(shorter) >= 2 and longer.endswith(shorter):
                return True
        # 「万灯阁命灯」与「万灯阁黑色命灯」这类非角色状态变体
        if asset_type != "character":
            left_core = cls._variant_name_core(left_text, asset_type)
            right_core = cls._variant_name_core(right_text, asset_type)
            if left_core and right_core and left_core == right_core:
                return True
        return False

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
            if cls._asset_name_matches(name, row.name, asset_type=asset_type):
                scored.append((1, row))
        if not scored:
            return None, ""
        best_score = max(score for score, _ in scored)
        candidates = [row for score, row in scored if score == best_score]
        if len(candidates) > 1:
            # 命中多个已有素材说明别名或变体存在歧义冲突，不可强行并入其中某一个，应独立创建
            return None, ""
        matched = candidates[0]
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
            visual_bible=parse_visual_bible(current_project),
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

    def _representative_image(
        self, connection: Any, record: dict[str, Any], project_id: str
    ) -> dict[str, Any] | None:
        """素材展示图：每个素材只有一张当前图（角色/场景/道具一致）。"""
        return self._current_image(connection, record["id"], project_id)

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
                record["current_image"] = self._representative_image(
                    connection, record, project_id
                )
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
            result["current_image"] = self._representative_image(
                connection, result, result["project_id"]
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
            visual_bible=parse_visual_bible(project_record),
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
                result = connection.execute(
                    update(asset).where(asset.c.id == asset_id).values(**changes)
                )
                if result.rowcount != 1:
                    # 前置 get 之后素材被并发删除/修改：明确报 404，不静默成功
                    raise not_found("素材")
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
        target_name = ""
        source_names: set[str] = set()
        if target_id is not None:
            target_row = connection.execute(
                select(asset.c.name).where(asset.c.id == target_id)
            ).first()
            if target_row:
                target_name = str(target_row.name or "")
            source_rows = connection.execute(
                select(asset.c.name).where(asset.c.id.in_(source_ids))
            ).all()
            source_names = {str(r.name or "") for r in source_rows if str(r.name or "").strip()}

        rows = connection.execute(select(shot).where(shot.c.project_id == project_id)).all()
        for row in rows:
            decoded = dict(row._mapping)
            for alias, column in _SHOT_JSON_FIELDS.items():
                decoded[alias] = json_loads(decoded[column], [])
            characters = decoded["character_ids"]
            props = decoded["prop_ids"]
            bindings = decoded["reference_bindings"]
            positions = decoded["positions"]
            expressions = decoded["expressions"]
            characters = characters if isinstance(characters, list) else []
            props = props if isinstance(props, list) else []
            bindings = bindings if isinstance(bindings, list) else []
            positions = positions if isinstance(positions, list) else []
            expressions = expressions if isinstance(expressions, list) else []
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
                remapped_positions = positions
                remapped_expressions = expressions
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
                remapped_positions = []
                for item in positions:
                    if (
                        isinstance(item, dict)
                        and item.get("character") in source_names
                        and target_name
                    ):
                        item_copy = dict(item)
                        item_copy["character"] = target_name
                        remapped_positions.append(item_copy)
                    else:
                        remapped_positions.append(item)
                remapped_expressions = []
                for item in expressions:
                    if (
                        isinstance(item, dict)
                        and item.get("character") in source_names
                        and target_name
                    ):
                        item_copy = dict(item)
                        item_copy["character"] = target_name
                        remapped_expressions.append(item_copy)
                    else:
                        remapped_expressions.append(item)
            if scene_asset_id != row.scene_asset_id:
                changes["scene_asset_id"] = scene_asset_id
            if remapped_characters != characters:
                changes["character_ids_json"] = json_dumps(remapped_characters)
            if remapped_props != props:
                changes["prop_ids_json"] = json_dumps(remapped_props)
            if remapped_bindings != bindings:
                changes["reference_bindings_json"] = json_dumps(remapped_bindings)
            if remapped_positions != positions:
                changes["positions_json"] = json_dumps(remapped_positions)
            if remapped_expressions != expressions:
                changes["expressions_json"] = json_dumps(remapped_expressions)
            if changes:
                decoded["scene_asset_id"] = scene_asset_id
                decoded["character_ids"] = remapped_characters
                decoded["prop_ids"] = remapped_props
                decoded["reference_bindings"] = remapped_bindings
                decoded["positions"] = remapped_positions
                decoded["expressions"] = remapped_expressions
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
        # 事务已提交，文件清理失败只记日志，不让 500 掩盖已完成的删除
        for path in paths:
            try:
                file_service.delete_file(current["project_id"], path)
            except (FileServiceError, OSError) as exc:
                logger.warning("删除素材图片文件失败：%s（%s）", path, exc)

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
            target_has_current = (
                connection.execute(
                    select(asset_image.c.id).where(
                        asset_image.c.asset_id == target_id,
                        asset_image.c.is_current.is_(True),
                    )
                ).first()
                is not None
            )
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

            promoted_image_id: str | None = None
            if not target_has_current and source_images:
                source_currents = [
                    img._mapping["id"]
                    for img in source_images
                    if bool(img._mapping.get("is_current"))
                ]
                if source_currents:
                    promoted_image_id = source_currents[-1]
                else:
                    promoted_image_id = source_images[-1]._mapping["id"]

            for image in source_images:
                next_version += 1
                img_id = image._mapping["id"]
                connection.execute(
                    update(asset_image)
                    .where(asset_image.c.id == img_id)
                    .values(
                        asset_id=target_id,
                        version_no=next_version,
                        is_current=(img_id == promoted_image_id),
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
                        prompt_override=target.get("prompt_override") or "",
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

    def _screenplay_scene_names(self, chapter_id: str) -> list[str]:
        """读取本集已生成剧本的场次名，供素材提取锚定场景命名。"""
        with self.database.engine.connect() as connection:
            raw = connection.execute(
                select(screenplay.c.content_json).where(
                    screenplay.c.chapter_id == chapter_id
                )
            ).scalar()
        if not raw:
            return []
        content = json_loads(raw, {})
        scenes = content.get("scenes") if isinstance(content, dict) else None
        if not isinstance(scenes, list):
            return []
        names: list[str] = []
        for scene in scenes:
            if not isinstance(scene, dict):
                continue
            name = str(scene.get("scene_name") or "").strip()
            if name and name not in names:
                names.append(name)
        return names

    def _screenplay_content(self, chapter_id: str) -> dict[str, Any] | None:
        """读取本集已生成剧本的完整结构，供素材提取作为权威事实依据。"""
        with self.database.engine.connect() as connection:
            raw = connection.execute(
                select(screenplay.c.content_json).where(
                    screenplay.c.chapter_id == chapter_id
                )
            ).scalar()
        if not raw:
            return None
        content = json_loads(raw, {})
        if isinstance(content, dict) and isinstance(content.get("scenes"), list):
            return content
        return None

    def _confirmed_screenplay(self, chapter_id: str) -> dict[str, Any]:
        with self.database.engine.connect() as connection:
            record = row_dict(
                connection.execute(
                    select(screenplay).where(screenplay.c.chapter_id == chapter_id)
                ).first()
            )
        if record is None or not record.get("own_signature"):
            raise conflict("请先生成并确认剧本，再提取素材")
        if record.get("status") != "confirmed":
            raise conflict("剧本尚未确认，请先在剧本页面确认当前版本")
        content = json_loads(record.get("content_json"), {})
        if not isinstance(content, dict) or not isinstance(content.get("scenes"), list):
            raise conflict("当前确认剧本结构无效，请重新生成或修正后再确认")
        current_chapter = self._chapter(chapter_id)
        # 原文覆盖校验只报告不阻断：存量剧本的 source_refs 可能为空，
        # 硬校验会让这些项目永远无法提取素材。
        coverage_warnings = validate_screenplay_source_coverage(
            content,
            current_chapter["source_text"],
        )
        coverage_warnings.extend(screenplay_spatial_issues(content))
        if coverage_warnings:
            raise conflict(
                "确认剧本完整性校验未通过："
                + "；".join(coverage_warnings)
                + "。请修正并重新确认剧本"
            )
        record["content"] = content
        record["coverage_warnings"] = coverage_warnings
        return record

    @classmethod
    def _screenplay_asset_requirements(
        cls,
        content: dict[str, Any],
    ) -> dict[str, dict[str, set[str]]]:
        requirements: dict[str, dict[str, set[str]]] = {
            "character": {},
            "scene": {},
            "prop": {},
        }
        for scene in content.get("scenes", []):
            if not isinstance(scene, dict):
                continue
            scene_no = scene.get("scene_no")
            source_ref = f"scene:{scene_no}" if scene_no else "screenplay"
            scene_name = str(scene.get("scene_name") or "").strip()
            if scene_name:
                requirements["scene"].setdefault(scene_name, set()).add(source_ref)
            for value in scene.get("characters") or []:
                name = str(value or "").strip()
                if name:
                    requirements["character"].setdefault(name, set()).add(source_ref)
            # 对白 speaker 里混有旁白/画外音/群像等非人物标注；它们不登记为
            # 角色素材需求，避免审计要求为旁白补建角色素材。
            for item in scene.get("dialogue") or []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("speaker") or "").strip()
                if name and name.casefold() not in _NON_CHARACTER_SPEAKERS:
                    requirements["character"].setdefault(name, set()).add(source_ref)
            # 剧本与素材统一标准：仅真正的剧情关键道具登记为素材需求，
            # 日常普通器具、家具软装、包装、建筑固定陈设等非关键道具自动忽略，避免阻断分镜提取。
            for value in scene.get("props") or []:
                name = str(value or "").strip()
                if name and is_critical_prop(name):
                    requirements["prop"].setdefault(name, set()).add(source_ref)
        return requirements

    def audit_screenplay_assets(
        self,
        chapter_id: str,
        *,
        asset_types: set[str] | None = None,
        require_confirmed: bool = True,
    ) -> dict[str, Any]:
        screenplay_record = (
            self._confirmed_screenplay(chapter_id)
            if require_confirmed
            else None
        )
        content = (
            screenplay_record["content"]
            if screenplay_record is not None
            else self._screenplay_content(chapter_id)
        )
        if content is None:
            raise conflict("当前章节还没有可用剧本")
        current_chapter = self._chapter(chapter_id)
        requirements = self._screenplay_asset_requirements(content)
        selected = asset_types or set(_EXTRACTION_KEYS)
        with self.database.engine.connect() as connection:
            rows = connection.execute(
                select(
                    asset.c.id,
                    asset.c.asset_type,
                    asset.c.name,
                    asset.c.profile_json,
                ).where(asset.c.project_id == current_chapter["project_id"])
            ).all()

        catalogs: dict[str, list[dict[str, Any]]] = {
            "character": [],
            "scene": [],
            "prop": [],
        }
        for row in rows:
            profile = json_loads(row.profile_json, {})
            aliases = profile.get("aliases", [])
            catalogs[row.asset_type].append(
                {
                    "id": row.id,
                    "name": row.name,
                    "labels": [
                        row.name,
                        *(aliases if isinstance(aliases, list) else []),
                    ],
                }
            )

        missing_assets: list[dict[str, Any]] = []
        ambiguous: list[str] = []
        bindings: dict[str, dict[str, str]] = {
            "character": {},
            "scene": {},
            "prop": {},
        }
        for asset_type in selected:
            for required_name, source_refs in requirements[asset_type].items():
                primary = [
                    item
                    for item in catalogs[asset_type]
                    if self._binding_name_matches(
                        required_name,
                        item["name"],
                        asset_type,
                    )
                ]
                candidates = primary or [
                    item
                    for item in catalogs[asset_type]
                    if any(
                        self._binding_name_matches(
                            required_name,
                            str(label),
                            asset_type,
                        )
                        for label in item["labels"]
                    )
                ]
                unique = {item["id"]: item for item in candidates}
                if len(unique) == 1:
                    bindings[asset_type][required_name] = next(iter(unique))
                    continue
                if len(unique) > 1:
                    ambiguous.append(
                        f"{asset_type}「{required_name}」同时匹配："
                        + "、".join(sorted(item["name"] for item in unique.values()))
                    )
                    continue
                missing_assets.append(
                    {
                        "asset_type": asset_type,
                        "reason": "确认剧本要求该素材，但素材库没有唯一匹配项",
                        "source_ref": "、".join(sorted(source_refs)),
                        "suggested_name": required_name,
                        "suggested_aliases": [],
                    }
                )

        # 视觉空间提示（非阻断）：本场 action 文本里出现其它场景素材的名称/别名。
        # 该判定只是纯子串匹配，剧本正文可能正在描述「照片 / 回忆 / 转述」里的
        # 另一个地点（例如「照片上是一家写着『林记钟表』的旧店」），并不代表本场
        # 真的发生了空间切换；而且 asset_extract.md 约定剧本已按视觉空间完成拆场。
        # 因此这里只产出提示，交由用户判断是否需要拆场，绝不作为硬门禁。
        visual_space_conflicts: list[str] = []
        scene_bindings = bindings["scene"]
        if "scene" in selected and scene_bindings:
            scene_catalog = catalogs["scene"]
            for scene in content.get("scenes", []):
                if not isinstance(scene, dict):
                    continue
                scene_name = str(scene.get("scene_name") or "").strip()
                current_id = scene_bindings.get(scene_name)
                if not current_id:
                    continue
                action = self._normalize_label(str(scene.get("action") or ""))
                for item in scene_catalog:
                    if item["id"] == current_id:
                        continue
                    matched_label = next(
                        (
                            str(label).strip()
                            for label in item["labels"]
                            if len(self._normalize_label(str(label))) >= 2
                            and self._normalize_label(str(label)) in action
                        ),
                        "",
                    )
                    if matched_label:
                        visual_space_conflicts.append(
                            f"场次 {scene.get('scene_no')}「{scene_name}」的动作描述"
                            f"提到了其它场景「{item['name']}」（命中“{matched_label}”），"
                            "若该场确实发生空间切换请拆场，否则可忽略"
                        )
                        break

        warnings = list(visual_space_conflicts)
        family_issues = self.audit_scene_families(current_chapter["project_id"])

        return {
            "complete": not missing_assets and not ambiguous and not family_issues,
            "missing_assets": missing_assets,
            "ambiguous": ambiguous,
            "family_issues": family_issues,
            "visual_space_conflicts": visual_space_conflicts,
            "warnings": warnings,
            "bindings": bindings,
            "screenplay_signature": (
                screenplay_record.get("own_signature") if screenplay_record else ""
            ),
        }

    def audit_scene_families(self, project_id: str) -> list[str]:
        """确保每个已声明的场景族在项目素材库中恰好有一个锚点。"""
        with self.database.engine.connect() as connection:
            rows = connection.execute(
                select(asset.c.name, asset.c.profile_json).where(
                    asset.c.project_id == project_id,
                    asset.c.asset_type == "scene",
                )
            ).all()
        families: dict[str, dict[str, Any]] = {}
        for row in rows:
            profile = json_loads(row.profile_json, {})
            family = str(profile.get("scene_family") or "").strip()
            if not family:
                continue
            current = families.setdefault(family, {"members": [], "anchors": []})
            current["members"].append(row.name)
            if bool(profile.get("family_anchor")):
                current["anchors"].append(row.name)
        issues: list[str] = []
        for family, current in sorted(families.items()):
            anchors = current["anchors"]
            if len(anchors) != 1:
                issues.append(
                    f"场景族「{family}」必须恰好有一个锚点，当前为 {len(anchors)} 个"
                    f"（成员：{'、'.join(current['members'])}）"
                )
        return issues

    @classmethod
    def _source_refs_for_asset(
        cls,
        screenplay_content: dict[str, Any] | None,
        asset_type: str,
        name: str,
        aliases: list[str],
    ) -> list[str]:
        if screenplay_content is None:
            return []
        labels = [name, *(alias for alias in aliases if str(alias).strip())]
        requirements = cls._screenplay_asset_requirements(screenplay_content)
        result: set[str] = set()
        for required_name, source_refs in requirements[asset_type].items():
            if any(
                cls._binding_name_matches(required_name, str(label), asset_type)
                for label in labels
            ):
                result.update(source_refs)
        return sorted(result)

    @classmethod
    def _matches_screenplay_requirement(
        cls,
        screenplay_content: dict[str, Any] | None,
        asset_type: str,
        name: str,
        aliases: list[str],
    ) -> bool:
        if screenplay_content is None:
            return True
        labels = [name, *aliases]
        requirements = cls._screenplay_asset_requirements(screenplay_content)
        return any(
            cls._binding_name_matches(required_name, str(label), asset_type)
            for required_name in requirements[asset_type]
            for label in labels
            if str(label).strip()
        )

    @classmethod
    def _remove_ungrounded_aliases(
        cls,
        extracted: dict[str, Any],
        story_context: str,
        warnings: list[str],
        trusted_aliases: set[str] | None = None,
    ) -> None:
        aliases = extracted.get("aliases")
        if not isinstance(aliases, list) or not aliases:
            return
        normalized_story = cls._normalize_label(story_context)
        grounded: list[str] = []
        removed: list[str] = []
        for alias in aliases:
            text = str(alias or "").strip()
            if not text:
                continue
            if text in (trusted_aliases or set()) or cls._normalize_label(text) in normalized_story:
                grounded.append(text)
            else:
                removed.append(text)
        extracted["aliases"] = list(dict.fromkeys(grounded))
        if removed:
            warnings.append(
                f"素材「{extracted.get('name') or ''}」移除了未在原文或确认剧本中出现的别名："
                + "、".join(removed)
            )

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
            # 小说分析可能把主角名存成「顾珩（注释）」形式；核对素材库时先剥离
            # 括号注释取主名，避免已提取角色被误报为缺失。
            match_names = [name]
            main_name = re.split(r"[（(]", name, maxsplit=1)[0].strip()
            if main_name and main_name != name:
                match_names.append(main_name)
            if not any(
                self._asset_name_matches(match_name, label, asset_type="character")
                for match_name in match_names
                for label in labels
            ):
                missing.append(name)
        return missing

    def queue_extract(self, chapter_id: str, options: dict[str, Any]) -> dict[str, Any]:
        current_chapter = self._chapter(chapter_id)
        project_id = current_chapter["project_id"]
        current_project = self._project(project_id)
        current_screenplay = self._confirmed_screenplay(chapter_id)
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
                "expected_screenplay_signature": current_screenplay["own_signature"],
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

    def _analysis_content(self, chapter_id: str) -> dict[str, Any] | None:
        """获取章节对应的小说分析结构。"""
        with self.database.engine.connect() as connection:
            raw = connection.execute(
                select(novel_analysis.c.content_json).where(
                    novel_analysis.c.chapter_id == chapter_id
                )
            ).scalar()
        if not raw:
            return None
        content = json_loads(raw, {})
        return content if isinstance(content, dict) else None

    async def handle_extract(self, job: dict[str, Any]) -> dict[str, Any]:
        chapter_id = str(job.get("chapter_id") or job.get("target_id") or "")
        current_chapter = self._chapter(chapter_id)
        current_project = self._project(current_chapter["project_id"])
        input_data = json_loads(job.get("input_json"), {})
        selected_types = set(input_data["asset_types"])
        expected_screenplay_signature = str(
            input_data.get("expected_screenplay_signature") or ""
        )
        screenplay_data = self._screenplay_content(chapter_id)
        if expected_screenplay_signature:
            current_screenplay = self._confirmed_screenplay(chapter_id)
            if current_screenplay["own_signature"] != expected_screenplay_signature:
                raise conflict("剧本已在素材任务排队后变化，请基于最新确认剧本重新提取素材")
            screenplay_data = current_screenplay["content"]
        provider, config = self.providers.resolve(kind="text", provider_id=job.get("provider_id"))
        existing_catalog = self._existing_catalog(current_project["id"])
        # 当前所选分类首次提取且剧本确有必需素材时，空结果才判为异常并自动重试。
        # 增量提取（素材库已有内容）结果为空属合法结果（提示词允许数组为空），不重试。
        first_extract = not any(
            item.get("asset_type") in selected_types for item in existing_catalog
        )
        required_assets = (
            self._screenplay_asset_requirements(screenplay_data)
            if screenplay_data is not None
            else None
        )
        require_nonempty = first_extract and (
            required_assets is None
            or any(required_assets[asset_type] for asset_type in selected_types)
        )
        # 视觉语义重解释治理（Phase 2）：注入项目统一画风与「只记事实、不写画法词」硬约束，
        # 画风由项目统一画风唯一决定。
        project_style = current_project.get("style_prompt") or ""
        user_prompt = (
            f"项目统一画风：{project_style}\n"
            f"{visual_bible_brief(current_project)}"
            f"{_ASSET_FACT_ONLY_CONSTRAINT}\n"
            f"本章正文：\n{current_chapter['source_text']}\n\n"
            f"已有素材目录（已存在，本章若出现同一对象请勿重复提取）：\n"
            f"{json_dumps(existing_catalog)}"
        )
        analysis_data = self._analysis_content(chapter_id)
        if analysis_data:
            user_prompt += (
                "\n\n【小说正文分析确立的核心资产基准（权威依据，提取的角色、场景与道具必须以此为准，统一规范名称）】：\n"
                f"- 核心角色清单：{json_dumps(analysis_data.get('main_characters') or [])}\n"
                f"- 核心场景地点：{json_dumps(analysis_data.get('locations') or [])}\n"
                f"- 核心关键道具：{json_dumps(analysis_data.get('key_props') or [])}\n"
                "提取规则：\n"
                "1. 提取的角色必须覆盖核心角色清单，名称严格与清单一致；\n"
                "2. 提取的场景必须覆盖核心场景地点，名称优先与清单一致；\n"
                "3. 提取的道具必须仅限核心关键道具，严禁提取日常普通生活用品或场景陈设。"
            )
        if screenplay_data:
            # 剧本是漫剧影视化改编的最终定稿事实：素材提取必须优先覆盖剧本中实际出场的角色与场景
            user_prompt += (
                "\n\n【本集剧本事实（权威依据，提取的角色与场景必须覆盖剧本实际出场对象）】：\n"
                f"{json_dumps(screenplay_data)}\n"
                "提取规则：\n"
                "1. 剧本中登场的所有角色必须全部提取（包括角色名、身份、服装特征）；\n"
                "2. 场景素材名称必须与剧本场次名严格一致（或将剧本场次名列入 aliases）；\n"
                "3. 原文正文仅作为视觉细节与外貌描写的补充参考，"
                "不得提取剧本中已删减的无关角色或地点；\n"
                "4. 物品素材（props）：严格只为剧本各场次 props 中列出的关键道具提取素材，"
                "严禁提取任何日常用品、餐具、包装袋、手机或场景固定陈设；"
                "若剧本 props 为空，则 props 输出空数组 []。"
            )
        else:
            screenplay_scene_names = self._screenplay_scene_names(chapter_id)
            if screenplay_scene_names:
                # 剧本先生成、素材后提取的顺序：把剧本场次名喂给素材模型，
                # 场景素材的 name 优先逐字采用剧本场次名（或至少收入 aliases），
                # 与剧本生成阶段复用素材名互为反向锚定，避免分镜按名称绑定失败。
                user_prompt += (
                    "\n\n本集已生成剧本的场次名（提取场景素材时：正文地点若与本集剧本"
                    "场次一致，`name` 必须逐字采用剧本场次名，或至少把剧本场次名收进"
                    " `aliases`，保证后续分镜按名称绑定场景素材）：\n"
                    f"{json_dumps(screenplay_scene_names)}"
                )

        async def _generate(prompt: str) -> dict[str, Any]:
            output = await provider.generate_json(
                system_prompt=load_prompt("asset_extract"),
                user_prompt=prompt,
                schema=AssetExtractionOutput,
                params={**config["params"], **input_data.get("params", {})},
            )
            return output.model_dump(mode="json")

        def _save(payload: dict[str, Any]) -> tuple[list[str], list[str]]:
            saved: list[str] = []
            warns: list[str] = []
            story_context = (
                f"{current_chapter['source_text']}\n{json_dumps(screenplay_data or {})}"
            )
            for asset_type, key in _EXTRACTION_KEYS.items():
                if asset_type not in selected_types:
                    continue
                for extracted in payload[key]:
                    extracted = dict(extracted)
                    aliases = extracted.get("aliases", [])
                    alias_list = aliases if isinstance(aliases, list) else []
                    if not self._matches_screenplay_requirement(
                        screenplay_data,
                        asset_type,
                        str(extracted.get("name") or ""),
                        alias_list,
                    ):
                        warns.append(
                            f"模型返回的{asset_type}素材「{extracted.get('name') or ''}」"
                            "不在确认剧本素材需求中，已忽略"
                        )
                        continue
                    self._remove_ungrounded_aliases(extracted, story_context, warns)
                    aliases = extracted.get("aliases", [])
                    existing_refs = extracted.get("source_refs", [])
                    source_refs = self._source_refs_for_asset(
                        screenplay_data,
                        asset_type,
                        str(extracted.get("name") or ""),
                        aliases if isinstance(aliases, list) else [],
                    )
                    extracted["source_refs"] = list(
                        dict.fromkeys(
                            [
                                *(
                                    existing_refs
                                    if isinstance(existing_refs, list)
                                    else []
                                ),
                                *source_refs,
                            ]
                        )
                    )
                    saved.append(
                        self._upsert_extracted(
                            current_project,
                            asset_type,
                            extracted,
                            warnings=warns,
                        )
                    )
            return saved, warns

        payload = await _generate(user_prompt)
        if expected_screenplay_signature:
            latest_screenplay = self._confirmed_screenplay(chapter_id)
            if latest_screenplay["own_signature"] != expected_screenplay_signature:
                raise conflict(
                    "剧本在素材生成期间发生变化，已中止保存；请重新提取素材"
                )
            # 审计门禁前置：写库前只拦截「歧义匹配」（同一名称命中多个素材，
            # 无法确定绑定对象），避免任务失败却留下半提交素材。
            # 视觉空间冲突已降级为提示，不再参与这里的阻断。
            pre_audit = self.audit_screenplay_assets(
                chapter_id,
                asset_types=selected_types,
            )
            if pre_audit["ambiguous"]:
                detail = "；".join(pre_audit["ambiguous"])
                raise bad_request(f"素材覆盖审计失败：{detail}")
        saved_ids, warnings = _save(payload)
        # 首次提取却未返回任何素材：判为异常，追加强化措辞重试，
        # 仍为空则抛出异常交由 runner 标记失败。
        if require_nonempty and not saved_ids:
            for _ in range(_EXTRACT_EMPTY_RETRIES):
                payload = await _generate(user_prompt + _EXTRACT_EMPTY_RETRY_PROMPT)
                saved_ids, warnings = _save(payload)
                if saved_ids:
                    break
            if not saved_ids:
                raise RuntimeError("首次素材提取未返回任何素材，请检查正文或模型输出")
        with self.database.engine.connect() as connection:
            for character in self._missing_key_characters(
                connection,
                chapter_id=chapter_id,
                project_id=current_project["id"],
            ):
                warnings.append(f"核心角色「{character}」尚未在素材库中")
        audit: dict[str, Any] | None = None
        if expected_screenplay_signature:
            audit = self.audit_screenplay_assets(
                chapter_id,
                asset_types=selected_types,
            )
            if audit["missing_assets"]:
                completed = await self.complete_missing(
                    chapter_id,
                    audit["missing_assets"],
                    provider_id=job.get("provider_id"),
                )
                saved_ids.extend(completed["asset_ids"])
                warnings.extend(completed["warnings"])
                audit = self.audit_screenplay_assets(
                    chapter_id,
                    asset_types=selected_types,
                )
            if not audit["complete"]:
                missing = "、".join(
                    f"{item['asset_type']}「{item['suggested_name']}」"
                    for item in audit["missing_assets"]
                )
                detail = "；".join(
                    [
                        *(audit["ambiguous"] or []),
                        *(audit["family_issues"] or []),
                        *([f"缺少 {missing}"] if missing else []),
                    ]
                )
                raise bad_request(
                    f"素材覆盖审计失败：{detail}。"
                    "以上「缺少」项即自动补建后仍未通过审计的素材。"
                )
            warnings.extend(audit["warnings"])
        saved_ids = list(dict.fromkeys(saved_ids))
        return {
            "asset_ids": saved_ids,
            "count": len(saved_ids),
            "warnings": warnings,
            # 复用上面最后一次审计结果，避免再全量加载素材与剧本
            "coverage_passed": bool(
                not expected_screenplay_signature
                or (audit is not None and audit["complete"])
            ),
        }

    async def complete_missing(
        self,
        chapter_id: str,
        missing_assets: list[dict[str, Any]],
        *,
        provider_id: str | None = None,
    ) -> dict[str, Any]:
        """根据缺失素材清单调用 asset_complete 提示词补全素材并入库。"""
        if not missing_assets:
            return {"asset_ids": [], "count": 0, "warnings": []}
        current_chapter = self._chapter(chapter_id)
        current_project = self._project(current_chapter["project_id"])
        provider, config = self.providers.resolve(
            kind="text",
            provider_id=provider_id or current_project["default_text_provider_id"],
        )
        existing_catalog = self._existing_catalog(current_project["id"])
        screenplay_data = self._screenplay_content(chapter_id)
        user_prompt = (
            f"项目统一画风：{current_project.get('style_prompt') or ''}\n"
            f"{visual_bible_brief(current_project)}"
            f"{_ASSET_FACT_ONLY_CONSTRAINT}\n"
            f"本章正文：\n{current_chapter['source_text']}\n\n"
            f"本集确认剧本事实：\n{json_dumps(screenplay_data or {})}\n\n"
            f"缺失素材清单：\n{json_dumps(missing_assets)}\n\n"
            f"已有素材目录（已存在，请勿重复提取）：\n{json_dumps(existing_catalog)}"
        )
        output = await provider.generate_json(
            system_prompt=load_prompt("asset_complete"),
            user_prompt=user_prompt,
            schema=AssetExtractionOutput,
            params={**config["params"]},
        )
        payload = output.model_dump(mode="json")
        saved_ids: list[str] = []
        warns: list[str] = []
        missing_by_type: dict[str, list[dict[str, Any]]] = {}
        for item in missing_assets:
            if isinstance(item, dict) and item.get("suggested_name"):
                missing_by_type.setdefault(str(item.get("asset_type")), []).append(item)

        for asset_type, key in _EXTRACTION_KEYS.items():
            expected_items = missing_by_type.get(asset_type, [])
            for extracted in payload[key]:
                extracted = dict(extracted)
                extracted_name = str(extracted.get("name") or "").strip()
                # 补全对齐：单项缺失且单项提取时严格校准为建议名称，
                # 避免模型命名微小漂移导致分镜重新提取仍报缺失。
                if (
                    len(expected_items) == 1
                    and len(payload[key]) == 1
                    and extracted_name != expected_items[0]["suggested_name"]
                ):
                    target_name = expected_items[0]["suggested_name"]
                    aliases = extracted.setdefault("aliases", [])
                    if extracted_name and extracted_name not in aliases:
                        aliases.append(extracted_name)
                    extracted["name"] = target_name
                aliases = extracted.get("aliases", [])
                alias_list = aliases if isinstance(aliases, list) else []
                if not any(
                    self._binding_name_matches(
                        str(item["suggested_name"]),
                        label,
                        asset_type,
                    )
                    for item in expected_items
                    for label in [str(extracted.get("name") or ""), *alias_list]
                ):
                    warns.append(
                        f"补素材模型返回了清单外的{asset_type}素材"
                        f"「{extracted.get('name') or ''}」，已忽略"
                    )
                    continue
                self._remove_ungrounded_aliases(
                    extracted,
                    f"{current_chapter['source_text']}\n{json_dumps(screenplay_data or {})}",
                    warns,
                    trusted_aliases={
                        str(alias).strip()
                        for item in expected_items
                        for alias in item.get("suggested_aliases", [])
                        if str(alias).strip()
                    },
                )
                extracted["source_refs"] = list(
                    dict.fromkeys(
                        [
                            *(
                                extracted.get("source_refs", [])
                                if isinstance(extracted.get("source_refs"), list)
                                else []
                            ),
                            *self._source_refs_for_asset(
                                screenplay_data,
                                asset_type,
                                str(extracted.get("name") or ""),
                                extracted.get("aliases", []),
                            ),
                        ]
                    )
                )
                saved_ids.append(
                    self._upsert_extracted(
                        current_project,
                        asset_type,
                        extracted,
                        warnings=warns,
                    )
                )
        return {"asset_ids": saved_ids, "count": len(saved_ids), "warnings": warns}

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
                            prompt_override=existing.get("prompt_override") or "",
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
            # 清理同类型其他已有素材别名中与新素材 canonical name 重复的项，消除别名歧义。
            # 用条件把候选行收敛到确实把该名称当别名的极少行，避免每次新建都全表读取
            # 同类型素材（O(n²) 读放大）；profile/别名属于签名相关字段，同步重算签名。
            alias_token = json_dumps(name)
            other_rows = connection.execute(
                select(asset).where(
                    asset.c.project_id == current_project["id"],
                    asset.c.asset_type == asset_type,
                    asset.c.id != asset_id,
                    asset.c.profile_json.contains(alias_token, autoescape=True),
                )
            ).all()
            for other_row in other_rows:
                other = dict(other_row._mapping)
                other_profile = json_loads(other["profile_json"], {})
                other_aliases = other_profile.get("aliases")
                if not isinstance(other_aliases, list) or name not in other_aliases:
                    continue
                other_profile["aliases"] = [a for a in other_aliases if a != name]
                connection.execute(
                    update(asset)
                    .where(asset.c.id == other["id"])
                    .values(
                        profile_json=json_dumps(other_profile),
                        own_signature=self._signature(
                            current_project,
                            profile=other_profile,
                            prompt=other["prompt"],
                            negative_prompt=other["negative_prompt"],
                            prompt_override=other.get("prompt_override") or "",
                        ),
                        revision=other["revision"] + 1,
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
                f"{visual_bible_brief(current_project)}"
                f"{_ASSET_FACT_ONLY_CONSTRAINT}\n"
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
