from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, insert, select, update

from app.db import Database, migrate_schema, utc_now
from app.dynamic_pipeline.schemas import CharacterBiosOutput, RhythmOutput, SourceAnalysisOutput
from app.dynamic_pipeline.stage_service import (
    DYNAMIC_STAGE_KEYS,
    DynamicStageService,
    StageExecutionError,
)
from app.models.tables import api_provider, chapter, job, project, workflow_run, workflow_stage
from app.services.signature_service import calculate_signature


def _run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def _meta() -> dict[str, Any]:
    return {
        "schema_version": "provider-schema",
        "normalizer_version": "provider-normalizer",
        "input_signature": "provider-input",
        "content_signature": "provider-content",
        "source_attempt": 1,
    }


def _source_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "artifact_meta": _meta(),
        "metadata": {
            "source_kind": "full_text",
            "title": "测试故事",
            "chapter_title": "第一章",
            "source_signature": "source-signature",
            "target_duration_sec": 30,
            "aspect_ratio": "9:16",
        },
        "episode_logline": "主角在旧屋中发现关键线索。",
        "scenes": [
            {
                "scene_id": "scene-1",
                "ordinal": 1,
                "location": "旧屋",
                "time_weather": "夜，阴",
                "characters": [],
                "narrative_task": "建立线索",
                "plot_event": "主角发现线索",
                "conflict_or_turn": "线索引出新的阻碍",
                "visual_focus": "主角拿起发光物品",
                "estimated_duration_sec": 30,
                "shootability": {
                    "status": "shootable",
                    "visible_action": "主角拿起发光物品",
                    "required_asset_keys": [],
                    "production_risks": [],
                    "adaptation_note": None,
                },
                "source_refs": [],
            }
        ],
        "character_roster": [],
        "conflict_turns": [],
        "visualizable_content": ["主角拿起发光物品"],
        "hard_to_visualize": [],
        "rewrite_requirements": [],
        "assumptions": [],
        "open_questions": [],
        "continuation_outline": [],
    }
    payload.update(overrides)
    return payload


def _character_payload() -> dict[str, Any]:
    return {
        "artifact_meta": _meta(),
        "characters": [
            {
                "basic_info": {
                    "character_key": "hero",
                    "canonical_name": "主角",
                    "aliases": [],
                    "importance": "core",
                    "identity": "线索追寻者",
                    "age": "成年",
                    "gender": "未设定",
                },
                "background": "主角一直在寻找失踪的线索。",
                "personality_behavior_logic": "先观察，再用明确动作回应危险。",
                "dynamic_comic_setting": "行动时保持稳定轮廓和服装。",
                "plot_relation": "当前冲突的承受者与解决者。",
                "visual_anchor": {
                    "visual_facts": ["黑色短发"],
                    "face": "清晰的面部轮廓",
                    "hair": "黑色短发",
                    "body": "身形匀称",
                    "costume": "深色外套",
                    "accessories": [],
                    "immutable_features": ["黑色短发"],
                    "forbidden_changes": ["不得改变发型"],
                    "makeup_keyword_string": "清晰面部轮廓|黑色短发|深色外套",
                    "variants": [],
                    "current_variant": None,
                    "approved_reference_ids": [],
                },
                "behavior_evidence": {
                    "trigger": "发现发光物品",
                    "action": "抬手拿起物品",
                    "expression": "目光专注",
                    "voice": "短促吸气",
                    "source_refs": ["scene-1"],
                },
            }
        ],
        "ensemble_anchors": [],
        "unresolved_character_refs": [],
    }


def _rhythm_payload() -> dict[str, Any]:
    event = {
        "event_id": "dynamic-1",
        "summary": "主角举起线索并作出决定",
        "segment_id": "segment-1",
        "dynamic_level": 1.0,
        "visual_action": "主角举起线索并作出决定",
        "source_refs": [],
    }
    return {
        "artifact_meta": _meta(),
        "target_duration_sec": 30,
        "timeline": [
            {
                "segment_id": "segment-1",
                "ordinal": 1,
                "start_sec": 0,
                "end_sec": 30,
                "beat_type": "payoff",
                "narrative_task": "建立冲突并完成回应",
                "event_summary": "主角举起线索并作出决定",
                "visual_action": "主角举起线索并作出决定",
                "focus_characters": ["hero"],
                "dynamic_level": 0.8,
                "strong_dynamic_event_id": "dynamic-1",
                "source_refs": [],
            }
        ],
        "core_payoff": "主角完成关键回应",
        "small_beats": [],
        "strong_dynamic_budget": {
            "max_events": 1,
            "event_ids": ["dynamic-1"],
            "events": [event],
        },
        "foreshadowing": ["线索将在后续内容中继续发挥作用"],
        "open_space": ["保留动机揭示空间"],
        "dialogue_suggestions": [],
        "sound_suggestions": [],
        "assumptions": ["目标时长为 30 秒"],
    }


class FakeTextProvider:
    def __init__(self, payloads: dict[str, dict[str, Any]], error: Exception | None = None) -> None:
        self.payloads = payloads
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: type[Any],
        params: dict[str, Any],
    ) -> Any:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "schema": schema,
                "params": params,
            }
        )
        if self.error is not None:
            raise self.error
        key = {
            SourceAnalysisOutput: "0-source",
            CharacterBiosOutput: "1-character-bios",
            RhythmOutput: "2-rhythm",
        }[schema]
        return schema.model_validate(copy.deepcopy(self.payloads[key]))


