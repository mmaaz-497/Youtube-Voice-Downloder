"""In-memory job store (T024, plan D7): dict + one threading.Lock.

Admission checks run in exactly this order inside ONE lock acquisition:
per-origin cap → queue bound → disk floor (Principle VI). The OriginLedger
increments at admission and decrements only at the terminal transition
(completed/failed), in the same critical sections (data-model invariant 6).
Illegal state transitions raise (Principle II). Artifacts are deleted in the
same critical section that marks a job terminal (invariant 4).

T045 adds the two reclamation passes (Principle V), both inside that same
critical section: a TTL purge of terminal jobs and an orphan-file pass over
WORK_DIR. Time comes from `self.clock` so expiry is drivable in tests
without sleeping.
"""

import logging
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from backend.config import Config
from backend.models.errors import AppError, ErrorCode

logger = logging.getLogger("yt-audio-extractor")


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


# Purge (delivery / TTL expiry) is removal from the store, not a state change.
_LEGAL_TRANSITIONS: dict[JobState, set[JobState]] = {
    JobState.QUEUED: {JobState.RUNNING, JobState.FAILED},
    JobState.RUNNING: {JobState.COMPLETED, JobState.FAILED},
    JobState.COMPLETED: set(),
    JobState.FAILED: set(),
}


class IllegalTransitionError(RuntimeError):
    """A state transition outside the legal set — a bug, never user error."""


@dataclass
class Job:
    job_id: str
    video_id: str
    bitrate_kbps: int
    title: str
    origin_hash: str
    duration_seconds: int = 0
    state: JobState = JobState.QUEUED
    phase: str | None = None
    progress: int = 0
    error_code: ErrorCode | None = None
    error_message: str | None = None
    source_path: Path | None = None
    output_path: Path | None = None
    created_at: float = 0.0  # set by the store from its injected clock
    started_at: float | None = None
    finished_at: float | None = None
    retries_used: int = 0


def _free_disk_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def _delete_artifact(path) -> None:
    if path is None:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except (OSError, TypeError):
        logger.warning("Could not delete artifact %s", path)


TERMINAL_STATES = (JobState.COMPLETED, JobState.FAILED)


