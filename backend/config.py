"""Environment-driven configuration (data-model.md Config table).

Every limit is operator-overridable via env vars (FR-022). Derived defaults:
MAX_CONCURRENCY = max(2, cpu_count); QUEUE_LIMIT = 10 x concurrency.
"""

import os
import tempfile
from pathlib import Path

_TRUE_VALUES = {"1", "true", "yes", "on"}

# Published bitrate set (product contract, not env-tunable). JobCreateRequest
# keeps bitrate_kbps as a plain int; the route validates against this set so
# out-of-set values get the distinct INVALID_BITRATE code instead of a
# generic INVALID_INPUT.
ALLOWED_BITRATES: tuple[int, ...] = (96, 128, 192, 320)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


class Config:
    def __init__(self) -> None:
        raw_work_dir = os.environ.get("WORK_DIR")
        if raw_work_dir:
            self.work_dir = Path(raw_work_dir)
        else:
            self.work_dir = Path(tempfile.gettempdir()) / "yt-audio-extractor"
        self.work_dir.mkdir(parents=True, exist_ok=True)

        self.max_concurrency = _env_int("MAX_CONCURRENCY", max(2, os.cpu_count() or 1))
        self.queue_limit = _env_int("QUEUE_LIMIT", 10 * self.max_concurrency)
        self.per_origin_cap = _env_int("PER_ORIGIN_CAP", 3)
        self.disk_floor_bytes = _env_int("DISK_FLOOR_BYTES", 1 * 1024**3)
        self.ttl_seconds = _env_int("TTL_SECONDS", 900)
        self.sweep_interval_seconds = _env_int("SWEEP_INTERVAL_SECONDS", 60)
        self.download_timeout_seconds = _env_int("DOWNLOAD_TIMEOUT_SECONDS", 600)
        self.transcode_timeout_seconds = _env_int("TRANSCODE_TIMEOUT_SECONDS", 300)
        self.max_duration_seconds = _env_int("MAX_DURATION_SECONDS", 3600)
        self.probe_timeout_seconds = _env_int("PROBE_TIMEOUT_SECONDS", 15)
        self.trusted_proxy = _env_bool("TRUSTED_PROXY")
        self.real_stack = _env_bool("REAL_STACK")
        self.allowed_bitrates = ALLOWED_BITRATES
