"""T044 — US5 sweeps: the TTL pass purges terminal jobs at TTL_SECONDS, the
orphan pass reclaims WORK_DIR files no live job owns, files belonging to a
LIVE job are never touched, and the work dir returns to baseline after a
mixed run of successes and failures. Also pins the sweeper thread's clean
start/stop through the app lifespan so tests leak no threads.
"""

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.models.errors import AppError, ErrorCode
from tests.conftest import FakeClock, wait_until

VID = "dQw4w9WgXcQ"
WATCH_URL = f"https://www.youtube.com/watch?v={VID}"


def make_info(**overrides):
    base = {
        "video_id": VID,
        "title": "Never Gonna Give You Up",
        "channel": "Rick Astley",
        "duration_seconds": 213,
        "thumbnail_url": f"https://i.ytimg.com/vi/{VID}/hqdefault.jpg",
        "available": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def working_boundary(media_boundary):
    """Mocks that create real artifacts, named from the runner-supplied stem
    exactly as production does."""
    media_boundary.youtube.probe_video_info.return_value = make_info()
    media_boundary.audio.check_libmp3lame.return_value = True

    def fake_download(video_id, work_dir, **kwargs):
        stem = kwargs.get("name_stem") or "source"
        source = Path(work_dir) / f"{stem}.source"
        source.write_bytes(b"raw-bestaudio")
        return source

    def fake_transcode(src, dst, bitrate_kbps, title, **kwargs):
        Path(dst).write_bytes(b"ID3-mp3")
        return Path(dst)

    media_boundary.youtube.download_audio.side_effect = fake_download
    media_boundary.audio.transcode.side_effect = fake_transcode


def status_of(client, job_id):
    return client.get(f"/api/jobs/{job_id}").json().get("status")


@pytest.fixture
def store_clock(client):
    """Inject the clock before any job runs so finished_at and the expiry
    decision that reads it share one clock (anchored to wall time)."""
    clock = FakeClock(start=time.time())
    client.app.state.store.clock = clock.time
    return clock


def test_orphan_pass_reclaims_unowned_files(client, media_boundary, work_dir):
    working_boundary(media_boundary)
    store = client.app.state.store

    # Left behind by a crashed process / failed background deletion.
    orphan = work_dir / "deadbeefdeadbeef.source"
    orphan.write_bytes(b"nobody owns me")
    stray = work_dir / "11111111-2222-3333-4444-555555555555.mp3"
    stray.write_bytes(b"ID3-nobody")

    store.run_sweep()

    assert not orphan.exists()
    assert not stray.exists()


def test_orphan_pass_never_deletes_a_live_jobs_files(client, media_boundary, work_dir):
    """A running job's in-flight artifacts must survive a sweep tick — this
    is why ownership is keyed on the job id, not on registration timing."""
    working_boundary(media_boundary)
    store = client.app.state.store
    started, release = threading.Event(), threading.Event()

    def parked_download(video_id, work_dir_arg, **kwargs):
        stem = kwargs.get("name_stem")
        assert stem, "runner must hand the download a job-derived name stem"
        source = Path(work_dir_arg) / f"{stem}.source"
        source.write_bytes(b"partially-downloaded")
        started.set()
        release.wait(timeout=10)
        return source

    media_boundary.youtube.download_audio.side_effect = parked_download
    try:
        job_id = client.post("/api/jobs", json={"url": WATCH_URL}).json()["job_id"]
        assert started.wait(5)
        in_flight = work_dir / f"{job_id}.source"
        assert in_flight.exists()

        store.run_sweep()

        # Still downloading: the file is NOT an orphan.
        assert in_flight.exists(), "sweep deleted a live job's in-flight download"
        assert status_of(client, job_id) == "running"
    finally:
        release.set()
    assert wait_until(lambda: status_of(client, job_id) == "completed")


def test_ttl_pass_purges_terminal_jobs_only(client, store_clock, media_boundary, work_dir):
    working_boundary(media_boundary)
    store = client.app.state.store

    done = client.post("/api/jobs", json={"url": WATCH_URL}).json()["job_id"]
    assert wait_until(lambda: status_of(client, done) == "completed")

    started, release = threading.Event(), threading.Event()

    def parked(video_id, work_dir_arg, **kwargs):
        started.set()
        release.wait(timeout=10)
        return MagicMock()

    media_boundary.youtube.download_audio.side_effect = parked
    try:
        live = client.post("/api/jobs", json={"url": WATCH_URL}).json()["job_id"]
        assert started.wait(5)

        store_clock.advance(client.app.state.config.ttl_seconds + 1)
        store.run_sweep()

        # Terminal job purged; the running one is untouched however old it is.
        assert client.get(f"/api/jobs/{done}").status_code == 404
        assert status_of(client, live) == "running"
    finally:
        release.set()
    assert wait_until(lambda: status_of(client, live) == "completed")


def test_work_dir_returns_to_baseline_after_mixed_run(
    client, store_clock, media_boundary, work_dir
):
    """Successes (delivered and undelivered) plus failures: after delivery
    and one TTL sweep, nothing is left on disk."""
    working_boundary(media_boundary)
    store = client.app.state.store
    assert list(work_dir.iterdir()) == [], "baseline should start empty"

    delivered = client.post("/api/jobs", json={"url": WATCH_URL}).json()["job_id"]
    assert wait_until(lambda: status_of(client, delivered) == "completed")
    abandoned = client.post("/api/jobs", json={"url": WATCH_URL}).json()["job_id"]
    assert wait_until(lambda: status_of(client, abandoned) == "completed")

    media_boundary.youtube.download_audio.side_effect = AppError(
        ErrorCode.EXTRACTION_FAILED
    )
    failed = client.post("/api/jobs", json={"url": WATCH_URL}).json()["job_id"]
    assert wait_until(lambda: status_of(client, failed) == "failed")

    media_boundary.audio.transcode.side_effect = AppError(ErrorCode.TRANSCODE_FAILED)
    working_boundary(media_boundary)
    media_boundary.audio.transcode.side_effect = AppError(ErrorCode.TRANSCODE_FAILED)
    mid_failed = client.post("/api/jobs", json={"url": WATCH_URL}).json()["job_id"]
    assert wait_until(lambda: status_of(client, mid_failed) == "failed")

    # One delivery, then expire the rest.
    assert client.get(f"/api/jobs/{delivered}/file").status_code == 200
    store_clock.advance(client.app.state.config.ttl_seconds + 1)
    store.run_sweep()

    assert wait_until(lambda: list(work_dir.iterdir()) == []), [
        p.name for p in work_dir.iterdir()
    ]
    for job_id in (delivered, abandoned, failed, mid_failed):
        assert client.get(f"/api/jobs/{job_id}").status_code == 404


def test_sweeper_thread_starts_and_stops_with_the_app(work_dir, media_boundary):
    """Lifespan owns the sweeper: exactly one daemon thread while the app is
    up, and none left behind after shutdown."""
    from fastapi.testclient import TestClient

    from backend.config import Config
    from backend.main import create_app

    def sweeper_threads():
        return [t for t in threading.enumerate() if t.name == "job-sweeper"]

    before = len(sweeper_threads())
    app = create_app(Config())
    with TestClient(app) as client:
        client.get("/api/jobs/does-not-exist")  # app is live
        during = sweeper_threads()
        assert len(during) == before + 1
        assert during[-1].daemon is True

    assert wait_until(lambda: len(sweeper_threads()) == before), "sweeper thread leaked"


def test_sweeper_thread_actually_runs_the_pass(work_dir, media_boundary):
    """The thread wakes on its own interval — the sweep is not merely
    callable, it is scheduled."""
    from fastapi.testclient import TestClient

    from backend.config import Config
    from backend.main import create_app

    app = create_app(Config())
    app.state.sweeper.interval_seconds = 0.01  # tighten for this test only
    with TestClient(app):
        orphan = work_dir / "orphaned-by-nobody.source"
        orphan.write_bytes(b"x")
        assert wait_until(lambda: not orphan.exists(), timeout=5.0), (
            "background sweeper never reclaimed the orphan"
        )