class FakeProviderService:
    def __init__(self, provider: FakeTextProvider) -> None:
        self.provider = provider
        self.resolve_calls: list[dict[str, Any]] = []
        self.config = {
            "id": "fake-text-provider",
            "model": "fake-model-v1",
            "params": {"temperature": 0.2, "provider_only": True},
        }

    def resolve(self, *, kind: str, provider_id: str | None) -> tuple[Any, dict[str, Any]]:
        self.resolve_calls.append({"kind": kind, "provider_id": provider_id})
        return self.provider, self.config


@pytest.fixture
def stage_context(tmp_path: Path):
    database = Database(f"sqlite:///{(tmp_path / 'stage-service.db').as_posix()}")
    migrate_schema(database)
    now = utc_now()
    source_snapshot = {
        "source_kind": "full_text",
        "text": "SNAPSHOT_SOURCE_TEXT",
        "title": "测试故事",
        "chapter_title": "第一章",
        "source_signature": "source-signature",
        "aspect_ratio": "9:16",
    }
    config_snapshot = {
        "target_duration_sec": 30,
        "strong_dynamic_max_events": 3,
        "aspect_ratio": "9:16",
        "style_prompt": "测试风格",
    }
    with database.engine.begin() as connection:
        connection.execute(
            insert(api_provider).values(
                id="job-provider",
                kind="text",
                name="测试文字 Provider",
                adapter="mock",
                base_url="",
                api_key_env="",
                model="test-model",
                params_json="{}",
                timeout_sec=120,
                max_retries=0,
                enabled=1,
                is_default=0,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(project).values(
                id="project-stage-service",
                name="阶段服务项目",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(chapter).values(
                id="chapter-stage-service",
                project_id="project-stage-service",
                chapter_no=1,
                title="第一章",
                source_text="CHAPTER_SOURCE_TEXT_MUST_NOT_ENTER_PROMPT",
                source_signature="chapter-source-signature",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(workflow_run).values(
                id="run-stage-service",
                project_id="project-stage-service",
                chapter_id="chapter-stage-service",
                kind="dynamic_comic_0_6",
                schema_version="1",
                status="queued",
                current_stage="0-source",
                source_kind="full_text",
                source_snapshot_json=json.dumps(source_snapshot, ensure_ascii=False),
                config_snapshot_json=json.dumps(config_snapshot, ensure_ascii=False),
                asset_mapping_json="[]",
                input_signature="stage-signature",
                revision=1,
                error="",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(workflow_stage),
            [
                {
                    "id": f"stage-{index}",
                    "workflow_run_id": "run-stage-service",
                    "stage_key": stage_key,
                    "ordinal": index,
                    "status": "pending",
                    "input_signature": "stage-signature",
                    "raw_output_json": "{}",
                    "normalized_output_json": "{}",
                    "draft_output_json": "{}",
                    "output_refs_json": "[]",
                    "gate_result_json": "{}",
                    "attempt": 1,
                    "error": "",
                    "created_at": now,
                    "updated_at": now,
                }
                for index, stage_key in enumerate(
                    (
                        "0-source",
                        "1-character-bios",
                        "2-rhythm",
                        "3-script",
                        "4-storyboard",
                        "5a-image-prompts",
                        "5b-video-prompts",
                        "6-delivery",
                    )
                )
            ],
        )
    try:
        yield database
    finally:
        database.dispose()


def _set_run_stage(
    database: Database,
    stage_key: str,
    *,
    run_status: str = "queued",
    stage_status: str = "queued",
    normalized_output: dict[str, Any] | None = None,
    stage_signature: str = "stage-signature",
) -> str:
    stage_id = f"stage-{DYNAMIC_STAGE_KEYS.index(stage_key)}"
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            update(workflow_run)
            .where(workflow_run.c.id == "run-stage-service")
            .values(status=run_status, current_stage=stage_key, updated_at=now)
        )
        connection.execute(
            update(workflow_stage)
            .where(workflow_stage.c.id == stage_id)
            .values(
                status=stage_status,
                normalized_output_json=json.dumps(normalized_output or {}, ensure_ascii=False),
                updated_at=now,
                input_signature=stage_signature,
            )
        )
    return stage_id


def _insert_job(
    database: Database,
    stage_key: str,
    *,
    status: str = "queued",
    input_signature: str | None = "stage-signature",
    provider_id: str | None = None,
    params: dict[str, Any] | None = None,
    job_suffix: str = "",
) -> dict[str, Any]:
    stage_id = f"stage-{DYNAMIC_STAGE_KEYS.index(stage_key)}"
    job_id = f"job-{stage_key}{job_suffix}"
    now = utc_now()
    input_data = {"params": params or {}}
    with database.engine.begin() as connection:
        connection.execute(
            insert(job).values(
                id=job_id,
                project_id="project-stage-service",
                chapter_id="chapter-stage-service",
                workflow_run_id="run-stage-service",
                workflow_stage_id=stage_id,
                job_type="dynamic_stage_generate",
                target_type="workflow_stage",
                target_id=stage_id,
                provider_kind="text",
                provider_id=provider_id,
                provider_task_id="",
                status=status,
                progress_current=0,
                progress_total=1,
                message="",
                input_json=json.dumps(input_data, ensure_ascii=False),
                input_signature=input_signature,
                depends_on_json="[]",
                result_json="{}",
                error="",
                retry_count=0,
                created_at=now,
                updated_at=now,
            )
        )
    with database.engine.connect() as connection:
        row = connection.execute(select(job).where(job.c.id == job_id)).first()
    assert row is not None
    record = dict(row._mapping)
    record["input_data"] = input_data
    return record


def _service(
    database: Database, *, mode: str = "happy"
) -> tuple[DynamicStageService, FakeProviderService]:
    payloads = {
        "0-source": _source_payload(),
        "1-character-bios": _character_payload(),
        "2-rhythm": _rhythm_payload(),
    }
    if mode == "warning":
        payloads["0-source"]["scenes"][0]["shootability"] = {
            "status": "needs_adaptation",
            "visible_action": "主角拿起发光物品",
            "required_asset_keys": [],
            "production_risks": ["动作需要简化"],
            "adaptation_note": "将动作改为单人完成",
        }
    if mode == "blocking":
        payloads["0-source"]["scenes"][0]["shootability"] = {
            "status": "unshootable",
            "visible_action": "无法落地的复杂动作",
            "required_asset_keys": [],
            "production_risks": [],
            "adaptation_note": None,
        }
    provider_service = FakeProviderService(FakeTextProvider(payloads))
    return DynamicStageService(database, provider_service), provider_service


def test_source_happy_path_persists_all_canonical_outputs_and_metadata(stage_context) -> None:
    database = stage_context
    service, fake_service = _service(database)
    _set_run_stage(database, "0-source")
    job_record = _insert_job(
        database, "0-source", provider_id="job-provider", params={"temperature": 0.8}
    )

    result = _run(service.execute_job(job_record))

    assert result == {
        "stage_id": "stage-0",
        "stage_key": "0-source",
        "stage_status": "ready",
        "gate_status": "ready",
    }
    assert fake_service.resolve_calls == [{"kind": "text", "provider_id": "job-provider"}]
    assert fake_service.provider.calls[0]["params"] == {
        "temperature": 0.8,
        "provider_only": True,
    }
    with database.engine.connect() as connection:
        stage_row = connection.execute(
            select(workflow_stage).where(workflow_stage.c.id == "stage-0")
        ).first()
        job_row = connection.execute(select(job).where(job.c.id == "job-0-source")).first()
        run_row = connection.execute(
            select(workflow_run).where(workflow_run.c.id == "run-stage-service")
        ).first()
    assert stage_row is not None and job_row is not None and run_row is not None
    assert stage_row.status == "ready"
    assert json.loads(stage_row.raw_output_json)["capture_mode"] == "normalized_provider_model"
    assert json.loads(stage_row.raw_output_json)["provider_output"]["episode_logline"]
    assert json.loads(stage_row.normalized_output_json)["artifact_meta"]["schema_version"] == (
        "dynamic-source-v1"
    )
    assert json.loads(stage_row.draft_output_json) == json.loads(stage_row.normalized_output_json)
    assert json.loads(stage_row.gate_result_json)["status"] == "ready"
    refs = json.loads(stage_row.output_refs_json)
    assert refs[0] == {
        "capture_mode": "normalized_provider_model",
        "gate_version": "dynamic-gate-v1",
        "normalizer_version": "dynamic-normalizer-v1",
        "provider_id": "fake-text-provider",
        "provider_model": "fake-model-v1",
        "schema_version": "dynamic-source-v1",
    }
    assert job_row.status == "completed"
    assert json.loads(job_row.result_json) == result
    assert run_row.status == "ready"
    assert run_row.revision == 3


@pytest.mark.parametrize(
    ("mode", "expected_gate", "expected_stage", "expected_run"),
    [
        ("warning", "warning", "ready", "ready"),
        ("blocking", "blocked", "blocked", "blocked"),
    ],
)
def test_source_gate_warning_and_blocking_control_stage_and_run_status(
    stage_context,
    mode: str,
    expected_gate: str,
    expected_stage: str,
    expected_run: str,
) -> None:
    database = stage_context
    service, fake_service = _service(database, mode=mode)
    _set_run_stage(database, "0-source")
    job_record = _insert_job(database, "0-source")

    result = _run(service.execute_job(job_record))

    assert result["gate_status"] == expected_gate
    assert fake_service.provider.calls
    with database.engine.connect() as connection:
        stage_status = connection.scalar(
            select(workflow_stage.c.status).where(workflow_stage.c.id == "stage-0")
        )
        run_status = connection.scalar(
            select(workflow_run.c.status).where(workflow_run.c.id == "run-stage-service")
        )
    assert stage_status == expected_stage
    assert run_status == expected_run


def test_rhythm_stage_uses_server_default_budget_instead_of_model_max_events(stage_context) -> None:
    database = stage_context
    service, provider_service = _service(database)
    _set_run_stage(
        database,
        "0-source",
        stage_status="confirmed",
        normalized_output=_source_payload(),
    )
    _set_run_stage(
        database,
        "1-character-bios",
        stage_status="confirmed",
        normalized_output=_character_payload(),
    )
    _set_run_stage(database, "2-rhythm")
    payload = _rhythm_payload()
    budget = payload["strong_dynamic_budget"]
    budget["max_events"] = 99
    for index in range(2, 3):
        event_id = f"dynamic-{index}"
        budget["event_ids"].append(event_id)
        budget["events"].append(
            {
                "event_id": event_id,
                "summary": f"额外强动态 {index}",
                "segment_id": "segment-1",
                "dynamic_level": 1.0,
                "visual_action": f"执行额外动作 {index}",
                "source_refs": [],
            }
        )
    provider_service.provider.payloads["2-rhythm"] = payload
    job_record = _insert_job(database, "2-rhythm")

    result = _run(service.execute_job(job_record))

    assert result["stage_status"] == "ready"
    with database.engine.connect() as connection:
        stage_row = connection.execute(
            select(workflow_stage).where(workflow_stage.c.id == "stage-2")
        ).one()
        gate = json.loads(
            connection.scalar(
                select(workflow_stage.c.gate_result_json).where(workflow_stage.c.id == "stage-2")
            )
        )
    assert json.loads(stage_row.normalized_output_json)["strong_dynamic_budget"]["max_events"] == 3
    assert json.loads(stage_row.draft_output_json)["strong_dynamic_budget"]["max_events"] == 3
    assert gate["status"] == "ready"
    normalized = json.loads(stage_row.normalized_output_json)
    draft = json.loads(stage_row.draft_output_json)
    assert normalized == draft
    assert normalized["strong_dynamic_budget"]["max_events"] == 3
    assert draft["strong_dynamic_budget"]["max_events"] == 3
    expected_signature = calculate_signature(
        {key: value for key, value in normalized.items() if key != "artifact_meta"},
        normalized["artifact_meta"]["input_signature"],
    )
    assert normalized["artifact_meta"]["content_signature"] == expected_signature


@pytest.mark.parametrize("stage_key", ["1-character-bios", "2-rhythm"])
def test_stage_requires_confirmed_direct_prerequisite_without_provider_call(
    stage_context,
    stage_key: str,
) -> None:
    database = stage_context
    service, fake_service = _service(database)
    _set_run_stage(database, stage_key)
    job_record = _insert_job(database, stage_key)

    with pytest.raises(StageExecutionError, match="前置确认"):
        _run(service.execute_job(job_record))

    assert fake_service.provider.calls == []
    with database.engine.connect() as connection:
        job_status = connection.scalar(select(job.c.status).where(job.c.id == job_record["id"]))
        stage_status = connection.scalar(
            select(workflow_stage.c.status).where(
                workflow_stage.c.id == f"stage-{DYNAMIC_STAGE_KEYS.index(stage_key)}"
            )
        )
    assert job_status == "failed"
    assert stage_status == "queued"


def test_character_and_rhythm_execute_after_confirmed_prerequisites(stage_context) -> None:
    database = stage_context
    service, fake_service = _service(database)
    source_output = {"episode_logline": "已确认源文"}
    _set_run_stage(database, "1-character-bios", normalized_output=source_output)
    _set_run_stage(database, "0-source", stage_status="confirmed", normalized_output=source_output)
    character_job = _insert_job(database, "1-character-bios")

    character_result = _run(service.execute_job(character_job))

    assert character_result["stage_status"] == "ready"
    _set_run_stage(
        database,
        "2-rhythm",
        normalized_output={"characters": [{"basic_info": {"character_key": "hero"}}]},
    )
    _set_run_stage(
        database, "1-character-bios", stage_status="confirmed", normalized_output={"characters": []}
    )
    rhythm_job = _insert_job(database, "2-rhythm")

    rhythm_result = _run(service.execute_job(rhythm_job))

    assert rhythm_result["stage_status"] == "ready"
    assert [call["schema"] for call in fake_service.provider.calls] == [
        CharacterBiosOutput,
        RhythmOutput,
    ]


def test_signature_mismatch_interrupts_job_and_stales_stage_without_provider_call(
    stage_context,
) -> None:
    database = stage_context
    service, fake_service = _service(database)
    _set_run_stage(database, "0-source", stage_signature="stage-signature")
    job_record = _insert_job(database, "0-source", input_signature="new-signature")

    with pytest.raises(StageExecutionError, match="input_signature"):
        _run(service.execute_job(job_record))

    assert fake_service.provider.calls == []
    with database.engine.connect() as connection:
        job_status = connection.scalar(select(job.c.status).where(job.c.id == job_record["id"]))
        stage_status = connection.scalar(
            select(workflow_stage.c.status).where(workflow_stage.c.id == "stage-0")
        )
        run_status = connection.scalar(
            select(workflow_run.c.status).where(workflow_run.c.id == "run-stage-service")
        )
    assert job_status == "interrupted"
    assert stage_status == "stale"
    assert run_status == "interrupted"


def test_result_submission_rechecks_upstream_confirmation_before_persisting(stage_context) -> None:
    database = stage_context
    service, provider_service = _phase34_service(database)
    _confirm_phase34_upstream(database)
    _set_run_stage(database, "3-script")
    job_record = _insert_job(database, "3-script")

    class StaleUpstreamService(DynamicStageService):
        async def _generate_and_normalize(self, context, incoming_job):  # type: ignore[no-untyped-def]
            result = await super()._generate_and_normalize(context, incoming_job)
            with self.database.engine.begin() as connection:
                connection.execute(
                    update(workflow_stage)
                    .where(workflow_stage.c.id == "stage-2")
                    .values(status="stale")
                )
            return result

    stale_service = StaleUpstreamService(database, service.provider_service)
    with pytest.raises(StageExecutionError, match="前置确认已变化"):
        _run(stale_service.execute_job(job_record))

    assert provider_service.provider.calls
    with database.engine.connect() as connection:
        statuses = connection.execute(
            select(job.c.status, workflow_stage.c.status, workflow_run.c.status)
            .select_from(
                job.join(workflow_stage, job.c.workflow_stage_id == workflow_stage.c.id).join(
                    workflow_run, job.c.workflow_run_id == workflow_run.c.id
                )
            )
            .where(job.c.id == job_record["id"])
        ).one()
    assert tuple(statuses) == ("interrupted", "stale", "running")


def test_provider_exception_fails_job_stage_and_run_with_sanitized_error(stage_context) -> None:
    database = stage_context
    provider = FakeTextProvider(
        {
            "0-source": _source_payload(),
            "1-character-bios": _character_payload(),
            "2-rhythm": _rhythm_payload(),
        },
        error=RuntimeError("boom https://provider.invalid/path token=secret-value"),
    )
    fake_service = FakeProviderService(provider)
    service = DynamicStageService(database, fake_service)
    _set_run_stage(database, "0-source")
    job_record = _insert_job(database, "0-source")

    with pytest.raises(RuntimeError, match="boom"):
        _run(service.execute_job(job_record))

    with database.engine.connect() as connection:
        job_row = connection.execute(select(job).where(job.c.id == job_record["id"])).first()
        stage_row = connection.execute(
            select(workflow_stage).where(workflow_stage.c.id == "stage-0")
        ).first()
        run_row = connection.execute(
            select(workflow_run).where(workflow_run.c.id == "run-stage-service")
        ).first()
    assert job_row is not None and stage_row is not None and run_row is not None
    assert job_row.status == "failed"
    assert stage_row.status == "failed"
    assert run_row.status == "failed"
    assert "[已隐藏 URL]" in job_row.error
    assert "token=[已隐藏]" in job_row.error
    assert "secret-value" not in job_row.error


@pytest.mark.parametrize("protected_field", ["job", "stage", "run"])
def test_protected_or_completed_job_is_rejected_without_provider_call(
    stage_context, protected_field: str
) -> None:
    database = stage_context
    service, fake_service = _service(database)
    _set_run_stage(database, "0-source")
    job_status = "completed" if protected_field == "job" else "queued"
    job_record = _insert_job(database, "0-source", status=job_status)
    with database.engine.begin() as connection:
        if protected_field == "stage":
            connection.execute(
                update(workflow_stage)
                .where(workflow_stage.c.id == "stage-0")
                .values(status="confirmed")
            )
        elif protected_field == "run":
            connection.execute(
                update(workflow_run)
                .where(workflow_run.c.id == "run-stage-service")
                .values(status="blocked")
            )

    with pytest.raises(StageExecutionError):
        _run(service.execute_job(job_record))
    assert fake_service.provider.calls == []


def test_completed_job_cannot_be_overwritten_by_repeated_execution(stage_context) -> None:
    database = stage_context
    service, fake_service = _service(database)
    _set_run_stage(database, "0-source")
    job_record = _insert_job(database, "0-source")
    _run(service.execute_job(job_record))
    with database.engine.connect() as connection:
        completed = dict(
            connection.execute(select(job).where(job.c.id == job_record["id"])).first()._mapping
        )

    with pytest.raises(StageExecutionError, match="完成"):
        _run(service.execute_job(completed))
    assert len(fake_service.provider.calls) == 1


def test_user_prompt_uses_run_snapshots_and_confirmed_outputs_not_chapter_source(
    stage_context,
) -> None:
    database = stage_context
    service, fake_service = _service(database)
    _set_run_stage(database, "1-character-bios", normalized_output={"source": "confirmed-source"})
    _set_run_stage(
        database,
        "0-source",
        stage_status="confirmed",
        normalized_output={"source": "confirmed-source"},
    )
    job_record = _insert_job(database, "1-character-bios")

    _run(service.execute_job(job_record))

    prompt = fake_service.provider.calls[0]["user_prompt"]
    assert "SNAPSHOT_SOURCE_TEXT" in prompt
    assert "confirmed-source" in prompt
    assert "CHAPTER_SOURCE_TEXT_MUST_NOT_ENTER_PROMPT" not in prompt
    assert "source_snapshot" in prompt
    assert "config_snapshot" in prompt


def test_missing_dynamic_prompt_template_fails_before_provider_call(
    stage_context,
    monkeypatch,
) -> None:
    database = stage_context
    service, provider_service = _service(database)
    _set_run_stage(database, "0-source")
    job_record = _insert_job(database, "0-source")

    def missing_template(_: str) -> str:
        raise FileNotFoundError("dynamic template missing")

    monkeypatch.setattr("app.dynamic_pipeline.stage_service.load_prompt", missing_template)

    with pytest.raises(StageExecutionError, match="缺少权威 dynamic Prompt 模板"):
        _run(service.execute_job(job_record))

    assert provider_service.provider.calls == []
    with database.engine.connect() as connection:
        stage_status = connection.scalar(
            select(workflow_stage.c.status).where(workflow_stage.c.id == "stage-0")
        )
        job_status = connection.scalar(select(job.c.status).where(job.c.id == job_record["id"]))
    assert stage_status == "failed"
    assert job_status == "failed"


class _Phase34TextProvider:
    def __init__(self, payloads: dict[str, dict[str, Any]], error: Exception | None = None) -> None:
        self.payloads = payloads
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: type[Any],
        params: dict[str, Any],
    ) -> Any:
        from app.dynamic_pipeline.schemas import ScriptOutput
        from app.dynamic_pipeline.storyboard_stage import StoryboardOutput

        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "schema": schema,
                "params": params,
            }
        )
        if self.error is not None:
            raise self.error
        key = {ScriptOutput: "3-script", StoryboardOutput: "4-storyboard"}[schema]
        return schema.model_validate(copy.deepcopy(self.payloads[key]))


