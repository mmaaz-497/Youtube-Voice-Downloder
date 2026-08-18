"""T043 — US5 purge semantics: delivery purges the job and its file; TTL
expiry purges unretrieved results; and the delivery-vs-TTL race is asserted
BOTH ways — whoever loses observes JOB_NOT_FOUND and a partial file is never
streamed. Expiry is driven by an injected clock, so no test sleeps.
"""

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import FakeClock, wait_until

VID = "dQw4w9WgXcQ"
WATCH_URL = f"https://www.youtube.com/watch?v={VID}"
MP3_BYTES = b"ID3" + b"audio-payload" * 20_000  # big enough to stream in chunks


def make_info(**overrides):
    base = {
        "video_id": VID,
        "title": "Never Gonna Give You Up",
        "channel": "Rick Astley",
        "duration_seconds": 213,
        "thumbnail_url": f"https://i.ytimg.com/vi/{VID}/hqdefault.jpg",
        "available": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def store_clock(client):
    """Inject the clock BEFORE any job runs, so a job's finished_at and the
    expiry decision that reads it always come from the same clock. Anchored
    to wall time so nothing recorded elsewhere looks like the distant past."""
    clock = FakeClock(start=time.time())
    client.app.state.store.clock = clock.time
    return clock


@pytest.fixture
def completed_job(client, media_boundary):
    """Factory running a job to completion against the mocked boundary and
    returning its job_id. The fake download names its artifact from the
    runner-supplied stem, exactly as production does."""
    media_boundary.youtube.probe_video_info.return_value = make_info()
    media_boundary.audio.check_libmp3lame.return_value = True

    def fake_download(video_id, work_dir, **kwargs):
        stem = kwargs.get("name_stem") or "source"
        source = Path(work_dir) / f"{stem}.source"
        source.write_bytes(b"raw-bestaudio")
        return source

    def fake_transcode(src, dst, bitrate_kbps, title, **kwargs):
        Path(dst).write_bytes(MP3_BYTES)
        return Path(dst)

    media_boundary.youtube.download_audio.side_effect = fake_download
    media_boundary.audio.transcode.side_effect = fake_transcode

    def _create() -> str:
        resp = client.post("/api/jobs", json={"url": WATCH_URL})
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]
        assert wait_until(
            lambda: client.get(f"/api/jobs/{job_id}").json().get("status") == "completed"
        ), "job never completed"
        return job_id

    return _create


def assert_gone(client, job_id):
    """Both surfaces report the closed enum's JOB_NOT_FOUND — no new code."""
    status = client.get(f"/api/jobs/{job_id}")
    assert status.status_code == 404
    assert status.json()["error"]["code"] == "JOB_NOT_FOUND"
    file_resp = client.get(f"/api/jobs/{job_id}/file")
    assert file_resp.status_code == 404
    assert file_resp.json()["error"]["code"] == "JOB_NOT_FOUND"


def test_delivery_purges_job_and_file(client, completed_job, work_dir):
    job_id = completed_job()

    resp = client.get(f"/api/jobs/{job_id}/file")
    assert resp.status_code == 200
    assert resp.content == MP3_BYTES

    assert_gone(client, job_id)
    assert list(work_dir.iterdir()) == []


def test_ttl_expiry_purges_unretrieved_result(client, store_clock, completed_job, work_dir):
    job_id = completed_job()
    assert list(work_dir.glob("*.mp3")) != []

    store = client.app.state.store
    # Nothing expires before the TTL elapses...
    store_clock.advance(client.app.state.config.ttl_seconds - 1)
    store.run_sweep()
    assert client.get(f"/api/jobs/{job_id}").status_code == 200

    # ...and everything does once it has.
    store_clock.advance(2)
    store.run_sweep()
    assert_gone(client, job_id)
    assert list(work_dir.iterdir()) == []


def test_ttl_fires_mid_delivery_stream_stays_intact(
    client, store_clock, completed_job, work_dir
):
    """Race direction A: the sweep ticks while bytes are in flight. Delivery
    already removed the job under the lock, so the sweep must not yank the
    file out from under the open stream."""
    job_id = completed_job()
    store = client.app.state.store

    with client.stream("GET", f"/api/jobs/{job_id}/file") as response:
        assert response.status_code == 200
        # TTL tick lands mid-delivery, well past expiry.
        store_clock.advance(client.app.state.config.ttl_seconds * 10)
        store.run_sweep()
        body = response.read()

    assert body == MP3_BYTES, "delivery served a truncated stream"
    assert_gone(client, job_id)
    # The background task still reclaims the file after the response finishes.
    assert wait_until(lambda: list(work_dir.iterdir()) == []), list(work_dir.iterdir())


def test_expiry_wins_race_loser_gets_404_never_a_partial_file(
    client, store_clock, completed_job, work_dir
):
    """Race direction B: the sweep ticks just before the download request
    arrives. The client must get a typed 404 envelope — never audio bytes."""
    job_id = completed_job()
    store = client.app.state.store

    store_clock.advance(client.app.state.config.ttl_seconds + 1)
    store.run_sweep()

    resp = client.get(f"/api/jobs/{job_id}/file")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "JOB_NOT_FOUND"
    assert not resp.content.startswith(b"ID3")
    assert list(work_dir.iterdir()) == []


def test_second_delivery_loses_to_the_first(client, completed_job):
    """Two deliveries of the same job: the first wins, the second sees the
    job already gone (the store removes it under the lock)."""
    job_id = completed_job()

    first = client.get(f"/api/jobs/{job_id}/file")
    assert first.status_code == 200
    assert first.content == MP3_BYTES

    second = client.get(f"/api/jobs/{job_id}/file")
    assert second.status_code == 404
    assert second.json()["error"]["code"] == "JOB_NOT_FOUND"


def test_failed_job_also_expires_by_ttl(client, store_clock, media_boundary, work_dir):
    from backend.models.errors import AppError, ErrorCode

    media_boundary.youtube.probe_video_info.return_value = make_info()
    media_boundary.audio.check_libmp3lame.return_value = True
    media_boundary.youtube.download_audio.side_effect = AppError(
        ErrorCode.EXTRACTION_FAILED
    )

    job_id = client.post("/api/jobs", json={"url": WATCH_URL}).json()["job_id"]
    assert wait_until(
        lambda: client.get(f"/api/jobs/{job_id}").json().get("status") == "failed"
    )

    store = client.app.state.store
    store_clock.advance(client.app.state.config.ttl_seconds + 1)
    store.run_sweep()

    assert_gone(client, job_id)
    assert list(work_dir.iterdir()) == []
