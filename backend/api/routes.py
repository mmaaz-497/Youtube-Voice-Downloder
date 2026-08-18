"""Thin API handlers: validate → delegate to services → typed schema.

Boundary functions are always called through the module object
(youtube.probe_video_info) so the test harness's function-level mocks take
effect. Any AppError raised here or below flows through the global handler
untouched (Principle II).
"""

import logging
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from backend.models.errors import AppError, ErrorCode
from backend.models.schemas import (
    ErrorEnvelope,
    HealthResponse,
    InfoResponse,
    JobAcceptedResponse,
    JobCreateRequest,
    JobStatusResponse,
)
from backend.services import health, youtube
from backend.services.runner import run_job

logger = logging.getLogger("yt-audio-extractor")

router = APIRouter(prefix="/api")


def error_responses(*status_codes: int) -> dict:
    """Declare the typed-envelope responses each endpoint can return, so the
    app-generated OpenAPI matches the published contract exactly (T049)."""
    return {
        code: {"model": ErrorEnvelope, "description": "Typed error envelope"}
        for code in status_codes
    }


def _enforce_duration(duration_seconds: int, max_duration_seconds: int) -> None:
    if duration_seconds > max_duration_seconds:
        raise AppError(
            ErrorCode.DURATION_EXCEEDED,
            f"Video is {duration_seconds} seconds long; the maximum supported "
            f"duration is {max_duration_seconds} seconds.",
        )


@router.get(
    "/info",
    response_model=InfoResponse,
    responses=error_responses(400, 404, 500, 504),
)
def get_info(request: Request, url: str) -> InfoResponse:
    """Preview metadata: validate URL (pure) → probe → duration cap."""
    config = request.app.state.config
    video_id = youtube.validate_and_extract_video_id(url)
    info = youtube.probe_video_info(video_id, timeout_seconds=config.probe_timeout_seconds)
    _enforce_duration(info.duration_seconds, config.max_duration_seconds)
    return InfoResponse(
        video_id=info.video_id,
        title=info.title,
        channel=info.channel,
        duration_seconds=info.duration_seconds,
        thumbnail_url=info.thumbnail_url,
        available=info.available,
    )


@router.post(
    "/jobs",
    response_model=JobAcceptedResponse,
    status_code=202,
    responses=error_responses(400, 404, 429, 500, 503, 504),
)
def create_job(request: Request, body: JobCreateRequest) -> JobAcceptedResponse:
    """Validate → probe (duration re-check) → admission → enqueue → 202."""
    config = request.app.state.config
    if body.bitrate_kbps not in config.allowed_bitrates:
        published = ", ".join(str(kbps) for kbps in config.allowed_bitrates)
        raise AppError(
            ErrorCode.INVALID_BITRATE,
            f"Unsupported bitrate {body.bitrate_kbps} kbps; choose one of: {published}.",
        )
    video_id = youtube.validate_and_extract_video_id(body.url)
    info = youtube.probe_video_info(video_id, timeout_seconds=config.probe_timeout_seconds)
    _enforce_duration(info.duration_seconds, config.max_duration_seconds)

    origin_hash = request.app.state.resolve_origin(request)
    store = request.app.state.store
    job, queue_position = store.admit(
        origin_hash=origin_hash,
        video_id=video_id,
        title=info.title,
        bitrate_kbps=body.bitrate_kbps,
        duration_seconds=info.duration_seconds,
    )
    request.app.state.pool.submit(run_job, store, config, job.job_id)
    return JobAcceptedResponse(
        job_id=job.job_id, status="queued", queue_position=queue_position
    )


@router.get(
    "/jobs/{job_id}",
    response_model=JobStatusResponse,
    response_model_exclude_none=True,
    responses=error_responses(404, 500),
)
def get_job_status(request: Request, job_id: str) -> JobStatusResponse:
    return JobStatusResponse(**request.app.state.store.snapshot(job_id))


@router.get(
    "/jobs/{job_id}/file",
    responses={
        200: {
            "description": "MP3 attachment; delivery purges the job and its files.",
            "content": {"audio/mpeg": {"schema": {"type": "string", "format": "binary"}}},
        },
        **error_responses(404, 409, 500),
    },
)
def download_file(request: Request, job_id: str) -> FileResponse:
    """Streaming-safe delivery purge: the store removes the job under its lock
    (concurrent requests immediately see JOB_NOT_FOUND) but the file is only
    deleted AFTER the response finishes streaming, via a background task —
    unlinking first would break the stream mid-delivery. The orphan sweep is
    the safety net if the background deletion ever fails."""
    store = request.app.state.store
    job = store.take_delivered(job_id)
    output_path = Path(job.output_path)
    filename = youtube.sanitize_filename(job.title, job.video_id) + ".mp3"
    # The sanitized filename is ASCII/quote-safe by construction, so the plain
    # form is set explicitly (Starlette would otherwise percent-encode spaces
    # into the filename* form).
    return FileResponse(
        output_path,
        media_type="audio/mpeg",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        background=BackgroundTask(_delete_after_stream, store, output_path),
    )


@router.get("/health", response_model=HealthResponse)
def get_health(request: Request) -> HealthResponse:
    """Operator signal. Queue depths, disk, and uptime are read fresh; the
    external-tool probes come from a short-lived cache so a monitoring poll
    cannot spawn a subprocess storm (T048)."""
    state = request.app.state
    config = state.config
    store = state.store

    running, queued = store.counts()
    free_disk_bytes = store.free_disk_bytes()
    tools = state.tool_probe.snapshot()
    status, degraded_reasons = health.assess(
        tools, free_disk_bytes, config.disk_floor_bytes
    )

    return HealthResponse(
        status=status,
        running=running,
        queued=queued,
        capacity=config.max_concurrency,
        queue_limit=config.queue_limit,
        free_disk_bytes=free_disk_bytes,
        ytdlp_available=tools.ytdlp_available,
        ytdlp_version=tools.ytdlp_version,
        ffmpeg_available=tools.ffmpeg_available,
        uptime_seconds=max(0.0, time.monotonic() - state.started_at),
        degraded_reasons=degraded_reasons,
    )


def _delete_after_stream(store, path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Post-delivery deletion failed for %s (orphan sweep will reclaim)", path)
    finally:
        # Release the reservation either way: if the unlink failed, the file
        # becomes an orphan the sweep is allowed to reclaim.
        store.release_delivered(path)
