"""T019 — US2 job creation: 202 + job handle, distinct INVALID_BITRATE for
out-of-set values (missing/malformed fields stay INVALID_INPUT), default 192,
INVALID_URL, and duration re-checked at job time (DURATION_EXCEEDED).
"""

import uuid
from types import SimpleNamespace

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


def test_job_create_returns_202_with_handle(client, media_boundary):
    media_boundary.youtube.probe_video_info.return_value = make_info()

    resp = client.post("/api/jobs", json={"url": WATCH_URL, "bitrate_kbps": 128})

    assert resp.status_code == 202
    body = resp.json()
    assert uuid.UUID(body["job_id"])  # unguessable handle
    assert body["status"] == "queued"
    assert body["queue_position"] == 1


def test_each_published_bitrate_accepted(make_client, media_boundary):
    media_boundary.youtube.probe_video_info.return_value = make_info()
    client = make_client(PER_ORIGIN_CAP=100)

    for kbps in (96, 128, 192, 320):
        resp = client.post("/api/jobs", json={"url": WATCH_URL, "bitrate_kbps": kbps})
        assert resp.status_code == 202, f"{kbps}: {resp.text}"


def test_out_of_set_bitrate_gets_distinct_code(client, media_boundary):
    media_boundary.youtube.probe_video_info.return_value = make_info()

    resp = client.post("/api/jobs", json={"url": WATCH_URL, "bitrate_kbps": 160})

    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "INVALID_BITRATE"
    assert "bitrate" in err["message"].lower()


def test_missing_url_stays_invalid_input(client):
    resp = client.post("/api/jobs", json={"bitrate_kbps": 192})

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_INPUT"


def test_malformed_bitrate_stays_invalid_input(client, media_boundary):
    media_boundary.youtube.probe_video_info.return_value = make_info()

    resp = client.post("/api/jobs", json={"url": WATCH_URL, "bitrate_kbps": "fast"})

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_INPUT"


def test_omitted_bitrate_defaults_to_192(client, media_boundary):
    media_boundary.youtube.probe_video_info.return_value = make_info()

    resp = client.post("/api/jobs", json={"url": WATCH_URL})

    assert resp.status_code == 202
    assert wait_until(lambda: media_boundary.audio.transcode.called)
    call = media_boundary.audio.transcode.call_args
    passed = list(call.args) + list(call.kwargs.values())
    assert 192 in passed


def test_invalid_url_rejected_before_probe(client, media_boundary):
    resp = client.post("/api/jobs", json={"url": "https://vimeo.com/12345"})

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_URL"
    media_boundary.youtube.probe_video_info.assert_not_called()


def test_duration_rechecked_at_job_time(client, media_boundary):
    media_boundary.youtube.probe_video_info.return_value = make_info(duration_seconds=3601)

    resp = client.post("/api/jobs", json={"url": WATCH_URL, "bitrate_kbps": 192})

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "DURATION_EXCEEDED"
    media_boundary.youtube.download_audio.assert_not_called()
