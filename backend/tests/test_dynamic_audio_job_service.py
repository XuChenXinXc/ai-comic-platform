from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, insert, select, update

from app.db import Database, migrate_schema, utc_now
from app.dynamic_pipeline.audio_job_service import DynamicAudioJobService
from app.jobs.repository import JobRepository
from app.models.tables import (
    api_provider,
    chapter,
    project,
    shot,
    shot_audio,
    workflow_run,
    workflow_stage,
)
from app.services.file_service import FileService


class _FakeTTSProvider:
    def __init__(self, calls: list[dict[str, Any]], *, error: BaseException | None = None) -> None:
        self.calls = calls
        self.error = error

    def supports_voice(self, _voice_id: str) -> bool:
        return True

    async def synthesize(
        self,
        *,
        text: str,
        voice_id: str,
        params: dict[str, Any],
    ) -> bytes:
        self.calls.append({"text": text, "voice_id": voice_id, "params": dict(params)})
        if self.error is not None:
            raise self.error
        return b"fake-mp3-data"


class _FakeProviderService:
    def __init__(self, database: Database, provider: _FakeTTSProvider) -> None:
        self.database = database
        self.provider = provider
        self.resolve_calls: list[dict[str, Any]] = []
        self.checked_out_during_resolve: list[int] = []

    def resolve(self, *, kind: str, provider_id: str | None = None):
        self.resolve_calls.append({"kind": kind, "provider_id": provider_id})
        checkedout = getattr(self.database.engine.pool, "checkedout", lambda: 0)()
        self.checked_out_during_resolve.append(checkedout)
        return self.provider, {"id": provider_id, "model": "fake-model"}


