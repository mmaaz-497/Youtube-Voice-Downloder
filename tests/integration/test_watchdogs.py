"""T038 — US4 watchdogs + read isolation: per-phase timeout budgets are
injected from config (no real seconds slept in tests); TIMEOUT marks the job
failed, frees the worker slot (a queued job starts after the kill), and every
partial artifact is reclaimed in the terminal critical section. Mid-job
LOW_DISK fails the job likewise. With all workers parked, /api/info,
/api/jobs/{id}, and /api/health all still answer (FR-031/SC-008, C2 guard).
"""

import itertools
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from backend.models.errors import AppError, ErrorCode
from tests.conftest import wait_until

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


def wait_terminal(client, job_id, timeout=5.0):
    def _terminal():
        return client.get(f"/api/jobs/{job_id}").json().get("status") in (
            "completed",
            "failed",
        )

    assert wait_until(_terminal, timeout)
    return client.get(f"/api/jobs/{job_id}").json()


def test_download_timeout_marks_failed_and_frees_slot(
    make_client, media_boundary, work_dir
):
    media_boundary.youtube.probe_video_info.return_value = make_info()
    media_boundary.audio.check_libmp3lame.return_value = True
    counter = itertools.count()
    captured: dict = {}
    started_first = threading.Event()
    fire_watchdog = threading.Event()
    started_second = threading.Event()

    def download(video_id, target_dir, **kwargs):
        if next(counter) == 0:
            captured.update(kwargs)
            started_first.set()
            fire_watchdog.wait(timeout=10)
            # The boundary's watchdog kills the hung subprocess and deletes
            # its own partial, surfacing as a typed TIMEOUT.
            raise AppError(
                ErrorCode.TIMEOUT, "Downloading the audio took too long and was stopped."
            )
        started_second.set()
        source = Path(target_dir) / "second.source"
        source.write_bytes(b"x")
        return source

    media_boundary.youtube.download_audio.side_effect = download

    def fake_transcode(src, dst, bitrate_kbps, title, **kwargs):
        Path(dst).write_bytes(b"mp3")
        return Path(dst)

    media_boundary.audio.transcode.side_effect = fake_transcode

    client = make_client(
        MAX_CONCURRENCY=1, DOWNLOAD_TIMEOUT_SECONDS=7, TRANSCODE_TIMEOUT_SECONDS=9
    )
    try:
        first = client.post("/api/jobs", json={"url": WATCH_URL}).json()["job_id"]
        assert started_first.wait(5)
        second = client.post("/api/jobs", json={"url": WATCH_URL}).json()["job_id"]

        # Watchdog budget is injected from config — never hardcoded.
        assert captured.get("timeout_seconds") == 7

        fire_watchdog.set()
        final_first = wait_terminal(client, first)
        assert final_first["status"] == "failed"
        assert final_first["error"]["code"] == "TIMEOUT"

        # Slot freed: the queued job starts and completes after the kill.
        assert started_second.wait(5), "queued job never started after the kill"
        final_second = wait_terminal(client, second)
        assert final_second["status"] == "completed"

        # Only the completed job's output remains on disk.
        assert [p.name for p in work_dir.iterdir()] == [f"{second}.mp3"]
    finally:
        fire_watchdog.set()


def test_transcode_timeout_reclaims_partials(make_client, media_boundary, work_dir):
    media_boundary.youtube.probe_video_info.return_value = make_info()
    media_boundary.audio.check_libmp3lame.return_value = True
    captured: dict = {}

    def download(video_id, target_dir, **kwargs):
        source = Path(target_dir) / "hung.source"
        source.write_bytes(b"x")
        return source

    def hung_transcode(src, dst, bitrate_kbps, title, **kwargs):
        captured.update(kwargs)
        Path(dst).write_bytes(b"partial-mp3")  # what the kill leaves behind
        raise AppError(
            ErrorCode.TIMEOUT, "Converting the audio took too long and was stopped."
        )

    media_boundary.youtube.download_audio.side_effect = download
    media_boundary.audio.transcode.side_effect = hung_transcode

    client = make_client(TRANSCODE_TIMEOUT_SECONDS=9)
    job_id = client.post("/api/jobs", json={"url": WATCH_URL}).json()["job_id"]
    final = wait_terminal(client, job_id)

    assert final["status"] == "failed"
    assert final["error"]["code"] == "TIMEOUT"
    assert captured.get("timeout_seconds") == 9
    # Source AND partial output reclaimed in the same critical section that
    # marked the job terminal — failed jobs own no files.
    assert list(work_dir.iterdir()) == []


def test_mid_job_low_disk_fails_typed_and_clean(
    make_client, media_boundary, monkeypatch, work_dir
):
    media_boundary.youtube.probe_video_info.return_value = make_info()
    media_boundary.audio.check_libmp3lame.return_value = True

    def download(video_id, target_dir, **kwargs):
        source = Path(target_dir) / "mid.source"
        source.write_bytes(b"x")
        return source

    media_boundary.youtube.download_audio.side_effect = download

    import backend.services.jobs as jobs_module

    checks = itertools.count()
    # Admission and job-start checks pass; the post-download re-check hits
    # the floor.
    monkeypatch.setattr(
        jobs_module,
        "_free_disk_bytes",
        lambda path: 0 if next(checks) >= 2 else 10**12,
    )

    client = make_client()
    job_id = client.post("/api/jobs", json={"url": WATCH_URL}).json()["job_id"]
    final = wait_terminal(client, job_id)

    assert final["status"] == "failed"
    assert final["error"]["code"] == "LOW_DISK"
    assert list(work_dir.iterdir()) == []


def test_reads_answer_while_all_workers_are_parked(make_client, media_boundary):
    media_boundary.youtube.probe_video_info.return_value = make_info()
    media_boundary.audio.check_libmp3lame.return_value = True
    started = threading.Event()
    release = threading.Event()

    def parked_download(*args, **kwargs):
        started.set()
        release.wait(timeout=10)
        return MagicMock()

    media_boundary.youtube.download_audio.side_effect = parked_download
    client = make_client(MAX_CONCURRENCY=1)
    try:
        running = client.post("/api/jobs", json={"url": WATCH_URL}).json()["job_id"]
        assert started.wait(5)
        queued = client.post("/api/jobs", json={"url": WATCH_URL}).json()["job_id"]

        # Reads never queue behind extraction work (C2 guard).
        info = client.get("/api/info", params={"url": WATCH_URL})
        assert info.status_code == 200
        assert info.json()["video_id"] == VID

        status = client.get(f"/api/jobs/{queued}")
        assert status.status_code == 200
        assert status.json()["status"] == "queued"

        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["running"] == 1
    finally:
        release.set()
    wait_terminal(client, running)
    wait_terminal(client, queued)
