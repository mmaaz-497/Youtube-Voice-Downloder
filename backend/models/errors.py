"""Typed error spine: closed ErrorCode enum, AppError, one envelope shape.

One HTTP status per code (spec Error Taxonomy). Codes whose primary surface
is a failed job carry the status used if they ever surface over HTTP
(LOW_DISK 503, NETWORK_ERROR 504, others 500). INTERNAL logs the stack
server-side and returns a generic message (Principle II).
"""

import logging
from enum import Enum

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger("yt-audio-extractor")


class ErrorCode(str, Enum):
    INVALID_INPUT = "INVALID_INPUT"
    INVALID_URL = "INVALID_URL"
    INVALID_BITRATE = "INVALID_BITRATE"
    DURATION_EXCEEDED = "DURATION_EXCEEDED"
    LIVE_STREAM = "LIVE_STREAM"
    VIDEO_UNAVAILABLE = "VIDEO_UNAVAILABLE"
    CLIENT_LIMIT = "CLIENT_LIMIT"
    AT_CAPACITY = "AT_CAPACITY"
    LOW_DISK = "LOW_DISK"
    NETWORK_ERROR = "NETWORK_ERROR"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    FFMPEG_MISSING = "FFMPEG_MISSING"
    TRANSCODE_FAILED = "TRANSCODE_FAILED"
    TIMEOUT = "TIMEOUT"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    NOT_READY = "NOT_READY"
    INTERNAL = "INTERNAL"


CODE_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.INVALID_INPUT: 400,
    ErrorCode.INVALID_URL: 400,
    ErrorCode.INVALID_BITRATE: 400,
    ErrorCode.DURATION_EXCEEDED: 400,
    ErrorCode.LIVE_STREAM: 400,
    ErrorCode.VIDEO_UNAVAILABLE: 404,
    ErrorCode.CLIENT_LIMIT: 429,
    ErrorCode.AT_CAPACITY: 503,
    ErrorCode.LOW_DISK: 503,
    ErrorCode.NETWORK_ERROR: 504,
    ErrorCode.EXTRACTION_FAILED: 500,
    ErrorCode.FFMPEG_MISSING: 500,
    ErrorCode.TRANSCODE_FAILED: 500,
    ErrorCode.TIMEOUT: 500,
    ErrorCode.JOB_NOT_FOUND: 404,
    ErrorCode.NOT_READY: 409,
    ErrorCode.INTERNAL: 500,
}


# Canonical human-readable message per code — every entry pairwise distinct
# (asserted end-to-end in the taxonomy test). Call sites may still pass a
# more specific message (e.g. VIDEO_UNAVAILABLE sub-reasons).
DEFAULT_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.INVALID_INPUT: "Malformed request body or parameters.",
    ErrorCode.INVALID_URL: "Not a supported YouTube URL.",
    ErrorCode.INVALID_BITRATE: "Unsupported bitrate; choose one of: 96, 128, 192, 320.",
    ErrorCode.DURATION_EXCEEDED: "The video exceeds the maximum supported duration.",
    ErrorCode.LIVE_STREAM: (
        "Live streams and premieres are not supported; try again after the stream has ended."
    ),
    ErrorCode.VIDEO_UNAVAILABLE: "This video is unavailable.",
    ErrorCode.CLIENT_LIMIT: (
        "You already have the maximum number of active extractions; wait for one to finish."
    ),
    ErrorCode.AT_CAPACITY: "The server queue is full; try again in a moment.",
    ErrorCode.LOW_DISK: "Not enough free disk space on the server; try again later.",
    ErrorCode.NETWORK_ERROR: (
        "A network error occurred while contacting YouTube; please try again."
    ),
    ErrorCode.EXTRACTION_FAILED: "Could not extract audio from this video.",
    ErrorCode.FFMPEG_MISSING: (
        "MP3 encoding is unavailable on the server (FFmpeg with libmp3lame is required)."
    ),
    ErrorCode.TRANSCODE_FAILED: "Could not convert the audio to MP3.",
    ErrorCode.TIMEOUT: "The operation took too long and was stopped.",
    ErrorCode.JOB_NOT_FOUND: "No such job; it may have been delivered or expired.",
    ErrorCode.NOT_READY: "The file is not ready yet; poll the job status until it completes.",
    ErrorCode.INTERNAL: "An unexpected internal error occurred.",
}


class AppError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str | None = None,
        http_status: int | None = None,
    ):
        resolved = message if message is not None else DEFAULT_MESSAGES[code]
        super().__init__(resolved)
        self.code = code
        self.message = resolved
        self.http_status = http_status if http_status is not None else CODE_HTTP_STATUS[code]


def envelope(code: ErrorCode, message: str) -> dict:
    return {"error": {"code": code.value, "message": message}}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=envelope(exc.code, exc.message))

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=envelope(
                ErrorCode.INVALID_INPUT, DEFAULT_MESSAGES[ErrorCode.INVALID_INPUT]
            ),
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content=envelope(ErrorCode.INTERNAL, DEFAULT_MESSAGES[ErrorCode.INTERNAL]),
        )
