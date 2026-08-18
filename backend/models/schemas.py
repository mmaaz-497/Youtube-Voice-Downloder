"""Pydantic v2 contract models — field shapes exactly per contracts/openapi.yaml."""

from typing import Literal

from pydantic import BaseModel, Field

from backend.models.errors import ErrorCode


class InfoResponse(BaseModel):
    video_id: str = Field(min_length=11, max_length=11)
    title: str
    channel: str
    duration_seconds: int = Field(ge=0)
    thumbnail_url: str
    available: bool


class JobCreateRequest(BaseModel):
    # Kept as a plain int (not Literal): the route validates against
    # config.allowed_bitrates and raises the distinct INVALID_BITRATE code;
    # a Literal would collapse out-of-set values into generic INVALID_INPUT.
    url: str
    bitrate_kbps: int = 192


class JobAcceptedResponse(BaseModel):
    job_id: str
    status: Literal["queued"]
    queue_position: int = Field(ge=1)


class JobErrorDetail(BaseModel):
    code: ErrorCode
    message: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: Literal["queued", "running", "completed", "failed"]
    queue_position: int | None = Field(default=None, ge=1)
    phase: Literal["downloading", "converting"] | None = None
    progress: int | None = Field(default=None, ge=0, le=100)
    error: JobErrorDetail | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    running: int
    queued: int
    capacity: int
    queue_limit: int
    free_disk_bytes: int
    ytdlp_available: bool
    # Always emitted (null when the version is unknown), so it is required
    # rather than defaulted — a default would tell the schema the key can be
    # absent, which the endpoint never does.
    ytdlp_version: str | None
    ffmpeg_available: bool
    uptime_seconds: float
    # Empty iff status == "ok"; otherwise names every failing condition.
    degraded_reasons: list[str]


class ErrorEnvelope(BaseModel):
    error: JobErrorDetail
