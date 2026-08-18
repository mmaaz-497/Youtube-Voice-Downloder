"""yt-dlp boundary: pure URL validation and canonicalization (research.md R5)
plus the metadata probe (T014). The raw user URL never reaches a subprocess;
only the reconstructed canonical URL does (SSRF prevention by construction,
FR-001..003). yt-dlp is invoked strictly as a subprocess with a fixed argv
template (plan D1); its stderr goes to logs only, never into client-visible
messages.

The bestaudio download (T026) uses the same fixed-argv discipline: the argv
template never contains cookie, credential, or DRM flags of any kind (FR-030).
"""

import json
import logging
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from backend.models.errors import AppError, ErrorCode

logger = logging.getLogger("yt-audio-extractor")

ALLOWED_HOSTS = {"www.youtube.com", "youtube.com", "m.youtube.com", "youtu.be"}
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _invalid(reason: str) -> AppError:
    return AppError(ErrorCode.INVALID_URL, f"Not a supported YouTube URL: {reason}")


def validate_and_extract_video_id(url: str) -> str:
    """Validate a user-supplied URL against the allowlist and return the 11-char
    video ID. Raises AppError(INVALID_URL) for anything else. Pure — no network."""
    if not url or not isinstance(url, str):
        raise _invalid("empty URL")

    try:
        parts = urlsplit(url.strip())
    except ValueError:
        raise _invalid("malformed URL")

    if parts.scheme not in ("http", "https"):
        raise _invalid("only http(s) YouTube URLs are accepted")

    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise _invalid("userinfo is not allowed")

    try:
        port = parts.port
    except ValueError:
        raise _invalid("malformed port")
    if port is not None:
        raise _invalid("explicit ports are not allowed")

    host = (parts.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise _invalid("host is not an allowlisted YouTube domain")

    path = parts.path or ""

    if host == "youtu.be":
        candidate = path.lstrip("/").split("/")[0]
    elif path == "/watch":
        values = parse_qs(parts.query).get("v", [])
        candidate = values[0] if values else ""
    elif path.startswith("/shorts/") or path.startswith("/embed/"):
        candidate = path.split("/", 2)[2].split("/")[0]
    else:
        raise _invalid("unsupported URL shape (single videos only, not playlists)")

    if not VIDEO_ID_RE.match(candidate):
        raise _invalid("could not find an 11-character video ID")

    return candidate


def canonical_url(video_id: str) -> str:
    """Reconstruct the canonical watch URL used for all downstream work."""
    return f"https://www.youtube.com/watch?v={video_id}"


# Safe-char allowlist for download filenames: letters, digits, space, and a
# small punctuation set. Everything else (path separators, quotes, control
# chars, non-ASCII) is dropped — the result is only ever a header value,
# never a filesystem path.
_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9 ._()\[\]-]+")
_FILENAME_MAX_LENGTH = 80


