import asyncio
import concurrent.futures
import logging
import re
from collections.abc import Coroutine
from typing import Any

from pydantic import ValidationError
from sqlalchemy import insert, select, update

from app.db import Database, db
from app.models.tables import asset, chapter, novel_analysis, project, screenplay
from app.prompts import load_prompt
from app.schemas import (
    NovelAnalysisOutput,
    ScreenplayOutput,
    ScreenplayScene,
)
from app.services.errors import bad_request, conflict, not_found
from app.services.job_service import JobService
from app.services.prop_utils import filter_critical_props, is_critical_prop
from app.services.provider_service import ProviderService
from app.services.signature_service import screenplay_signature
from app.services.utils import (
    json_dumps,
    json_loads,
    row_dict,
    sanitize_error_message,
    utc_now,
)

logger = logging.getLogger(__name__)

_REVISION_CONFLICT_REASON = "内容在生成期间已被编辑，生成结果未覆盖当前版本"


def screenplay_source_passages(
    source_text: str,
    *,
    max_chars: int = 420,
) -> list[dict[str, str]]:
    """把原文按自然段聚合为可追踪片段，避免逐句清单挤占模型输出。"""
    blocks = [
        block.strip()
        for block in re.split(r"\n\s*\n", str(source_text or ""))
        if block.strip() and block.strip() != "——"
    ]
    grouped: list[str] = []
    current = ""
    for block in blocks:
        candidate = f"{current}\n\n{block}".strip() if current else block
        if current and len(candidate) > max_chars:
            grouped.append(current)
            current = block
        else:
            current = candidate
    if current:
        grouped.append(current)
    return [
        {"source_ref": f"P{index:03d}", "text": text}
        for index, text in enumerate(grouped, start=1)
    ]


def validate_screenplay_source_coverage(
    content: dict[str, Any],
    source_text: str,
) -> list[str]:
    """返回剧本原文覆盖问题的人类可读警告，而不是抛异常。

    存量剧本的 source_refs 可能为空且没有回填迁移，硬校验会让这些项目永远
    无法确认剧本、提取素材或分镜，因此这里只报告不阻断。
    """
    expected = {
        item["source_ref"] for item in screenplay_source_passages(source_text)
    }
    actual = {
        str(source_ref).strip()
        for scene in content.get("scenes", [])
        if isinstance(scene, dict)
        for source_ref in (scene.get("source_refs") or [])
        if str(source_ref).strip()
    }
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    warnings: list[str] = []
    if missing:
        warnings.append("剧本未覆盖原文段落：" + "、".join(missing))
    if unknown:
        warnings.append("剧本引用了不存在的原文段落：" + "、".join(unknown))
    return warnings


_SPATIAL_PAIR_PATTERNS: tuple[tuple[str, str], ...] = (
    ("客厅", "厨房"),
    ("卧室", "客厅"),
    ("玄关", "公共走廊"),
    ("门外", "门内"),
    ("屋外", "屋内"),
    ("室外", "室内"),
    ("店门口", "店内"),
    ("楼外", "大堂"),
)
_DOOR_CROSSING_PATTERN = re.compile(
    r"(?:打开|推开|拉开|解开|开启|开了)[^。！？\n]{0,40}(?:门|锁)"
    r"|(?:门|锁)[^。！？\n]{0,40}(?:打开|推开|拉开|解开|开启|开了)"
)
_ENTER_OTHER_SPACE_PATTERN = re.compile(
    r"(?:走进|进入|跨进|冲进|跑进|店里|店内|屋里|屋内|室内|大堂内)"
)


def screenplay_spatial_issues(content: dict[str, Any]) -> list[str]:
    """识别一个场次中可确定的跨视觉空间动作，避免单场绑定多张场景图。"""
    issues: list[str] = []
    for scene in content.get("scenes", []):
        if not isinstance(scene, dict):
            continue
        action = str(scene.get("action") or "").strip()
        if not action:
            continue
        reasons: list[str] = []
        for left, right in _SPATIAL_PAIR_PATTERNS:
            if left in action and right in action:
                reasons.append(f"同时出现“{left}”与“{right}”")
        door_match = _DOOR_CROSSING_PATTERN.search(action)
        if door_match is not None:
            tail = action[door_match.end() : door_match.end() + 120]
            if _ENTER_OTHER_SPACE_PATTERN.search(tail):
                reasons.append("开门或开锁后进入了另一视觉空间")
        if reasons:
            issues.append(
                f"场次 {scene.get('scene_no')}「{scene.get('scene_name') or '未命名'}」"
                f"疑似跨越多个视觉空间：{'、'.join(dict.fromkeys(reasons))}。"
                "请从空间变化处拆成新场次，并用 transition_to_next 承接动作"
            )
    return issues


