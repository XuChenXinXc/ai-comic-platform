from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import insert, select, update

from app.db import Database, migrate_schema, utc_now
from app.jobs.dynamic_recovery import (
    reconcile_dynamic_workflow_runs,
    recover_dynamic_jobs,
)
from app.jobs.recovery import recover_jobs
from app.jobs.repository import JobRepository
from app.models.tables import chapter, project, workflow_run, workflow_stage


@pytest.fixture
def recovery_context(tmp_path: Path) -> Iterator[tuple[Database, JobRepository]]:
    database = Database(f"sqlite:///{(tmp_path / 'dynamic-recovery.db').as_posix()}")
    migrate_schema(database)
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(project).values(
                id="project-recovery",
                name="动态恢复测试项目",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(chapter).values(
                id="chapter-recovery",
                project_id="project-recovery",
                chapter_no=1,
                title="动态恢复测试章节",
                created_at=now,
                updated_at=now,
            )
        )
        _insert_workflow(connection, run_id="run-recovery", stage_id="stage-recovery")
    try:
        yield database, JobRepository(database)
    finally:
        database.dispose()


def _insert_workflow(
    connection: Any,
    *,
    run_id: str,
    stage_id: str,
    run_status: str = "running",
    stage_status: str = "queued",
    run_revision: int = 1,
    stage_signature: str = "stage-signature",
) -> None:
    now = utc_now()
    connection.execute(
        insert(workflow_run).values(
            id=run_id,
            project_id="project-recovery",
            chapter_id="chapter-recovery",
            source_kind="full_text",
            status=run_status,
            current_stage="0-source",
            revision=run_revision,
            created_at=now,
            updated_at=now,
        )
    )
    connection.execute(
        insert(workflow_stage).values(
            id=stage_id,
            workflow_run_id=run_id,
            stage_key="0-source",
            ordinal=0,
            status=stage_status,
            input_signature=stage_signature,
            created_at=now,
            updated_at=now,
        )
    )


def _job_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "project_id": "project-recovery",
        "chapter_id": "chapter-recovery",
        "job_type": "dynamic_stage_generate",
        "provider_kind": "text",
        "workflow_run_id": "run-recovery",
        "workflow_stage_id": "stage-recovery",
        "input_signature": "stage-signature",
    }
    data.update(overrides)
    return data


def _set_workflow_status(
    database: Database,
    *,
    run_id: str,
    stage_id: str,
    run_status: str,
    stage_status: str,
) -> None:
    with database.engine.begin() as connection:
        connection.execute(
            update(workflow_run).where(workflow_run.c.id == run_id).values(status=run_status)
        )
        connection.execute(
            update(workflow_stage)
            .where(workflow_stage.c.id == stage_id)
            .values(status=stage_status)
        )


def test_legacy_jobs_stay_out_of_dynamic_recovery_and_old_recovery_sees_only_legacy(
    recovery_context: tuple[Database, JobRepository],
) -> None:
    database, repository = recovery_context
    legacy_queued = repository.create_job(
        _job_data(workflow_run_id=None, workflow_stage_id=None, job_type="legacy_job")
    )
    legacy_running = repository.create_job(
        _job_data(workflow_run_id=None, workflow_stage_id=None, job_type="legacy_job")
    )
    repository.update(legacy_running["id"], status="running")
    dynamic = repository.create_job(_job_data())

    dynamic_report = recover_dynamic_jobs(repository)
    assert dynamic_report.enqueue_ids == [dynamic["id"]]
    assert dynamic_report.interrupted_ids == []
    assert repository.require(legacy_running["id"])["status"] == "running"

    legacy_recovered = recover_jobs(repository)
    assert legacy_recovered == [legacy_queued["id"]]
    assert repository.require(legacy_running["id"])["status"] == "interrupted"
    assert dynamic["id"] not in legacy_recovered
    database.dispose()


def test_queued_dynamic_job_with_matching_stage_signature_is_recovered(
    recovery_context: tuple[Database, JobRepository],
) -> None:
    database, repository = recovery_context
    record = repository.create_job(_job_data())

    report = recover_dynamic_jobs(repository)

    assert report.enqueue_ids == [record["id"]]
    assert report.skipped_ids == []
    assert repository.require(record["id"])["status"] == "queued"
    database.dispose()


