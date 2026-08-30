from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import inspect

from app.db import Database
from app.jobs.repository import JobRepository
from app.jobs.runner import JobRunner
from app.services import task_handlers
from app.services.task_handlers import register_handlers


class _RecordingRunner:
    def __init__(self, repository: JobRepository) -> None:
        self.repository = repository
        self.handlers: dict[str, Any] = {}

    def register_handler(self, job_type: str, handler: Any, *, replace: bool = False) -> None:
        del replace
        self.handlers[job_type] = handler


class _FakeDynamicAudioJobService:
    instances: list[_FakeDynamicAudioJobService] = []

    def __init__(self, database: Database) -> None:
        self.database = database
        self.calls: list[dict[str, Any]] = []
        self.instances.append(self)

    async def handle_job(self, record: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(record)
        return {"status": "completed", "job_id": record["id"]}


@pytest.fixture
def recording_runner(tmp_path: Path) -> Iterator[tuple[Database, _RecordingRunner]]:
    database = Database(f"sqlite:///{(tmp_path / 'dynamic-audio-handler.db').as_posix()}")
    runner = _RecordingRunner(JobRepository(database))
    try:
        yield database, runner
    finally:
        database.dispose()


def test_register_handlers_adds_dynamic_audio_handler_without_creating_data(
    recording_runner: tuple[Database, _RecordingRunner],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, runner = recording_runner
    _FakeDynamicAudioJobService.instances.clear()
    monkeypatch.setattr(task_handlers, "DynamicAudioJobService", _FakeDynamicAudioJobService)

    register_handlers(database=database, runner=cast(JobRunner, runner))

    assert "generate_dialogue_audio" in runner.handlers
    assert "generate_video" in runner.handlers
    assert "dynamic_stage_generate" in runner.handlers
    assert list(runner.handlers)[-1] == "dynamic_stage_generate"
    assert len(_FakeDynamicAudioJobService.instances) == 1
    assert _FakeDynamicAudioJobService.instances[0].database is database
    assert inspect(database.engine).has_table("job") is False


def test_dynamic_audio_handler_is_async_and_executes_tts_record(
    recording_runner: tuple[Database, _RecordingRunner],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, runner = recording_runner
    _FakeDynamicAudioJobService.instances.clear()
    monkeypatch.setattr(task_handlers, "DynamicAudioJobService", _FakeDynamicAudioJobService)

    register_handlers(database=database, runner=cast(JobRunner, runner))
    handler = runner.handlers["generate_dialogue_audio"]
    record = {"id": "job-dynamic-audio-handler", "provider_kind": "tts"}

    assert asyncio.iscoroutinefunction(handler)
    assert asyncio.run(handler(record)) == {"status": "completed", "job_id": record["id"]}
    assert _FakeDynamicAudioJobService.instances[0].calls == [record]
