"""T011 — config defaults, derived values, env overrides, TRUSTED_PROXY parsing.

Covers the Config table in specs/002-extract-audio/data-model.md.
"""

import os

import pytest

from backend.config import Config

CONFIG_ENV_VARS = [
    "WORK_DIR",
    "MAX_CONCURRENCY",
    "QUEUE_LIMIT",
    "PER_ORIGIN_CAP",
    "DISK_FLOOR_BYTES",
    "TTL_SECONDS",
    "SWEEP_INTERVAL_SECONDS",
    "DOWNLOAD_TIMEOUT_SECONDS",
    "TRANSCODE_TIMEOUT_SECONDS",
    "MAX_DURATION_SECONDS",
    "PROBE_TIMEOUT_SECONDS",
    "TRUSTED_PROXY",
    "REAL_STACK",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for var in CONFIG_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    # Keep default WORK_DIR creation out of the real OS temp dir during tests
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "work"))


def test_defaults(monkeypatch, tmp_path):
    cfg = Config()
    assert cfg.per_origin_cap == 3
    assert cfg.disk_floor_bytes == 1 * 1024**3
    assert cfg.ttl_seconds == 900
    assert cfg.sweep_interval_seconds == 60
    assert cfg.download_timeout_seconds == 600
    assert cfg.transcode_timeout_seconds == 300
    assert cfg.max_duration_seconds == 3600
    assert cfg.probe_timeout_seconds == 15
    assert cfg.trusted_proxy is False
    assert cfg.real_stack is False


def test_derived_max_concurrency_is_at_least_two():
    cfg = Config()
    assert cfg.max_concurrency == max(2, os.cpu_count() or 1)
    assert cfg.max_concurrency >= 2


def test_derived_queue_limit_is_ten_times_concurrency():
    cfg = Config()
    assert cfg.queue_limit == 10 * cfg.max_concurrency


def test_queue_limit_follows_overridden_concurrency(monkeypatch):
    monkeypatch.setenv("MAX_CONCURRENCY", "5")
    cfg = Config()
    assert cfg.max_concurrency == 5
    assert cfg.queue_limit == 50


def test_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("MAX_CONCURRENCY", "4")
    monkeypatch.setenv("QUEUE_LIMIT", "7")
    monkeypatch.setenv("PER_ORIGIN_CAP", "1")
    monkeypatch.setenv("DISK_FLOOR_BYTES", "2048")
    monkeypatch.setenv("TTL_SECONDS", "10")
    monkeypatch.setenv("SWEEP_INTERVAL_SECONDS", "5")
    monkeypatch.setenv("DOWNLOAD_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("TRANSCODE_TIMEOUT_SECONDS", "20")
    monkeypatch.setenv("MAX_DURATION_SECONDS", "120")
    monkeypatch.setenv("PROBE_TIMEOUT_SECONDS", "3")
    cfg = Config()
    assert cfg.max_concurrency == 4
    assert cfg.queue_limit == 7  # explicit override beats the derived 10x rule
    assert cfg.per_origin_cap == 1
    assert cfg.disk_floor_bytes == 2048
    assert cfg.ttl_seconds == 10
    assert cfg.sweep_interval_seconds == 5
    assert cfg.download_timeout_seconds == 30
    assert cfg.transcode_timeout_seconds == 20
    assert cfg.max_duration_seconds == 120
    assert cfg.probe_timeout_seconds == 3


def test_work_dir_env_override_and_creation(monkeypatch, tmp_path):
    target = tmp_path / "custom" / "workdir"
    monkeypatch.setenv("WORK_DIR", str(target))
    cfg = Config()
    assert cfg.work_dir == target
    assert target.is_dir()  # created on construction


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("yes", True),
        ("0", False),
        ("false", False),
        ("", False),
        ("garbage", False),
    ],
)
def test_trusted_proxy_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("TRUSTED_PROXY", raw)
    cfg = Config()
    assert cfg.trusted_proxy is expected


def test_trusted_proxy_default_off():
    cfg = Config()
    assert cfg.trusted_proxy is False