def test_recovery_reconciles_missing_queued_stage_job_idempotently(
    recovery_context: tuple[Database, JobRepository],
) -> None:
    database, repository = recovery_context

    first = recover_dynamic_jobs(repository)
    created_ids = [record["id"] for record in repository.list_by_workflow("run-recovery")]
    second = recover_dynamic_jobs(repository)

    assert len(created_ids) == 1
    assert first.enqueue_ids == created_ids
    assert second.enqueue_ids == created_ids
    assert repository.require(created_ids[0])["status"] == "queued"
    database.dispose()


@pytest.mark.parametrize(
    ("stage_status", "expected_job_status", "expected_report"),
    [("stale", "interrupted", "interrupted"), ("blocked", "interrupted", "interrupted")],
)
def test_stale_or_blocked_stage_is_not_recovered(
    recovery_context: tuple[Database, JobRepository],
    stage_status: str,
    expected_job_status: str,
    expected_report: str,
) -> None:
    database, repository = recovery_context
    _set_workflow_status(
        database,
        run_id="run-recovery",
        stage_id="stage-recovery",
        run_status="queued",
        stage_status=stage_status,
    )
    record = repository.create_job(_job_data())

    report = recover_dynamic_jobs(repository)

    assert repository.require(record["id"])["status"] == expected_job_status
    assert getattr(report, f"{expected_report}_ids") == [record["id"]]
    assert _workflow_status(repository, "run-recovery", "stage-recovery")[1] == stage_status
    database.dispose()


def test_queued_job_with_running_stage_is_skipped_without_state_change(
    recovery_context: tuple[Database, JobRepository],
) -> None:
    database, repository = recovery_context
    _set_workflow_status(
        database,
        run_id="run-recovery",
        stage_id="stage-recovery",
        run_status="running",
        stage_status="running",
    )
    record = repository.create_job(_job_data())

    report = recover_dynamic_jobs(repository)

    assert report.enqueue_ids == []
    assert report.skipped_ids == [record["id"]]
    assert repository.require(record["id"])["status"] == "queued"
    assert _workflow_status(repository, "run-recovery", "stage-recovery") == (
        "running",
        "running",
    )
    database.dispose()


def test_running_job_with_ready_stage_is_interrupted_not_skipped_forever(
    recovery_context: tuple[Database, JobRepository],
) -> None:
    """崩溃落在「stage 已写 ready、job 未写 completed」窗口时，Job 必须中断。

    回归：此前状态组合不在允许集时仅静默 skip，Job 永久卡 running，
    且 stage 不再 queued/running，reconcile 永远不再处理该 Job。
    """
    database, repository = recovery_context
    record = repository.create_job(_job_data())
    repository.update(record["id"], status="running")
    _set_workflow_status(
        database,
        run_id="run-recovery",
        stage_id="stage-recovery",
        run_status="running",
        stage_status="ready",
    )

    report = recover_dynamic_jobs(repository)

    assert report.enqueue_ids == []
    assert report.skipped_ids == []
    assert report.interrupted_ids == [record["id"]]
    assert repository.require(record["id"])["status"] == "interrupted"
    # stage 自身状态由正常流程写好，恢复过程不再改动。
    assert _workflow_status(repository, "run-recovery", "stage-recovery") == (
        "running",
        "ready",
    )
    database.dispose()


def test_provider_task_and_saved_download_result_are_requeued_safely(
    recovery_context: tuple[Database, JobRepository],
) -> None:
    database, repository = recovery_context
    running = repository.create_job(_job_data(job_type="dynamic_video_task"))
    waiting = repository.create_job(_job_data(job_type="dynamic_video_task"))
    downloading = repository.create_job(_job_data(job_type="dynamic_video_task"))
    repository.set_provider_task(running["id"], "provider-running", status="running")
    repository.set_provider_task(waiting["id"], "provider-waiting", status="waiting_provider")
    repository.set_provider_task(
        downloading["id"],
        "provider-downloading",
        status="downloading",
        result_data={"result_url": "https://example.invalid/result.mp4"},
    )

    report = recover_dynamic_jobs(repository)

    assert set(report.enqueue_ids) == {running["id"], waiting["id"], downloading["id"]}
    assert report.interrupted_ids == []
    assert report.failed_ids == []
    assert repository.require(running["id"])["status"] == "queued"
    assert repository.require(waiting["id"])["status"] == "queued"
    assert repository.require(downloading["id"])["status"] == "queued"
    assert repository.require(running["id"])["message"] == "服务重启后继续轮询原 Provider 任务"
    assert (
        repository.require(downloading["id"])["message"]
        == "服务重启后继续下载已保存的 Provider 结果"
    )
    database.dispose()


