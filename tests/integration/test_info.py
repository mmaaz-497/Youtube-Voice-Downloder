"""T012 — US1 happy path: GET /api/info returns InfoResponse fields for a
mocked reachable video. Boundary mocked at the public-function level
(probe_video_info); the pure URL validator stays real.
"""

from types import SimpleNamespace

VID = "dQw4w9WgXcQ"


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


def test_info_happy_path_returns_all_fields(client, media_boundary):
    media_boundary.youtube.probe_video_info.return_value = make_info()

    resp = client.get("/api/info", params={"url": f"https://www.youtube.com/watch?v={VID}"})

    assert resp.status_code == 200
    assert resp.json() == {
        "video_id": VID,
        "title": "Never Gonna Give You Up",
        "channel": "Rick Astley",
        "duration_seconds": 213,
        "thumbnail_url": f"https://i.ytimg.com/vi/{VID}/hqdefault.jpg",
        "available": True,
    }


def test_probe_called_with_video_id_never_raw_url(client, media_boundary):
    """The raw user string must never reach the boundary — only the
    validated 11-char ID (SSRF prevention by construction, plan D1)."""
    media_boundary.youtube.probe_video_info.return_value = make_info()
    raw_url = f"https://youtu.be/{VID}?t=42"

    resp = client.get("/api/info", params={"url": raw_url})

    assert resp.status_code == 200
    call = media_boundary.youtube.probe_video_info.call_args
    passed_values = list(call.args) + list(call.kwargs.values())
    assert VID in passed_values
    assert all(value != raw_url for value in passed_values if isinstance(value, str))


def test_all_accepted_url_shapes_reach_the_probe(client, media_boundary):
    media_boundary.youtube.probe_video_info.return_value = make_info()
    for url in (
        f"https://www.youtube.com/watch?v={VID}",
        f"https://www.youtube.com/shorts/{VID}",
        f"https://www.youtube.com/embed/{VID}",
        f"https://youtu.be/{VID}",
    ):
        resp = client.get("/api/info", params={"url": url})
        assert resp.status_code == 200, url
        assert resp.json()["video_id"] == VID


def test_duration_at_cap_is_accepted(client, media_boundary):
    """Boundary value: exactly MAX_DURATION_SECONDS (default 3600) is fine."""
    media_boundary.youtube.probe_video_info.return_value = make_info(duration_seconds=3600)

    resp = client.get("/api/info", params={"url": f"https://www.youtube.com/watch?v={VID}"})

    assert resp.status_code == 200
    assert resp.json()["duration_seconds"] == 3600