def _script_stage_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "artifact_meta": _meta(),
        "title": "测试详细剧本",
        "logline": "主角在旧屋中完成关键回应。",
        "target_duration_sec": 30,
        "scenes": [
            {
                "scene_id": "scene-1",
                "ordinal": 1,
                "scene_heading": "旧屋",
                "time_weather": "夜",
                "location": "旧屋",
                "characters": [],
                "objective": "建立线索",
                "action": "主角举起线索并作出决定",
                "dialogue": [
                    {
                        "dialogue_line_id": "scene-1-dialogue-1",
                        "speaker": "主角",
                        "text": "我知道该怎么做。",
                        "start_sec": 0,
                        "end_sec": 1,
                    }
                ],
                "narration": [],
                "emotion": "坚定",
                "dynamic_level": 0.8,
                "sound_cues": [],
                "subtitle_style": "",
                "duration_sec": 30,
                "transition_to_next": "",
                "source_refs": [],
            }
        ],
        "strong_dynamic_event_ids": ["dynamic-1"],
        "assumptions": [],
        "open_questions": [],
    }
    payload.update(overrides)
    return payload


def _storyboard_stage_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "artifact_meta": _meta(),
        "target_duration_sec": 30,
        "shots": [
            {
                "external_key": "storyboard:run-stage-service:scene-1:shot-1:ordinal-1",
                "shot_no": 1,
                "ordinal": 1,
                "scene_id": "scene-1",
                "scene_no": 1,
                "scene_name": "旧屋",
                "time_weather": "夜",
                "scene_asset_id": "scene-asset-1",
                "character_names": [],
                "character_asset_ids": [],
                "prop_names": [],
                "prop_asset_ids": [],
                "positions": [],
                "expressions": [],
                "start_state": "主角站在旧屋中央",
                "action": "主角举起线索并作出决定",
                "end_state": "主角握紧线索",
                "next_hook": "",
                "transition_from_prev": "scene_change",
                "duration_sec": 10,
                "provider_duration_sec": 10,
                "shot_type": "中景",
                "camera_angle": "平视",
                "camera_move": "固定",
                "dialogue": [
                    {
                        "dialogue_line_id": "scene-1-dialogue-1",
                        "speaker": "主角",
                        "text": "我知道该怎么做。",
                    }
                ],
                "narration": "",
                "sound_cues": [],
                "dynamic_level": 0.8,
                "dynamic_event_id": "dynamic-1",
                "frame_description": "旧屋中主角举起线索",
                "source_refs": [],
                "reference_bindings": [],
                "prompt_intent": {},
                "continuity_state": {
                    "start": {"主角": "站在旧屋中央"},
                    "end": {"主角": "握紧线索"},
                },
            },
            {
                "external_key": "storyboard:run-stage-service:scene-1:shot-2:ordinal-2",
                "shot_no": 2,
                "ordinal": 2,
                "scene_id": "scene-1",
                "scene_no": 1,
                "scene_name": "旧屋",
                "time_weather": "夜",
                "scene_asset_id": "scene-asset-1",
                "character_names": [],
                "character_asset_ids": [],
                "prop_names": [],
                "prop_asset_ids": [],
                "positions": [],
                "expressions": [],
                "start_state": "主角握紧线索",
                "action": "主角举起线索并作出决定",
                "end_state": "主角握紧线索并看向门外",
                "next_hook": "",
                "transition_from_prev": "continuous",
                "duration_sec": 10,
                "provider_duration_sec": 10,
                "shot_type": "近景",
                "camera_angle": "平视",
                "camera_move": "固定",
                "dialogue": [],
                "narration": "",
                "sound_cues": [],
                "dynamic_level": 0.5,
                "dynamic_event_id": None,
                "frame_description": "主角手中的线索",
                "source_refs": [],
                "reference_bindings": [],
                "prompt_intent": {},
                "continuity_state": {
                    "start": {"主角": "握紧线索"},
                    "end": {"主角": "握紧线索并看向门外"},
                },
            },
            {
                "external_key": "storyboard:run-stage-service:scene-1:shot-3:ordinal-3",
                "shot_no": 3,
                "ordinal": 3,
                "scene_id": "scene-1",
                "scene_no": 1,
                "scene_name": "旧屋",
                "time_weather": "夜",
                "scene_asset_id": "scene-asset-1",
                "character_names": [],
                "character_asset_ids": [],
                "prop_names": [],
                "prop_asset_ids": [],
                "positions": [],
                "expressions": [],
                "start_state": "主角握紧线索并看向门外",
                "action": "主角举起线索并作出决定",
                "end_state": "主角迈步走向门外",
                "next_hook": "",
                "transition_from_prev": "continuous",
                "duration_sec": 10,
                "provider_duration_sec": 10,
                "shot_type": "全景",
                "camera_angle": "平视",
                "camera_move": "推镜",
                "dialogue": [],
                "narration": "",
                "sound_cues": [],
                "dynamic_level": 0.5,
                "dynamic_event_id": None,
                "frame_description": "主角走向门外",
                "source_refs": [],
                "reference_bindings": [],
                "prompt_intent": {},
                "continuity_state": {
                    "start": {"主角": "握紧线索并看向门外"},
                    "end": {"主角": "迈步走向门外"},
                },
            }
        ],
        "scenes": [],
        "assumptions": [],
        "open_questions": [],
    }
    payload.update(overrides)
    return payload


