"""T022 — US2 full lifecycle: queued→running→completed; audio/mpeg attachment
with sanitized filename; 409 NOT_READY before completion; and subprocess-level
argv hygiene — FFmpeg argv contains `-b:a <chosen>k` + `-metadata title=...`;
yt-dlp argv ALWAYS contains `--no-playlist` and `--retries 0` and NEVER any
cookie/credential/DRM flag (FR-030, plan D1).
"""

import io
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import backend.services.youtube as youtube_module
from tests.conftest import wait_until

try:
    import backend.services.audio as audio_module
except ImportError:  # red phase: created in T027
    audio_module = None

# Real boundary functions captured at import time, before the media_boundary
# fixture replaces them — used for subprocess-level argv assertions.
REAL_DOWNLOAD = getattr(youtube_module, "download_audio", None)
REAL_TRANSCODE = getattr(audio_module, "transcode", None) if audio_module else None

VID = "dQw4w9WgXcQ"
WATCH_URL = f"https://www.youtube.com/watch?v={VID}"
CANONICAL_URL = f"https://www.youtube.com/watch?v={VID}"

# Any argv element starting with one of these is a violation (covers both
# --cookies and --cookies-from-browser via the shared prefix).
FORBIDDEN_YTDLP_FLAGS = (
    "--cookies",
    "--username",
    "--password",
    "--video-password",
    "--ap-username",
    "--ap-password",
    "--netrc",
    "--client-certificate",
    "--allow-unplayable-formats",
)


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


def poll_until_terminal(client, job_id, timeout=5.0):
    last = {}

    def _terminal():
        resp = client.get(f"/api/jobs/{job_id}")
        last["resp"] = resp
        return resp.status_code == 200 and resp.json()["status"] in ("completed", "failed")

    assert wait_until(_terminal, timeout), (
        f"job never reached a terminal state: {last['resp'].status_code} {last['resp'].text}"
    )
    return last["resp"].json()


def test_full_lifecycle_delivery_and_purge(client, media_boundary, work_dir):
    media_boundary.youtube.probe_video_info.return_value = make_info()

    def fake_download(video_id, target_dir, **kwargs):
        source = Path(target_dir) / "bestaudio.source"
        source.write_bytes(b"raw-bestaudio")
        return source

    def fake_transcode(src, dst, bitrate_kbps, title, **kwargs):
        Path(dst).write_bytes(b"ID3-fake-mp3")
        return Path(dst)

    media_boundary.youtube.download_audio.side_effect = fake_download
    media_boundary.audio.transcode.side_effect = fake_transcode

    resp = client.post("/api/jobs", json={"url": WATCH_URL, "bitrate_kbps": 192})
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    status = poll_until_terminal(client, job_id)
    assert status["status"] == "completed", status
    assert status["progress"] == 100
    # Source deleted immediately after transcode, before delivery.
    assert not (work_dir / "bestaudio.source").exists()

    file_resp = client.get(f"/api/jobs/{job_id}/file")
    assert file_resp.status_code == 200
    assert file_resp.headers["content-type"].startswith("audio/mpeg")
    disposition = file_resp.headers["content-disposition"]
    assert "attachment" in disposition
    assert "Never Gonna Give You Up.mp3" in disposition
    assert file_resp.content == b"ID3-fake-mp3"

    # Delivery purge: job unknown afterwards, and the MP3 is gone from disk
    # (deleted by the post-stream background task, so the stream never breaks).
    assert client.get(f"/api/jobs/{job_id}").status_code == 404
    again = client.get(f"/api/jobs/{job_id}/file")
    assert again.status_code == 404
    assert again.json()["error"]["code"] == "JOB_NOT_FOUND"
    assert list(work_dir.glob("*.mp3")) == []


