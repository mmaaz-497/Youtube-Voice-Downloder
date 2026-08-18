"""T021 — US2 origin resolution (plan D6): TRUSTED_PROXY=0 ignores
X-Forwarded-For entirely (socket IP); TRUSTED_PROXY=1 uses the RIGHTMOST XFF
value only — forged leftmost entries never create distinct origins or bypass
the per-origin cap.
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
    event = threading.Event()
    yield event
    event.set()


def park_downloads(media_boundary, release: threading.Event) -> None:
    media_boundary.youtube.probe_video_info.return_value = make_info()

    def _download(*args, **kwargs):
        release.wait(timeout=10)
        return MagicMock()

    media_boundary.youtube.download_audio.side_effect = _download


def post_job(client, xff: str | None = None):
    headers = {"X-Forwarded-For": xff} if xff else {}
    return client.post("/api/jobs", json={"url": WATCH_URL}, headers=headers)


def test_untrusted_proxy_ignores_xff_entirely(make_client, media_boundary, release_event):
    park_downloads(media_boundary, release_event)
    client = make_client(MAX_CONCURRENCY=1, PER_ORIGIN_CAP=1, QUEUE_LIMIT=10)  # TRUSTED_PROXY off

    assert post_job(client).status_code == 202
    # Forged XFF must NOT mint a fresh origin: same socket IP → same origin → cap hit.
    resp = post_job(client, xff="1.2.3.4")
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "CLIENT_LIMIT"


def test_trusted_proxy_uses_rightmost_xff_value(make_client, media_boundary, release_event):
    park_downloads(media_boundary, release_event)
    client = make_client(
        MAX_CONCURRENCY=1, PER_ORIGIN_CAP=1, QUEUE_LIMIT=10, TRUSTED_PROXY=1
    )

    assert post_job(client, xff="9.9.9.9").status_code == 202
    # Different rightmost value → genuinely distinct origin → admitted.
    assert post_job(client, xff="8.8.8.8").status_code == 202
    # Same rightmost value again → same origin → cap hit.
    resp = post_job(client, xff="9.9.9.9")
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "CLIENT_LIMIT"


def test_forged_leftmost_entries_do_not_bypass_cap(make_client, media_boundary, release_event):
    park_downloads(media_boundary, release_event)
    client = make_client(
        MAX_CONCURRENCY=1, PER_ORIGIN_CAP=1, QUEUE_LIMIT=10, TRUSTED_PROXY=1
    )

    assert post_job(client, xff="203.0.113.7").status_code == 202

    # Attacker varies the (client-forgeable) leftmost entries; the trusted
    # rightmost value is unchanged, so the origin must be unchanged too.
    for forged in ("6.6.6.6, 203.0.113.7", "7.7.7.7, 6.6.6.6, 203.0.113.7"):
        resp = post_job(client, xff=forged)
        assert resp.status_code == 429, forged
        assert resp.json()["error"]["code"] == "CLIENT_LIMIT"
