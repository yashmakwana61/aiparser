from __future__ import annotations

import asyncio
import hashlib
import inspect
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import structlog

from order_parser.config import get_settings
from order_parser.core import metrics
from order_parser.core.job import JobRecord, JobStatus

logger = structlog.get_logger(__name__)


@dataclass
class QueuedJob:
    job_id: str
    func: Callable[..., Awaitable[Any]] | Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    enqueued_at: float


class JobQueue:
    """Lightweight async job queue with configurable worker concurrency.

    - Backed by asyncio.Queue (no external infra).
    - Worker pool processes jobs concurrently.
    - Queue depth and processing metrics exposed via /metrics and /ready.
    """

    def __init__(
        self,
        max_workers: int | None = None,
        max_queue_size: int | None = None,
        job_store=None,
    ) -> None:
        settings = get_settings()
        self.max_workers = int(max_workers if max_workers is not None else getattr(settings, "worker_count", 3) or 3)
        self.max_queue_size = int(max_queue_size if max_queue_size is not None else getattr(settings, "queue_max_size", 500) or 500)
        # asyncio.Queue size 0 means infinite
        self._queue: asyncio.Queue[QueuedJob] = asyncio.Queue(maxsize=self.max_queue_size if self.max_queue_size > 0 else 0)
        self._workers: list[asyncio.Task] = []
        self._running = False
        self.job_store = job_store
        self._processed = 0
        self._failed = 0

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    @property
    def worker_count(self) -> int:
        return len(self._workers)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        for i in range(max(1, self.max_workers)):
            task = asyncio.create_task(self._worker_loop(i), name=f"job-worker-{i}")
            self._workers.append(task)
        logger.info("job_queue.started", workers=self.max_workers, max_queue=self.max_queue_size)

    async def stop(self) -> None:
        self._running = False
        for w in self._workers:
            w.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        logger.info("job_queue.stopped")

    async def enqueue(
        self,
        job_id: str,
        func: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> bool:
        """Enqueue a callable. Returns False if queue is full."""
        item = QueuedJob(job_id=job_id, func=func, args=args, kwargs=kwargs, enqueued_at=time.monotonic())
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            metrics.incr("job_queue_full_total")
            logger.warning("job_queue.full_rejected", job_id=job_id)
            return False
        metrics.incr("job_queue_enqueued_total")
        # update gauge via metric counter pattern
        metrics.incr("job_queue_depth", value=float(self.depth))
        logger.info("job_queue.enqueued", job_id=job_id, depth=self.depth)
        return True

    async def enqueue_or_raise(self, job_id: str, func: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        ok = await self.enqueue(job_id, func, *args, **kwargs)
        if not ok:
            raise RuntimeError("Job queue is full; try again later")

    async def _worker_loop(self, worker_id: int) -> None:
        while self._running:
            try:
                item = await self._queue.get()
            except asyncio.CancelledError:
                break
            job_id = item.job_id
            started = time.monotonic()
            # bind job_id into context for downstream logging
            structlog.contextvars.bind_contextvars(job_id=job_id, worker=worker_id)
            try:
                settings = get_settings()
                timeout = float(getattr(settings, "job_timeout_seconds", 300.0) or 0)
                # Support both sync and async callables. Sync callables run in
                # a worker thread so the event loop stays responsive and the
                # job-level timeout can actually fire (resource control: one
                # huge job cannot stall the pool forever).
                if inspect.iscoroutinefunction(item.func):
                    coro = item.func(*item.args, **item.kwargs)
                    if timeout > 0:
                        await asyncio.wait_for(coro, timeout)
                    else:
                        await coro
                else:
                    coro_or_result = await asyncio.to_thread(item.func, *item.args, **item.kwargs) if timeout <= 0 else await asyncio.wait_for(asyncio.to_thread(item.func, *item.args, **item.kwargs), timeout)
                    if asyncio.iscoroutine(coro_or_result):
                        # Callable returned a coroutine (e.g. async closure
                        # hidden behind a sync def): await it too.
                        if timeout > 0:
                            remaining = timeout - (time.monotonic() - started)
                            await asyncio.wait_for(coro_or_result, max(remaining, 0.1))
                        else:
                            await coro_or_result
                self._processed += 1
                metrics.incr("job_queue_completed_total")
                metrics.observe("job_queue_processing_seconds", time.monotonic() - started)
                logger.info("job_queue.job_completed", job_id=job_id, worker=worker_id)
            except asyncio.CancelledError:
                metrics.incr("job_queue_cancelled_total")
                raise
            except (asyncio.TimeoutError, TimeoutError) as exc:
                self._failed += 1
                metrics.incr("job_queue_failed_total")
                metrics.incr("job_queue_timeout_total")
                logger.exception("job_queue.job_timeout", job_id=job_id, error=str(exc))
                if self.job_store is not None:
                    try:
                        rec = self.job_store.get(job_id)
                        if rec is not None:
                            try:
                                rec.transition(JobStatus.FAILED)
                            except ValueError:
                                rec.status = JobStatus.FAILED
                            rec.error_code = "RESOURCE-001"
                            rec.error_message = f"job exceeded timeout budget: {str(exc)[:300]}"
                            self.job_store.save(rec)
                    except Exception:
                        logger.exception("job_queue.mark_failed_error", job_id=job_id)
            except Exception as exc:
                self._failed += 1
                metrics.incr("job_queue_failed_total")
                logger.exception("job_queue.job_failed", job_id=job_id, error=str(exc))
                # Mark job as failed in store if available
                if self.job_store is not None:
                    try:
                        rec = self.job_store.get(job_id)
                        if rec is not None:
                            try:
                                rec.transition(JobStatus.FAILED)
                            except ValueError:
                                rec.status = JobStatus.FAILED
                            rec.error_code = "SYS-001"
                            rec.error_message = str(exc)[:500]
                            self.job_store.save(rec)
                    except Exception:
                        logger.exception("job_queue.mark_failed_error", job_id=job_id)
            finally:
                structlog.contextvars.unbind_contextvars("job_id", "worker")
                self._queue.task_done()

    def stats(self) -> dict[str, Any]:
        return {
            "depth": self.depth,
            "workers": self.worker_count,
            "max_workers": self.max_workers,
            "max_queue_size": self.max_queue_size,
            "processed": self._processed,
            "failed": self._failed,
            "running": self._running,
        }
