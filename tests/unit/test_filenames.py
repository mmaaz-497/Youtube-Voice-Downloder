"""T023 — sanitize_filename: safe-char allowlist, length cap, emoji-only
fallback to `audio-<video_id>`, and header-only safety (never usable as a
path, never breaks a Content-Disposition header).
"""

import backend.services.youtube as youtube_module

VID = "dQw4w9WgXcQ"


def sanitize(title: str) -> str:
    return youtube_module.sanitize_filename(title, VID)


def test_allowlisted_characters_preserved():
    assert sanitize("My Song (Live) [HD] - part 1.0") == "My Song (Live) [HD] - part 1.0"


def test_disallowed_characters_stripped():
    result = sanitize('Na/me: "quoted" <tag> | 100%?')
    for forbidden in '/:"<>|%?\\':
        assert forbidden not in result
    assert "Name" in result and "100" in result


def test_length_capped():
    result = sanitize("A" * 500)
    assert 0 < len(result) <= 100


def test_emoji_only_title_falls_back_to_video_id():
    assert sanitize("\U0001f3b5\U0001f3b6\U0001f525") == f"audio-{VID}"


def test_empty_and_whitespace_fall_back():
    assert sanitize("") == f"audio-{VID}"
    assert sanitize("   ") == f"audio-{VID}"


def test_path_traversal_neutralized():
    for hostile in ("../../etc/passwd", "..\\..\\windows\\system32", "C:\\Users\\x", "/etc/shadow"):
        result = sanitize(hostile)
        assert "/" not in result and "\\" not in result, hostile
        assert not result.startswith("."), hostile


def test_header_injection_characters_removed():
    result = sanitize('bad\r\nheader: "attack"; filename=x')
    for forbidden in ('\r', '\n', '"', ';'):
        assert forbidden not in result


def test_no_trailing_dots_or_spaces():
    assert sanitize("Song... ") == "Song"