def ensure_screenplay_source_coverage(
    content: dict[str, Any],
    source_text: str,
) -> list[str]:
    """确认前补齐唯一原文段落的兼容引用，并返回仍未解决的覆盖问题。"""
    passages = screenplay_source_passages(source_text)
    scenes = content.get("scenes", [])
    has_any_ref = any(
        scene.get("source_refs")
        for scene in scenes
        if isinstance(scene, dict)
    )
    if len(passages) == 1 and not has_any_ref and scenes:
        # 只有一个原文段落时不存在归属歧义，可安全回填；多段原文禁止猜测。
        first_scene = next((scene for scene in scenes if isinstance(scene, dict)), None)
        if first_scene is not None:
            first_scene["source_refs"] = [passages[0]["source_ref"]]
    return validate_screenplay_source_coverage(content, source_text)


def _screenplay_validation_error_detail(exc: ValidationError, *, limit: int = 400) -> str:
    """把 Pydantic 校验错误转成包含字段路径的可读信息，避免向用户暴露裸 500。"""
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error.get("loc", ()))
        message = str(error.get("msg", "")).strip()
        parts.append(f"{location}: {message}" if location else message)
    detail = "剧本内容校验失败：" + "；".join(parts)
    return detail[:limit]


def clean_screenplay_props(content: dict[str, Any]) -> dict[str, Any]:
    """清洗剧本中的 props，确保只保留剧情关键道具，自动剔除普通日常物品与场景固定陈设。"""
    if not isinstance(content, dict):
        return content
    scenes = content.get("scenes")
    if not isinstance(scenes, list):
        return content
    cleaned_scenes: list[dict[str, Any]] = []
    for scene in scenes:
        if isinstance(scene, dict):
            scene_copy = dict(scene)
            if "props" in scene_copy and isinstance(scene_copy["props"], list):
                scene_copy["props"] = filter_critical_props(scene_copy["props"])
            cleaned_scenes.append(scene_copy)
        else:
            cleaned_scenes.append(scene)
    content_copy = dict(content)
    content_copy["scenes"] = cleaned_scenes
    return content_copy


