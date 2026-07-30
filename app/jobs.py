"""Background job registry for reconciliation runs.

A run cold-starts a proxy process per variant and drives a multi-turn session
through each. On a constrained host that is minutes, not seconds — measured at
~47s for a small file on 0.1 CPU — which is well past what a platform will hold
an HTTP request open for. Doing it inline returns a gateway timeout, or worse a
500 that looks like a crash.

So a run is a job: POST starts it and returns an id, the client polls. The stage
the client displays is then the stage the server is actually in, rather than a
guess on a timer.
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Callable

# Finished jobs are kept briefly so a client that polls late still gets its
# result, then dropped so a long-lived instance does not accumulate them.
JOB_TTL_SECONDS = 900
MAX_JOBS = 64


@dataclass
class Job:
    id: str
    status: str = "queued"          # queued | running | done | error
    stage: str = "boot"             # boot | stock | pinned | done
    detail: str = ""
    result: dict | None = None
    error: str | None = None
    created: float = field(default_factory=time.monotonic)
    finished: float | None = None

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "stage": self.stage,
            "detail": self.detail,
            "result": self.result,
            "error": self.error,
            "elapsed": round((self.finished or time.monotonic()) - self.created, 1),
        }


class JobRegistry:
    """Runs one job at a time — the proxies bind fixed resources, so concurrent
    runs would measure each other rather than the thing under test."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._busy = threading.Lock()

    def _reap(self) -> None:
        now = time.monotonic()
        stale = [
            jid for jid, j in self._jobs.items()
            if j.finished is not None and now - j.finished > JOB_TTL_SECONDS
        ]
        for jid in stale:
            self._jobs.pop(jid, None)
        while len(self._jobs) > MAX_JOBS:
            oldest = min(self._jobs.values(), key=lambda j: j.created)
            self._jobs.pop(oldest.id, None)

    @property
    def running(self) -> bool:
        return self._busy.locked()

    def submit(self, work: Callable[[Job], dict]) -> Job:
        job = Job(id=uuid.uuid4().hex[:12])
        with self._lock:
            self._reap()
            self._jobs[job.id] = job

        def runner() -> None:
            # Serialise: a second run would contend for the same proxy ports and
            # the same recorder, and both results would be wrong.
            with self._busy:
                job.status = "running"
                try:
                    job.result = work(job)
                    job.stage = "done"
                    job.status = "done"
                except Exception as exc:  # surfaced to the client verbatim
                    job.status = "error"
                    job.error = str(exc) or exc.__class__.__name__
                    traceback.print_exc()
                finally:
                    job.finished = time.monotonic()

        threading.Thread(target=runner, daemon=True,
                         name=f"run-{job.id}").start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)
