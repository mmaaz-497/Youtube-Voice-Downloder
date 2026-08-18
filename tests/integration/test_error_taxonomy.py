"""T037 — US4 full taxonomy sweep: every one of the 17 ErrorCodes produces
its exact envelope + HTTP status (or failed-job status body) end to end, with
a message pairwise-distinct from all 16 others — asserted programmatically.
INTERNAL responses must leak no paths, traceback text, or tool stderr.
"""

import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from backend.models.errors import CODE_HTTP_STATUS, AppError, ErrorCode
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


def run_failed_job(client):
    resp = client.post("/api/jobs", json={"url": WATCH_URL})
    assert resp.status_code == 202
    status = wait_terminal(client, resp.json()["job_id"])
    assert status["status"] == "failed", status
    assert set(status["error"].keys()) == {"code", "message"}
    return status["error"]


def test_all_17_codes_distinct_and_exact(
    client, make_client, media_boundary, monkeypatch, work_dir
):
    media_boundary.youtube.probe_video_info.return_value = make_info()
    media_boundary.audio.check_libmp3lame.return_value = True
    observed: dict[str, str] = {}

    def record_envelope(code: ErrorCode, resp):
        body = resp.json()
        assert set(body.keys()) == {"error"}, body
        err = body["error"]
        assert set(err.keys()) == {"code", "message"}, err
        assert err["code"] == code.value
        assert resp.status_code == CODE_HTTP_STATUS[code], (code, resp.status_code)
        observed[code.value] = err["message"]

    # ---- envelope codes over HTTP ----
    record_envelope(ErrorCode.INVALID_INPUT, client.post("/api/jobs", json={}))
    record_envelope(
        ErrorCode.INVALID_URL,
        client.post("/api/jobs", json={"url": "https://vimeo.com/12345"}),
    )
    record_envelope(
        ErrorCode.INVALID_BITRATE,
        client.post("/api/jobs", json={"url": WATCH_URL, "bitrate_kbps": 160}),
    )
    media_boundary.youtube.probe_video_info.return_value = make_info(
        duration_seconds=999_999
    )
    record_envelope(
        ErrorCode.DURATION_EXCEEDED, client.post("/api/jobs", json={"url": WATCH_URL})
    )
    media_boundary.youtube.probe_video_info.return_value = make_info()

    # Boundary-raised codes flow through with their PRODUCTION default
    # messages (AppError(code) with no explicit message).
    for code in (
        ErrorCode.LIVE_STREAM,
        ErrorCode.VIDEO_UNAVAILABLE,
        ErrorCode.NETWORK_ERROR,
    ):
        media_boundary.youtube.probe_video_info.side_effect = AppError(code)
        record_envelope(code, client.get("/api/info", params={"url": WATCH_URL}))
    media_boundary.youtube.probe_video_info.side_effect = None

    record_envelope(
        ErrorCode.JOB_NOT_FOUND, client.get(f"/api/jobs/{uuid.uuid4()}")
    )

    # NOT_READY: file requested while the job is still running.
    started, release = threading.Event(), threading.Event()

    def parked_download(*args, **kwargs):
        started.set()
        release.wait(timeout=10)
        return MagicMock()

    media_boundary.youtube.download_audio.side_effect = parked_download
    try:
        resp = client.post("/api/jobs", json={"url": WATCH_URL})
        assert resp.status_code == 202
        parked_id = resp.json()["job_id"]
        assert started.wait(5)
        record_envelope(
            ErrorCode.NOT_READY, client.get(f"/api/jobs/{parked_id}/file")
        )
    finally:
        release.set()
    wait_terminal(client, parked_id)
    media_boundary.youtube.download_audio.side_effect = None

    # INTERNAL: unexpected exception with juicy secrets — nothing may leak.
    media_boundary.youtube.probe_video_info.side_effect = RuntimeError(
        "kaboom C:\\top\\secret\\tool-stderr-marker"
    )
    resp = client.get("/api/info", params={"url": WATCH_URL})
    record_envelope(ErrorCode.INTERNAL, resp)
    internal_message = resp.json()["error"]["message"]
    for leak in ("kaboom", "secret", "marker", "Traceback", "\\", "/"):
        assert leak not in internal_message, leak
    media_boundary.youtube.probe_video_info.side_effect = None
    media_boundary.youtube.probe_video_info.return_value = make_info()

    # ---- failed-job codes (surface in JobStatusResponse.error) ----
    for code in (ErrorCode.EXTRACTION_FAILED, ErrorCode.TIMEOUT):
        media_boundary.youtube.download_audio.side_effect = AppError(code)
        err = run_failed_job(client)
        assert err["code"] == code.value
        observed[code.value] = err["message"]
    media_boundary.youtube.download_audio.side_effect = None

    def real_file_download(video_id, target_dir, **kwargs):
        source = Path(target_dir) / "taxonomy.source"
        source.write_bytes(b"x")
        return source

    media_boundary.youtube.download_audio.side_effect = real_file_download
    media_boundary.audio.transcode.side_effect = AppError(ErrorCode.TRANSCODE_FAILED)
    err = run_failed_job(client)
    assert err["code"] == ErrorCode.TRANSCODE_FAILED.value
    observed[err["code"]] = err["message"]
    media_boundary.audio.transcode.side_effect = None

    media_boundary.audio.check_libmp3lame.return_value = False
    err = run_failed_job(client)
    assert err["code"] == ErrorCode.FFMPEG_MISSING.value
    observed[err["code"]] = err["message"]
    media_boundary.audio.check_libmp3lame.return_value = True

    # Failed jobs must never leave artifacts behind (invariant 1/4).
    leftovers = [p.name for p in work_dir.iterdir() if p.suffix != ".mp3"]
    assert leftovers == [], leftovers

    # ---- admission codes need dedicated configs ----
    # Every knob is set explicitly on each build: make_client's env overrides
    # persist across calls, and the admission order means a leftover cap of 0
    # would mask the code under test.
    record_envelope(
        ErrorCode.CLIENT_LIMIT,
        make_client(PER_ORIGIN_CAP=0, QUEUE_LIMIT=10).post(
            "/api/jobs", json={"url": WATCH_URL}
        ),
    )
    record_envelope(
        ErrorCode.AT_CAPACITY,
        make_client(PER_ORIGIN_CAP=10, QUEUE_LIMIT=0).post(
            "/api/jobs", json={"url": WATCH_URL}
        ),
    )
    import backend.services.jobs as jobs_module

    monkeypatch.setattr(jobs_module, "_free_disk_bytes", lambda path: 0)
    record_envelope(
        ErrorCode.LOW_DISK,
        make_client(PER_ORIGIN_CAP=10, QUEUE_LIMIT=10).post(
            "/api/jobs", json={"url": WATCH_URL}
        ),
    )

    # ---- the sweep itself: all 17, pairwise-distinct messages ----
    assert set(observed.keys()) == {code.value for code in ErrorCode}
    messages = list(observed.values())
    duplicates = {m for m in messages if messages.count(m) > 1}
    assert duplicates == set(), f"non-distinct messages: {duplicates}"
