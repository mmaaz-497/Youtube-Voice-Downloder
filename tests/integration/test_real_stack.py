"""T052 — the one test that touches the real world.

Runs a short, known-safe video through the ACTUAL yt-dlp and FFmpeg binaries
end to end. Excluded from CI two ways: the `real_stack` marker is deselected
by pytest.ini's default addopts, and the module skips itself unless
REAL_STACK=1. Everything else in the suite stays offline and deterministic
(constitution: Testing Discipline).

Run it deliberately:

    REAL_STACK=1 pytest tests/integration/test_real_stack.py -m real_stack
"""

import os
import shutil

import pytest

from tests.conftest import wait_until

# "Me at the zoo" — the first video uploaded to YouTube, 19 seconds, public
# domain-adjacent and famously stable. Short enough to keep the test quick.
SMOKE_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
SMOKE_VIDEO_ID = "jNQXAC9IVRw"
BITRATE_KBPS = 128

pytestmark = [
    pytest.mark.real_stack,
    pytest.mark.skipif(
        os.environ.get("REAL_STACK") != "1",
        reason="real-stack smoke test; set REAL_STACK=1 to run (never in CI)",
    ),
]


@pytest.fixture
def real_client(work_dir, monkeypatch):
    """A client with the media boundary NOT mocked — the whole point here."""
    if shutil.which("yt-dlp") is None:
        pytest.skip("yt-dlp is not on PATH")
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not on PATH")

    from fastapi.testclient import TestClient

    from backend.config import Config
    from backend.main import create_app

    monkeypatch.setenv("DISK_FLOOR_BYTES", "0")
    with TestClient(create_app(Config())) as client:
        yield client


def test_health_sees_the_real_tools(real_client):
    body = real_client.get("/api/health").json()

    assert body["ytdlp_available"] is True
    assert body["ytdlp_version"], "yt-dlp --version should be reported"
    assert body["ffmpeg_available"] is True, (
        f"ffmpeg lacks libmp3lame: {body['degraded_reasons']}"
    )
    assert body["status"] == "ok", body["degraded_reasons"]


def test_real_url_to_real_mp3(real_client, work_dir):
    info = real_client.get("/api/info", params={"url": SMOKE_URL})
    assert info.status_code == 200, info.text
    assert info.json()["video_id"] == SMOKE_VIDEO_ID
    assert info.json()["duration_seconds"] > 0

    created = real_client.post(
        "/api/jobs", json={"url": SMOKE_URL, "bitrate_kbps": BITRATE_KBPS}
    )
    assert created.status_code == 202, created.text
    job_id = created.json()["job_id"]

    def terminal():
        return real_client.get(f"/api/jobs/{job_id}").json().get("status") in (
            "completed",
            "failed",
        )

    # Real network + real transcode: allow a generous wall-clock budget.
    assert wait_until(terminal, timeout=180.0, interval=0.5), "job never finished"
    status = real_client.get(f"/api/jobs/{job_id}").json()
    assert status["status"] == "completed", status
    assert status["progress"] == 100

    delivered = real_client.get(f"/api/jobs/{job_id}/file")
    assert delivered.status_code == 200
    assert delivered.headers["content-type"].startswith("audio/mpeg")
    assert delivered.content.startswith(b"ID3"), "delivered file is not an MP3"
    assert len(delivered.content) > 10_000

    # Privacy posture holds against the real stack too.
    assert real_client.get(f"/api/jobs/{job_id}").status_code == 404
    assert wait_until(lambda: list(work_dir.iterdir()) == [], timeout=10.0), [
        p.name for p in work_dir.iterdir()
    ]