def _phase34_service(
    database: Database,
    *,
    error: Exception | None = None,
    script_payload: dict[str, Any] | None = None,
    storyboard_payload: dict[str, Any] | None = None,
) -> tuple[DynamicStageService, FakeProviderService]:
    provider = _Phase34TextProvider(
        {
            "3-script": script_payload or _script_stage_payload(),
            "4-storyboard": storyboard_payload or _storyboard_stage_payload(),
        },
        error=error,
    )
    provider_service = FakeProviderService(provider)  # type: ignore[arg-type]
    return DynamicStageService(database, provider_service), provider_service


def _confirm_phase34_upstream(database: Database, *, include_script: bool = False) -> None:
    _set_run_stage(
        database,
        "0-source",
        stage_status="confirmed",
        normalized_output=_source_payload(),
    )
    _set_run_stage(
        database,
        "1-character-bios",
        stage_status="confirmed",
        normalized_output=_character_payload(),
    )
    _set_run_stage(
        database,
        "2-rhythm",
        stage_status="confirmed",
        normalized_output=_rhythm_payload(),
    )
    if include_script:
        _set_run_stage(
            database,
            "3-script",
            stage_status="confirmed",
            normalized_output=_script_stage_payload(),
        )


def test_script_stage_service_happy_path_persists_gate_provenance_and_avoids_chapter_text(
    stage_context,
) -> None:
    from app.dynamic_pipeline.schemas import ScriptOutput

    database = stage_context
    service, provider_service = _phase34_service(database)
    _confirm_phase34_upstream(database)
    _set_run_stage(database, "3-script")
    job_record = _insert_job(database, "3-script")

    result = _run(service.execute_job(job_record))

    assert result == {
        "stage_id": "stage-3",
        "stage_key": "3-script",
        "stage_status": "ready",
        "gate_status": "ready",
    }
    call = provider_service.provider.calls[0]
    assert call["schema"] is ScriptOutput
    assert "SNAPSHOT_SOURCE_TEXT" in call["user_prompt"]
    assert "CHAPTER_SOURCE_TEXT_MUST_NOT_ENTER_PROMPT" not in call["user_prompt"]
    assert "主角举起线索并作出决定" in call["user_prompt"]

    with database.engine.connect() as connection:
        stage_row = connection.execute(
            select(workflow_stage).where(workflow_stage.c.id == "stage-3")
        ).one()
        job_row = connection.execute(select(job).where(job.c.id == job_record["id"])).one()
        run_row = connection.execute(
            select(workflow_run).where(workflow_run.c.id == "run-stage-service")
        ).one()
    assert stage_row.status == "ready"
    assert job_row.status == "completed"
    assert run_row.status == "ready"
    normalized = json.loads(stage_row.normalized_output_json)
    assert normalized["artifact_meta"]["schema_version"] == "dynamic-script-v2"
    assert normalized["artifact_meta"]["input_signature"] == "stage-signature"
    assert normalized["artifact_meta"]["source_attempt"] == 1
    assert json.loads(stage_row.draft_output_json) == normalized
    assert json.loads(stage_row.gate_result_json)["status"] == "ready"
    refs = json.loads(stage_row.output_refs_json)
    assert refs[0]["gate_version"] == "dynamic-gate-v1"
    assert refs[0]["gate_provenance"]["stage_key"] == "3-script"
    assert refs[0]["gate_provenance"]["input_signature"] == "stage-signature"


