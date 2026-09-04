"""Review 修复回归：runner compose 池、handler None 返回、complete/fail 竞态、并发 enqueue。

覆盖项：
- B-H2：合成 Job 路由到独立 compose 线程池（并发 1）。
- C3 根因侧：handler 返回 None 且 Job 未进终态时标记 failed，
  不再隐式 complete（避免「Job completed 但 stage 仍 queued」）。
- B-M4：complete/fail 的条件 UPDATE 不覆盖并发写入的 cancelled 终态。
- B-M5：并发 enqueue（BEGIN IMMEDIATE）不触发 SQLITE_BUSY，任务只执行一次。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import insert

from app.db import Database, utc_now
from app.jobs.repository import JobRepository
from app.jobs.runner import JobRunner
from app.models.tables import project
from app.schemas import JobCreate


def _repository(tmp_path: Path) -> tuple[Database, JobRepository]:
    database = Database(f"sqlite:///{(tmp_path / 'review-runner.db').as_posix()}")
    database.create_all()
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(project).values(
                id="project-1",
                name="回归测试项目",
                created_at=now,
                updated_at=now,
            )
        )
    return database, JobRepository(database)


def _wait_for_status(
    repository: JobRepository,
    job_id: str,
    expected: str,
    timeout: float = 5,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = repository.require(job_id)
        if record["status"] == expected:
            return record
        time.sleep(0.01)
    pytest.fail(f"任务未进入预期状态：{expected}")


def test_compose_pool_is_created_and_routed_with_provider_kind() -> None:
    """B-H2：runner 默认创建 compose 池；provider_kind=compose 的 Job 路由到该池。"""
    runner = JobRunner(worker_limits={"text": 1, "image": 1, "video": 1})
    try:
        assert runner.worker_limits["compose"] == 1
        assert "compose" in runner._executors
        assert JobRunner._provider_kind({"provider_kind": "compose"}) == "compose"
        # 未声明 provider_kind 的历史合成 Job 按 job_type 兜底路由。
        assert JobRunner._provider_kind({"job_type": "compose_chapter"}) == "compose"
        assert JobRunner._provider_kind({"job_type": "generate_video"}) == "video"
    finally:
        runner.shutdown()


def test_compose_job_executes_in_compose_executor_thread(tmp_path: Path) -> None:
    """B-H2：compose Job 在 comic-compose 线程池中执行，不占用 text 池。"""
    database, repository = _repository(tmp_path)
    runner = JobRunner(repository, {"text": 1, "image": 1, "video": 1})
    seen_threads: list[str] = []

    def handler(_record: dict[str, Any]) -> dict[str, Any]:
        seen_threads.append(threading.current_thread().name)
        return {"ok": True}

    runner.register_handler("compose_chapter", handler)
    record = repository.create_job(
        JobCreate(
            project_id="project-1",
            job_type="compose_chapter",
            provider_kind="compose",
        )
    )
    try:
        assert runner.enqueue(record["id"]) is True
        completed = _wait_for_status(repository, record["id"], "completed")
        assert completed["result_data"] == {"ok": True}
        assert seen_threads and seen_threads[0].startswith("comic-compose")
    finally:
        runner.shutdown()
        database.dispose()


def test_handler_returning_none_marks_job_failed_instead_of_completing(
    tmp_path: Path,
) -> None:
    """C3 根因侧：handler 返回 None 时标记 failed，绝不隐式 complete。"""
    database, repository = _repository(tmp_path)
    runner = JobRunner(repository, {"text": 1, "image": 1, "video": 1})

    def handler(_record: dict[str, Any]) -> None:
        # 模拟动态 handler 并发冲突回滚后返回 None。
        return None

    runner.register_handler("analyze_novel", handler)
    record = repository.create_job(
        JobCreate(project_id="project-1", job_type="analyze_novel")
    )
    try:
        runner.enqueue(record["id"])
        failed = _wait_for_status(repository, record["id"], "failed")
        assert "处理器未返回结果" in failed["error"]
    finally:
        runner.shutdown()
        database.dispose()


def test_handler_returning_none_keeps_handler_written_intermediate_status(
    tmp_path: Path,
) -> None:
    """C3 根因侧：handler 自行写入中间态（waiting_provider）时返回 None 不覆盖。"""
    database, repository = _repository(tmp_path)
    runner = JobRunner(repository, {"text": 1, "image": 1, "video": 1})

    def handler(record: dict[str, Any]) -> None:
        # handler 已把任务转入 waiting_provider（例如等待 Provider 轮询）。
        repository.set_status(record["id"], "waiting_provider")
        return None

    runner.register_handler("analyze_novel", handler)
    record = repository.create_job(
        JobCreate(project_id="project-1", job_type="analyze_novel")
    )
    try:
        runner.enqueue(record["id"])
        deadline = time.monotonic() + 5
        status = ""
        while time.monotonic() < deadline:
            status = repository.require(record["id"])["status"]
            if status == "waiting_provider":
                break
            time.sleep(0.01)
        assert status == "waiting_provider"
    finally:
        runner.shutdown()
        database.dispose()


def test_complete_does_not_overwrite_concurrent_cancelled_job(tmp_path: Path) -> None:
    """B-M4：complete 读取与写入之间任务被并发取消时，条件 UPDATE 不覆盖。"""
    database, repository = _repository(tmp_path)
    record = repository.create_job(
        JobCreate(project_id="project-1", job_type="analyze_novel")
    )
    original_require = repository.require
    state = {"armed": False}

    def require_with_injected_cancel(job_id: str) -> dict[str, Any]:
        current = original_require(job_id)
        if job_id == record["id"] and not state["armed"]:
            state["armed"] = True
            # 模拟竞态：complete 读取之后、UPDATE 之前任务被并发取消。
            repository.cancel(job_id)
        return current

    repository.require = require_with_injected_cancel  # type: ignore[method-assign]
    returned = repository.complete(record["id"], {"ok": True})

    final = original_require(record["id"])
    assert final["status"] == "cancelled"
    assert returned["status"] == "cancelled"
    assert final["result_data"] == {}
    database.dispose()


def test_fail_does_not_overwrite_concurrent_cancelled_job(tmp_path: Path) -> None:
    """B-M4：fail 同样不覆盖并发写入的 cancelled 终态。"""
    database, repository = _repository(tmp_path)
    record = repository.create_job(
        JobCreate(project_id="project-1", job_type="analyze_novel")
    )
    original_require = repository.require
    state = {"armed": False}

    def require_with_injected_cancel(job_id: str) -> dict[str, Any]:
        current = original_require(job_id)
        if job_id == record["id"] and not state["armed"]:
            state["armed"] = True
            repository.cancel(job_id)
        return current

    repository.require = require_with_injected_cancel  # type: ignore[method-assign]
    returned = repository.fail(record["id"], "迟到的失败")

    final = original_require(record["id"])
    assert final["status"] == "cancelled"
    assert returned["status"] == "cancelled"
    database.dispose()


def test_concurrent_enqueue_of_same_job_never_raises_and_executes_once(
    tmp_path: Path,
) -> None:
    """B-M5：多个线程同时 enqueue 同一 Job（BEGIN IMMEDIATE 路径）不报错、只执行一次。"""
    database, repository = _repository(tmp_path)
    runner = JobRunner(repository, {"text": 1, "image": 1, "video": 1})
    started = threading.Event()
    release = threading.Event()
    call_count = 0
    lock = threading.Lock()

    def handler(_record: dict[str, Any]) -> dict[str, Any]:
        nonlocal call_count
        with lock:
            call_count += 1
        started.set()
        release.wait(timeout=5)
        return {"ok": True}

    runner.register_handler("analyze_novel", handler)
    record = repository.create_job(
        JobCreate(project_id="project-1", job_type="analyze_novel")
    )
    barrier = threading.Barrier(4)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            runner.enqueue(record["id"])
        except BaseException as exc:  # noqa: BLE001 — 回归测试需要收集所有异常
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert started.wait(timeout=5) is True
        release.set()
        completed = _wait_for_status(repository, record["id"], "completed")
        assert completed["result_data"] == {"ok": True}
    finally:
        release.set()
        runner.shutdown()
        for thread in threads:
            thread.join(timeout=5)
        database.dispose()

    assert errors == []
    assert call_count == 1