def sanitize_filename(title: str, video_id: str) -> str:
    """Sanitize a video title for use in Content-Disposition. Falls back to
    `audio-<video_id>` when nothing safe survives (e.g. emoji-only titles)."""
    cleaned = _FILENAME_SAFE.sub("", title or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    cleaned = cleaned[:_FILENAME_MAX_LENGTH].rstrip(" .")
    if not cleaned:
        return f"audio-{video_id}"
    return cleaned


def ytdlp_version(timeout_seconds: int = 10) -> str | None:
    """Reported in /api/health so an operator can tell at a glance whether a
    stale yt-dlp explains a wave of extraction failures. Returns None when the
    binary is absent or unresponsive — never raises."""
    try:
        proc = subprocess.run(
            ["yt-dlp", "--version"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None


@dataclass
class VideoInfo:
    """Transient metadata — never stored (data-model.md VideoInfo)."""

    video_id: str
    title: str
    channel: str
    duration_seconds: int
    thumbnail_url: str
    available: bool = True


# Classification patterns over lowercased yt-dlp stderr. Order matters:
# specific sub-reasons before the generic "video unavailable" catch-all.
_LIVE_PATTERNS = ("live event will begin", "premieres in", "premiere will begin")
_UNAVAILABLE_SUB_REASONS = (
    (("private video", "this video is private"), "This video is private."),
    (
        ("confirm your age", "age-restricted", "age restricted"),
        "This video is age-restricted and cannot be accessed.",
    ),
    (
        ("available in your country", "geo restriction", "blocked it in your country"),
        "This video is not available in your region.",
    ),
    (
        ("has been removed", "no longer available", "account associated with this video has been terminated"),
        "This video has been removed or deleted.",
    ),
)
_NETWORK_PATTERNS = (
    "unable to download",
    "connection reset",
    "connection refused",
    "timed out",
    "timeout",
    "getaddrinfo",
    "name resolution",
    "network is unreachable",
)


def _classify_probe_failure(stderr: str) -> AppError:
    """Map yt-dlp stderr to a typed AppError. The stderr text itself is
    logged by the caller and never placed in the returned message."""
    text = stderr.lower()
    for pattern in _LIVE_PATTERNS:
        if pattern in text:
            return AppError(
                ErrorCode.LIVE_STREAM,
                "Live streams and premieres are not supported; try again after the stream has ended.",
            )
    for patterns, message in _UNAVAILABLE_SUB_REASONS:
        if any(pattern in text for pattern in patterns):
            return AppError(ErrorCode.VIDEO_UNAVAILABLE, message)
    for pattern in _NETWORK_PATTERNS:
        if pattern in text:
            return AppError(
                ErrorCode.NETWORK_ERROR,
                "A network error occurred while contacting YouTube; please try again.",
            )
    return AppError(ErrorCode.VIDEO_UNAVAILABLE, "This video is unavailable.")


def probe_video_info(video_id: str, timeout_seconds: int = 15) -> VideoInfo:
    """Probe metadata via yt-dlp with a fixed argv template on the canonical
    URL only. Bounded by PROBE_TIMEOUT_SECONDS: kill-on-timeout surfaces as
    NETWORK_ERROR 504 (plan D1) so a hung probe never occupies a thread."""
    argv = [
        "yt-dlp",
        "--dump-single-json",
        "--no-download",
        "--no-playlist",
        canonical_url(video_id),
    ]
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        logger.error("yt-dlp binary not found while probing video_id=%s", video_id)
        raise AppError(ErrorCode.INTERNAL, "Metadata service is unavailable on the server.")

    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        logger.warning("Metadata probe timed out after %ss video_id=%s", timeout_seconds, video_id)
        raise AppError(
            ErrorCode.NETWORK_ERROR,
            "Timed out while fetching video metadata; please try again.",
        )

    if proc.returncode != 0:
        logger.warning(
            "Metadata probe failed video_id=%s exit=%s stderr=%s",
            video_id,
            proc.returncode,
            (stderr or "").strip(),
        )
        raise _classify_probe_failure(stderr or "")

    try:
        data = json.loads(stdout)
    except (TypeError, ValueError):
        logger.error("Metadata probe returned unparseable JSON video_id=%s", video_id)
        raise AppError(ErrorCode.INTERNAL, "Could not read video metadata.")

    if data.get("is_live") or data.get("live_status") in ("is_live", "is_upcoming"):
        raise AppError(
            ErrorCode.LIVE_STREAM,
            "Live streams and premieres are not supported; try again after the stream has ended.",
        )

    return VideoInfo(
        video_id=video_id,
        title=data.get("title") or "",
        channel=data.get("channel") or data.get("uploader") or "",
        duration_seconds=int(data.get("duration") or 0),
        thumbnail_url=data.get("thumbnail")
        or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        available=True,
    )


def _classify_download_failure(stderr: str) -> AppError:
    """Map yt-dlp download stderr to a typed AppError. Deterministic causes
    (unavailable sub-reasons, extraction errors) are checked before transient
    network patterns, so a deterministic failure is never retried (T040).
    Stderr is logged by the caller, never surfaced."""
    text = stderr.lower()
    for patterns, message in _UNAVAILABLE_SUB_REASONS:
        if any(pattern in text for pattern in patterns):
            return AppError(ErrorCode.VIDEO_UNAVAILABLE, message)
    for pattern in _NETWORK_PATTERNS:
        if pattern in text:
            return AppError(
                ErrorCode.NETWORK_ERROR,
                "A network error occurred while downloading from YouTube; please try again.",
            )
    return AppError(ErrorCode.EXTRACTION_FAILED, "Could not extract audio from this video.")


# yt-dlp --newline progress lines look like "[download]  12.6% of 246KiB ...".
_DOWNLOAD_PERCENT_RE = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")


def _parse_download_percent(line: str) -> float | None:
    match = _DOWNLOAD_PERCENT_RE.search(line)
    return float(match.group(1)) if match else None


def _pump_download_progress(stream, progress_callback) -> None:
    """Feed parsed percentages to the callback. Unparseable lines only
    degrade granularity — they NEVER fail the job and never spam logs."""
    degraded_logged = False
    try:
        for line in stream:
            percent = _parse_download_percent(line)
            if percent is None:
                continue
            if progress_callback is not None:
                try:
                    progress_callback(percent)
                except Exception:
                    if not degraded_logged:
                        logger.debug("Download progress callback failed; continuing without it")
                        degraded_logged = True
                    progress_callback = None
    except Exception:
        logger.debug("Download progress stream ended abnormally; granularity degraded")


def _drain(stream, sink: list[str]) -> None:
    try:
        sink.append(stream.read() or "")
    except Exception:
        sink.append("")


class _TransientDownloadFailure(Exception):
    """Internal marker: a download failure classified as transient network
    trouble (retryable, research.md R3). Carries the typed error to raise on
    exhaustion."""

    def __init__(self, error: AppError):
        self.error = error


# Retry policy (plan D3): exactly 2 retries, exponential backoff 1 s then 4 s,
# transient network failures only. The whole loop lives inside the download
# watchdog's budget.
_RETRY_BACKOFF_SECONDS = (1, 4)


def download_audio(
    video_id: str,
    work_dir: Path,
    timeout_seconds: int = 600,
    progress_callback=None,
    name_stem: str | None = None,
) -> Path:
    """Download bestaudio via yt-dlp with the FIXED argv template on the
    canonical URL only (plan D1). The template never contains --cookies,
    --cookies-from-browser, or any credential/DRM flag (FR-030). Returns the
    UUID-named source path inside WORK_DIR. Progress percentages parsed from
    `--newline --progress` stdout are fed to progress_callback (T033).
    Transient network failures are retried per plan D3; deterministic
    failures (unavailable, extraction) are not (T040). `name_stem` (the job
    id) names the artifact so the orphan sweep can recognise an in-flight
    download as owned before the store ever learns its path (T045)."""
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    attempt = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AppError(
                ErrorCode.TIMEOUT, "Downloading the audio took too long and was stopped."
            )
        try:
            return _run_download_attempt(
                video_id, work_dir, remaining, progress_callback, name_stem
            )
        except _TransientDownloadFailure as failure:
            if attempt >= len(_RETRY_BACKOFF_SECONDS):
                raise failure.error
            delay = _RETRY_BACKOFF_SECONDS[attempt]
            attempt += 1
            # Video ID only — never titles (log privacy).
            logger.info(
                "Transient download failure video_id=%s; retry %d/%d in %ss",
                video_id,
                attempt,
                len(_RETRY_BACKOFF_SECONDS),
                delay,
            )
            time.sleep(delay)


def _run_download_attempt(
    video_id: str,
    work_dir: Path,
    timeout_seconds: float,
    progress_callback,
    name_stem: str | None = None,
) -> Path:
    dest = work_dir / f"{name_stem or uuid.uuid4().hex}.source"
    argv = [
        "yt-dlp",
        "-f",
        "bestaudio",
        "--no-playlist",
        "--retries",
        "0",
        "--newline",
        "--progress",
        "-o",
        str(dest),
        canonical_url(video_id),
    ]
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        logger.error("yt-dlp binary not found while downloading video_id=%s", video_id)
        raise AppError(
            ErrorCode.EXTRACTION_FAILED, "Audio download tooling is unavailable on the server."
        )

    stderr_chunks: list[str] = []
    stdout_thread = threading.Thread(
        target=_pump_download_progress, args=(proc.stdout, progress_callback), daemon=True
    )
    stderr_thread = threading.Thread(
        target=_drain, args=(proc.stderr, stderr_chunks), daemon=True
    )
    stdout_thread.start()
    stderr_thread.start()

    try:
        exit_code = proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        _delete_partial_download(dest)
        logger.warning("Download timed out after %ss video_id=%s", timeout_seconds, video_id)
        raise AppError(ErrorCode.TIMEOUT, "Downloading the audio took too long and was stopped.")
    finally:
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)

    stderr = "".join(stderr_chunks)
    if exit_code != 0:
        logger.warning(
            "Download failed video_id=%s exit=%s stderr=%s",
            video_id,
            exit_code,
            stderr.strip(),
        )
        _delete_partial_download(dest)
        error = _classify_download_failure(stderr)
        # NETWORK_ERROR is exactly the transient class (connection reset,
        # timeouts, DNS) — the only class the retry loop re-attempts.
        if error.code is ErrorCode.NETWORK_ERROR:
            raise _TransientDownloadFailure(error)
        raise error

    if not dest.exists():
        logger.error("Download reported success but produced no file video_id=%s", video_id)
        raise AppError(ErrorCode.EXTRACTION_FAILED, "Could not extract audio from this video.")

    return dest


def _delete_partial_download(path: Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not delete partial download %s", path)
