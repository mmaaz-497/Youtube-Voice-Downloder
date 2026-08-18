"""Worker body v1 (T028): disk-floor re-check → download → transcode →
delete source immediately → completed. ANY exception marks the job failed
with a typed code, and the store deletes all artifacts inside the same
critical section that marks it terminal (plan D7 invariants). Watchdog and
retry hardening land in T040/T041; progress mapping in T035.

Boundary calls go through the module objects (youtube.download_audio,
audio.transcode) so the test harness's function-level mocks take effect.
"""

import logging
from pathlib import Path

from backend.config import Config
from backend.models.errors import AppError, ErrorCode
from backend.services import audio, youtube
from backend.services.jobs import JobStore

logger = logging.getLogger("yt-audio-extractor")


def map_download_progress(percent: float) -> int:
    """Downloading phase owns 0-80 of the single monotone bar (plan D5)."""
    return max(0, min(80, int(percent * 0.8)))


def map_convert_progress(percent: float) -> int:
    """Converting phase owns 80-100; convert(0) == download(100) == 80, so
    the phase boundary can never step backwards."""
    return 80 + max(0, min(20, int(percent * 0.2)))


def run_job(store: JobStore, config: Config, job_id: str) -> None:
    try:
        job = store.mark_running(job_id, phase="downloading")
    except KeyError:
        logger.warning("Job %s vanished before it could start; nothing to run", job_id)
        return

    try:
        store.check_disk_floor()
        # Pre-check the encoder before spending a download on a box that
        # cannot produce MP3 at all (Principle IV: never substitute).
        if not audio.check_libmp3lame():
            raise AppError(ErrorCode.FFMPEG_MISSING)

        source = youtube.download_audio(
            job.video_id,
            config.work_dir,
            timeout_seconds=config.download_timeout_seconds,
            progress_callback=lambda percent: store.update_progress(
                job_id, map_download_progress(percent)
            ),
            name_stem=job_id,
        )
        store.set_source_path(job_id, source)
        # Mid-job re-check: the download just consumed space, and other jobs
        # may have too (plan D7).
        store.check_disk_floor()

        store.set_phase(job_id, "converting")
        store.update_progress(job_id, 80)  # download phase complete
        output = Path(config.work_dir) / f"{job_id}.mp3"
        # Registered BEFORE the encoder runs so a killed/failed FFmpeg's
        # partial file is still owned by the job and reclaimed by mark_failed.
        store.set_output_path(job_id, output)
        audio.transcode(
            source,
            output,
            job.bitrate_kbps,
            job.title,
            timeout_seconds=config.transcode_timeout_seconds,
            duration_seconds=job.duration_seconds,
            progress_callback=lambda percent: store.update_progress(
                job_id, map_convert_progress(percent)
            ),
        )

        # The source is deleted the moment the MP3 exists (Principle V).
        _delete_quietly(source)
        store.set_source_path(job_id, None)

        store.mark_completed(job_id)
        logger.info("Job %s completed video_id=%s", job_id, job.video_id)
    except AppError as err:
        logger.warning(
            "Job %s failed video_id=%s code=%s", job_id, job.video_id, err.code.value
        )
        store.mark_failed(job_id, err.code, err.message)
    except Exception:
        logger.exception("Job %s crashed video_id=%s", job_id, job.video_id)
        store.mark_failed(
            job_id, ErrorCode.INTERNAL, "An unexpected internal error occurred."
        )


def _delete_quietly(path) -> None:
    if path is None:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except (OSError, TypeError):
        logger.warning("Could not delete source artifact %s", path)
