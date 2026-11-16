"""Long-running work as pollable jobs.

Cloning a repo, installing dependencies and running a test suite can each take minutes,
far longer than an MCP client will wait on one tool call. `start_*` tools return a job
handle immediately and `get_job` polls it (optionally long-polling for a few seconds).

Explicit handles were chosen over the protocol's Tasks extension because every client
can call two ordinary tools, while Tasks support still varies by client. Finished jobs
are written to disk so a result survives a server restart.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .models import JobHandle, JobStatus


class UnknownJob(Exception):
    pass


@dataclass
class _Job:
    job_id: str
    kind: str
    task: asyncio.Task[Any]
    started: float = field(default_factory=time.monotonic)


class JobRegistry:
    def __init__(self, directory: Path):
        self.directory = directory
        self._jobs: dict[str, _Job] = {}

    def start(self, kind: str, work: Callable[[], Awaitable[BaseModel | dict]]) -> JobHandle:
        job_id = f"job_{secrets.token_hex(5)}"
        started = time.monotonic()

        async def runner() -> JobStatus:
            try:
                value = await work()
                result = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
                status = JobStatus(
                    job_id=job_id,
                    kind=kind,
                    state="succeeded",
                    elapsed_s=round(time.monotonic() - started, 2),
                    result=result,
                )
            except Exception as exc:  # noqa: BLE001 - a job reports any failure as data
                status = JobStatus(
                    job_id=job_id,
                    kind=kind,
                    state="failed",
                    elapsed_s=round(time.monotonic() - started, 2),
                    error=f"{type(exc).__name__}: {exc}",
                )
            self.directory.mkdir(parents=True, exist_ok=True)
            (self.directory / f"{job_id}.json").write_text(status.model_dump_json())
            return status

        self._jobs[job_id] = _Job(job_id, kind, asyncio.create_task(runner()), started)
        return JobHandle(job_id=job_id, kind=kind, state="running")

    async def status(self, job_id: str, wait_s: float = 0) -> JobStatus:
        job = self._jobs.get(job_id)
        if job is None:
            path = self.directory / f"{job_id}.json"
            if job_id.startswith("job_") and job_id[4:].isalnum() and path.exists():
                return JobStatus.model_validate(json.loads(path.read_text()))
            raise UnknownJob(
                f"Unknown job {job_id!r}. If the server restarted while it was running, "
                "start it again."
            )
        if not job.task.done() and wait_s > 0:
            await asyncio.wait({job.task}, timeout=min(wait_s, 60))
        if job.task.done():
            return job.task.result()
        return JobStatus(
            job_id=job_id,
            kind=job.kind,
            state="running",
            elapsed_s=round(time.monotonic() - job.started, 2),
        )

    async def wait(self, job_id: str) -> JobStatus:
        job = self._jobs[job_id]
        return await job.task
