"""T050 — static-response security headers (plan D4): the exact CSP string
and nosniff, plus a source-level check that frontend/ carries zero inline
event handlers (an inline handler would need 'unsafe-inline' and break the
policy the header promises).
"""

import re
from pathlib import Path

import pytest

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"

EXACT_CSP = (
    "default-src 'self'; "
    "img-src 'self' https://i.ytimg.com; "
    "script-src 'self'; "
    "style-src 'self'"
)

STATIC_PATHS = ("/", "/index.html", "/app.js", "/style.css")

# on<event>="..." style attributes, e.g. onclick=, onsubmit=, onerror=.
INLINE_HANDLER_RE = re.compile(r"\son[a-z]+\s*=", re.IGNORECASE)
JS_URL_RE = re.compile(r"""(?:href|src)\s*=\s*["']\s*javascript:""", re.IGNORECASE)


@pytest.mark.parametrize("path", STATIC_PATHS)
def test_static_responses_carry_exact_csp_and_nosniff(client, path):
    resp = client.get(path)

    assert resp.status_code == 200, path
    assert resp.headers["content-security-policy"] == EXACT_CSP
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_thumbnail_host_is_the_only_external_origin():
    """CSP allows exactly one external origin; nothing else may be embedded."""
    assert EXACT_CSP.count("https://") == 1
    assert "https://i.ytimg.com" in EXACT_CSP
    for directive in ("default-src", "script-src", "style-src"):
        clause = next(part for part in EXACT_CSP.split("; ") if part.startswith(directive))
        assert clause.endswith("'self'"), clause
    assert "unsafe-inline" not in EXACT_CSP
    assert "unsafe-eval" not in EXACT_CSP


def test_frontend_has_zero_inline_event_handlers():
    offenders = {}
    for path in FRONTEND_DIR.rglob("*"):
        if path.suffix.lower() not in {".html", ".htm"}:
            continue
        text = path.read_text(encoding="utf-8")
        hits = INLINE_HANDLER_RE.findall(text) + JS_URL_RE.findall(text)
        if hits:
            offenders[path.name] = hits
    assert offenders == {}, f"inline handlers would require 'unsafe-inline': {offenders}"


def test_frontend_has_no_inline_script_or_style_blocks():
    """Inline <script>/<style> bodies are blocked by 'self'-only directives,
    so they would silently not run in a browser."""
    offenders = {}
    for path in FRONTEND_DIR.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        for tag in ("script", "style"):
            for match in re.finditer(
                rf"<{tag}(?P<attrs>[^>]*)>(?P<body>.*?)</{tag}>", text, re.DOTALL | re.I
            ):
                if match.group("body").strip():
                    offenders.setdefault(path.name, []).append(tag)
    assert offenders == {}, f"inline script/style blocks violate the CSP: {offenders}"


def test_api_responses_are_not_given_the_static_csp(client, media_boundary):
    """The CSP governs the document; JSON endpoints are not documents."""
    resp = client.get("/api/health")

    assert resp.status_code == 200
    assert "content-security-policy" not in resp.headers
