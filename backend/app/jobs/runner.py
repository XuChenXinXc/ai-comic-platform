from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from sqlalchemy import select, update

from app.config import get_settings
from app.dynamic_pipeline.stage_catalog import get_stage_prerequisites
from app.jobs.recovery import (
    recover_jobs,  # noqa: F401  # 保留 export 兼容，recover() 已统一走 recover_all_once
)
from app.jobs.recovery_all import recover_all_once
from app.jobs.repository import (
    NON_TERMINAL_STATUSES,
    JobRepository,
    job_repository,
)
from app.models.tables import job as job_table
from app.models.tables import workflow_run, workflow_stage
from app.services.utils import sanitize_error_message, utc_now

JobHandler = Callable[
    [dict[str, Any]],
    dict[str, Any] | None | Awaitable[dict[str, Any] | None],
]


class JobRunner:
    def __init__(
        self,
        repository: JobRepository | None = None,
        worker_limits: Mapping[str, int] | None = None,
    ) -> None:
        self.repository = repository or job_repository
        limits = dict(worker_limits or get_settings().worker_limits)
        for kind in ("text", "image", "video"):
            if int(limits.get(kind, 0)) < 1:
                raise ValueError(f"{kind} 线程数不能小于 1")
        self.worker_limits = {kind: int(limits[kind]) for kind in ("text", "image", "video")}
        self._executors = {
            kind: ThreadPoolExecutor(
                max_workers=limit,
                thread_name_prefix=f"comic-{kind}",
            )
            for kind, limit in self.worker_limits.items()
        }
        self._handlers: dict[str, JobHandler] = {}
        self._futures: dict[str, Future[None]] = {}
        self._lock = threading.RLock()
        self._started = False
        self._shutdown = False

    def register_handler(
        self,
        job_type: str,
        handler: JobHandler,
        *,
        replace: bool = False,
    ) -> None:
        if job_type in self._handlers and not replace:
            raise ValueError(f"任务处理器已注册：{job_type}")
        self._handlers[job_type] = handler

    def register_handlers(
        self,
        handlers: Mapping[str, JobHandler],
        *,
        replace: bool = False,
    ) -> None:
        for job_type, handler in handlers.items():
            self.register_handler(job_type, handler, replace=replace)

    def start(self, *, recover: bool = True) -> list[str]:
        with self._lock:
            if self._shutdown:
                raise RuntimeError("JobRunner 已关闭")
            if self._started:
                return []
            self._started = True
        return self.recover() if recover else []

    def recover(self) -> list[str]:
        recovered = recover_all_once(self.repository)
        for job_id in recovered:
            self.enqueue(job_id)
        return recovered

    def enqueue(self, job_id: str) -> bool:
        if self._shutdown:
            raise RuntimeError("JobRunner 已关闭")
        # 在 dbapi 层发 BEGIN IMMEDIATE，绕过 SQLAlchemy isolation_level 限制，
        # 避免两个并发 enqueue 在 SQLite SHARED->RESERVED 升级路径上撞 SQLITE_BUSY。
        with self.repository.engine.connect() as conn:
            try:
                # SQLAlchemy 2.0: conn.connection.dbapi_connection 拿 dbapi 连接
                dbapi_conn = conn.connection.dbapi_connection
            except AttributeError:
                # 兼容旧版本/不同 driver 包装
                dbapi_conn = conn.connection.driver_connection
            # 显式升级 SQLite 事务到 IMMEDIATE，写锁被立即拿到
            dbapi_conn.execute("BEGIN IMMEDIATE")
            # 后续 read 都在 IMMEDIATE 事务内
            record = self.repository.get(job_id, connection=conn)
            if record is None or record["status"] not in NON_TERMINAL_STATUSES:
                dbapi_conn.execute("ROLLBACK")
                return False
            dependency_state, dependency_error = self._dependency_state(
                record, connection=conn
            )
            dbapi_conn.execute("COMMIT")
        if dependency_error is not None:
            if self._fail_dependency_blocked_job(record, dependency_error):
                self._propagate_dependency_failure(job_id, dependency_error)
            return False
        if dependency_state == "pending":
            if record["status"] != "queued":
                self.repository.requeue_for_recovery(
                    job_id,
                    expected_status=record["status"],
                    message="任务依赖尚未完成，保持 queued",
                )
            return False
        with self._lock:
            active = self._futures.get(job_id)
            if active is not None and not active.done():
                return False
            kind = self._provider_kind(record)
            future = self._executors[kind].submit(self._execute, job_id)
            self._futures[job_id] = future
        future.add_done_callback(lambda completed, item=job_id: self._forget(item, completed))
        return True

    def _forget(self, job_id: str, completed: Future[None]) -> None:
        with self._lock:
            if self._futures.get(job_id) is completed:
                self._futures.pop(job_id, None)

    def _execute(self, job_id: str) -> None:
        record = self.repository.get(job_id)
        if record is None:
            return
        dependency_state, dependency_error = self._dependency_state(record)
        if dependency_error is not None:
            if self._fail_dependency_blocked_job(record, dependency_error):
                self._propagate_dependency_failure(job_id, dependency_error)
            return
        if dependency_state == "pending":
            if record["status"] != "queued":
                self.repository.requeue_for_recovery(
                    job_id,
                    expected_status=record["status"],
                    message="任务依赖尚未完成，保持 queued",
                )
            return
        if record["status"] in {"queued", "waiting_provider", "downloading"}:
            record = self.repository.claim(job_id, expected_status=record["status"])
            if record is None:
                return
        else:
            return
        handler = self._handlers.get(record["job_type"])
        if handler is None:
            error = f"未注册任务处理器：{record['job_type']}"
            self.repository.fail(job_id, error)
            self._mark_dynamic_failure_state(record, error)
            self._propagate_dependency_failure(job_id, error)
            return
        try:
            result = handler(record)
            if inspect.isawaitable(result):
                # 在工作线程里运行 async handler：使用独立事件循环而不是
                # asyncio.run()，避免与线程池/连接池/全局 session 生命周期冲突。
                # Provider adapter 内部可能维护 httpx / aiohttp 连接，
                # asyncio.run() 每次创建销毁循环会让 keep-alive 失效并触发
                # "Event loop is closed"。这里用 new_event_loop 显式管理。
                result = self._run_async(result)  # type: ignore[arg-type]
            current = self.repository.get(job_id)
            result_mapping = result if isinstance(result, Mapping) else {}
            result_status = result_mapping.get("status")
            if result_status in {"failed", "partial_failed", "blocked"}:
                error = str(result_mapping.get("error") or result_status)
                if current is not None and current["status"] in NON_TERMINAL_STATUSES:
                    self.repository.fail(job_id, error)
                self._mark_dynamic_failure_state(record, error)
                self._propagate_dependency_failure(job_id, error)
                return
            if result_status == "interrupted":
                if current is not None and current["status"] in NON_TERMINAL_STATUSES:
                    self.repository.interrupt(
                        job_id,
                        str(result_mapping.get("error") or result_status),
                    )
                return
            if result_status in {"queued", "waiting_provider", "downloading"}:
                current = self.repository.get(job_id)
                if current is not None and current["status"] == "running":
                    if result_status == "queued":
                        self.repository.requeue_for_recovery(
                            job_id,
                            expected_status="running",
                            message="任务依赖或 Provider 尚未完成，保持 queued",
                        )
                    else:
                        self.repository.set_status(job_id, result_status)
                return
            can_complete = current is not None and current["status"] not in {
                "completed",
                "failed",
                "cancelled",
                "interrupted",
            }
            if can_complete:
                self.repository.complete(job_id, result or {})
                self._enqueue_ready_dependents(job_id)
        except Exception as exc:
            error = sanitize_error_message(exc) or str(exc)
            current = self.repository.get(job_id)
            if current is not None and current["status"] not in {
                "completed",
                "cancelled",
                "interrupted",
                "failed",
            }:
                self.repository.fail(job_id, error)
            self._mark_dynamic_failure_state(record, error)
            self._propagate_dependency_failure(job_id, error)

    @staticmethod
    def _dependency_ids(record: Mapping[str, Any]) -> list[str]:
        dependencies = record.get("depends_on")
        if not isinstance(dependencies, list | tuple):
            input_data = record.get("input_data")
            dependencies = (
                input_data.get("depends_on", [])
                if isinstance(input_data, Mapping)
                else []
            )
        return [str(value).strip() for value in dependencies if str(value).strip()]

    def _dependencies_completed(self, record: Mapping[str, Any]) -> bool:
        dependencies = self._dependency_ids(record)
        if not dependencies:
            return True
        return all(
            (dependency := self.repository.get(dependency_id)) is not None
            and dependency.get("status") == "completed"
            for dependency_id in dependencies
        )

    def _dependency_state(
        self,
        record: Mapping[str, Any],
        *,
        connection: Any | None = None,
    ) -> tuple[str, str | None]:
        """在 claim/execute 边界统一判断依赖是否可执行。"""
        pending = False
        for dependency_id in self._dependency_ids(record):
            dependency = self.repository.get(dependency_id, connection=connection)
            if dependency is None:
                return "failed", f"任务依赖不存在：{dependency_id}"
            status = str(dependency.get("status") or "")
            if status in {"failed", "interrupted", "cancelled", "stale"}:
                return "failed", f"任务依赖未成功完成：{dependency_id}（{status}）"
            if status != "completed":
                pending = True
        workflow_state, workflow_error = self._workflow_dependency_state(
            record, connection=connection
        )
        if workflow_error is not None:
            return workflow_state, workflow_error
        if workflow_state == "pending":
            pending = True
        return ("pending", None) if pending else ("ready", None)

    def _workflow_dependency_state(
        self,
        record: Mapping[str, Any],
        *,
        connection: Any | None = None,
    ) -> tuple[str, str | None]:
        if str(record.get("job_type") or "") != "dynamic_stage_generate":
            return "ready", None
        run_id = str(record.get("workflow_run_id") or "")
        stage_id = str(record.get("workflow_stage_id") or "")
        if not run_id or not stage_id:
            return "ready", None

        def _probe(conn: Any) -> tuple[str, str | None]:
            stage = conn.execute(
                select(workflow_stage).where(
                    workflow_stage.c.id == stage_id,
                    workflow_stage.c.workflow_run_id == run_id,
                )
            ).first()
            if stage is None:
                return "failed", f"动态任务关联的阶段不存在：{stage_id}"
            prerequisite_keys = get_stage_prerequisites(str(stage.stage_key))
            if not prerequisite_keys:
                return "ready", None
            pending = False
            for prerequisite_key in prerequisite_keys:
                prerequisite = conn.execute(
                    select(workflow_stage)
                    .where(
                        workflow_stage.c.workflow_run_id == run_id,
                        workflow_stage.c.stage_key == prerequisite_key,
                    )
                    .order_by(
                        workflow_stage.c.attempt.desc(),
                        workflow_stage.c.updated_at.desc(),
                        workflow_stage.c.id.desc(),
                    )
                    .limit(1)
                ).first()
                if prerequisite is None:
                    return "failed", f"动态任务前置阶段不存在：{prerequisite_key}"
                status = str(prerequisite.status or "")
                if status in {
                    "failed",
                    "partial_failed",
                    "interrupted",
                    "cancelled",
                    "stale",
                    "blocked",
                }:
                    return (
                        "failed",
                        f"动态任务前置阶段未成功完成：{prerequisite_key}（{status}）",
                    )
                if status != "confirmed":
                    pending = True
            return ("pending", None) if pending else ("ready", None)

        try:
            if connection is not None:
                return _probe(connection)
            with self.repository.engine.connect() as conn:
                return _probe(conn)
        except ValueError as exc:
            return "failed", str(exc)
        except Exception:
            # 读取依赖失败时保持 queued，由恢复/下一次执行重新检查，避免误执行。
            return "pending", None

    def _dependency_error(self, record: Mapping[str, Any]) -> str | None:
        return self._dependency_state(record)[1]

    def _fail_dependency_blocked_job(
        self,
        record: Mapping[str, Any],
        message: str,
    ) -> bool:
        job_id = str(record.get("id") or "")
        if not job_id:
            return False
        try:
            now = utc_now()
            with self.repository.engine.begin() as connection:
                result = connection.execute(
                    update(job_table)
                    .where(
                        job_table.c.id == job_id,
                        job_table.c.status.in_(NON_TERMINAL_STATUSES),
                    )
                    .values(
                        status="failed",
                        error=sanitize_error_message(message),
                        message="任务因依赖失败而失败",
                        finished_at=now,
                        updated_at=now,
                    )
                )
            if result.rowcount != 1:
                return False
        except Exception:
            return False
        self._mark_dynamic_failure_state(record, message)
        return True

    def _mark_dynamic_failure_state(
        self,
        record: Mapping[str, Any],
        message: str,
    ) -> None:
        """把动态 Job 的 Provider/媒体/组合失败即时汇总到 stage/run。"""
        run_id = str(record.get("workflow_run_id") or "")
        stage_id = str(record.get("workflow_stage_id") or "")
        if not run_id or not stage_id:
            return
        try:
            with self.repository.engine.begin() as connection:
                run = connection.execute(
                    select(workflow_run).where(workflow_run.c.id == run_id)
                ).first()
                stage = connection.execute(
                    select(workflow_stage).where(
                        workflow_stage.c.id == stage_id,
                        workflow_stage.c.workflow_run_id == run_id,
                    )
                ).first()
                if run is None or stage is None:
                    return
                latest = connection.execute(
                    select(workflow_stage)
                    .where(
                        workflow_stage.c.workflow_run_id == run_id,
                        workflow_stage.c.stage_key == stage.stage_key,
                    )
                    .order_by(
                        workflow_stage.c.attempt.desc(),
                        workflow_stage.c.updated_at.desc(),
                        workflow_stage.c.id.desc(),
                    )
                    .limit(1)
                ).first()
                if latest is None or latest.id != stage_id:
                    return
                now = utc_now()
                stage_update = connection.execute(
                    update(workflow_stage)
                    .where(
                        workflow_stage.c.id == stage_id,
                        workflow_stage.c.workflow_run_id == run_id,
                        workflow_stage.c.attempt == latest.attempt,
                        workflow_stage.c.input_signature == stage.input_signature,
                        workflow_stage.c.updated_at == stage.updated_at,
                        workflow_stage.c.status.in_(
                            ("queued", "running", "ready", "confirmed", "partial_failed")
                        ),
                    )
                    .values(
                        status="partial_failed",
                        error=sanitize_error_message(message),
                        finished_at=now,
                        updated_at=now,
                    )
                )
                if stage_update.rowcount != 1:
                    return
                run_update = connection.execute(
                    update(workflow_run)
                    .where(
                        workflow_run.c.id == run_id,
                        workflow_run.c.revision == run.revision,
                        workflow_run.c.updated_at == run.updated_at,
                        workflow_run.c.status.not_in(
                            ("cancelled", "stale", "blocked", "failed", "interrupted")
                        ),
                    )
                    .values(
                        status="partial_failed",
                        error=sanitize_error_message(message),
                        revision=workflow_run.c.revision + 1,
                        updated_at=now,
                    )
                )
                if run_update.rowcount != 1:
                    raise RuntimeError("动态 run 条件更新未命中")
        except Exception:
            return

    def _propagate_dependency_failure(self, dependency_job_id: str, message: str) -> None:
        """失败后立即安全失败直接及间接下游，避免等到下一次重启才汇总。

        重构为 collect+commit 模式：
        - Phase 1（持锁）：一次性读取所有非终态记录，在内存里按
          ``_dependency_ids`` 关系 BFS 收集所有需要 fail 的候选列表
          ``(record, child_message)``，**不发起任何写库事务**。
        - Phase 2（无锁）：按候选顺序循环调用
          ``_fail_dependency_blocked_job`` 写库；写库失败不传播该 child。
        持锁时间从 O(写库 × N²) 降到 O(读全表 + 内存 BFS)。
        """
        # Phase 1: collect candidates under lock, no write transactions.
        with self._lock:
            # 一次性扫描，避免每轮 BFS 都触发一次全表读。
            snapshot = self.repository.list_non_terminal(include_dynamic=True)
            pending: list[tuple[str, str]] = [(dependency_job_id, message)]
            visited = {dependency_job_id}
            candidates: list[tuple[dict[str, Any], str]] = []
            while pending:
                failed_id, failed_message = pending.pop(0)
                for record in snapshot:
                    child_id = str(record.get("id") or "")
                    if not child_id or child_id in visited:
                        continue
                    if failed_id not in self._dependency_ids(record):
                        continue
                    child_message = f"任务依赖 {failed_id} 失败：{failed_message}"
                    visited.add(child_id)
                    candidates.append((record, child_message))
                    pending.append((child_id, child_message))

        # Phase 2: commit without holding the runner lock; per-candidate short tx.
        for record, child_message in candidates:
            self._fail_dependency_blocked_job(record, child_message)

    def _enqueue_ready_dependents(self, completed_job_id: str) -> None:
        """上游完成后只唤醒依赖全部 completed 的 queued Job。

        重构为 collect+commit 模式：
        - Phase 1（持锁）：一次性 ``list_non_terminal`` 收集所有可能受
          ``completed_job_id`` 影响的 queued Job，按依赖关系分到两个
          候选列表（``ready_ids`` / ``error_records``），**不调用
          ``enqueue``、不写库**。
        - Phase 2（无锁）：对 ``ready_ids`` 循环 ``enqueue``，对
          ``error_records`` 循环 ``_fail_dependency_blocked_job`` 并继续
          传播。
        持锁时间从 O(写库 × N) 降到 O(读全表 + 内存分类)。
        """
        # Phase 1: collect candidates under lock, no write transactions.
        with self._lock:
            ready_ids: list[str] = []
            error_records: list[tuple[dict[str, Any], str]] = []
            for record in self.repository.list_non_terminal(include_dynamic=True):
                if record.get("status") != "queued":
                    continue
                if completed_job_id not in self._dependency_ids(record):
                    continue
                dependency_state, dependency_error = self._dependency_state(record)
                if dependency_error is not None:
                    error_records.append((record, dependency_error))
                elif dependency_state == "ready":
                    ready_ids.append(str(record["id"]))

        # Phase 2: commit without holding the runner lock.
        for child_id in ready_ids:
            try:
                self.enqueue(child_id)
            except RuntimeError:
                # 关闭阶段不应把已经完成的上游 Job 改回失败。
                continue
        for record, dependency_error in error_records:
            try:
                if self._fail_dependency_blocked_job(record, dependency_error):
                    self._propagate_dependency_failure(
                        str(record["id"]), dependency_error
                    )
            except Exception:
                # shutdown 阶段数据库连接可能已 dispose，吞掉所有写库异常，
                # 避免把失败传播给其他仍在运行的任务。
                continue

    @staticmethod
    def _provider_kind(record: Mapping[str, Any]) -> str:
        kind = str(record.get("provider_kind") or "")
        if kind in {"text", "image", "video"}:
            return kind
        job_type = str(record.get("job_type") or "")
        if "video" in job_type:
            return "video"
        if any(token in job_type for token in ("image", "keyframe")):
            return "image"
        return "text"

    def active_job_ids(self) -> list[str]:
        with self._lock:
            return [job_id for job_id, future in self._futures.items() if not future.done()]

    @staticmethod
    def _run_async(coro: Awaitable[dict[str, Any] | None]) -> dict[str, Any] | None:
        """在线程中运行 async handler：使用独立事件循环。

        为什么不直接 ``asyncio.run``：
        - 每次 ``asyncio.run`` 都创建/销毁循环，httpx/aiohttp 内部持有的
          ``asyncio.AbstractEventLoop`` 引用会立刻失效，触发
          "Event loop is closed"。
        - ``asyncio.run`` 在已有循环的线程里会抛 ``RuntimeError``，未来如果
          runner 被 FastAPI lifespan 之类的现有循环调用就会炸。

        正确做法：每个 async handler 单独建一个 loop、用 ``run_until_complete``
        执行后 close。loop 句柄仅在执行期间有效，对外不暴露。
        """
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)  # type: ignore[return-value]
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()

    def wait(self, job_id: str, timeout: float | None = None) -> None:
        """等待一个已提交 Job 的执行 future，供同步测试/关闭流程使用。"""
        with self._lock:
            future = self._futures.get(job_id)
        if future is not None:
            future.result(timeout=timeout)

    def shutdown(self, *, wait: bool = True, cancel_futures: bool = False) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
        for executor in self._executors.values():
            executor.shutdown(wait=wait, cancel_futures=cancel_futures)


job_runner = JobRunner()