def test_running_job_without_provider_task_or_result_is_interrupted_with_workflow_updates(
    recovery_context: tuple[Database, JobRepository],
) -> None:
    database, repository = recovery_context
    record = repository.create_job(_job_data())
    repository.update(record["id"], status="running")

    report = recover_dynamic_jobs(repository)

    assert report.enqueue_ids == []
    assert report.interrupted_ids == [record["id"]]
    assert report.failed_ids == []
    assert repository.require(record["id"])["status"] == "interrupted"
    assert "没有 Provider task_id" in repository.require(record["id"])["error"]
    assert _workflow_status(repository, "run-recovery", "stage-recovery") == (
        "interrupted",
        "interrupted",
    )
    database.dispose()


def test_downloading_job_without_saved_result_is_failed_with_partial_failed_workflow_updates(
    recovery_context: tuple[Database, JobRepository],
) -> None:
    database, repository = recovery_context
    record = repository.create_job(_job_data(job_type="dynamic_video_task"))
    repository.set_provider_task(record["id"], "provider-downloading", status="downloading")

    report = recover_dynamic_jobs(repository)

    assert report.enqueue_ids == []
    assert report.failed_ids == [record["id"]]
    assert report.interrupted_ids == []
    assert report.partial_failed_stage_ids == ["stage-recovery"]
    assert repository.require(record["id"])["status"] == "failed"
    assert "缺少已保存结果地址" in repository.require(record["id"])["error"]
    assert _workflow_status(repository, "run-recovery", "stage-recovery") == (
        "partial_failed",
        "partial_failed",
    )
    database.dispose()


@pytest.mark.parametrize(
    "run_status, stage_status",
    [
        ("blocked", "blocked"),
        ("cancelled", "cancelled"),
        ("stale", "queued"),
        ("confirmed", "queued"),
    ],
)
def test_blocked_or_cancelled_workflow_does_not_reactivate_dynamic_job(
    recovery_context: tuple[Database, JobRepository],
    run_status: str,
    stage_status: str,
) -> None:
    database, repository = recovery_context
    _set_workflow_status(
        database,
        run_id="run-recovery",
        stage_id="stage-recovery",
        run_status=run_status,
        stage_status=stage_status,
    )
    record = repository.create_job(_job_data())

    report = recover_dynamic_jobs(repository)

    # B-M3：受保护 run 的非终态 Job 不再永久悬浮，统一标记 interrupted；
    # 受保护的 run/stage 状态本身保持不变。
    assert report.enqueue_ids == []
    assert report.interrupted_ids == [record["id"]]
    assert repository.require(record["id"])["status"] == "interrupted"
    assert "受保护" in repository.require(record["id"])["error"]
    assert _workflow_status(repository, "run-recovery", "stage-recovery") == (
        run_status,
        stage_status,
    )
    database.dispose()


def test_changed_job_and_stage_signature_interrupts_job_and_stales_stage(
    recovery_context: tuple[Database, JobRepository],
) -> None:
    database, repository = recovery_context
    _set_workflow_status(
        database,
        run_id="run-recovery",
        stage_id="stage-recovery",
        run_status="running",
        stage_status="running",
    )
    with database.engine.begin() as connection:
        connection.execute(
            update(workflow_stage)
            .where(workflow_stage.c.id == "stage-recovery")
            .values(input_signature="stage-signature")
        )
    record = repository.create_job(_job_data(input_signature="job-signature"))

    report = recover_dynamic_jobs(repository)

    assert report.enqueue_ids == []
    assert report.interrupted_ids == [record["id"]]
    assert "输入签名已变化" in repository.require(record["id"])["error"]
    assert _workflow_status(repository, "run-recovery", "stage-recovery") == (
        "interrupted",
        "stale",
    )
    database.dispose()


