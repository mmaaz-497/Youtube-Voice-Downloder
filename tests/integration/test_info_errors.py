"""T013 — US1 distinct errors on GET /api/info.

Two layers:
1. Route-level (mocked boundary): every AppError raised by the probe flows
   through the global handler untouched — right status, right envelope,
   message verbatim; INVALID_URL and DURATION_EXCEEDED are decided by the
   route itself.
2. Probe-level (real probe_video_info, subprocess faked): classification of
   yt-dlp output/stderr into VIDEO_UNAVAILABLE sub-reasons, LIVE_STREAM, and
   the kill-on-timeout → NETWORK_ERROR 504 path (plan D1); stderr never
   leaks into client-visible messages.
"""

import json
import subprocess
from types import SimpleNamespace

import pytest

from backend.models.errors import AppError, ErrorCode
from backend.services import youtube

VID = "dQw4w9WgXcQ"
WATCH_URL = f"https://www.youtube.com/watch?v={VID}"


def make_info(**overrides):
    base = {
        "video_id": VID,
        "title": "A title",
        "channel": "A channel",
        "duration_seconds": 213,
        "thumbnail_url": f"https://i.ytimg.com/vi/{VID}/hqdefault.jpg",
        "available": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------
# Layer 1: route-level — AppErrors pass through the global handler untouched
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "message", "status"),
    [
        (ErrorCode.VIDEO_UNAVAILABLE, "This video is private.", 404),
        (ErrorCode.VIDEO_UNAVAILABLE, "This video has been removed or deleted.", 404),
        (ErrorCode.VIDEO_UNAVAILABLE, "This video is age-restricted and cannot be accessed.", 404),
        (ErrorCode.VIDEO_UNAVAILABLE, "This video is not available in your region.", 404),
        (ErrorCode.LIVE_STREAM, "Live streams and premieres are not supported.", 400),
        (ErrorCode.NETWORK_ERROR, "Timed out while fetching video metadata; please try again.", 504),
    ],
)
def test_probe_apperrors_surface_verbatim(client, media_boundary, code, message, status):
    media_boundary.youtube.probe_video_info.side_effect = AppError(code, message)

    resp = client.get("/api/info", params={"url": WATCH_URL})

    assert resp.status_code == status
    assert resp.json() == {"error": {"code": code.value, "message": message}}


def test_invalid_url_rejected_before_probe(client, media_boundary):
    resp = client.get("/api/info", params={"url": "https://vimeo.com/123456789"})

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_URL"
    media_boundary.youtube.probe_video_info.assert_not_called()


def test_missing_url_param_is_invalid_input(client, media_boundary):
    resp = client.get("/api/info")

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_INPUT"
    media_boundary.youtube.probe_video_info.assert_not_called()


def test_duration_exceeded_after_probe(client, media_boundary):
    media_boundary.youtube.probe_video_info.return_value = make_info(duration_seconds=3601)

    resp = client.get("/api/info", params={"url": WATCH_URL})

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "DURATION_EXCEEDED"


# --------------------------------------------------------------------------
# Layer 2: probe-level — real probe_video_info over a faked subprocess
# --------------------------------------------------------------------------


class FakeProc:
    def __init__(self, stdout="", stderr="", returncode=0, hang=False):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.hang = hang
        self.killed = False
        self.timeout_seen = None

    def communicate(self, timeout=None):
        if self.hang and not self.killed:
            self.timeout_seen = timeout
            raise subprocess.TimeoutExpired(cmd="yt-dlp", timeout=timeout or 0)
        return self.stdout, self.stderr

    def kill(self):
        self.killed = True


@pytest.fixture
def fake_popen(monkeypatch):
    state = SimpleNamespace(argv=None, proc=None)

    def factory(**proc_kwargs):
        def _popen(argv, *args, **kwargs):
            state.argv = list(argv)
            state.proc = FakeProc(**proc_kwargs)
            return state.proc

        monkeypatch.setattr(subprocess, "Popen", _popen)
        return state

    return factory