class JobStore:
    def __init__(self, config: Config, clock=None) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._queued: list[str] = []  # FIFO; index+1 == visible queue position
        self._origin_counts: dict[str, int] = {}
        # Files handed to an in-flight FileResponse: path -> handover time.
        # The orphan pass must not yank these out from under an open stream.
        self._delivering: dict[str, float] = {}
        # Injectable clock (tests drive TTL expiry with it). Resolved through
        # the module on every call when unset, so it is never bound early.
        self.clock = clock

    def _now(self) -> float:
        return self.clock() if self.clock is not None else time.time()

    # ---- admission ----------------------------------------------------

    def admit(
        self,
        origin_hash: str,
        video_id: str,
        title: str,
        bitrate_kbps: int,
        duration_seconds: int = 0,
    ) -> tuple[Job, int]:
        with self._lock:
            if self._origin_counts.get(origin_hash, 0) >= self._config.per_origin_cap:
                raise AppError(ErrorCode.CLIENT_LIMIT)
            if len(self._queued) >= self._config.queue_limit:
                raise AppError(ErrorCode.AT_CAPACITY)
            self._check_disk_floor()

            job = Job(
                job_id=str(uuid.uuid4()),
                video_id=video_id,
                bitrate_kbps=bitrate_kbps,
                title=title,
                origin_hash=origin_hash,
                duration_seconds=duration_seconds,
                created_at=self._now(),
            )
            self._jobs[job.job_id] = job
            self._queued.append(job.job_id)
            self._origin_counts[origin_hash] = self._origin_counts.get(origin_hash, 0) + 1
            return job, len(self._queued)

    def check_disk_floor(self) -> None:
        """Runner's mid-pipeline re-check; read-only, safe without the lock."""
        self._check_disk_floor()

    def _check_disk_floor(self) -> None:
        if _free_disk_bytes(self._config.work_dir) < self._config.disk_floor_bytes:
            raise AppError(ErrorCode.LOW_DISK)

    # ---- transitions ---------------------------------------------------

    def _transition(self, job: Job, new_state: JobState) -> None:
        if new_state not in _LEGAL_TRANSITIONS[job.state]:
            raise IllegalTransitionError(
                f"Illegal job transition {job.state.value} -> {new_state.value} "
                f"for job {job.job_id}"
            )
        job.state = new_state

    def mark_running(self, job_id: str, phase: str = "downloading") -> Job:
        with self._lock:
            job = self._jobs[job_id]  # KeyError if purged before start
            self._transition(job, JobState.RUNNING)
            self._queued.remove(job_id)
            job.phase = phase
            job.started_at = self._now()
            return job

    def set_phase(self, job_id: str, phase: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            if job.state is not JobState.RUNNING:
                raise IllegalTransitionError(
                    f"Cannot set phase on a {job.state.value} job {job_id}"
                )
            job.phase = phase

    def update_progress(self, job_id: str, value: int) -> None:
        with self._lock:
            job = self._jobs[job_id]
            # Monotone by construction: the store rejects decreases (plan D5).
            job.progress = max(job.progress, min(100, int(value)))

    def set_source_path(self, job_id: str, path: Path | None) -> None:
        with self._lock:
            self._jobs[job_id].source_path = path

    def set_output_path(self, job_id: str, path: Path | None) -> None:
        with self._lock:
            self._jobs[job_id].output_path = path

    def mark_completed(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            self._transition(job, JobState.COMPLETED)
            job.phase = None
            job.progress = 100
            job.finished_at = self._now()
            self._decrement_origin(job.origin_hash)

    def mark_failed(self, job_id: str, code: ErrorCode, message: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                logger.warning("mark_failed on unknown job %s (already purged)", job_id)
                return
            self._transition(job, JobState.FAILED)
            if job_id in self._queued:
                self._queued.remove(job_id)
            job.phase = None
            job.error_code = code
            job.error_message = message
            job.finished_at = self._now()
            self._decrement_origin(job.origin_hash)
            # Invariant 4: artifacts die in the SAME critical section that
            # marks the job terminal — failed jobs never own files.
            for attr in ("source_path", "output_path"):
                _delete_artifact(getattr(job, attr))
                setattr(job, attr, None)

    def _decrement_origin(self, origin_hash: str) -> None:
        remaining = self._origin_counts.get(origin_hash, 0) - 1
        if remaining > 0:
            self._origin_counts[origin_hash] = remaining
        else:
            self._origin_counts.pop(origin_hash, None)  # no durable identity

    # ---- reads & delivery ----------------------------------------------

    def snapshot(self, job_id: str) -> dict:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise AppError(ErrorCode.JOB_NOT_FOUND)
            payload: dict = {
                "job_id": job.job_id,
                "status": job.state.value,
                "progress": job.progress,
            }
            if job.state is JobState.QUEUED:
                payload["queue_position"] = self._queued.index(job.job_id) + 1
            if job.state is JobState.RUNNING:
                payload["phase"] = job.phase
            if job.state is JobState.FAILED:
                payload["error"] = {"code": job.error_code, "message": job.error_message}
            return payload

    def counts(self) -> tuple[int, int]:
        """(running, queued) — the health endpoint's queue-depth signal."""
        with self._lock:
            running = sum(1 for job in self._jobs.values() if job.state is JobState.RUNNING)
            return running, len(self._queued)

    def free_disk_bytes(self) -> int:
        return _free_disk_bytes(self._config.work_dir)

    def take_delivered(self, job_id: str) -> Job:
        """Delivery purge, streaming-safe: mark delivered by removing the job
        from the store (concurrent requests see JOB_NOT_FOUND) but do NOT
        delete the file here — the caller streams it and deletes afterwards
        (the orphan sweep is the safety net if that ever fails)."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise AppError(ErrorCode.JOB_NOT_FOUND)
            if job.state in (JobState.QUEUED, JobState.RUNNING):
                raise AppError(ErrorCode.NOT_READY)
            if job.state is JobState.FAILED:
                raise AppError(
                    ErrorCode.NOT_READY,
                    "The job did not complete successfully; no file is available.",
                )
            if job.output_path is None or not Path(job.output_path).exists():
                del self._jobs[job_id]
                raise AppError(ErrorCode.INTERNAL, "The output file is missing.")
            del self._jobs[job_id]
            # Shield the file from the orphan pass until the response has
            # finished streaming; release_delivered() clears the reservation.
            self._delivering[str(Path(job.output_path))] = self._now()
            return job

    def release_delivered(self, path) -> None:
        """Called once the response has streamed and its file is deleted."""
        with self._lock:
            self._delivering.pop(str(Path(path)), None)

    # ---- reclamation passes (T045) --------------------------------------

    def run_sweep(self) -> dict[str, int]:
        """One TTL + orphan pass. Every deletion happens inside the store's
        critical section, so no other thread can observe a terminal job that
        still owns files (data-model invariant 4)."""
        now = self._now()
        ttl = self._config.ttl_seconds
        expired = 0
        orphaned = 0

        with self._lock:
            # --- TTL purge: terminal jobs whose finished_at + TTL has passed.
            for job_id in [
                job_id
                for job_id, job in self._jobs.items()
                if job.state in TERMINAL_STATES
                and job.finished_at is not None
                and now - job.finished_at >= ttl
            ]:
                job = self._jobs.pop(job_id)
                _delete_artifact(job.source_path)
                _delete_artifact(job.output_path)
                expired += 1

            # A delivery whose background deletion never ran would pin its
            # file forever; time-bounding the reservation keeps the orphan
            # pass the ultimate safety net.
            for path_str, handed_over in list(self._delivering.items()):
                if now - handed_over >= ttl:
                    del self._delivering[path_str]

            # --- orphan pass: any WORK_DIR file no live job can claim.
            owned = {
                str(Path(path))
                for job in self._jobs.values()
                for path in (job.source_path, job.output_path)
                if path is not None
            }
            live_ids = set(self._jobs)
            for path in self._iter_work_files():
                if str(path) in self._delivering or str(path) in owned:
                    continue
                # Artifacts are named <job_id>.<ext>, so a file is claimable
                # from its name alone — a download in flight is owned before
                # the store ever learns its path.
                if path.name.split(".")[0] in live_ids:
                    continue
                _delete_artifact(path)
                orphaned += 1

        if expired or orphaned:
            logger.info("Sweep purged %d expired job(s), %d orphan file(s)", expired, orphaned)
        return {"expired": expired, "orphaned": orphaned}

    def _iter_work_files(self):
        try:
            return [p for p in Path(self._config.work_dir).iterdir() if p.is_file()]
        except OSError:
            logger.warning("Could not list WORK_DIR during sweep")
            return []


class Sweeper:
    """Single daemon thread running JobStore.run_sweep on an interval.

    The wait is a threading.Event, not time.sleep: it is interruptible, so
    shutdown is immediate, and it stays real-time even where the clock is
    faked. A failing pass is logged and retried next tick — the sweeper must
    never die silently (Principle II).
    """

    def __init__(self, store: JobStore, interval_seconds: float) -> None:
        self._store = store
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="job-sweeper", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self._store.run_sweep()
            except Exception:
                logger.exception("Sweep pass failed; retrying next interval")

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.warning("Sweeper thread did not stop within %ss", timeout)
