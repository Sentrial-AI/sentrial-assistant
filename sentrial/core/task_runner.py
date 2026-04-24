"""
Task runner — the approval-gated autonomous workflow engine.

Flow:
  1. agent calls create_job(kind, request, scope_preview, params) → returns Job(status=PENDING_APPROVAL)
  2. agent sends scope_preview to Liam via notifier
  3. Liam replies 'yes' → approve(job_id) flips to APPROVED and dispatches executor
  4. executor runs in background (typically 5–10 min)
  5. on completion, notifier fires with deliverable link
  6. on 10-min mark while still running, status ping fires

Jobs are persisted to ~/Library/Application Support/Sentrial/jobs/<id>.json so they
survive daemon restarts. In-flight tasks are lost on restart (expected — user can re-approve).
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable

from sentrial.core import audit, paths

log = logging.getLogger(__name__)

JOBS_DIR = paths.jobs_dir()
STATUS_PING_SECONDS = 600  # 10 min


class JobStatus(str, Enum):
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DENIED = "denied"


@dataclass
class Job:
    id: str
    kind: str
    request: str
    scope_preview: str
    status: JobStatus
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    deliverable_path: str | None = None
    error: str | None = None
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d


Executor = Callable[[Job], Awaitable[str]]      # returns absolute path to deliverable
Notifier = Callable[[str], Awaitable[None]]      # sends a message to Liam


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskRunner:
    def __init__(
        self,
        notifier: Notifier,
        executors: dict[str, Executor],
        max_concurrent: int = 3,
    ):
        self.notifier = notifier
        self.executors = executors
        self.sem = asyncio.Semaphore(max_concurrent)
        self.jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._load_persisted()

    # ----- persistence -----

    def _persist(self, job: Job) -> None:
        (JOBS_DIR / f"{job.id}.json").write_text(json.dumps(job.to_dict(), indent=2))

    def _load_persisted(self) -> None:
        for f in JOBS_DIR.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                data["status"] = JobStatus(data["status"])
                # Re-hydrate but don't resume in-flight work
                if data["status"] in (JobStatus.RUNNING, JobStatus.APPROVED):
                    data["status"] = JobStatus.FAILED
                    data["error"] = "daemon restarted during job"
                self.jobs[data["id"]] = Job(**data)
            except Exception as e:  # noqa: BLE001
                log.warning(f"failed to load persisted job {f.name}: {e}")

    # ----- lifecycle -----

    def register_executor(self, kind: str, fn: Executor) -> None:
        self.executors[kind] = fn

    def create_job(
        self,
        kind: str,
        request: str,
        scope_preview: str,
        params: dict | None = None,
    ) -> Job:
        if kind not in self.executors:
            raise ValueError(
                f"no executor registered for kind={kind} "
                f"(known: {list(self.executors)})"
            )
        job = Job(
            id=uuid.uuid4().hex[:12],
            kind=kind,
            request=request,
            scope_preview=scope_preview,
            status=JobStatus.PENDING_APPROVAL,
            created_at=_now(),
            params=params or {},
        )
        self.jobs[job.id] = job
        self._persist(job)
        audit.log(
            "sentrial",
            f"job_created:{kind}",
            1,
            args={"id": job.id, "params": params or {}},
            result=scope_preview[:300],
            job_id=job.id,
        )
        return job

    async def approve(self, job_id: str) -> None:
        job = self._require(job_id)
        if job.status != JobStatus.PENDING_APPROVAL:
            raise ValueError(f"job {job_id} is not pending approval (status={job.status.value})")
        job.status = JobStatus.APPROVED
        self._persist(job)
        audit.log("user", f"job_approved:{job.kind}", 2, args={"id": job.id}, job_id=job.id)
        self._tasks[job.id] = asyncio.create_task(self._run(job))

    def deny(self, job_id: str, reason: str = "") -> None:
        job = self._require(job_id)
        job.status = JobStatus.DENIED
        job.error = reason or "denied by user"
        job.finished_at = _now()
        self._persist(job)
        audit.log("user", f"job_denied:{job.kind}", 1, args={"id": job.id}, job_id=job.id)

    def cancel(self, job_id: str) -> None:
        job = self._require(job_id)
        job.status = JobStatus.CANCELLED
        job.finished_at = _now()
        self._persist(job)
        t = self._tasks.pop(job_id, None)
        if t:
            t.cancel()
        audit.log("user", f"job_cancelled:{job.kind}", 1, args={"id": job.id}, job_id=job.id)

    # ----- execution -----

    async def _run(self, job: Job) -> None:
        async with self.sem:
            if job.status == JobStatus.CANCELLED:
                return
            job.status = JobStatus.RUNNING
            job.started_at = _now()
            self._persist(job)
            audit.log("sentrial", f"job_started:{job.kind}", 1, args={"id": job.id}, job_id=job.id)

            ping_task = asyncio.create_task(self._ping_if_slow(job))

            try:
                executor = self.executors[job.kind]
                deliverable = await executor(job)
                job.deliverable_path = deliverable
                job.status = JobStatus.COMPLETED
                job.finished_at = _now()
                self._persist(job)
                audit.log(
                    "sentrial",
                    f"job_completed:{job.kind}",
                    1,
                    args={"id": job.id},
                    result=deliverable,
                    job_id=job.id,
                )
                await self.notifier(
                    f"Done: {job.kind}\n{_as_link(deliverable)}"
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                job.status = JobStatus.FAILED
                job.error = str(e)
                job.finished_at = _now()
                self._persist(job)
                audit.log(
                    "sentrial",
                    f"job_failed:{job.kind}",
                    1,
                    args={"id": job.id},
                    result=str(e),
                    status="error",
                    job_id=job.id,
                )
                await self.notifier(f"Job {job.kind} ({job.id}) failed: {e}")
            finally:
                ping_task.cancel()
                self._tasks.pop(job.id, None)

    async def _ping_if_slow(self, job: Job) -> None:
        try:
            await asyncio.sleep(STATUS_PING_SECONDS)
            if job.status == JobStatus.RUNNING:
                await self.notifier(
                    f"Still working on {job.kind} ({job.id}). Taking longer than expected."
                )
        except asyncio.CancelledError:
            pass

    # ----- queries -----

    def _require(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(f"no such job: {job_id}")
        return job

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def list_active(self) -> list[Job]:
        return [
            j for j in self.jobs.values()
            if j.status in (JobStatus.PENDING_APPROVAL, JobStatus.APPROVED, JobStatus.RUNNING)
        ]

    def list_recent(self, n: int = 20) -> list[Job]:
        sorted_jobs = sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True)
        return sorted_jobs[:n]


def _as_link(path: str) -> str:
    """Format a deliverable path as a clickable computer:// link."""
    if path.startswith(("http://", "https://", "computer://")):
        return path
    return f"computer://{path}"
