"""Shared test harness (constitution: offline, deterministic; media boundary
mocked at the public-function level of services.youtube / services.audio,
never at subprocess level).
"""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import backend.services.audio as audio_service
import backend.services.youtube as youtube_service

# Public media-boundary functions to mock per module. Pure helpers
# (validate_and_extract_video_id, canonical_url, sanitize_filename) stay real.
YOUTUBE_BOUNDARY = ("probe_video_info", "download_audio", "ytdlp_version")
AUDIO_BOUNDARY = ("check_libmp3lame", "transcode")

# Documented baseline applied on EVERY make_client build, so a knob set for
# one case can never leak into the next and mask the code under test (the
# US4 deviation: a leftover PER_ORIGIN_CAP=0 turned an AT_CAPACITY case into
# CLIENT_LIMIT). Values mirror data-model.md's Config table, except
# MAX_CONCURRENCY/QUEUE_LIMIT which are pinned to keep tests independent of
# the host's CPU count.
BASELINE_ENV: dict[str, str] = {
    "MAX_CONCURRENCY": "2",
    "QUEUE_LIMIT": "20",
    "PER_ORIGIN_CAP": "3",
    "DISK_FLOOR_BYTES": str(1024**3),
    "TTL_SECONDS": "900",
    "SWEEP_INTERVAL_SECONDS": "60",
    "DOWNLOAD_TIMEOUT_SECONDS": "600",
    "TRANSCODE_TIMEOUT_SECONDS": "300",
    "MAX_DURATION_SECONDS": "3600",
    "PROBE_TIMEOUT_SECONDS": "15",
    "TRUSTED_PROXY": "0",
}


@pytest.fixture(autouse=True)
def work_dir(tmp_path, monkeypatch):
    """Fresh WORK_DIR per test; nothing ever touches the real OS temp dir."""
    path = tmp_path / "work"
    monkeypatch.setenv("WORK_DIR", str(path))
    return path


@pytest.fixture
def set_env(monkeypatch):
    """Env override helper: set_env(TTL_SECONDS="10", TRUSTED_PROXY="1")."""

    def _set(**overrides: str) -> None:
        for name, value in overrides.items():
            monkeypatch.setenv(name, value)

    return _set


@pytest.fixture
def media_boundary(monkeypatch):
    """MagicMocks replacing the media-boundary functions; tests configure
    return values / side effects on e.g. media_boundary.youtube.probe_video_info."""
    yt = SimpleNamespace()
    for name in YOUTUBE_BOUNDARY:
        mock = MagicMock(name=f"youtube.{name}")
        monkeypatch.setattr(youtube_service, name, mock)
        setattr(yt, name, mock)

    audio = SimpleNamespace()
    for name in AUDIO_BOUNDARY:
        mock = MagicMock(name=f"audio.{name}")
        monkeypatch.setattr(audio_service, name, mock)
        setattr(audio, name, mock)

    return SimpleNamespace(youtube=yt, audio=audio)


@pytest.fixture
def app(work_dir, media_boundary):
    """Fresh app instance reading current env, media boundary already mocked."""
    from backend.config import Config
    from backend.main import create_app

    return create_app(Config())


@pytest.fixture
def client(app):
    # raise_server_exceptions=False so the global INTERNAL handler is exercised
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def make_client(work_dir, media_boundary, monkeypatch):
    """Factory building a client AFTER applying env overrides — needed by
    admission/origin tests whose Config must read MAX_CONCURRENCY etc.

    Every build starts from BASELINE_ENV, so each call configures exactly the
    knobs it names and inherits nothing from earlier builds in the same test.
    """
    created: list[TestClient] = []

    def _make(**env) -> TestClient:
        for name, value in {**BASELINE_ENV, **{k: str(v) for k, v in env.items()}}.items():
            monkeypatch.setenv(name, value)
        from backend.config import Config
        from backend.main import create_app

        test_client = TestClient(create_app(Config()), raise_server_exceptions=False)
        test_client.__enter__()
        created.append(test_client)
        return test_client

    yield _make
    for test_client in created:
        test_client.__exit__(None, None, None)


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    """Poll a condition with a real-time deadline (worker threads are real)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


class FakeClock:
    """Deterministic clock for TTL/backoff/watchdog tests."""

    def __init__(self, start: float = 1_000_000.0):
        self.now = start
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def fake_clock(monkeypatch):
    """FakeClock patched over time.time/time.monotonic/time.sleep."""
    clock = FakeClock()
    monkeypatch.setattr(time, "time", clock.time)
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    return clock
