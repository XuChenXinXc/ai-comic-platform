from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from app.config import get_settings
from app.jobs.recovery import recover_jobs
from app.jobs.repository import (
    NON_TERMINAL_STATUSES,
    JobRepository,
    job_repository,
)

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
        self._lock = threading.Lock()
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
        recovered = recover_jobs(self.repository)
        for job_id in recovered:
            self.enqueue(job_id)
        return recovered

    def enqueue(self, job_id: str) -> bool:
        if self._shutdown:
            raise RuntimeError("JobRunner 已关闭")
        record = self.repository.get(job_id)
        if record is None or record["status"] not in NON_TERMINAL_STATUSES:
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
        if record["status"] in {"queued", "waiting_provider", "downloading"}:
            record = self.repository.claim(job_id, expected_status=record["status"])
            if record is None:
                return
        else:
            return
        handler = self._handlers.get(record["job_type"])
        if handler is None:
            self.repository.fail(job_id, f"未注册任务处理器：{record['job_type']}")
            return
        try:
            result = handler(record)
            if inspect.isawaitable(result):
                result = asyncio.run(result)
            current = self.repository.get(job_id)
            if current is not None and current["status"] not in {
                "completed",
                "failed",
                "cancelled",
                "interrupted",
            }:
                self.repository.complete(job_id, result or {})
        except Exception as exc:
            current = self.repository.get(job_id)
            if current is not None and current["status"] not in {
                "completed",
                "cancelled",
                "interrupted",
            }:
                self.repository.fail(job_id, str(exc))

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

    def shutdown(self, *, wait: bool = True, cancel_futures: bool = False) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
        for executor in self._executors.values():
            executor.shutdown(wait=wait, cancel_futures=cancel_futures)


job_runner = JobRunner()
