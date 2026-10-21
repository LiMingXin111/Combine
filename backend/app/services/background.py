"""Task-owned background jobs. Late responses must never revive a cancelled task."""
from __future__ import annotations

import asyncio

from .. import db
from ..util import ApiError

_jobs: dict[str, asyncio.Task] = {}


def ensure_active(task_id: str) -> dict:
    task = db.q1("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not task or task["status"] == "cancelled":
        raise asyncio.CancelledError()
    return task


def start(task_id: str, coro) -> asyncio.Task:
    old = _jobs.get(task_id)
    if old and not old.done():
        coro.close()
        raise ApiError(409, "STATE_CONFLICT", "任务已有后台工作正在执行")
    job = asyncio.create_task(coro)
    _jobs[task_id] = job

    def done(finished):
        if _jobs.get(task_id) is finished:
            _jobs.pop(task_id, None)
        if not finished.cancelled():
            finished.exception()  # Domain entrypoints record errors; retrieve to avoid orphan warnings.

    job.add_done_callback(done)
    return job


def cancel(task_id: str) -> None:
    job = _jobs.pop(task_id, None)
    if job and not job.done():
        job.cancel()


async def shutdown() -> None:
    jobs = list(_jobs.values())
    for job in jobs:
        job.cancel()
    await asyncio.gather(*jobs, return_exceptions=True)
    _jobs.clear()