class WritingService:
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
        """章节记录：正文与签名的归属方。"""
        with self.database.engine.connect() as connection:
            record = row_dict(
                connection.execute(select(chapter).where(chapter.c.id == chapter_id)).first()
            )
        if record is None:
            raise not_found("章节")
        return record

    def _scene_asset_names(self, project_id: str) -> list[dict[str, Any]]:
        """项目内已有的场景素材名（含别名），供剧本阶段锚定 scene_name。"""
        return self._project_asset_registry(project_id)["scene"]

    def _project_asset_registry(
        self,
        project_id: str,
        analysis_content: dict[str, Any] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """项目内已有的核心资产基准（角色、场景、道具，含别名），供剧本阶段全面锚定。

        小说正文与素材库是全剧资产的唯一真源。剧本的角色出场、场次命名与道具使用必须
        严格复用此基准，杜绝剧本模型自创别名导致分镜绑定失败。若素材库尚无某类素材，
        优先从小说的结构化分析（main_characters, locations, key_props）中提取基准。
        """
        registry: dict[str, list[dict[str, Any]]] = {
            "character": [],
            "scene": [],
            "prop": [],
        }
        with self.database.engine.connect() as connection:
            for row in connection.execute(
                select(asset.c.asset_type, asset.c.name, asset.c.profile_json).where(
                    asset.c.project_id == project_id
                )
            ):
                if row.asset_type in registry:
                    profile = json_loads(row.profile_json, {})
                    aliases = profile.get("aliases")
                    registry[row.asset_type].append(
                        {
                            "name": row.name,
                            "aliases": aliases if isinstance(aliases, list) else [],
                        }
                    )
        # 若素材库为空，以小说正文分析中的核心资产为基准补充
        if analysis_content and isinstance(analysis_content, dict):
            if not registry["character"] and analysis_content.get("main_characters"):
                for name in analysis_content.get("main_characters") or []:
                    cleaned = str(name or "").strip()
                    if (
                        cleaned
                        and not any(item["name"] == cleaned for item in registry["character"])
                    ):
                        registry["character"].append({"name": cleaned, "aliases": []})
            if not registry["scene"] and analysis_content.get("locations"):
                for name in analysis_content.get("locations") or []:
                    cleaned = str(name or "").strip()
                    if cleaned and not any(item["name"] == cleaned for item in registry["scene"]):
                        registry["scene"].append({"name": cleaned, "aliases": []})
            if not registry["prop"] and analysis_content.get("key_props"):
                for name in analysis_content.get("key_props") or []:
                    cleaned = str(name or "").strip()
                    if (
                        cleaned
                        and is_critical_prop(cleaned)
                        and not any(item["name"] == cleaned for item in registry["prop"])
                    ):
                        registry["prop"].append({"name": cleaned, "aliases": []})
        return registry

    def _content_record(self, table: Any, chapter_id: str) -> dict[str, Any] | None:
        with self.database.engine.connect() as connection:
            record = row_dict(
                connection.execute(select(table).where(table.c.chapter_id == chapter_id)).first()
            )
        if record is None:
            return None
        content = json_loads(record["content_json"], {})
        record["content_json"] = content
        record["content"] = content
        record["error"] = sanitize_error_message(record.get("error", ""))
        return record

    def _empty_record(self, chapter_id: str, *, with_own_signature: bool) -> dict[str, Any]:
        record: dict[str, Any] = {
            "chapter_id": chapter_id,
            "content_json": {},
            "content": {},
            "status": "draft",
            "source_signature": "",
            "revision": 0,
            "generated_by": "",
            "error": "",
            "updated_at": "",
            "is_stale": False,
        }
        if with_own_signature:
            record["own_signature"] = ""
        return record

    def get_analysis(self, chapter_id: str) -> dict[str, Any]:
        current_chapter = self._chapter(chapter_id)
        record = self._content_record(novel_analysis, chapter_id)
        if record is None:
            return self._empty_record(chapter_id, with_own_signature=False)
        record["is_stale"] = bool(
            record["source_signature"]
            and record["source_signature"] != current_chapter["source_signature"]
        )
        return record

    def get_screenplay(self, chapter_id: str) -> dict[str, Any]:
        current_chapter = self._chapter(chapter_id)
        record = self._content_record(screenplay, chapter_id)
        if record is None:
            return self._empty_record(chapter_id, with_own_signature=True)
        record["is_stale"] = bool(
            record["source_signature"]
            and record["source_signature"] != current_chapter["source_signature"]
        )
        return record

    def _save_content(
        self,
        *,
        table: Any,
        chapter_record: dict[str, Any],
        content: dict[str, Any],
        status: str,
        generated_by: str = "",
        error: str = "",
        expected_revision: int | None = None,
        preserve_generating: bool = False,
    ) -> dict[str, Any] | None:
        chapter_id = chapter_record["id"]
        now = utc_now()
        with self.database.engine.begin() as connection:
            existing = row_dict(
                connection.execute(select(table).where(table.c.chapter_id == chapter_id)).first()
            )
            current_revision = existing["revision"] if existing else 0
            if expected_revision is not None and current_revision != expected_revision:
                return None
            if preserve_generating and existing and existing["status"] == "generating":
                # 生成任务在跑时的用户草稿保存：内容照存，状态保持 generating。
                # 否则「生成中」标记被抹成 ready 而任务实际还在跑（H1 残留 /
                # 报告 3.8:210）。生成结果落库走 expected_revision 条件写入，
                # 不经过这个分支；那条路径跳过时由 _revert_generating_status 收尾。
                status = "generating"
            values: dict[str, Any] = {
                "content_json": json_dumps(content),
                "status": status,
                "source_signature": chapter_record["source_signature"],
                "revision": current_revision + 1,
                "generated_by": generated_by,
                "error": sanitize_error_message(error),
                "updated_at": now,
            }
            if "own_signature" in table.c:
                values["own_signature"] = screenplay_signature(content)
            if existing:
                statement = update(table).where(table.c.chapter_id == chapter_id)
                if expected_revision is not None:
                    statement = statement.where(table.c.revision == expected_revision)
                result = connection.execute(statement.values(**values))
                if result.rowcount != 1:
                    # 无期望修订号时目标行在执行前仍存在，命中 0 行说明并发删除
                    # 窗口内内容已消失，必须明确报错而不是静默成功。
                    if expected_revision is not None:
                        return None
                    raise not_found("写作内容")
            else:
                connection.execute(insert(table).values(chapter_id=chapter_id, **values))
        getter = self.get_screenplay if table is screenplay else self.get_analysis
        return getter(chapter_id)

    def _set_status(
        self,
        table: Any,
        chapter_id: str,
        status: str,
        error: str = "",
        *,
        expected_revision: int | None = None,
    ) -> int | None:
        now = utc_now()
        with self.database.engine.begin() as connection:
            existing = row_dict(
                connection.execute(
                    select(table.c.chapter_id, table.c.revision).where(
                        table.c.chapter_id == chapter_id
                    )
                ).first()
            )
            current_revision = existing["revision"] if existing else 0
            if expected_revision is not None and current_revision != expected_revision:
                return None
            if existing:
                statement = update(table).where(table.c.chapter_id == chapter_id)
                if expected_revision is not None:
                    statement = statement.where(table.c.revision == expected_revision)
                result = connection.execute(
                    statement.values(
                        status=status,
                        error=sanitize_error_message(error),
                        updated_at=now,
                    )
                )
                if expected_revision is not None and result.rowcount != 1:
                    return None
                return current_revision
            values: dict[str, Any] = {
                "chapter_id": chapter_id,
                "content_json": "{}",
                "status": status,
                "source_signature": "",
                "revision": 0,
                "generated_by": "",
                "error": sanitize_error_message(error),
                "updated_at": now,
            }
            if "own_signature" in table.c:
                values["own_signature"] = ""
            connection.execute(insert(table).values(**values))
            return current_revision

    def _revert_generating_status(self, table: Any, chapter_id: str) -> None:
        """建任务失败时把 generating 状态回退为 ready（有内容）或 draft（空行）。

        queue_analysis/queue_screenplay 在 jobs.create 抛异常时调用，
        避免内容行永久停在 generating 导致前端一直显示「生成中」。
        """
        now = utc_now()
        with self.database.engine.begin() as connection:
            existing = row_dict(
                connection.execute(
                    select(table.c.chapter_id, table.c.status, table.c.content_json).where(
                        table.c.chapter_id == chapter_id
                    )
                ).first()
            )
            if existing is None or existing["status"] != "generating":
                return
            fallback = (
                "ready"
                if (existing.get("content_json") or "") not in ("", "{}")
                else "draft"
            )
            connection.execute(
                update(table)
                .where(table.c.chapter_id == chapter_id, table.c.status == "generating")
                .values(status=fallback, updated_at=now)
            )

    @staticmethod
    def _expected_revision(
        input_data: dict[str, Any],
        current: dict[str, Any],
    ) -> int | None:
        if "expected_revision" in input_data:
            value = input_data["expected_revision"]
            if type(value) is int and value >= 0:
                return value
            return None
        if current["status"] == "generating":
            return current["revision"]
        return None

    @staticmethod
    def _skipped_result(generated_content: dict[str, Any]) -> dict[str, Any]:
        return {
            "skipped": True,
            "reason": _REVISION_CONFLICT_REASON,
            "generated_content": generated_content,
        }

    def update_analysis(self, chapter_id: str, values: dict[str, Any]) -> dict[str, Any]:
        chapter_record = self._chapter(chapter_id)
        content = (
            values.get("content")
            if values.get("content") is not None
            else values.get("content_json")
        )
        if content is None:
            raise bad_request("必须提供 content 或 content_json")
        NovelAnalysisOutput.model_validate(content)
        saved = self._save_content(
            table=novel_analysis,
            chapter_record=chapter_record,
            content=content,
            status=values.get("status", "ready"),
            preserve_generating=True,
        )
        assert saved is not None
        return saved

    def update_screenplay(self, chapter_id: str, values: dict[str, Any]) -> dict[str, Any]:
        chapter_record = self._chapter(chapter_id)
        content = (
            values.get("content")
            if values.get("content") is not None
            else values.get("content_json")
        )
        if content is None:
            raise bad_request("必须提供 content 或 content_json")
        try:
            validated = ScreenplayOutput.model_validate(content)
        except ValidationError as exc:
            # 用户清空「时间与天气/情绪」等必填字段时应得到 400 与字段路径，
            # 而不是裸 500 且编辑内容丢失。
            raise bad_request(_screenplay_validation_error_detail(exc)) from exc
        content = validated.model_dump(mode="json")
        content = clean_screenplay_props(content)
        status = values.get("status", "ready")
        warnings: list[str] = []
        if status == "confirmed":
            if self.get_screenplay(chapter_id)["status"] == "generating":
                raise conflict("剧本正在生成中，请等待生成完成后再确认")
            warnings = ensure_screenplay_source_coverage(
                content,
                chapter_record["source_text"],
            )
            spatial_warnings = screenplay_spatial_issues(content)
            if spatial_warnings:
                logger.info("确认剧本时发现空间冲突，触发文字模型自动修复：%s", spatial_warnings)
                repaired = self._run_async_safely(
                    self._repair_spatial_issues(
                        chapter_record=chapter_record,
                        content=content,
                        issues=spatial_warnings,
                    )
                )
                if repaired is not None:
                    content = clean_screenplay_props(repaired)
                    warnings = ensure_screenplay_source_coverage(
                        content,
                        chapter_record["source_text"],
                    )
                    spatial_warnings = screenplay_spatial_issues(content)
            warnings.extend(spatial_warnings)
            if warnings:
                raise bad_request("剧本不能确认：" + "；".join(warnings))
        saved = self._save_content(
            table=screenplay,
            chapter_record=chapter_record,
            content=content,
            status=status,
            preserve_generating=True,
        )
        assert saved is not None
        if warnings:
            logger.warning("确认剧本 %s 时存在覆盖警告：%s", chapter_id, "；".join(warnings))
        saved["warnings"] = warnings
        return saved

    def queue_analysis(self, chapter_id: str, options: dict[str, Any]) -> dict[str, Any]:
        chapter_record = self._chapter(chapter_id)
        project_record = self._project(chapter_record["project_id"])
        expected_revision = self._set_status(novel_analysis, chapter_id, "generating")
        assert expected_revision is not None
        try:
            return self.jobs.create(
                project_id=chapter_record["project_id"],
                chapter_id=chapter_id,
                job_type="analyze_novel",
                target_type="analysis",
                target_id=chapter_id,
                provider_kind="text",
                provider_id=options.get("provider_id")
                or project_record["default_text_provider_id"],
                input_data={
                    "expected_revision": expected_revision,
                    "params": options.get("params", {}),
                },
            )
        except Exception:
            # 建任务失败时回置状态，避免内容行永久停在 generating。
            self._revert_generating_status(novel_analysis, chapter_id)
            raise

    def queue_screenplay(self, chapter_id: str, options: dict[str, Any]) -> dict[str, Any]:
        chapter_record = self._chapter(chapter_id)
        project_record = self._project(chapter_record["project_id"])
        expected_analysis_revision = self._set_status(
            novel_analysis,
            chapter_id,
            "generating",
        )
        assert expected_analysis_revision is not None
        expected_revision = self._set_status(screenplay, chapter_id, "generating")
        assert expected_revision is not None
        try:
            return self.jobs.create(
                project_id=chapter_record["project_id"],
                chapter_id=chapter_id,
                job_type="generate_screenplay",
                target_type="screenplay",
                target_id=chapter_id,
                provider_kind="text",
                provider_id=options.get("provider_id")
                or project_record["default_text_provider_id"],
                input_data={
                    "expected_analysis_revision": expected_analysis_revision,
                    "expected_revision": expected_revision,
                    "params": options.get("params", {}),
                },
            )
        except Exception:
            # 建任务失败时回置状态，避免内容行永久停在 generating。
            self._revert_generating_status(novel_analysis, chapter_id)
            self._revert_generating_status(screenplay, chapter_id)
            raise

    def queue_screenplay_scene(self, chapter_id: str, options: dict[str, Any]) -> dict[str, Any]:
        chapter_record = self._chapter(chapter_id)
        project_record = self._project(chapter_record["project_id"])
        current = self.get_screenplay(chapter_id)
        scenes = current["content"].get("scenes", {})
        scene_no = options["scene_no"]
        if not any(scene.get("scene_no") == scene_no for scene in scenes):
            raise not_found("剧本场次")
        expected_revision = self._set_status(screenplay, chapter_id, "generating")
        assert expected_revision is not None
        try:
            return self.jobs.create(
                project_id=chapter_record["project_id"],
                chapter_id=chapter_id,
                job_type="generate_screenplay_scene",
                target_type="screenplay_scene",
                target_id=f"{chapter_id}:{scene_no}",
                provider_kind="text",
                provider_id=options.get("provider_id")
                or project_record["default_text_provider_id"],
                input_data={
                    "expected_revision": expected_revision,
                    "scene_no": scene_no,
                    "instructions": options.get("instructions", ""),
                    "params": options.get("params", {}),
                },
            )
        except Exception:
            # 建任务失败时回置状态，避免内容行永久停在 generating。
            self._revert_generating_status(screenplay, chapter_id)
            raise

    def _provider(self, provider_id: str | None) -> tuple[Any, dict[str, Any]]:
        return self.providers.resolve(kind="text", provider_id=provider_id)

    @staticmethod
    def _run_async_safely(coro: Coroutine[Any, Any, Any]) -> Any:
        """安全在同步上下文中执行异步协程，无论当前线程是否已有 running event loop。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                return executor.submit(lambda: asyncio.run(coro)).result()
        return asyncio.run(coro)

    async def _repair_spatial_issues(
        self,
        chapter_record: dict[str, Any],
        content: dict[str, Any],
        issues: list[str],
        provider_id: str | None = None,
    ) -> dict[str, Any] | None:
        """调用文字模型修复剧本中的跨视觉空间问题（如客厅与厨房混在单场），将冲突场次拆分或收拢。"""
        if not issues:
            return content
        try:
            provider, config = self._provider(provider_id)
        except Exception as exc:
            logger.warning("获取文字模型 Provider 失败，无法执行剧本空间自动修复：%s", exc)
            return None

        source_passages = screenplay_source_passages(chapter_record.get("source_text", ""))
        scene_asset_names = self._scene_asset_names(chapter_record["project_id"])
        user_prompt = (
            "【需要修复的具体视觉空间问题】\n"
            + "\n".join(f"- {issue}" for issue in issues)
            + "\n\n【本章原文段落】\n"
            + json_dumps(source_passages)
            + "\n\n【已有场景素材名称（拆分场次命名必须优先复用）】\n"
            + json_dumps(scene_asset_names)
            + "\n\n【待修复的当前剧本】\n"
            + json_dumps(content)
        )
        try:
            output = await provider.generate_json(
                system_prompt=load_prompt("screenplay_spatial_repair"),
                user_prompt=user_prompt,
                schema=ScreenplayOutput,
                params=config.get("params", {}),
            )
            repaired = output.model_dump(mode="json")
            remaining_issues = screenplay_spatial_issues(repaired)
            if not remaining_issues:
                logger.info("文字模型成功修复剧本空间问题，原问题：%s", issues)
                return repaired
            logger.warning(
                "文字模型修复剧本空间问题后仍有残留（原 %d 个，现 %d 个）：%s",
                len(issues),
                len(remaining_issues),
                remaining_issues,
            )
            if len(remaining_issues) < len(issues):
                return repaired
            return None
        except Exception as exc:
            logger.warning("调用文字模型自动修复剧本空间问题异常：%s", exc)
            return None

    async def _generate_analysis(
        self,
        *,
        provider: Any,
        config: dict[str, Any],
        chapter_record: dict[str, Any],
        params: dict[str, Any],
    ) -> dict[str, Any]:
        output = await provider.generate_json(
            system_prompt=load_prompt("novel_analysis"),
            user_prompt=f"本章正文：\n{chapter_record['source_text']}",
            schema=NovelAnalysisOutput,
            params={**config["params"], **params},
        )
        return output.model_dump(mode="json")

    async def handle_analysis(self, job: dict[str, Any]) -> dict[str, Any]:
        chapter_id = job["chapter_id"]
        input_data = json_loads(job.get("input_json"), {})
        expected_revision: int | None = None
        try:
            current = self.get_analysis(chapter_id)
            expected_revision = self._expected_revision(input_data, current)
            if expected_revision is None or current["revision"] != expected_revision:
                # 用户在生成期间编辑过：本次生成结果不再落库，状态可能仍停在
                # generating，必须收尾，否则前端一直显示「生成中」。
                self._revert_generating_status(novel_analysis, chapter_id)
                return self._skipped_result({})
            chapter_record = self._chapter(chapter_id)
            provider, config = self._provider(job.get("provider_id"))
            result = await self._generate_analysis(
                provider=provider,
                config=config,
                chapter_record=chapter_record,
                params=input_data.get("params", {}),
            )
            saved = (
                self._save_content(
                    table=novel_analysis,
                    chapter_record=chapter_record,
                    content=result,
                    status="ready",
                    generated_by=config["id"],
                    expected_revision=expected_revision,
                )
                if expected_revision is not None
                else None
            )
            if saved is None:
                self._revert_generating_status(novel_analysis, chapter_id)
                return self._skipped_result(result)
            return {"content": result}
        except Exception as exc:
            if expected_revision is not None:
                self._set_status(
                    novel_analysis,
                    chapter_id,
                    "failed",
                    str(exc),
                    expected_revision=expected_revision,
                )
            raise

    async def handle_screenplay(self, job: dict[str, Any]) -> dict[str, Any]:
        chapter_id = job["chapter_id"]
        input_data = json_loads(job.get("input_json"), {})
        expected_revision: int | None = None
        expected_analysis_revision: int | None = None
        raw_analysis_revision = input_data.get("expected_analysis_revision")
        if type(raw_analysis_revision) is int and raw_analysis_revision >= 0:
            expected_analysis_revision = raw_analysis_revision
        try:
            current = self.get_screenplay(chapter_id)
            expected_revision = self._expected_revision(input_data, current)
            if expected_revision is None or current["revision"] != expected_revision:
                # 剧本与分析行在入队时都被置为 generating；这里统一收尾，
                # 避免分析行因修订号不匹配而永久停在「生成中」。
                self._revert_generating_status(novel_analysis, chapter_id)
                self._revert_generating_status(screenplay, chapter_id)
                return self._skipped_result({})
            chapter_record = self._chapter(chapter_id)
            provider, config = self._provider(job.get("provider_id"))
            if expected_analysis_revision is None:
                expected_analysis_revision = self._set_status(
                    novel_analysis,
                    chapter_id,
                    "generating",
                )
            analysis_result = await self._generate_analysis(
                provider=provider,
                config=config,
                chapter_record=chapter_record,
                params=input_data.get("params", {}),
            )
            saved_analysis = self._save_content(
                table=novel_analysis,
                chapter_record=chapter_record,
                content=analysis_result,
                status="ready",
                generated_by=config["id"],
                expected_revision=expected_analysis_revision,
            )
            if saved_analysis is None:
                # 分析结果被用户编辑顶掉：analysis 可能仍停在 generating，
                # 而剧本流程后续不会再写它，这里收尾避免状态卡死。
                self._revert_generating_status(novel_analysis, chapter_id)
            analysis_content = (
                saved_analysis["content"]
                if saved_analysis is not None
                else self.get_analysis(chapter_id)["content"]
            )
            asset_registry = self._project_asset_registry(
                chapter_record["project_id"],
                analysis_content=analysis_content,
            )
            source_passages = screenplay_source_passages(chapter_record["source_text"])
            warnings: list[str] = []
            existing_scene_assets = self._scene_asset_names(chapter_record["project_id"])
            if not existing_scene_assets:
                warnings.append(
                    "项目尚无场景素材：剧本生成的场次名无法与素材库锚定，"
                    "分镜提取时可能因场景绑定失败。建议先提取场景素材，"
                    "再重新生成剧本（或逐场重写）。"
                )
            user_prompt = (
                "本章原文已按可追踪段落编号；每个场次必须填写 source_refs，"
                "全部 P 编号至少覆盖一次：\n"
                f"{json_dumps(source_passages)}\n\n"
                f"本次小说分析：\n{json_dumps(analysis_content)}\n\n"
                "【小说正文确立的核心视觉资产基准（全剧唯一规范，剧本必须严格对齐，严禁改写或自创新名）】：\n"
                f"- 核心角色清单（出场人物必须严格复用这些名称）：\n"
                f"{json_dumps(asset_registry['character'])}\n"
                f"- 核心场景清单/已有场景素材名称（scene_name 必须优先复用，不得改写）：\n"
                f"{json_dumps(asset_registry['scene'])}\n"
                f"- 核心关键道具清单（props 严格只能填写这里的关键道具，无时为空数组）：\n"
                f"{json_dumps(asset_registry['prop'])}"
            )
            output = await provider.generate_json(
                system_prompt=load_prompt("screenplay"),
                user_prompt=user_prompt,
                schema=ScreenplayOutput,
                params={**config["params"], **input_data.get("params", {})},
            )
            result = output.model_dump(mode="json")
            result = clean_screenplay_props(result)
            spatial_issues = screenplay_spatial_issues(result)
            if spatial_issues:
                logger.info("剧本初次生成检测到空间问题，触发自动修复：%s", spatial_issues)
                repaired = await self._repair_spatial_issues(
                    chapter_record=chapter_record,
                    content=result,
                    issues=spatial_issues,
                    provider_id=job.get("provider_id"),
                )
                if repaired is not None:
                    result = clean_screenplay_props(repaired)
            warnings.extend(
                validate_screenplay_source_coverage(
                    result,
                    chapter_record["source_text"],
                )
            )
            warnings.extend(screenplay_spatial_issues(result))
            saved = (
                self._save_content(
                    table=screenplay,
                    chapter_record=chapter_record,
                    content=result,
                    status="ready",
                    generated_by=config["id"],
                    expected_revision=expected_revision,
                )
                if expected_revision is not None
                else None
            )
            if saved is None:
                self._revert_generating_status(screenplay, chapter_id)
                return self._skipped_result(result)
            return {
                "analysis": analysis_result,
                "content": result,
                "warnings": warnings,
            }
        except Exception as exc:
            if expected_analysis_revision is not None:
                self._set_status(
                    novel_analysis,
                    chapter_id,
                    "failed",
                    str(exc),
                    expected_revision=expected_analysis_revision,
                )
            if expected_revision is not None:
                self._set_status(
                    screenplay,
                    chapter_id,
                    "failed",
                    str(exc),
                    expected_revision=expected_revision,
                )
            raise

    async def handle_screenplay_scene(self, job: dict[str, Any]) -> dict[str, Any]:
        chapter_id = job["chapter_id"]
        input_data = json_loads(job.get("input_json"), {})
        expected_revision: int | None = None
        try:
            current = self.get_screenplay(chapter_id)
            expected_revision = self._expected_revision(input_data, current)
            if expected_revision is None or current["revision"] != expected_revision:
                self._revert_generating_status(screenplay, chapter_id)
                return self._skipped_result({})
            chapter_record = self._chapter(chapter_id)
            provider, config = self._provider(job.get("provider_id"))
            source_passages = screenplay_source_passages(chapter_record["source_text"])
            asset_registry = self._project_asset_registry(chapter_record["project_id"])
            user_prompt = (
                "本章原文按段落编号如下；重生成场次的 source_refs 只能使用这些编号：\n"
                f"{json_dumps(source_passages)}\n\n"
                f"完整剧本：\n{json_dumps(current['content'])}\n\n"
                "【小说正文确立的核心视觉资产基准（全剧唯一规范，剧本必须严格对齐，严禁改写或自创新名）】：\n"
                f"- 核心角色清单：\n{json_dumps(asset_registry['character'])}\n"
                f"- 核心场景清单/已有场景素材名称（scene_name 必须严格优先复用，不得改写）：\n"
                f"{json_dumps(asset_registry['scene'])}\n"
                f"- 核心关键道具清单（props 严格只能填写这里的关键道具，无时为空数组）：\n"
                f"{json_dumps(asset_registry['prop'])}\n\n"
                f"目标场次：{input_data['scene_no']}\n"
                f"补充要求：{input_data.get('instructions', '')}"
            )
            output = await provider.generate_json(
                system_prompt=load_prompt("screenplay_scene"),
                user_prompt=user_prompt,
                schema=ScreenplayScene,
                params={**config["params"], **input_data.get("params", {})},
            )
            generated_scene = output.model_dump(mode="json")
            if "props" in generated_scene and isinstance(generated_scene["props"], list):
                generated_scene["props"] = filter_critical_props(generated_scene["props"])
            generated_scene["scene_no"] = input_data["scene_no"]
            content = dict(current["content"])
            content["scenes"] = [
                generated_scene if scene.get("scene_no") == input_data["scene_no"] else scene
                for scene in content["scenes"]
            ]
            content = ScreenplayOutput.model_validate(content).model_dump(mode="json")
            content = clean_screenplay_props(content)
            spatial_issues = screenplay_spatial_issues(content)
            if spatial_issues:
                logger.info("单场生成后检测到空间问题，触发自动修复：%s", spatial_issues)
                repaired = await self._repair_spatial_issues(
                    chapter_record=chapter_record,
                    content=content,
                    issues=spatial_issues,
                    provider_id=job.get("provider_id"),
                )
                if repaired is not None:
                    content = clean_screenplay_props(repaired)
            warnings = validate_screenplay_source_coverage(
                content,
                chapter_record["source_text"],
            )
            warnings.extend(screenplay_spatial_issues(content))
            saved = (
                self._save_content(
                    table=screenplay,
                    chapter_record=chapter_record,
                    content=content,
                    status="ready",
                    generated_by=config["id"],
                    expected_revision=expected_revision,
                )
                if expected_revision is not None
                else None
            )
            if saved is None:
                self._revert_generating_status(screenplay, chapter_id)
                return self._skipped_result(content)
            return {"scene": generated_scene, "warnings": warnings}
        except Exception as exc:
            if expected_revision is not None:
                self._set_status(
                    screenplay,
                    chapter_id,
                    "failed",
                    str(exc),
                    expected_revision=expected_revision,
                )
            raise
