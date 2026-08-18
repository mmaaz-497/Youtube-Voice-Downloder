"""FFmpeg boundary (T027): libmp3lame presence check and CBR MP3 transcode.

The transcode argv is a fixed vector — `-vn -codec:a libmp3lame -b:a <N>k
-metadata title=<sanitized title> -id3v2_version 3` — with the title fused
into a single argv element, so no shell quoting or injection is possible
(plan D2). FFmpeg stderr is captured to logs only, never client-visible.
"""

import logging
import re
import subprocess
import threading
from pathlib import Path

from backend.models.errors import AppError, ErrorCode

logger = logging.getLogger("yt-audio-extractor")

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

# FFmpeg -progress pipe:1 emits key=value lines; out_time_us tracks the
# transcoded position in microseconds.
_OUT_TIME_US_RE = re.compile(r"^out_time_us=(\d+)")


def _sanitize_title(title: str) -> str:
    return _CONTROL_CHARS.sub("", title or "").strip()[:200]


def _pump_transcode_progress(stream, duration_seconds, progress_callback) -> None:
    """Map out_time_us / duration into 0-100 for the callback. Unparseable
    lines or a missing duration only degrade granularity — they NEVER fail
    the job and never spam logs."""
    degraded_logged = False
    try:
        for line in stream:
            match = _OUT_TIME_US_RE.match(line.strip())
            if match is None:
                continue
            if not duration_seconds or progress_callback is None:
                continue
            percent = min(100.0, int(match.group(1)) / (duration_seconds * 1_000_000) * 100)
            try:
                progress_callback(percent)
            except Exception:
                if not degraded_logged:
                    logger.debug("Transcode progress callback failed; continuing without it")
                    degraded_logged = True
                progress_callback = None
    except Exception:
        logger.debug("Transcode progress stream ended abnormally; granularity degraded")


def _drain(stream, sink: list[str]) -> None:
    try:
        sink.append(stream.read() or "")
    except Exception:
        sink.append("")


def check_libmp3lame() -> bool:
    """True iff an FFmpeg with the libmp3lame encoder is on PATH."""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0 and "libmp3lame" in proc.stdout


def transcode(
    src: Path,
    dst: Path,
    bitrate_kbps: int,
    title: str,
    timeout_seconds: int = 300,
    progress_callback=None,
    duration_seconds: int = 0,
) -> Path:
    """CBR transcode to exactly `bitrate_kbps` (Principle IV). Raises typed
    AppErrors: FFMPEG_MISSING, TIMEOUT, TRANSCODE_FAILED. Progress parsed
    from `-progress pipe:1` stdout (out_time_us / duration, T034)."""
    argv = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-i",
        str(src),
        "-vn",
        "-codec:a",
        "libmp3lame",
        "-b:a",
        f"{bitrate_kbps}k",
        "-metadata",
        f"title={_sanitize_title(title)}",
        "-id3v2_version",
        "3",
        "-progress",
        "pipe:1",
        str(dst),
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
        raise AppError(ErrorCode.FFMPEG_MISSING, "FFmpeg is not installed on the server.")

    stderr_chunks: list[str] = []
    stdout_thread = threading.Thread(
        target=_pump_transcode_progress,
        args=(proc.stdout, duration_seconds, progress_callback),
        daemon=True,
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
        _delete_partial(dst)
        raise AppError(
            ErrorCode.TIMEOUT, "Converting the audio took too long and was stopped."
        )
    finally:
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)

    if exit_code != 0:
        logger.error(
            "Transcode failed exit=%s bitrate=%sk stderr=%s",
            exit_code,
            bitrate_kbps,
            "".join(stderr_chunks).strip(),
        )
        _delete_partial(dst)
        raise AppError(ErrorCode.TRANSCODE_FAILED, "Could not convert the audio to MP3.")

    dst = Path(dst)
    if not dst.exists():
        raise AppError(ErrorCode.TRANSCODE_FAILED, "Could not convert the audio to MP3.")
    return dst


def _delete_partial(path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except (OSError, TypeError):
        logger.warning("Could not delete partial transcode output %s", path)