def test_storyboard_stage_service_happy_path_persists_outputs_without_materializing_canonical_rows(
    stage_context,
) -> None:
    from app.dynamic_pipeline.storyboard_stage import StoryboardOutput
    from app.models.tables import screenplay, shot

    database = stage_context
    service, provider_service = _phase34_service(database)
    _confirm_phase34_upstream(database, include_script=True)
    _set_run_stage(database, "4-storyboard")
    job_record = _insert_job(database, "4-storyboard")

    result = _run(service.execute_job(job_record))

    assert result["stage_status"] == "ready"
    assert provider_service.provider.calls[0]["schema"] is StoryboardOutput
    with database.engine.connect() as connection:
        stage_row = connection.execute(
            select(workflow_stage).where(workflow_stage.c.id == "stage-4")
        ).one()
        job_row = connection.execute(select(job).where(job.c.id == job_record["id"])).one()
        run_status = connection.scalar(
            select(workflow_run.c.status).where(workflow_run.c.id == "run-stage-service")
        )
        screenplay_count = connection.scalar(select(func.count()).select_from(screenplay))
        shot_count = connection.scalar(select(func.count()).select_from(shot))
    assert stage_row.status == "ready"
    assert job_row.status == "completed"
    assert run_status == "ready"
    assert json.loads(stage_row.normalized_output_json)["artifact_meta"]["schema_version"] == (
        "dynamic-storyboard-v2"
    )
    assert json.loads(stage_row.gate_result_json)["status"] == "ready"
    assert json.loads(stage_row.output_refs_json)[0]["gate_provenance"]["stage_key"] == (
        "4-storyboard"
    )
    assert screenplay_count == 0
    assert shot_count == 0


