"""T048 — /api/health: the operator signal, and the explicit degraded
trigger. status is "ok" ONLY when yt-dlp is available AND ffmpeg/libmp3lame
is available AND free disk is at or above the floor; otherwise "degraded"
with every failing condition named. Tool probes are cached so polling cannot
spawn a subprocess storm.
"""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.conftest import FakeClock, wait_until

VID = "dQw4w9WgXcQ"
WATCH_URL = f"https://www.youtube.com/watch?v={VID}"
PLENTY_OF_DISK = 500 * 1024**3


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


@pytest.fixture
def healthy(media_boundary, monkeypatch):
    """Both tools present and plenty of disk — the "ok" baseline."""
    media_boundary.youtube.ytdlp_version.return_value = "2026.07.04"
    media_boundary.audio.check_libmp3lame.return_value = True
    media_boundary.youtube.probe_video_info.return_value = make_info()

    import backend.services.jobs as jobs_module

    monkeypatch.setattr(jobs_module, "_free_disk_bytes", lambda path: PLENTY_OF_DISK)
    return media_boundary


def test_health_reports_every_documented_field(client, healthy):
    resp = client.get("/api/health")

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "status",
        "running",
        "queued",
        "capacity",
        "queue_limit",
        "free_disk_bytes",
        "ytdlp_available",
        "ytdlp_version",
        "ffmpeg_available",
        "uptime_seconds",
        "degraded_reasons",
    }
    assert body["status"] == "ok"
    assert body["degraded_reasons"] == []
    assert body["ytdlp_available"] is True
    assert body["ytdlp_version"] == "2026.07.04"
    assert body["ffmpeg_available"] is True
    assert body["free_disk_bytes"] == PLENTY_OF_DISK
    assert body["capacity"] == client.app.state.config.max_concurrency
    assert body["queue_limit"] == client.app.state.config.queue_limit
    assert body["running"] == 0 and body["queued"] == 0
    assert body["uptime_seconds"] >= 0


def test_missing_ffmpeg_degrades_and_names_the_reason(client, healthy):
    healthy.audio.check_libmp3lame.return_value = False
    client.app.state.tool_probe.invalidate()

    body = client.get("/api/health").json()

    assert body["status"] == "degraded"
    assert body["ffmpeg_available"] is False
    assert any("ffmpeg" in reason for reason in body["degraded_reasons"])
    assert not any("yt-dlp" in reason for reason in body["degraded_reasons"])


def test_missing_ytdlp_degrades_and_names_the_reason(client, healthy):
    healthy.youtube.ytdlp_version.return_value = None
    client.app.state.tool_probe.invalidate()

    body = client.get("/api/health").json()

    assert body["status"] == "degraded"
    assert body["ytdlp_available"] is False
    assert body["ytdlp_version"] is None
    assert any("yt-dlp" in reason for reason in body["degraded_reasons"])


def test_low_disk_degrades_at_the_floor_boundary(client, healthy, monkeypatch):
    import backend.services.jobs as jobs_module

    floor = client.app.state.config.disk_floor_bytes

    # Exactly at the floor is still "ok" — the trigger is `< floor`.
    monkeypatch.setattr(jobs_module, "_free_disk_bytes", lambda path: floor)
    client.app.state.tool_probe.invalidate()
    assert client.get("/api/health").json()["status"] == "ok"

    # One byte under and it degrades.
    monkeypatch.setattr(jobs_module, "_free_disk_bytes", lambda path: floor - 1)
    body = client.get("/api/health").json()
    assert body["status"] == "degraded"
    assert any("free disk" in reason for reason in body["degraded_reasons"])


def test_every_failing_condition_is_named_at_once(client, healthy, monkeypatch):
    import backend.services.jobs as jobs_module

    healthy.audio.check_libmp3lame.return_value = False
    healthy.youtube.ytdlp_version.return_value = None
    monkeypatch.setattr(jobs_module, "_free_disk_bytes", lambda path: 0)
    client.app.state.tool_probe.invalidate()

    body = client.get("/api/health").json()

    assert body["status"] == "degraded"
    assert len(body["degraded_reasons"]) == 3


def test_tool_probes_are_cached_across_polls(client, healthy):
    """A monitoring loop must not turn into a subprocess storm."""
    probe = client.app.state.tool_probe
    probe.invalidate()
    clock = FakeClock(start=0.0)
    probe.clock = clock.time

    for _ in range(25):
        assert client.get("/api/health").status_code == 200

    assert healthy.youtube.ytdlp_version.call_count == 1
    assert healthy.audio.check_libmp3lame.call_count == 1

    # Once the window lapses, exactly one fresh probe happens.
    clock.advance(probe.ttl_seconds + 1)
    client.get("/api/health")
    client.get("/api/health")
    assert healthy.youtube.ytdlp_version.call_count == 2
    assert healthy.audio.check_libmp3lame.call_count == 2


def test_health_reports_live_queue_depth(make_client, healthy):
    started, release = threading.Event(), threading.Event()

    def parked_download(*args, **kwargs):
        started.set()
        release.wait(timeout=10)
        return MagicMock()

    healthy.youtube.download_audio.side_effect = parked_download
    client = make_client(MAX_CONCURRENCY=1)
    try:
        client.post("/api/jobs", json={"url": WATCH_URL})
        assert started.wait(5)
        client.post("/api/jobs", json={"url": WATCH_URL})

        body = client.get("/api/health").json()
        assert body["running"] == 1
        assert body["queued"] == 1
        assert body["capacity"] == 1
    finally:
        release.set()
    assert wait_until(lambda: client.get("/api/health").json()["running"] == 0)
