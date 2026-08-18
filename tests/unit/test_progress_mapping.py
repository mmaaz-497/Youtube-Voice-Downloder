"""T032 — US3 progress-mapping properties: downloading percent maps onto
0-80 and converting onto 80-100 with no decrease anywhere (including the
phase boundary); the store applies max(old, new) so decreases are rejected;
completed implies exactly 100.
"""

import backend.services.runner as runner_module
from backend.config import Config
from backend.services.jobs import JobStore

VID = "dQw4w9WgXcQ"


def test_download_percent_maps_onto_0_80():
    assert runner_module.map_download_progress(0) == 0
    assert runner_module.map_download_progress(100) == 80
    for percent in range(101):
        assert 0 <= runner_module.map_download_progress(percent) <= 80


def test_convert_percent_maps_onto_80_100():
    assert runner_module.map_convert_progress(0) == 80
    assert runner_module.map_convert_progress(100) == 100
    for percent in range(101):
        assert 80 <= runner_module.map_convert_progress(percent) <= 100


def test_full_pipeline_mapping_is_monotone_across_the_phase_boundary():
    sequence = [runner_module.map_download_progress(p) for p in range(101)]
    sequence += [runner_module.map_convert_progress(p) for p in range(101)]
    assert sequence == sorted(sequence)
    assert sequence[0] == 0
    assert sequence[-1] == 100


def test_out_of_range_inputs_are_clamped():
    assert runner_module.map_download_progress(-5) == 0
    assert runner_module.map_download_progress(250) == 80
    assert runner_module.map_convert_progress(-5) == 80
    assert runner_module.map_convert_progress(250) == 100


def _admitted_running_job(store):
    job, _ = store.admit(
        origin_hash="origin",
        video_id=VID,
        title="title",
        bitrate_kbps=192,
        duration_seconds=100,
    )
    store.mark_running(job.job_id)
    return job


def test_store_rejects_progress_decreases(work_dir):
    store = JobStore(Config())
    job = _admitted_running_job(store)

    store.update_progress(job.job_id, 50)
    assert store.snapshot(job.job_id)["progress"] == 50
    store.update_progress(job.job_id, 30)  # decrease rejected (max semantics)
    assert store.snapshot(job.job_id)["progress"] == 50
    store.update_progress(job.job_id, 51)
    assert store.snapshot(job.job_id)["progress"] == 51
    store.update_progress(job.job_id, 500)  # clamped, never above 100
    assert store.snapshot(job.job_id)["progress"] == 100


def test_completed_implies_exactly_100(work_dir):
    store = JobStore(Config())
    job = _admitted_running_job(store)

    store.update_progress(job.job_id, 42)
    store.mark_completed(job.job_id)
    snapshot = store.snapshot(job.job_id)
    assert snapshot["status"] == "completed"
    assert snapshot["progress"] == 100
