from typing import Any

from sqlalchemy import insert, select, update

from app.db import Database, db
from app.models.tables import novel_analysis, project, screenplay
from app.prompts import load_prompt
from app.schemas import (
    NovelAnalysisOutput,
    ScreenplayOutput,
    ScreenplayScene,
    VisualStyleOutput,
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

    def _content_record(self, table: Any, project_id: str) -> dict[str, Any] | None:
        with self.database.engine.connect() as connection:
            record = row_dict(
                connection.execute(select(table).where(table.c.project_id == project_id)).first()
            )
        if record is None:
            return None
        content = json_loads(record["content_json"], {})
        record["content_json"] = content
        record["content"] = content
        record["error"] = sanitize_error_message(record.get("error", ""))
        return record

    def _empty_record(self, project_id: str, *, with_own_signature: bool) -> dict[str, Any]:
        record: dict[str, Any] = {
            "project_id": project_id,
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

    def get_analysis(self, project_id: str) -> dict[str, Any]:
        current_project = self._project(project_id)
        record = self._content_record(novel_analysis, project_id)
        if record is None:
            return self._empty_record(project_id, with_own_signature=False)
        record["is_stale"] = bool(
            record["source_signature"]
            and record["source_signature"] != current_project["source_signature"]
        )
        return record

    def get_screenplay(self, project_id: str) -> dict[str, Any]:
        current_project = self._project(project_id)
        record = self._content_record(screenplay, project_id)
        if record is None:
            return self._empty_record(project_id, with_own_signature=True)
        record["is_stale"] = bool(
            record["source_signature"]
            and record["source_signature"] != current_project["source_signature"]
        )
        return record

    def _save_content(
        self,
        *,
        table: Any,
        project_record: dict[str, Any],
        content: dict[str, Any],
        status: str,
        generated_by: str = "",
        error: str = "",
        expected_revision: int | None = None,
    ) -> dict[str, Any] | None:
        project_id = project_record["id"]
        now = utc_now()
        with self.database.engine.begin() as connection:
            existing = row_dict(
                connection.execute(select(table).where(table.c.project_id == project_id)).first()
            )
            current_revision = existing["revision"] if existing else 0
            if expected_revision is not None and current_revision != expected_revision:
                return None
            values: dict[str, Any] = {
                "content_json": json_dumps(content),
                "status": status,
                "source_signature": project_record["source_signature"],
                "revision": current_revision + 1,
                "generated_by": generated_by,
                "error": sanitize_error_message(error),
                "updated_at": now,
            }
            if "own_signature" in table.c:
                values["own_signature"] = screenplay_signature(content)
            if existing:
                statement = update(table).where(table.c.project_id == project_id)
                if expected_revision is not None:
                    statement = statement.where(table.c.revision == expected_revision)
                result = connection.execute(statement.values(**values))
                if expected_revision is not None and result.rowcount != 1:
                    return None
            else:
                connection.execute(insert(table).values(project_id=project_id, **values))
        getter = self.get_screenplay if table is screenplay else self.get_analysis
        return getter(project_id)

    def _set_status(
        self,
        table: Any,
        project_id: str,
        status: str,
        error: str = "",
        *,
        expected_revision: int | None = None,
    ) -> int | None:
        now = utc_now()
        with self.database.engine.begin() as connection:
            existing = row_dict(
                connection.execute(
                    select(table.c.project_id, table.c.revision).where(
                        table.c.project_id == project_id
                    )
                ).first()
            )
            current_revision = existing["revision"] if existing else 0
            if expected_revision is not None and current_revision != expected_revision:
                return None
            if existing:
                statement = update(table).where(table.c.project_id == project_id)
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
                "project_id": project_id,
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

    def update_analysis(self, project_id: str, values: dict[str, Any]) -> dict[str, Any]:
        project_record = self._project(project_id)
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
            project_record=project_record,
            content=content,
            status=values.get("status", "ready"),
        )
        assert saved is not None
        return saved

    def update_screenplay(self, project_id: str, values: dict[str, Any]) -> dict[str, Any]:
        project_record = self._project(project_id)
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
            project_record=project_record,
            content=content,
            status=values.get("status", "ready"),
        )
        assert saved is not None
        return saved

    def queue_analysis(self, project_id: str, options: dict[str, Any]) -> dict[str, Any]:
        project_record = self._project(project_id)
        expected_revision = self._set_status(novel_analysis, project_id, "generating")
        assert expected_revision is not None
        return self.jobs.create(
            project_id=project_id,
            job_type="analyze_novel",
            target_type="analysis",
            target_id=project_id,
            provider_kind="text",
            provider_id=options.get("provider_id") or project_record["default_text_provider_id"],
            input_data={
                "expected_revision": expected_revision,
                "params": options.get("params", {}),
            },
        )

    def queue_screenplay(self, project_id: str, options: dict[str, Any]) -> dict[str, Any]:
        project_record = self._project(project_id)
        expected_analysis_revision = self._set_status(
            novel_analysis,
            project_id,
            "generating",
        )
        assert expected_analysis_revision is not None
        expected_revision = self._set_status(screenplay, project_id, "generating")
        assert expected_revision is not None
        return self.jobs.create(
            project_id=project_id,
            job_type="generate_screenplay",
            target_type="screenplay",
            target_id=project_id,
            provider_kind="text",
            provider_id=options.get("provider_id") or project_record["default_text_provider_id"],
            input_data={
                "expected_analysis_revision": expected_analysis_revision,
                "expected_revision": expected_revision,
                "expected_project_updated_at": project_record["updated_at"],
                "expected_source_signature": project_record["source_signature"],
                "expected_style_prompt": project_record["style_prompt"],
                "params": options.get("params", {}),
            },
        )

    def queue_screenplay_scene(self, project_id: str, options: dict[str, Any]) -> dict[str, Any]:
        project_record = self._project(project_id)
        current = self.get_screenplay(project_id)
        scenes = current["content"].get("scenes", {})
        scene_no = options["scene_no"]
        if not any(scene.get("scene_no") == scene_no for scene in scenes):
            raise not_found("剧本场次")
        expected_revision = self._set_status(screenplay, project_id, "generating")
        assert expected_revision is not None
        return self.jobs.create(
            project_id=project_id,
            job_type="generate_screenplay_scene",
            target_type="screenplay_scene",
            target_id=f"{project_id}:{scene_no}",
            provider_kind="text",
            provider_id=options.get("provider_id") or project_record["default_text_provider_id"],
            input_data={
                "expected_revision": expected_revision,
                "scene_no": scene_no,
                "instructions": options.get("instructions", ""),
                "params": options.get("params", {}),
            },
        )

    def _provider(self, provider_id: str | None) -> tuple[Any, dict[str, Any]]:
        return self.providers.resolve(kind="text", provider_id=provider_id)

    async def _generate_analysis(
        self,
        *,
        provider: Any,
        config: dict[str, Any],
        project_record: dict[str, Any],
        params: dict[str, Any],
    ) -> dict[str, Any]:
        output = await provider.generate_json(
            system_prompt=load_prompt("novel_analysis"),
            user_prompt=f"小说正文：\n{project_record['source_text']}",
            schema=NovelAnalysisOutput,
            params={**config["params"], **params},
        )
        return output.model_dump(mode="json")

    async def _generate_visual_style(
        self,
        *,
        provider: Any,
        config: dict[str, Any],
        project_record: dict[str, Any],
        params: dict[str, Any],
    ) -> str:
        output = await provider.generate_json(
            system_prompt=load_prompt("visual_style"),
            user_prompt=load_prompt("visual_style_user").replace(
                "{{在这里粘贴小说正文、剧本、故事梗概、项目设定或原始画风描述}}",
                project_record["source_text"],
            ),
            schema=VisualStyleOutput,
            params={**config["params"], **params},
        )
        return output.style_prompt

    def _update_style_prompt_if_unchanged(
        self,
        project_id: str,
        *,
        expected_project_updated_at: str,
        expected_source_signature: str,
        expected_style_prompt: str,
        style_prompt: str,
    ) -> bool:
        with self.database.engine.begin() as connection:
            result = connection.execute(
                update(project)
                .where(
                    project.c.id == project_id,
                    project.c.updated_at == expected_project_updated_at,
                    project.c.source_signature == expected_source_signature,
                    project.c.style_prompt == expected_style_prompt,
                )
                .values(style_prompt=style_prompt, updated_at=utc_now())
            )
        return result.rowcount == 1

    async def handle_analysis(self, job: dict[str, Any]) -> dict[str, Any]:
        project_id = job["project_id"]
        input_data = json_loads(job.get("input_json"), {})
        expected_revision: int | None = None
        try:
            current = self.get_analysis(project_id)
            expected_revision = self._expected_revision(input_data, current)
            if expected_revision is None or current["revision"] != expected_revision:
                return self._skipped_result({})
            project_record = self._project(project_id)
            provider, config = self._provider(job.get("provider_id"))
            result = await self._generate_analysis(
                provider=provider,
                config=config,
                project_record=project_record,
                params=input_data.get("params", {}),
            )
            saved = (
                self._save_content(
                    table=novel_analysis,
                    project_record=project_record,
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
                    project_id,
                    "failed",
                    str(exc),
                    expected_revision=expected_revision,
                )
            raise

    async def handle_screenplay(self, job: dict[str, Any]) -> dict[str, Any]:
        project_id = job["project_id"]
        input_data = json_loads(job.get("input_json"), {})
        expected_revision: int | None = None
        expected_analysis_revision: int | None = None
        raw_analysis_revision = input_data.get("expected_analysis_revision")
        if type(raw_analysis_revision) is int and raw_analysis_revision >= 0:
            expected_analysis_revision = raw_analysis_revision
        try:
            current = self.get_screenplay(project_id)
            expected_revision = self._expected_revision(input_data, current)
            if expected_revision is None or current["revision"] != expected_revision:
                if expected_analysis_revision is not None:
                    analysis = self.get_analysis(project_id)
                    self._set_status(
                        novel_analysis,
                        project_id,
                        "ready" if analysis["content"] else "draft",
                        expected_revision=expected_analysis_revision,
                    )
                return self._skipped_result({})
            project_record = self._project(project_id)
            provider, config = self._provider(job.get("provider_id"))
            if expected_analysis_revision is None:
                expected_analysis_revision = self._set_status(
                    novel_analysis,
                    project_id,
                    "generating",
                )
            analysis_result = await self._generate_analysis(
                provider=provider,
                config=config,
                project_record=project_record,
                params=input_data.get("params", {}),
            )
            saved_analysis = self._save_content(
                table=novel_analysis,
                project_record=project_record,
                content=analysis_result,
                status="ready",
                generated_by=config["id"],
                expected_revision=expected_analysis_revision,
            )
            analysis_content = (
                saved_analysis["content"]
                if saved_analysis is not None
                else self.get_analysis(project_id)["content"]
            )
            expected_project_updated_at = input_data.get("expected_project_updated_at")
            expected_source_signature = input_data.get("expected_source_signature")
            expected_style_prompt = input_data.get("expected_style_prompt")
            style_prompt = ""
            style_prompt_updated = False
            style_prompt_error = ""
            try:
                style_prompt = await self._generate_visual_style(
                    provider=provider,
                    config=config,
                    project_record=project_record,
                    params=input_data.get("params", {}),
                )
                if (
                    isinstance(expected_project_updated_at, str)
                    and isinstance(expected_source_signature, str)
                    and isinstance(expected_style_prompt, str)
                ):
                    style_prompt_updated = self._update_style_prompt_if_unchanged(
                        project_id,
                        expected_project_updated_at=expected_project_updated_at,
                        expected_source_signature=expected_source_signature,
                        expected_style_prompt=expected_style_prompt,
                        style_prompt=style_prompt,
                    )
            except Exception as exc:
                style_prompt_error = sanitize_error_message(str(exc))
            user_prompt = (
                f"小说正文：\n{project_record['source_text']}\n\n"
                f"本次小说分析：\n{json_dumps(analysis_content)}"
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
                    project_record=project_record,
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
                "style_prompt": style_prompt,
                "style_prompt_updated": style_prompt_updated,
                "style_prompt_error": style_prompt_error,
                "content": result,
            }
        except Exception as exc:
            if expected_analysis_revision is not None:
                self._set_status(
                    novel_analysis,
                    project_id,
                    "failed",
                    str(exc),
                    expected_revision=expected_analysis_revision,
                )
            if expected_revision is not None:
                self._set_status(
                    screenplay,
                    project_id,
                    "failed",
                    str(exc),
                    expected_revision=expected_revision,
                )
            raise

    async def handle_screenplay_scene(self, job: dict[str, Any]) -> dict[str, Any]:
        project_id = job["project_id"]
        input_data = json_loads(job.get("input_json"), {})
        expected_revision: int | None = None
        try:
            current = self.get_screenplay(project_id)
            expected_revision = self._expected_revision(input_data, current)
            if expected_revision is None or current["revision"] != expected_revision:
                return self._skipped_result({})
            project_record = self._project(project_id)
            provider, config = self._provider(job.get("provider_id"))
            user_prompt = (
                f"小说正文：\n{project_record['source_text']}\n\n"
                f"完整剧本：\n{json_dumps(current['content'])}\n\n"
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
                    project_record=project_record,
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
                    project_id,
                    "failed",
                    str(exc),
                    expected_revision=expected_revision,
                )
            raise