@pytest.mark.parametrize(
    ("stage_key", "include_script", "missing_text"),
    [
        ("3-script", False, "2-rhythm"),
        ("4-storyboard", True, "3-script"),
    ],
)
def test_phase34_stage_requires_direct_confirmed_prerequisite_without_provider_call(
    stage_context,
    stage_key: str,
    include_script: bool,
    missing_text: str,
) -> None:
    database = stage_context
    service, provider_service = _phase34_service(database)
    if include_script:
        _confirm_phase34_upstream(database, include_script=False)
    else:
        _set_run_stage(
            database,
            "0-source",
            stage_status="confirmed",
            normalized_output=_source_payload(),
        )
        _set_run_stage(
            database,
            "1-character-bios",
            stage_status="confirmed",
            normalized_output=_character_payload(),
        )
    _set_run_stage(database, stage_key)
    job_record = _insert_job(database, stage_key)

    with pytest.raises(StageExecutionError, match=missing_text):
        _run(service.execute_job(job_record))

    assert provider_service.provider.calls == []
    with database.engine.connect() as connection:
        job_status = connection.scalar(select(job.c.status).where(job.c.id == job_record["id"]))
        stage_status = connection.scalar(
            select(workflow_stage.c.status).where(
                workflow_stage.c.id == f"stage-{DYNAMIC_STAGE_KEYS.index(stage_key)}"
            )
        )
    assert job_status == "failed"
    assert stage_status == "queued"