def test_reconcile_marks_partial_failed_without_resubmitting_terminal_jobs(
    recovery_context: tuple[Database, JobRepository],
) -> None:
    database, repository = recovery_context
    _set_workflow_status(
        database,
        run_id="run-recovery",
        stage_id="stage-recovery",
        run_status="running",
        stage_status="running",
    )
    completed = repository.create_job(_job_data(idempotency_key="completed-job"))
    failed = repository.create_job(_job_data(idempotency_key="failed-job"))
    repository.complete(completed["id"], {"ok": True})
    repository.fail(failed["id"], "Provider 任务失败")

    report = recover_dynamic_jobs(repository)

    assert report.enqueue_ids == []
    assert report.interrupted_ids == []
    assert report.failed_ids == []
    assert report.partial_failed_stage_ids == ["stage-recovery"]
    assert repository.require(completed["id"])["status"] == "completed"
    assert repository.require(failed["id"])["status"] == "failed"
    assert _workflow_status(repository, "run-recovery", "stage-recovery") == (
        "partial_failed",
        "partial_failed",
    )
    database.dispose()


@pytest.mark.parametrize("stage_status", ["blocked", "stale", "cancelled", "confirmed"])
def test_reconcile_does_not_overwrite_protected_stage(
    recovery_context: tuple[Database, JobRepository],
    stage_status: str,
) -> None:
    database, repository = recovery_context
    _set_workflow_status(
        database,
        run_id="run-recovery",
        stage_id="stage-recovery",
        run_status="running",
        stage_status=stage_status,
    )
    completed = repository.create_job(_job_data(idempotency_key=f"completed-{stage_status}"))
    failed = repository.create_job(_job_data(idempotency_key=f"failed-{stage_status}"))
    repository.complete(completed["id"], {"ok": True})
    repository.fail(failed["id"], "Provider 任务失败")

    assert reconcile_dynamic_workflow_runs(repository) == []
    assert _workflow_status(repository, "run-recovery", "stage-recovery") == (
        "running",
        stage_status,
    )
    with repository.engine.connect() as connection:
        revision = connection.scalar(
            select(workflow_run.c.revision).where(workflow_run.c.id == "run-recovery")
        )
    assert revision == 1
    database.dispose()


def test_recovery_report_only_contains_recoverable_ids_and_interrupts_lost_running_jobs(
    recovery_context: tuple[Database, JobRepository],
) -> None:
    """enqueue_recovered_dynamic_jobs 已删除；恢复报告语义保持不变。

    - 只有可安全恢复的 queued Job 进入 enqueue_ids；
    - running 且无 task_id/结果的任务被标记 interrupted；
    - 入队失败（enqueue 返回 False）由调用方处理，Job 保持 queued。
    """
    database, repository = recovery_context
    with database.engine.begin() as connection:
        _insert_workflow(
            connection,
            run_id="run-recovery-running",
            stage_id="stage-recovery-running",
            run_status="running",
            stage_status="running",
        )
    queued = repository.create_job(_job_data(idempotency_key="enqueue-queued"))
    running = repository.create_job(
        _job_data(
            workflow_run_id="run-recovery-running",
            workflow_stage_id="stage-recovery-running",
            idempotency_key="enqueue-running",
        )
    )
    repository.update(running["id"], status="running")

    report = recover_dynamic_jobs(repository)

    assert report.enqueue_ids == [queued["id"]]
    assert report.interrupted_ids == [running["id"]]
    assert repository.require(queued["id"])["status"] == "queued"
    assert repository.require(running["id"])["status"] == "interrupted"
    database.dispose()


