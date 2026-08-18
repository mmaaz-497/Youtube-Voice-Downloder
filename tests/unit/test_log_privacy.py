"""T051 — log privacy (Principle V): log records carry video IDs and typed
outcomes but NEVER video titles; the client IP appears only as a hash; and
the boot salt rotates per process start so those hashes are short-lived by
construction (plan D6).
"""

import hashlib
import importlib
import logging
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.models.errors import AppError, ErrorCode
from tests.conftest import wait_until

VID = "dQw4w9WgXcQ"
WATCH_URL = f"https://www.youtube.com/watch?v={VID}"
SECRET_TITLE = "Zzyzx Confidential Unreleased Demo"
CLIENT_IP = "203.0.113.77"


def make_info(**overrides):
    base = {
        "video_id": VID,
        "title": SECRET_TITLE,
        "channel": "Rick Astley",
        "duration_seconds": 213,
        "thumbnail_url": f"https://i.ytimg.com/vi/{VID}/hqdefault.jpg",
        "available": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def captured_logs(caplog):
    caplog.set_level(logging.DEBUG, logger="yt-audio-extractor")
    return caplog


def all_log_text(captured_logs) -> str:
    return "\n".join(record.getMessage() for record in captured_logs.records)


def test_successful_job_logs_video_id_never_the_title(
    client, media_boundary, captured_logs
):
    media_boundary.youtube.probe_video_info.return_value = make_info()
    media_boundary.audio.check_libmp3lame.return_value = True

    def fake_download(video_id, work_dir, **kwargs):
        source = Path(work_dir) / f"{kwargs.get('name_stem')}.source"
        source.write_bytes(b"raw")
        return source

    def fake_transcode(src, dst, bitrate_kbps, title, **kwargs):
        Path(dst).write_bytes(b"ID3")
        return Path(dst)

    media_boundary.youtube.download_audio.side_effect = fake_download
    media_boundary.audio.transcode.side_effect = fake_transcode

    job_id = client.post("/api/jobs", json={"url": WATCH_URL}).json()["job_id"]
    assert wait_until(
        lambda: client.get(f"/api/jobs/{job_id}").json().get("status") == "completed"
    )

    text = all_log_text(captured_logs)
    assert VID in text, "video id should be logged for operator traceability"
    assert SECRET_TITLE not in text
    for word in SECRET_TITLE.split():
        assert word not in text


def test_failed_job_logs_typed_outcome_not_the_title(
    client, media_boundary, captured_logs
):
    media_boundary.youtube.probe_video_info.return_value = make_info()
    media_boundary.audio.check_libmp3lame.return_value = True
    media_boundary.youtube.download_audio.side_effect = AppError(
        ErrorCode.EXTRACTION_FAILED
    )

    job_id = client.post("/api/jobs", json={"url": WATCH_URL}).json()["job_id"]
    assert wait_until(
        lambda: client.get(f"/api/jobs/{job_id}").json().get("status") == "failed"
    )

    text = all_log_text(captured_logs)
    assert ErrorCode.EXTRACTION_FAILED.value in text, "typed outcome must be logged"
    assert VID in text
    assert SECRET_TITLE not in text


def test_retry_logs_carry_the_video_id_only(captured_logs, monkeypatch, tmp_path):
    """The retry path logs per attempt — still IDs, never titles."""
    import io
    import subprocess

    import backend.services.youtube as youtube_module

    class FailingPopen:
        def __init__(self, argv, **kwargs):
            self.returncode = 1
            self.stdout = io.StringIO("")
            self.stderr = io.StringIO("ERROR: connection reset by peer")

        def wait(self, timeout=None):
            return 1

        def kill(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", FailingPopen)
    monkeypatch.setattr("time.sleep", lambda seconds: None)

    with pytest.raises(AppError):
        youtube_module.download_audio(VID, tmp_path, timeout_seconds=600)

    retry_lines = [
        record.getMessage()
        for record in captured_logs.records
        if "retry" in record.getMessage().lower()
    ]
    assert retry_lines
    for line in retry_lines:
        assert VID in line
        assert SECRET_TITLE not in line


def test_origin_appears_only_as_a_hash(client, media_boundary, captured_logs):
    """The raw client IP must never reach the store, a response, or a log."""
    from backend.main import BOOT_SALT, resolve_origin_hash

    media_boundary.youtube.probe_video_info.return_value = make_info()
    config = client.app.state.config
    request = SimpleNamespace(
        client=SimpleNamespace(host=CLIENT_IP), headers={}
    )

    origin = resolve_origin_hash(request, config)

    assert origin != CLIENT_IP
    assert re.fullmatch(r"[0-9a-f]{16}", origin), origin
    assert origin == hashlib.sha256(f"{CLIENT_IP}{BOOT_SALT}".encode()).hexdigest()[:16]

    resp = client.post(
        "/api/jobs", json={"url": WATCH_URL}, headers={"X-Forwarded-For": CLIENT_IP}
    )
    assert resp.status_code == 202
    assert CLIENT_IP not in resp.text
    assert CLIENT_IP not in all_log_text(captured_logs)


def test_boot_salt_rotates_per_process_start():
    """Re-executing the module is what a fresh process does; the salt must
    differ, so yesterday's hashes cannot be correlated with today's."""
    import backend.main as main_module

    first = main_module.BOOT_SALT
    try:
        importlib.reload(main_module)
        second = main_module.BOOT_SALT
    finally:
        importlib.reload(main_module)

    assert first != second
    assert len(first) == len(second) == 32


def test_same_ip_hashes_stably_within_one_boot(client):
    """Fairness accounting needs a stable key for the life of the process."""
    from backend.main import resolve_origin_hash

    config = client.app.state.config
    request = SimpleNamespace(client=SimpleNamespace(host=CLIENT_IP), headers={})

    assert resolve_origin_hash(request, config) == resolve_origin_hash(request, config)