def test_script_blocking_gate_sets_blocked_stage_and_run_but_completes_job(stage_context) -> None:
    database = stage_context
    service, provider_service = _phase34_service(
        database,
        script_payload=_script_stage_payload(
            scenes=[
                {
                    **_script_stage_payload()["scenes"][0],
                    "action": "",
                }
            ]
        ),
    )
    _confirm_phase34_upstream(database)
    _set_run_stage(database, "3-script")
    job_record = _insert_job(database, "3-script")

    result = _run(service.execute_job(job_record))

    assert result["stage_status"] == "blocked"
    assert result["gate_status"] == "blocked"
    assert provider_service.provider.calls
    with database.engine.connect() as connection:
        statuses = connection.execute(
            select(
                job.c.status,
                workflow_stage.c.status,
                workflow_run.c.status,
            )
            .select_from(
                job.join(workflow_stage, job.c.workflow_stage_id == workflow_stage.c.id).join(
                    workflow_run, job.c.workflow_run_id == workflow_run.c.id
                )
            )
            .where(job.c.id == job_record["id"])
        ).one()
    assert statuses == ("completed", "blocked", "blocked")


def test_strict_script_schema_failure_marks_job_stage_and_run_failed(stage_context) -> None:
    class _InvalidProvider(_Phase34TextProvider):
        async def generate_json(self, **kwargs: Any) -> Any:
            self.calls.append(kwargs)
            return object()

    database = stage_context
    provider_service = FakeProviderService(_InvalidProvider({}))  # type: ignore[arg-type]
    service = DynamicStageService(database, provider_service)
    _confirm_phase34_upstream(database)
    _set_run_stage(database, "3-script")
    job_record = _insert_job(database, "3-script")

    with pytest.raises(TypeError, match="Pydantic 模型"):
        _run(service.execute_job(job_record))

    with database.engine.connect() as connection:
        statuses = connection.execute(
            select(
                job.c.status,
                workflow_stage.c.status,
                workflow_run.c.status,
            )
            .select_from(
                job.join(workflow_stage, job.c.workflow_stage_id == workflow_stage.c.id).join(
                    workflow_run, job.c.workflow_run_id == workflow_run.c.id
                )
            )
            .where(job.c.id == job_record["id"])
        ).one()
    assert statuses == ("failed", "failed", "failed")


