"""Health signal assembly (T048).

The external-tool probes each cost a subprocess, so they are cached for a
short window: /api/health is an operator polling endpoint and must never turn
a monitoring loop into a subprocess storm. Everything cheap (queue depths,
disk, uptime) is read fresh on every call.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from backend.services import audio, youtube

logger = logging.getLogger("yt-audio-extractor")

# Long enough that a 1 s monitoring poll costs at most one probe per window,
# short enough that installing/removing a tool shows up promptly.
TOOL_PROBE_TTL_SECONDS = 10.0


@dataclass
class ToolStatus:
    ytdlp_available: bool
    ytdlp_version: str | None
    ffmpeg_available: bool


@dataclass
class ToolProbe:
    """Cached view of external-tool availability."""

    ttl_seconds: float = TOOL_PROBE_TTL_SECONDS
    clock: Callable[[], float] | None = None
    _cached: ToolStatus | None = field(default=None, repr=False)
    _probed_at: float = field(default=0.0, repr=False)

    def _now(self) -> float:
        return self.clock() if self.clock is not None else time.monotonic()

    def snapshot(self) -> ToolStatus:
        now = self._now()
        if self._cached is not None and now - self._probed_at < self.ttl_seconds:
            return self._cached

        # Called through the module objects so the test harness's
        # function-level mocks apply (never a real subprocess in CI).
        version = youtube.ytdlp_version()
        # Defensive: anything that is not a plain string is reported as "no
        # version" rather than being allowed to fail response validation.
        version = version if isinstance(version, str) else None
        ffmpeg_available = bool(audio.check_libmp3lame())

        self._cached = ToolStatus(
            ytdlp_available=version is not None,
            ytdlp_version=version,
            ffmpeg_available=ffmpeg_available,
        )
        self._probed_at = now
        return self._cached

    def invalidate(self) -> None:
        self._cached = None


def assess(tools: ToolStatus, free_disk_bytes: int, disk_floor_bytes: int) -> tuple[str, list[str]]:
    """Degraded trigger (resolves the deferred U2 finding): the service is
    "ok" only when BOTH tools are available AND free disk is at or above the
    floor. Otherwise "degraded", naming every failing condition — an operator
    should never have to guess which one fired."""
    reasons: list[str] = []
    if not tools.ffmpeg_available:
        reasons.append("ffmpeg with libmp3lame is not available")
    if not tools.ytdlp_available:
        reasons.append("yt-dlp is not available")
    if free_disk_bytes < disk_floor_bytes:
        reasons.append(
            f"free disk {free_disk_bytes} B is below the floor of {disk_floor_bytes} B"
        )
    return ("ok" if not reasons else "degraded"), reasons
