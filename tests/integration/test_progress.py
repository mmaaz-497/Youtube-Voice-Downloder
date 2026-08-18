"""T031 — US3 live progress: queue position visible while queued and
decreasing as earlier jobs finish; phase label present iff running; download
progress flows through the callback into the mapped 0-80 band; completed
implies progress 100 with no phase/queue_position.
"""

import itertools
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from tests.conftest import wait_until

VID = "dQw4w9WgXcQ"
WATCH_URL = f"https://www.youtube.com/watch?v={VID}"


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


def get_status(client, job_id):
    resp = client.get(f"/api/jobs/{job_id}")
    assert resp.status_code == 200
    return resp.json()


def wait_terminal(client, job_id, timeout=5.0):
    assert wait_until(
        lambda: get_status(client, job_id)["status"] in ("completed", "failed"), timeout
    )
    return get_status(client, job_id)


def test_queue_positions_phases_and_progress(make_client, media_boundary):
    media_boundary.youtube.probe_video_info.return_value = make_info()

    gates = [
        {"started": threading.Event(), "release": threading.Event()} for _ in range(3)
    ]
    counter = itertools.count()

    def parked_download(video_id, work_dir, **kwargs):
        gate = gates[next(counter)]
        callback = kwargs.get("progress_callback")
        if callback is not None:
            callback(50.0)  # halfway through the download phase
        gate["started"].set()
        gate["release"].wait(timeout=10)
        return MagicMock()

    media_boundary.youtube.download_audio.side_effect = parked_download
    client = make_client(MAX_CONCURRENCY=1)

    try:
        ids = []
        for _ in range(3):
            resp = client.post("/api/jobs", json={"url": WATCH_URL})
            assert resp.status_code == 202
            ids.append(resp.json()["job_id"])
        assert gates[0]["started"].wait(5), "worker never started job 1"

        # Running job: phase label present, no queue position, download 50%
        # mapped into the 0-80 band (50 * 0.8 = 40).
        s1 = get_status(client, ids[0])
        assert s1["status"] == "running"
        assert s1["phase"] == "downloading"
        assert "queue_position" not in s1
        assert s1["progress"] == 40

        # Queued jobs: position visible, NO phase label.
        s2 = get_status(client, ids[1])
        assert s2["status"] == "queued"
        assert s2["queue_position"] == 1
        assert "phase" not in s2
        s3 = get_status(client, ids[2])
        assert s3["queue_position"] == 2

        # Earlier job finishes -> positions decrease.
        gates[0]["release"].set()
        assert gates[1]["started"].wait(5), "worker never started job 2"
        s3 = get_status(client, ids[2])
        assert s3["status"] == "queued"
        assert s3["queue_position"] == 1

        gates[1]["release"].set()
        gates[2]["release"].set()

        for job_id in ids:
            final = wait_terminal(client, job_id)
            assert final["status"] == "completed", final
            assert final["progress"] == 100
            assert "phase" not in final
            assert "queue_position" not in final

        # Converting-phase wiring: the runner hands transcode a progress
        # callback and the probed duration (for out_time_us mapping).
        call = media_boundary.audio.transcode.call_args
        assert call.kwargs.get("progress_callback") is not None
        assert call.kwargs.get("duration_seconds") == 213
    finally:
        for gate in gates:
            gate["release"].set()