def test_storyboard_regenerate_with_new_attempt_bypasses_batch_cache(
    stage_context,
) -> None:
    database = stage_context
    service, provider_service = _phase34_service(database)
    _confirm_phase34_upstream(database, include_script=True)
    _set_run_stage(database, "4-storyboard")
    first_job = _insert_job(database, "4-storyboard")

    first_result = _run(service.execute_job(first_job))

    assert first_result["stage_status"] == "ready"
    # 30 秒单场景只拆出一个批次，首次执行恰好一次 Provider 调用。
    assert len(provider_service.provider.calls) == 1

    # 模拟 regenerate：同 run 同签名但 stage attempt 递增，重新入队执行。
    _set_run_stage(database, "4-storyboard")
    with database.engine.begin() as connection:
        connection.execute(
            update(workflow_stage)
            .where(
                workflow_stage.c.workflow_run_id == "run-stage-service",
                workflow_stage.c.stage_key == "4-storyboard",
            )
            .values(attempt=2)
        )
    second_job = _insert_job(database, "4-storyboard", job_suffix="-retry")

    second_result = _run(service.execute_job(second_job))

    assert second_result["stage_status"] == "ready"
    # 缓存键包含 attempt：新 attempt 不得命中旧缓存，Provider 必须再次被调用。
    assert len(provider_service.provider.calls) == 2


def test_storyboard_batch_cache_prunes_by_run_instead_of_global_clear(
    stage_context,
) -> None:
    database = stage_context
    service, provider_service = _phase34_service(database)
    _confirm_phase34_upstream(database, include_script=True)
    _set_run_stage(database, "4-storyboard")
    job_record = _insert_job(database, "4-storyboard")

    # 预填超过上限的其他 run 缓存条目（每个 run 一条）。
    for index in range(service._STORYBOARD_CACHE_MAX_ENTRIES + 1):
        service._storyboard_batch_cache[
            (f"other-run-{index}", "other-signature", ((("scene-x",), 0.0, 30.0),), 1)
        ] = []

    _run(service.execute_job(job_record))

    surviving_keys = list(service._storyboard_batch_cache)
    surviving_runs = {key[0] for key in surviving_keys}
    # 当前 run 的缓存条目保留，可继续复用。
    assert "run-stage-service" in surviving_runs
    # 超限后按 run 淘汰最旧运行（其他 run 各一条，淘汰两个后回到上限），
    # 而不是 clear() 式全清导致所有 run 的缓存同时失效。
    other_runs = {
        key[0] for key in surviving_keys if key[0] != "run-stage-service"
    }
    assert len(other_runs) == service._STORYBOARD_CACHE_MAX_ENTRIES - 1
    assert "other-run-0" not in other_runs
    assert "other-run-1" not in other_runs
    assert "other-run-2" in other_runs
    assert len(surviving_keys) == service._STORYBOARD_CACHE_MAX_ENTRIES


def test_mark_generation_failure_logs_persistence_exception(
    stage_context,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    from app.dynamic_pipeline.stage_service import _ExecutionContext

    database = stage_context
    service, _ = _service(database)
    context = _ExecutionContext(
        job_record={"id": "job-logging"},
        run_record={"id": "run-logging"},
        stage_record={"id": "stage-logging", "stage_key": "0-source"},
        upstream_outputs={},
    )

    class _BrokenEngine:
        # 模拟落库阶段数据库引擎失效。
        def begin(self) -> Any:
            raise RuntimeError("database gone")

        def dispose(self) -> None:
            # fixture teardown 需要该方法，无需真实清理。
            return

    monkeypatch.setattr(service.database, "engine", _BrokenEngine())

    with caplog.at_level(
        logging.ERROR, logger="app.dynamic_pipeline.stage_service"
    ):
        # 落库失败不向上抛出（原始业务异常由调用方继续抛），但必须留下完整堆栈日志。
        service._mark_generation_failure(context, RuntimeError("provider boom"))

    failure_records = [
        record for record in caplog.records if "动态阶段失败状态落库异常" in record.message
    ]
    assert failure_records, "阶段失败落库异常必须记录 logger.exception 而不是静默吞掉"
    assert failure_records[0].exc_info is not None