def test_recovery_isolates_run_on_completed_job_with_queued_stage_invariant_violation(
    recovery_context: tuple[Database, JobRepository],
) -> None:
    """C3：「Job completed 但 stage 仍 queued」时隔离该 run，不再抛异常。"""
    database, repository = recovery_context
    record = repository.create_job(_job_data())
    repository.complete(record["id"], {"ok": True})
    # 人为保持 stage 为 queued，制造不变量违反。

    report = recover_dynamic_jobs(repository)

    assert report.enqueue_ids == []
    run_error = _run_error(repository, "run-recovery")
    assert run_error is not None
    assert "已隔离" in run_error
    assert "stage-recovery" in run_error
    assert _workflow_status(repository, "run-recovery", "stage-recovery") == (
        "interrupted",
        "queued",
    )
    database.dispose()


def test_recovery_isolation_skips_bad_run_and_continues_other_runs(
    recovery_context: tuple[Database, JobRepository],
) -> None:
    """C3：隔离坏 run 后，其余 run 的恢复不受影响。"""
    database, repository = recovery_context
    with database.engine.begin() as connection:
        _insert_workflow(
            connection,
            run_id="run-broken",
            stage_id="stage-broken",
            run_status="running",
            stage_status="queued",
        )
        _insert_workflow(
            connection,
            run_id="run-healthy",
            stage_id="stage-healthy",
            run_status="running",
            stage_status="queued",
        )
    broken_job = repository.create_job(
        _job_data(
            workflow_run_id="run-broken",
            workflow_stage_id="stage-broken",
            idempotency_key="broken-job",
        )
    )
    repository.complete(broken_job["id"], {"ok": True})
    healthy_job = repository.create_job(
        _job_data(
            workflow_run_id="run-healthy",
            workflow_stage_id="stage-healthy",
            idempotency_key="healthy-job",
        )
    )

    report = recover_dynamic_jobs(repository)

    assert healthy_job["id"] in report.enqueue_ids
    assert broken_job["id"] not in report.enqueue_ids
    assert _run_error(repository, "run-broken") is not None
    assert _run_error(repository, "run-healthy") == ""
    database.dispose()


def test_recovery_skips_run_when_record_processing_raises(
    recovery_context: tuple[Database, JobRepository],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C3：单 run 处理异常时记录日志并跳过，不让整体恢复失败。"""
    database, repository = recovery_context
    with database.engine.begin() as connection:
        _insert_workflow(
            connection,
            run_id="run-healthy",
            stage_id="stage-healthy",
            run_status="running",
            stage_status="queued",
        )
    healthy_job = repository.create_job(
        _job_data(
            workflow_run_id="run-healthy",
            workflow_stage_id="stage-healthy",
            idempotency_key="healthy-job-2",
        )
    )
    bad_job = repository.create_job(_job_data(idempotency_key="bad-job"))
    original_load = __import__(
        "app.jobs.dynamic_recovery", fromlist=["_load_workflow_records"]
    )._load_workflow_records

    def broken_load(repo: JobRepository, record: dict) -> tuple[None, None]:
        if record.get("id") == bad_job["id"]:
            raise RuntimeError("模拟恢复期数据库异常")
        return original_load(repo, record)

    monkeypatch.setattr(
        "app.jobs.dynamic_recovery._load_workflow_records", broken_load
    )

    report = recover_dynamic_jobs(repository)

    # 坏 run（run-recovery）被跳过：bad_job 保持 queued，未进入任何终态处理。
    assert repository.require(bad_job["id"])["status"] == "queued"
    # 健康 run 不受影响。
    assert healthy_job["id"] in report.enqueue_ids
    database.dispose()


def _run_error(repository: JobRepository, run_id: str) -> str | None:
    with repository.engine.connect() as connection:
        value = connection.scalar(
            workflow_run.select()
            .with_only_columns(workflow_run.c.error)
            .where(workflow_run.c.id == run_id)
        )
    return value


def _workflow_status(repository: JobRepository, run_id: str, stage_id: str) -> tuple[str, str]:
    with repository.engine.connect() as connection:
        run_status = connection.scalar(
            workflow_run.select()
            .with_only_columns(workflow_run.c.status)
            .where(workflow_run.c.id == run_id)
        )
        stage_status = connection.scalar(
            workflow_stage.select()
            .with_only_columns(workflow_stage.c.status)
            .where(workflow_stage.c.id == stage_id)
        )
    return str(run_status), str(stage_status)
