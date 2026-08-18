"""T010 — URL allowlist matrix for the pure validator (research.md R5).

Pure functions only: a blocked-socket fixture proves no network is touched.
Every rejection must be INVALID_URL (spec Error Taxonomy).
"""

import socket

import pytest

from backend.models.errors import AppError, ErrorCode
from backend.services.youtube import canonical_url, validate_and_extract_video_id

VID = "dQw4w9WgXcQ"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def _blocked(*args, **kwargs):
        raise AssertionError("URL validation must not touch the network")

    monkeypatch.setattr(socket, "getaddrinfo", _blocked)
    monkeypatch.setattr(socket.socket, "connect", _blocked)


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.youtube.com/watch?v={VID}",
        f"http://www.youtube.com/watch?v={VID}",
        f"https://youtube.com/watch?v={VID}",
        f"https://m.youtube.com/watch?v={VID}",
        f"https://www.youtube.com/shorts/{VID}",
        f"https://www.youtube.com/embed/{VID}",
        f"https://youtu.be/{VID}",
        f"https://youtu.be/{VID}?t=42",
        # Playlist parameter on a watch URL is ignored, not fatal
        f"https://www.youtube.com/watch?v={VID}&list=PL0123456789abcdef",
        f"https://www.youtube.com/watch?list=PL0123456789abcdef&v={VID}",
    ],
)
def test_accepts_allowlisted_shapes(url):
    assert validate_and_extract_video_id(url) == VID


@pytest.mark.parametrize(
    "url",
    [
        # Lookalike hosts
        f"https://youtube.com.evil.tld/watch?v={VID}",
        f"https://evilyoutube.com/watch?v={VID}",
        f"https://www.youtube.com.evil.tld/watch?v={VID}",
        # Userinfo tricks
        f"https://user@youtube.com/watch?v={VID}",
        f"https://youtube.com@evil.tld/watch?v={VID}",
        # Explicit ports
        f"https://www.youtube.com:8443/watch?v={VID}",
        f"http://youtu.be:80/{VID}",
        # Non-YouTube
        "https://vimeo.com/123456789",
        "https://example.com/watch?v=abcdefghijk",
        # Wrong scheme
        f"ftp://www.youtube.com/watch?v={VID}",
        f"file:///watch?v={VID}",
        # Playlist-only (no video ID)
        "https://www.youtube.com/playlist?list=PL0123456789abcdef",
        # Missing / malformed ID
        "https://www.youtube.com/watch",
        "https://www.youtube.com/watch?v=",
        "https://www.youtube.com/watch?v=tooshort",
        f"https://www.youtube.com/watch?v={VID}extra",
        "https://www.youtube.com/shorts/",
        "https://youtu.be/",
        # Malformed / empty
        "",
        "not a url",
        "youtube.com/watch?v=dQw4w9WgXcQ",  # no scheme
    ],
)
def test_rejects_with_invalid_url(url):
    with pytest.raises(AppError) as exc_info:
        validate_and_extract_video_id(url)
    assert exc_info.value.code == ErrorCode.INVALID_URL


def test_canonical_url_reconstruction():
    assert canonical_url(VID) == f"https://www.youtube.com/watch?v={VID}"