@pytest.fixture
def audio_context(tmp_path: Path):
    database = Database(f"sqlite:///{(tmp_path / 'dynamic-audio.db').as_posix()}")
    migrate_schema(database)
    storage = FileService(tmp_path / "workspace", database=database)
    repository = JobRepository(database)
    now = utc_now()
    project_id = "project-dynamic-audio"
    chapter_id = "chapter-dynamic-audio"
    shot_id = "shot-dynamic-audio"
    with database.engine.begin() as connection:
        connection.execute(
            insert(project).values(
                id=project_id,
                name="动态音频测试项目",
                storage_name=project_id,
                storage_root=str(storage.projects_dir),
                storage_layout="prefixed",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(chapter).values(
                id=chapter_id,
                project_id=project_id,
                chapter_no=1,
                title="动态音频测试章节",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(workflow_run).values(
                id="run-dynamic-audio",
                project_id=project_id,
                chapter_id=chapter_id,
                status="running",
                source_kind="full_text",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(workflow_stage).values(
                id="stage-dynamic-audio",
                workflow_run_id="run-dynamic-audio",
                stage_key="6-delivery",
                ordinal=6,
                status="running",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(shot).values(
                id=shot_id,
                project_id=project_id,
                chapter_id=chapter_id,
                shot_no=1,
                shot_order=1,
                source_type="screenplay",
                source_ref="dynamic-audio-test",
                external_key="shot-ext-dynamic-audio",
                created_at=now,
                updated_at=now,
            )
        )
        # Fake service 只验证 resolve(kind='tts')；Provider ID 仍使用真实 FK 行，
        # 避免测试绕过 shot_audio.provider_id 的约束。
        connection.execute(
            insert(api_provider).values(
                id="tts-fake",
                kind="text",
                name="测试 TTS Provider",
                adapter="mock",
                model="fake-model",
                created_at=now,
                updated_at=now,
            )
        )
    try:
        yield database, repository, storage, project_id, chapter_id, shot_id
    finally:
        database.dispose()


def _job_input(
    shot_id: str,
    *,
    dialogue_line_id: str = "line-1",
    line_order: int = 1,
    text: str = "你好，世界。",
    signature: str = "signature-1",
    voice_id: str = "voice-a",
) -> dict[str, Any]:
    return {
        "workflow_run_id": "run-dynamic-audio",
        "workflow_stage_id": "stage-dynamic-audio",
        "shot_id": shot_id,
        "dialogue_line_id": dialogue_line_id,
        "line_order": line_order,
        "text": text,
        "source_text_snapshot": text,
        "speaker": "主角",
        "voice_id": voice_id,
        "provider_id": "tts-fake",
        "provider_model": "fake-model",
        "input_signature": signature,
        "params": {"speed": 1.0},
    }


def _create_job(
    repository: JobRepository,
    project_id: str,
    chapter_id: str,
    shot_id: str,
    input_data: dict[str, Any],
    *,
    job_id: str,
) -> dict[str, Any]:
    return repository.create_job(
        {
            "id": job_id,
            "project_id": project_id,
            "chapter_id": chapter_id,
            "workflow_run_id": input_data["workflow_run_id"],
            "workflow_stage_id": input_data["workflow_stage_id"],
            "job_type": "generate_dialogue_audio",
            "target_type": "shot_audio",
            "target_id": shot_id,
            "provider_kind": "tts",
            "provider_id": "tts-fake",
            "input_signature": input_data["input_signature"],
            "input_data": input_data,
        }
    )


def _service(
    database: Database,
    storage: FileService,
    fake_provider_service: _FakeProviderService,
    monkeypatch: pytest.MonkeyPatch,
    *,
    duration: float = 1.25,
) -> DynamicAudioJobService:
    service = DynamicAudioJobService(database, fake_provider_service, storage)
    monkeypatch.setattr(service, "_get_audio_duration", lambda _path: duration)
    return service


def test_dynamic_audio_job_commits_file_fields_and_provenance_outside_transaction(
    audio_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, repository, storage, project_id, chapter_id, shot_id = audio_context
    calls: list[dict[str, Any]] = []
    fake_provider = _FakeProviderService(database, _FakeTTSProvider(calls))
    service = _service(database, storage, fake_provider, monkeypatch)
    record = _create_job(
        repository,
        project_id,
        chapter_id,
        shot_id,
        _job_input(shot_id),
        job_id="job-dynamic-audio-1",
    )

    result = asyncio.run(service.handle_job(record))

    assert result["status"] == "completed"
    assert len(calls) == 1
    assert fake_provider.resolve_calls == [{"kind": "tts", "provider_id": "tts-fake"}]
    assert fake_provider.checked_out_during_resolve == [0]
    saved_job = repository.get(record["id"])
    assert saved_job is not None
    assert saved_job["status"] == "completed"
    assert saved_job["result_data"]["audio_id"] == result["audio_id"]

    with database.engine.connect() as connection:
        audio = connection.execute(
            select(shot_audio).where(shot_audio.c.id == result["audio_id"])
        ).one()
    assert audio.shot_id == shot_id
    assert audio.dialogue_line_id == "line-1"
    assert audio.line_order == 1
    assert audio.dialogue_text == "你好，世界。"
    assert audio.source_text_snapshot == "你好，世界。"
    assert audio.speaker_name == "主角"
    assert audio.voice_id == "voice-a"
    assert audio.provider_id == "tts-fake"
    assert audio.model == "fake-model"
    assert audio.status == "completed"
    assert audio.is_current == 1
    assert audio.source == "generated"
    assert audio.input_signature == "signature-1"
    provenance = json.loads(audio.provenance_json)
    assert provenance["workflow_run_id"] == "run-dynamic-audio"
    assert provenance["workflow_stage_id"] == "stage-dynamic-audio"
    assert provenance["provider_id"] == "tts-fake"
    assert provenance["provider_model"] == "fake-model"
    assert provenance["voice_id"] == "voice-a"
    assert provenance["text_hash"] == hashlib.sha256("你好，世界。".encode()).hexdigest()
    assert "api_key" not in json.dumps(provenance).lower()

    relative_path = Path(audio.file_path)
    assert not relative_path.is_absolute()
    assert relative_path.parts[0] == "分镜配音"
    assert ".." not in relative_path.parts
    saved_file = storage.resolve_project_path(project_id, audio.file_path)
    assert saved_file.is_file()
    assert saved_file.read_bytes() == b"fake-mp3-data"


def test_dynamic_audio_job_keeps_multiple_dialogue_lines_and_legacy_current(
    audio_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, repository, storage, project_id, chapter_id, shot_id = audio_context
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(shot_audio).values(
                id="audio-legacy-current",
                shot_id=shot_id,
                dialogue_text="旧版单镜配音",
                version_no=1,
                status="completed",
                is_current=1,
                source="generated",
                created_at=now,
            )
        )
    calls: list[dict[str, Any]] = []
    fake_provider = _FakeProviderService(database, _FakeTTSProvider(calls))
    service = _service(database, storage, fake_provider, monkeypatch)
    first = _create_job(
        repository,
        project_id,
        chapter_id,
        shot_id,
        _job_input(shot_id, dialogue_line_id="line-1", line_order=1, signature="line-sig-1"),
        job_id="job-dynamic-audio-line-1",
    )
    second = _create_job(
        repository,
        project_id,
        chapter_id,
        shot_id,
        _job_input(
            shot_id,
            dialogue_line_id="line-2",
            line_order=2,
            text="这是第二句。",
            signature="line-sig-2",
        ),
        job_id="job-dynamic-audio-line-2",
    )

    first_result = asyncio.run(service.handle_job(first))
    second_result = asyncio.run(service.handle_job(second))

    assert first_result["status"] == "completed"
    assert second_result["status"] == "completed"
    assert len(calls) == 2
    with database.engine.connect() as connection:
        rows = connection.execute(
            select(shot_audio).where(shot_audio.c.shot_id == shot_id).order_by(shot_audio.c.id)
        ).all()
    current_by_line = {
        row.dialogue_line_id: row.is_current for row in rows if row.dialogue_line_id is not None
    }
    assert current_by_line == {"line-1": 1, "line-2": 1}
    legacy = next(row for row in rows if row.dialogue_line_id is None)
    assert legacy.is_current == 1
    assert {row.dialogue_line_id for row in rows} == {None, "line-1", "line-2"}


def test_dynamic_audio_job_reuses_matching_signature_but_regenerates_changed_signature(
    audio_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, repository, storage, project_id, chapter_id, shot_id = audio_context
    calls: list[dict[str, Any]] = []
    fake_provider = _FakeProviderService(database, _FakeTTSProvider(calls))
    service = _service(database, storage, fake_provider, monkeypatch)
    first_input = _job_input(shot_id)
    first = _create_job(
        repository,
        project_id,
        chapter_id,
        shot_id,
        first_input,
        job_id="job-dynamic-audio-idempotent-1",
    )

    first_result = asyncio.run(service.handle_job(first))
    repeated_result = asyncio.run(service.handle_job(first))
    same_signature_job = _create_job(
        repository,
        project_id,
        chapter_id,
        shot_id,
        first_input,
        job_id="job-dynamic-audio-idempotent-2",
    )
    reused_result = asyncio.run(service.handle_job(same_signature_job))
    changed_input = _job_input(
        shot_id,
        text="签名变化后的台词。",
        signature="signature-2",
    )
    changed_job = _create_job(
        repository,
        project_id,
        chapter_id,
        shot_id,
        changed_input,
        job_id="job-dynamic-audio-idempotent-3",
    )
    changed_result = asyncio.run(service.handle_job(changed_job))

    assert len(calls) == 2
    assert repeated_result["audio_id"] == first_result["audio_id"]
    assert reused_result["audio_id"] == first_result["audio_id"]
    assert changed_result["audio_id"] != first_result["audio_id"]
    with database.engine.connect() as connection:
        rows = connection.execute(select(shot_audio).where(shot_audio.c.shot_id == shot_id)).all()
        count = connection.scalar(
            select(func.count()).select_from(shot_audio).where(shot_audio.c.shot_id == shot_id)
        )
    assert count == 2
    assert {row.is_current for row in rows} == {0, 1}
    assert repository.get(same_signature_job["id"])["status"] == "completed"
    assert repository.get(changed_job["id"])["status"] == "completed"


def test_dynamic_audio_job_provider_failure_is_sanitized_without_audio_record(
    audio_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, repository, storage, project_id, chapter_id, shot_id = audio_context
    fake_provider = _FakeProviderService(
        database,
        _FakeTTSProvider(
            [],
            error=RuntimeError("Authorization: Bearer super-secret https://secret.example/a"),
        ),
    )
    service = _service(database, storage, fake_provider, monkeypatch)
    record = _create_job(
        repository,
        project_id,
        chapter_id,
        shot_id,
        _job_input(shot_id),
        job_id="job-dynamic-audio-provider-failed",
    )

    result = asyncio.run(service.handle_job(record))

    assert result["status"] == "failed"
    saved_job = repository.get(record["id"])
    assert saved_job is not None
    assert saved_job["status"] == "failed"
    assert "super-secret" not in saved_job["error"]
    assert "https://secret.example" not in saved_job["error"]
    with database.engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count()).select_from(shot_audio).where(shot_audio.c.shot_id == shot_id)
            )
            == 0
        )


def test_dynamic_audio_job_duration_failure_removes_file_and_marks_job_failed(
    audio_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, repository, storage, project_id, chapter_id, shot_id = audio_context
    calls: list[dict[str, Any]] = []
    fake_provider = _FakeProviderService(database, _FakeTTSProvider(calls))
    service = DynamicAudioJobService(database, fake_provider, storage)

    def fail_duration(_path: Path) -> float:
        raise RuntimeError("ffprobe 无法读取时长")

    monkeypatch.setattr(service, "_get_audio_duration", fail_duration)
    record = _create_job(
        repository,
        project_id,
        chapter_id,
        shot_id,
        _job_input(shot_id),
        job_id="job-dynamic-audio-duration-failed",
    )

    result = asyncio.run(service.handle_job(record))

    assert result["status"] == "failed"
    assert len(calls) == 1
    saved_job = repository.get(record["id"])
    assert saved_job is not None
    assert saved_job["status"] == "failed"
    assert "ffprobe 无法读取时长" in saved_job["error"]
    project_dir = storage.project_path(project_id)
    audio_dir = project_dir / "分镜配音"
    assert not audio_dir.exists() or list(audio_dir.iterdir()) == []
    with database.engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count()).select_from(shot_audio).where(shot_audio.c.shot_id == shot_id)
            )
            == 0
        )


def test_dynamic_audio_job_invalid_input_and_interrupt_do_not_call_provider(
    audio_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, repository, storage, project_id, chapter_id, shot_id = audio_context
    calls: list[dict[str, Any]] = []
    fake_provider = _FakeProviderService(database, _FakeTTSProvider(calls))
    service = _service(database, storage, fake_provider, monkeypatch)
    invalid_input = _job_input(shot_id)
    invalid_input.pop("dialogue_line_id")
    invalid = _create_job(
        repository,
        project_id,
        chapter_id,
        shot_id,
        invalid_input,
        job_id="job-dynamic-audio-invalid",
    )

    invalid_result = asyncio.run(service.handle_job(invalid))

    assert invalid_result["status"] == "failed"
    assert calls == []
    assert repository.get(invalid["id"])["status"] == "failed"

    interrupt_provider = _FakeProviderService(
        database,
        _FakeTTSProvider([], error=asyncio.CancelledError()),
    )
    interrupt_service = _service(database, storage, interrupt_provider, monkeypatch)
    interrupted = _create_job(
        repository,
        project_id,
        chapter_id,
        shot_id,
        _job_input(shot_id, dialogue_line_id="line-interrupted", signature="sig-interrupted"),
        job_id="job-dynamic-audio-interrupted",
    )

    interrupted_result = asyncio.run(interrupt_service.handle_job(interrupted))

    assert interrupted_result["status"] == "interrupted"
    assert repository.get(interrupted["id"])["status"] == "interrupted"
    with database.engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count()).select_from(shot_audio).where(shot_audio.c.shot_id == shot_id)
            )
            == 0
        )


