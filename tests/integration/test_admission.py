"""T020 — US2 admission order (one lock acquisition, Principle VI):
per-origin cap → 429 CLIENT_LIMIT, then queue bound → 503 AT_CAPACITY, then
disk floor → 503 LOW_DISK; plus boundary values. Workers are parked on a
threading.Event so admission state is deterministic.
"""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

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


@pytest.fixture
def release_event():
    """Event parking mocked downloads; always released at teardown so worker
    threads never outlive the test."""
    event = threading.Event()
    yield event
    event.set()


def blocking_download(started: threading.Event, release: threading.Event):
    def _download(*args, **kwargs):
        started.set()
        release.wait(timeout=10)
        return MagicMock()

    return _download


def test_client_limit_precedes_at_capacity(make_client, media_boundary, release_event):
    started = threading.Event()
    media_boundary.youtube.probe_video_info.return_value = make_info()
    media_boundary.youtube.download_audio.side_effect = blocking_download(started, release_event)
    client = make_client(MAX_CONCURRENCY=1, QUEUE_LIMIT=1, PER_ORIGIN_CAP=1, TRUSTED_PROXY=1)

    # Origin A occupies the single worker.
    r1 = client.post(
        "/api/jobs", json={"url": WATCH_URL}, headers={"X-Forwarded-For": "10.0.0.1"}
    )
    assert r1.status_code == 202
    assert started.wait(5), "worker never picked up the first job"

    # Origin B fills the queue (boundary: admitted at queued == limit - 1).
    r2 = client.post(
        "/api/jobs", json={"url": WATCH_URL}, headers={"X-Forwarded-For": "10.0.0.2"}
    )
    assert r2.status_code == 202
    assert r2.json()["queue_position"] == 1

    # Origin A again: BOTH the origin cap and the queue bound are violated —
    # the per-origin check must win (checked first).
    r3 = client.post(
        "/api/jobs", json={"url": WATCH_URL}, headers={"X-Forwarded-For": "10.0.0.1"}
    )
    assert r3.status_code == 429
    assert r3.json()["error"]["code"] == "CLIENT_LIMIT"

    # Origin C: only the queue bound is violated → AT_CAPACITY.
    r4 = client.post(
        "/api/jobs", json={"url": WATCH_URL}, headers={"X-Forwarded-For": "10.0.0.3"}
    )
    assert r4.status_code == 503
    assert r4.json()["error"]["code"] == "AT_CAPACITY"


def test_at_capacity_precedes_low_disk(make_client, media_boundary, release_event, monkeypatch):
    started = threading.Event()
    media_boundary.youtube.probe_video_info.return_value = make_info()
    media_boundary.youtube.download_audio.side_effect = blocking_download(started, release_event)
    client = make_client(MAX_CONCURRENCY=1, QUEUE_LIMIT=1, PER_ORIGIN_CAP=10, TRUSTED_PROXY=1)

    r1 = client.post(
        "/api/jobs", json={"url": WATCH_URL}, headers={"X-Forwarded-For": "10.0.0.1"}
    )
    assert r1.status_code == 202
    assert started.wait(5)
    r2 = client.post(
        "/api/jobs", json={"url": WATCH_URL}, headers={"X-Forwarded-For": "10.0.0.2"}
    )
    assert r2.status_code == 202

    import backend.services.jobs as jobs_module

    monkeypatch.setattr(jobs_module, "_free_disk_bytes", lambda path: 0)

    # Queue bound AND disk floor violated → queue bound wins (checked earlier).
    r3 = client.post(
        "/api/jobs", json={"url": WATCH_URL}, headers={"X-Forwarded-For": "10.0.0.3"}
    )
    assert r3.status_code == 503
    assert r3.json()["error"]["code"] == "AT_CAPACITY"


def test_low_disk_refused(make_client, media_boundary, monkeypatch):
    media_boundary.youtube.probe_video_info.return_value = make_info()

    import backend.services.jobs as jobs_module

    monkeypatch.setattr(jobs_module, "_free_disk_bytes", lambda path: 0)
    client = make_client()

    resp = client.post("/api/jobs", json={"url": WATCH_URL})

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "LOW_DISK"


def test_make_client_knobs_do_not_leak_between_builds(make_client, media_boundary):
    """Regression guard for the US4 deviation: a knob set for one client must
    not survive into the next build, or a leftover cap silently masks the
    code a later case is testing."""
    media_boundary.youtube.probe_video_info.return_value = make_info()

    refusing = make_client(PER_ORIGIN_CAP=0)
    assert refusing.post("/api/jobs", json={"url": WATCH_URL}).status_code == 429

    # This build never mentions PER_ORIGIN_CAP, so it must get the baseline.
    fresh = make_client(QUEUE_LIMIT=5)
    assert fresh.post("/api/jobs", json={"url": WATCH_URL}).status_code == 202


def test_admission_boundary_values(make_client, media_boundary, release_event):
    """cap-1 and limit-1 admit; cap and limit refuse."""
    started = threading.Event()
    media_boundary.youtube.probe_video_info.return_value = make_info()
    media_boundary.youtube.download_audio.side_effect = blocking_download(started, release_event)
    client = make_client(MAX_CONCURRENCY=1, QUEUE_LIMIT=2, PER_ORIGIN_CAP=2, TRUSTED_PROXY=1)

    # Origin A: first job runs, second queues → origin A at exactly the cap.
    r1 = client.post(
        "/api/jobs", json={"url": WATCH_URL}, headers={"X-Forwarded-For": "10.0.0.1"}
    )
    assert r1.status_code == 202
    assert started.wait(5)
    r2 = client.post(
        "/api/jobs", json={"url": WATCH_URL}, headers={"X-Forwarded-For": "10.0.0.1"}
    )
    assert r2.status_code == 202

    r3 = client.post(
        "/api/jobs", json={"url": WATCH_URL}, headers={"X-Forwarded-For": "10.0.0.1"}
    )
    assert r3.status_code == 429
    assert r3.json()["error"]["code"] == "CLIENT_LIMIT"

    # Origin B still fits: queue at limit - 1 admits...
    r4 = client.post(
        "/api/jobs", json={"url": WATCH_URL}, headers={"X-Forwarded-For": "10.0.0.2"}
    )
    assert r4.status_code == 202

    # ...and now the queue is exactly full for origin C.
    r5 = client.post(
        "/api/jobs", json={"url": WATCH_URL}, headers={"X-Forwarded-For": "10.0.0.3"}
    )
    assert r5.status_code == 503
    assert r5.json()["error"]["code"] == "AT_CAPACITY"
