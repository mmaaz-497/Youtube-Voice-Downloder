"""T039 — US4 retry policy at the subprocess level (real download_audio /
transcode over a scripted fake Popen, clock faked — no real seconds slept):
transient network failures retried exactly twice with 1 s then 4 s backoff →
NETWORK_ERROR on exhaustion; VIDEO_UNAVAILABLE and transcode failures never
retried; every retried invocation's yt-dlp argv re-verified (FR-030); retries
logged with the video ID.
"""

import io
import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import backend.services.audio as audio_module
import backend.services.youtube as youtube_module
from backend.models.errors import AppError, ErrorCode

VID = "dQw4w9WgXcQ"
TRANSIENT_STDERR = (
    "ERROR: unable to download video data: <urlopen error Connection reset by peer>"
)
FORBIDDEN_PREFIXES = ("--cookies", "--username", "--password", "--netrc")


@pytest.fixture
def scripted_popen(monkeypatch):
    """Fake subprocess.Popen driven by a per-call script of
    (returncode, stderr_text) tuples. yt-dlp calls always leave a partial
    file at the -o path (as a killed/failed real download would); ffmpeg
    calls create their output only on success."""
    calls: list[list[str]] = []
    script: list[tuple[int, str]] = []

    class _Popen:
        def __init__(self, argv, **kwargs):
            calls.append(list(argv))
            returncode, stderr_text = script.pop(0)
            self.returncode = returncode
            self.stdout = io.StringIO("[download]  42.0% of ~4.00MiB\n")
            self.stderr = io.StringIO(stderr_text)
            if argv and argv[0] == "yt-dlp" and "-o" in argv:
                Path(argv[argv.index("-o") + 1]).write_bytes(b"partial")
            elif argv and argv[0] == "ffmpeg" and returncode == 0:
                Path(argv[-1]).write_bytes(b"mp3")

        def wait(self, timeout=None):
            return self.returncode

        def communicate(self, timeout=None):
            return ("", self.stderr.read())

        def kill(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", _Popen)
    return SimpleNamespace(calls=calls, script=script)


def test_transient_failure_retried_twice_then_network_error(
    scripted_popen, fake_clock, tmp_path, caplog
):
    caplog.set_level(logging.INFO, logger="yt-audio-extractor")
    scripted_popen.script.extend([(1, TRANSIENT_STDERR)] * 3)

    with pytest.raises(AppError) as exc:
        youtube_module.download_audio(VID, tmp_path, timeout_seconds=600)

    assert exc.value.code is ErrorCode.NETWORK_ERROR
    # Exactly 1 initial attempt + 2 retries, 1 s then 4 s backoff (fake clock).
    assert len(scripted_popen.calls) == 3
    assert fake_clock.sleeps == [1, 4]

    # Every retried invocation keeps the fixed template (FR-030).
    for argv in scripted_popen.calls:
        assert "--no-playlist" in argv
        assert argv[argv.index("--retries") + 1] == "0"
        for prefix in FORBIDDEN_PREFIXES:
            assert not any(element.startswith(prefix) for element in argv), prefix

    # Partials from every failed attempt are deleted.
    assert list(tmp_path.glob("*.source")) == []

    retry_lines = [r.getMessage() for r in caplog.records if "retry" in r.getMessage().lower()]
    assert len(retry_lines) == 2
    assert all(VID in line for line in retry_lines)


def test_success_after_single_transient_failure(scripted_popen, fake_clock, tmp_path):
    scripted_popen.script.extend([(1, TRANSIENT_STDERR), (0, "")])

    result = youtube_module.download_audio(VID, tmp_path, timeout_seconds=600)

    assert result.exists()
    assert len(scripted_popen.calls) == 2
    assert fake_clock.sleeps == [1]


def test_dns_failure_counts_as_transient(scripted_popen, fake_clock, tmp_path):
    scripted_popen.script.extend(
        [(1, "ERROR: [Errno 11001] getaddrinfo failed")] * 3
    )

    with pytest.raises(AppError) as exc:
        youtube_module.download_audio(VID, tmp_path, timeout_seconds=600)

    assert exc.value.code is ErrorCode.NETWORK_ERROR
    assert len(scripted_popen.calls) == 3
    assert fake_clock.sleeps == [1, 4]


def test_video_unavailable_never_retried(scripted_popen, fake_clock, tmp_path):
    scripted_popen.script.append(
        (1, "ERROR: [youtube] x: Private video. This video is private.")
    )

    with pytest.raises(AppError) as exc:
        youtube_module.download_audio(VID, tmp_path, timeout_seconds=600)

    assert exc.value.code is ErrorCode.VIDEO_UNAVAILABLE
    assert len(scripted_popen.calls) == 1
    assert fake_clock.sleeps == []
    assert list(tmp_path.glob("*.source")) == []


def test_deterministic_extraction_error_never_retried(
    scripted_popen, fake_clock, tmp_path
):
    scripted_popen.script.append((1, "ERROR: unsupported codec mystery failure"))

    with pytest.raises(AppError) as exc:
        youtube_module.download_audio(VID, tmp_path, timeout_seconds=600)

    assert exc.value.code is ErrorCode.EXTRACTION_FAILED
    assert len(scripted_popen.calls) == 1
    assert fake_clock.sleeps == []


def test_transcode_failure_never_retried(scripted_popen, fake_clock, tmp_path):
    source = tmp_path / "in.source"
    source.write_bytes(b"x")
    scripted_popen.script.append((1, "ffmpeg: encoder exploded"))

    with pytest.raises(AppError) as exc:
        audio_module.transcode(source, tmp_path / "out.mp3", 192, "t", timeout_seconds=300)

    assert exc.value.code is ErrorCode.TRANSCODE_FAILED
    assert len(scripted_popen.calls) == 1
    assert fake_clock.sleeps == []
    assert not (tmp_path / "out.mp3").exists()