@pytest.mark.parametrize(
    ("run_status", "stage_status"),
    [("cancelled", "running"), ("running", "stale")],
)
def test_dynamic_audio_job_does_not_call_provider_after_run_or_stage_is_inactive(
    audio_context,
    monkeypatch: pytest.MonkeyPatch,
    run_status: str,
    stage_status: str,
) -> None:
    database, repository, storage, project_id, chapter_id, shot_id = audio_context
    calls: list[dict[str, Any]] = []
    fake_provider = _FakeProviderService(database, _FakeTTSProvider(calls))
    service = _service(database, storage, fake_provider, monkeypatch)
    record = _create_job(
        repository,
        project_id,
        chapter_id,
        shot_id,
        _job_input(shot_id),
        job_id=f"job-dynamic-audio-inactive-{run_status}-{stage_status}",
    )
    with database.engine.begin() as connection:
        connection.execute(
            update(workflow_run)
            .where(workflow_run.c.id == "run-dynamic-audio")
            .values(status=run_status)
        )
        connection.execute(
            update(workflow_stage)
            .where(workflow_stage.c.id == "stage-dynamic-audio")
            .values(status=stage_status)
        )

    result = asyncio.run(service.handle_job(record))

    assert result["status"] == "failed"
    assert calls == []
    assert repository.get(record["id"])["status"] == "failed"
    with database.engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(shot_audio).where(shot_audio.c.shot_id == shot_id)
        ) == 0


def test_dynamic_audio_job_blocks_stage_signature_mismatch_before_provider_call(
    audio_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, repository, storage, project_id, chapter_id, shot_id = audio_context
    calls: list[dict[str, Any]] = []
    fake_provider = _FakeProviderService(database, _FakeTTSProvider(calls))
    service = _service(database, storage, fake_provider, monkeypatch)
    input_data = _job_input(shot_id)
    input_data["upstream_input_signature"] = "wrong-stage-signature"
    record = _create_job(
        repository,
        project_id,
        chapter_id,
        shot_id,
        input_data,
        job_id="job-dynamic-audio-stage-signature-mismatch",
    )
    with database.engine.begin() as connection:
        connection.execute(
            update(workflow_stage)
            .where(workflow_stage.c.id == "stage-dynamic-audio")
            .values(input_signature="actual-stage-signature")
        )

    result = asyncio.run(service.handle_job(record))

    assert result["status"] == "failed"
    assert calls == []
    assert "upstream_input_signature" in repository.get(record["id"])["error"]