def _good_json():
    return json.dumps(
        {
            "id": VID,
            "title": "A title",
            "channel": "A channel",
            "duration": 213,
            "thumbnail": f"https://i.ytimg.com/vi/{VID}/hqdefault.jpg",
            "is_live": False,
        }
    )


def test_probe_argv_is_fixed_template_on_canonical_url(fake_popen):
    state = fake_popen(stdout=_good_json())

    info = youtube.probe_video_info(VID, timeout_seconds=15)

    assert "--dump-single-json" in state.argv
    assert "--no-download" in state.argv
    assert "--no-playlist" in state.argv
    assert f"https://www.youtube.com/watch?v={VID}" in state.argv
    assert info.duration_seconds == 213
    assert info.available is True


def test_probe_timeout_kills_process_and_raises_network_error_504(fake_popen):
    state = fake_popen(hang=True)

    with pytest.raises(AppError) as excinfo:
        youtube.probe_video_info(VID, timeout_seconds=15)

    assert excinfo.value.code == ErrorCode.NETWORK_ERROR
    assert excinfo.value.http_status == 504
    assert state.proc.killed is True
    assert state.proc.timeout_seen == 15


@pytest.mark.parametrize(
    ("stderr", "keyword"),
    [
        ("ERROR: [youtube] x: Private video. Sign in if you've been granted access", "private"),
        ("ERROR: [youtube] x: Video unavailable. This video has been removed by the uploader", "removed"),
        ("ERROR: [youtube] x: Sign in to confirm your age. This video may be inappropriate", "age"),
        (
            "ERROR: [youtube] x: The uploader has not made this video available in your country",
            "region",
        ),
    ],
)
def test_probe_classifies_unavailability_sub_reasons(fake_popen, stderr, keyword):
    fake_popen(stderr=stderr, returncode=1)

    with pytest.raises(AppError) as excinfo:
        youtube.probe_video_info(VID, timeout_seconds=15)

    assert excinfo.value.code == ErrorCode.VIDEO_UNAVAILABLE
    assert keyword in excinfo.value.message.lower()
    # stderr goes to logs only — never into the client-visible message
    assert "ERROR: [youtube]" not in excinfo.value.message


def test_unavailability_sub_reason_messages_are_distinct(fake_popen):
    messages = []
    for stderr in (
        "ERROR: Private video. Sign in if you've been granted access",
        "ERROR: Video unavailable. This video has been removed by the uploader",
        "ERROR: Sign in to confirm your age",
        "ERROR: The uploader has not made this video available in your country",
    ):
        fake_popen(stderr=stderr, returncode=1)
        with pytest.raises(AppError) as excinfo:
            youtube.probe_video_info(VID, timeout_seconds=15)
        messages.append(excinfo.value.message)
    assert len(set(messages)) == len(messages)


@pytest.mark.parametrize(
    "payload",
    [
        {"is_live": True},
        {"live_status": "is_live"},
        {"live_status": "is_upcoming"},  # premiere
    ],
)
def test_probe_classifies_live_and_premiere(fake_popen, payload):
    data = json.loads(_good_json())
    data.update(payload)
    fake_popen(stdout=json.dumps(data))

    with pytest.raises(AppError) as excinfo:
        youtube.probe_video_info(VID, timeout_seconds=15)

    assert excinfo.value.code == ErrorCode.LIVE_STREAM


def test_probe_classifies_upcoming_live_event_from_stderr(fake_popen):
    fake_popen(stderr="ERROR: [youtube] x: This live event will begin in 2 hours", returncode=1)

    with pytest.raises(AppError) as excinfo:
        youtube.probe_video_info(VID, timeout_seconds=15)

    assert excinfo.value.code == ErrorCode.LIVE_STREAM


def test_probe_classifies_transient_network_failure(fake_popen):
    fake_popen(
        stderr="ERROR: Unable to download webpage: <urlopen error [Errno 104] Connection reset by peer>",
        returncode=1,
    )

    with pytest.raises(AppError) as excinfo:
        youtube.probe_video_info(VID, timeout_seconds=15)

    assert excinfo.value.code == ErrorCode.NETWORK_ERROR
    assert "Connection reset" not in excinfo.value.message
