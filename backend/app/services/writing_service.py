from typing import Any

from sqlalchemy import insert, select, update

from app.db import Database, db
from app.models.tables import asset, chapter, novel_analysis, project, screenplay
from app.prompts import load_prompt
from app.schemas import (
    NovelAnalysisOutput,
    ScreenplayOutput,
    ScreenplayScene,
)
from app.services.errors import bad_request, not_found
from app.services.job_service import JobService
from app.services.provider_service import ProviderService
from app.services.signature_service import screenplay_signature
from app.services.utils import (
    json_dumps,
    json_loads,
    row_dict,
    sanitize_error_message,
    utc_now,
)

_REVISION_CONFLICT_REASON = "内容在生成期间已被编辑，生成结果未覆盖当前版本"


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
        """项目内已有的场景素材名（含别名），供剧本阶段锚定 scene_name。

        剧本的 scene_name 必须与场景素材同名，否则分镜保存时按名称绑定素材
        会失败。素材提取读的是章节正文、剧本生成读的是正文+分析，两步各自
        命名必然对不齐，因此这里把已有场景名显式喂给剧本模型。
        """
        names: list[dict[str, Any]] = []
        with self.database.engine.connect() as connection:
            for row in connection.execute(
                select(asset.c.name, asset.c.profile_json).where(
                    asset.c.project_id == project_id,
                    asset.c.asset_type == "scene",
                )
            ):
                profile = json_loads(row.profile_json, {})
                aliases = profile.get("aliases")
                names.append(
                    {
                        "name": row.name,
                        "aliases": aliases if isinstance(aliases, list) else [],
                    }
                )
        return names

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
                if expected_revision is not None and result.rowcount != 1:
                    return None
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
        ScreenplayOutput.model_validate(content)
        saved = self._save_content(
            table=screenplay,
            chapter_record=chapter_record,
            content=content,
            status=values.get("status", "ready"),
        )
        assert saved is not None
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
                if expected_analysis_revision is not None:
                    analysis = self.get_analysis(chapter_id)
                    self._set_status(
                        novel_analysis,
                        chapter_id,
                        "ready" if analysis["content"] else "draft",
                        expected_revision=expected_analysis_revision,
                    )
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
            analysis_content = (
                saved_analysis["content"]
                if saved_analysis is not None
                else self.get_analysis(chapter_id)["content"]
            )
            scene_asset_names = self._scene_asset_names(chapter_record["project_id"])
            warnings: list[str] = []
            if not scene_asset_names:
                warnings.append(
                    "项目尚无场景素材：剧本生成的场次名无法与素材库锚定，"
                    "分镜提取时可能因场景绑定失败。建议先提取场景素材，"
                    "再重新生成剧本（或逐场重写）。"
                )
            user_prompt = (
                f"本章正文：\n{chapter_record['source_text']}\n\n"
                f"本次小说分析：\n{json_dumps(analysis_content)}\n\n"
                f"已有场景素材名称（scene_name 必须优先复用，不得改写）：\n"
                f"{json_dumps(scene_asset_names)}"
            )
            output = await provider.generate_json(
                system_prompt=load_prompt("screenplay"),
                user_prompt=user_prompt,
                schema=ScreenplayOutput,
                params={**config["params"], **input_data.get("params", {})},
            )
            result = output.model_dump(mode="json")
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
                return self._skipped_result({})
            chapter_record = self._chapter(chapter_id)
            provider, config = self._provider(job.get("provider_id"))
            user_prompt = (
                f"本章正文：\n{chapter_record['source_text']}\n\n"
                f"完整剧本：\n{json_dumps(current['content'])}\n\n"
                f"已有场景素材名称（scene_name 必须优先复用，不得改写）：\n"
                f"{json_dumps(self._scene_asset_names(chapter_record['project_id']))}\n\n"
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
            generated_scene["scene_no"] = input_data["scene_no"]
            content = dict(current["content"])
            content["scenes"] = [
                generated_scene if scene.get("scene_no") == input_data["scene_no"] else scene
                for scene in content["scenes"]
            ]
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
                return self._skipped_result(content)
            return {"scene": generated_scene}
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