def test_file_before_completion_is_not_ready(client, media_boundary):
    media_boundary.youtube.probe_video_info.return_value = make_info()
    started, release = threading.Event(), threading.Event()

    def parked_download(*args, **kwargs):
        started.set()
        release.wait(timeout=10)
        return MagicMock()

    media_boundary.youtube.download_audio.side_effect = parked_download
    try:
        resp = client.post("/api/jobs", json={"url": WATCH_URL})
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]
        assert started.wait(5)

        file_resp = client.get(f"/api/jobs/{job_id}/file")
        assert file_resp.status_code == 409
        assert file_resp.json()["error"]["code"] == "NOT_READY"
    finally:
        release.set()
    poll_until_terminal(client, job_id)


def test_unknown_job_is_404(client):
    ghost = str(uuid.uuid4())
    for path in (f"/api/jobs/{ghost}", f"/api/jobs/{ghost}/file"):
        resp = client.get(path)
        assert resp.status_code == 404, path
        assert resp.json()["error"]["code"] == "JOB_NOT_FOUND"


@pytest.fixture
def real_media(monkeypatch, media_boundary):
    """Swap the real download/transcode back in over the function-level mocks
    so subprocess argv actually gets built."""
    assert REAL_DOWNLOAD is not None and REAL_TRANSCODE is not None, (
        "download_audio/transcode not implemented yet"
    )
    monkeypatch.setattr(youtube_module, "download_audio", REAL_DOWNLOAD)
    monkeypatch.setattr(audio_module, "transcode", REAL_TRANSCODE)


@pytest.fixture
def captured_argv(monkeypatch):
    """Fake subprocess.Popen recording every argv and materializing the
    expected output files so the real boundary code proceeds."""
    calls: list[list[str]] = []

    class FakePopen:
        def __init__(self, argv, **kwargs):
            calls.append(list(argv))
            self.returncode = 0
            self.stdout = io.StringIO("")
            self.stderr = io.StringIO("")
            if argv and argv[0] == "yt-dlp" and "-o" in argv:
                Path(argv[argv.index("-o") + 1]).write_bytes(b"fake-bestaudio")
            elif argv and argv[0] == "ffmpeg":
                Path(argv[-1]).write_bytes(b"fake-mp3")

        def wait(self, timeout=None):
            return self.returncode

        def communicate(self, timeout=None):
            return ("", "")

        def kill(self):
            pass

    import subprocess

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    return calls


def test_subprocess_argv_hygiene(client, media_boundary, real_media, captured_argv, work_dir):
    media_boundary.youtube.probe_video_info.return_value = make_info()

    resp = client.post("/api/jobs", json={"url": WATCH_URL, "bitrate_kbps": 320})
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    status = poll_until_terminal(client, job_id)
    assert status["status"] == "completed", status

    ytdlp_calls = [argv for argv in captured_argv if argv and argv[0] == "yt-dlp"]
    assert len(ytdlp_calls) == 1
    argv = ytdlp_calls[0]

    # C1: fixed template invariants.
    assert "--no-playlist" in argv
    retries_at = argv.index("--retries")
    assert argv[retries_at + 1] == "0"
    # Only the reconstructed canonical URL — exactly once, as the final element.
    assert argv[-1] == CANONICAL_URL
    assert sum(1 for element in argv if VID in element) == 1
    # Output path is a UUID path inside WORK_DIR.
    out_path = Path(argv[argv.index("-o") + 1])
    assert work_dir in out_path.parents
    # NEVER any cookie/credential/DRM flag.
    for flag in FORBIDDEN_YTDLP_FLAGS:
        assert not any(element.startswith(flag) for element in argv), flag

    ffmpeg_calls = [argv for argv in captured_argv if argv and argv[0] == "ffmpeg"]
    assert len(ffmpeg_calls) == 1
    fargv = ffmpeg_calls[0]
    assert "-vn" in fargv
    assert fargv[fargv.index("-codec:a") + 1] == "libmp3lame"
    assert fargv[fargv.index("-b:a") + 1] == "320k"
    # Title fused into ONE argv element — no shell, no quoting, no injection.
    assert fargv[fargv.index("-metadata") + 1] == "title=Never Gonna Give You Up"
    assert fargv[fargv.index("-id3v2_version") + 1] == "3"

    # Source artifact deleted immediately after successful transcode.
    assert not out_path.exists()
