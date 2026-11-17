"""Tests for the acquisition worker's source ownership and ordering."""

import threading
from unittest.mock import patch

import numpy as np
import pytest

from waveform_data_capture import WaveformDataCaptureManager
from waveform_pub import get_default_scope_settings
from waveform_publisher import WaveformPublisher
from waveform_sources import CaptureInterrupted


class FakeSource:
    def __init__(self):
        self.calls = []
        self.capture_started = threading.Event()
        self.wait_for_trigger = False

    def _record(self, operation, value=None):
        self.calls.append((operation, value, threading.get_ident()))

    def open(self):
        self._record("open")

    def update_settings(self, settings):
        self.samples = settings["sampling"]["num_samples"]
        self._record("settings", self.samples)

    def configure(self):
        self._record("configure", self.samples)
        if self.samples == 42:
            raise RuntimeError("rejected sample count")

    def capture(self, interrupt):
        self._record("capture")
        if self.wait_for_trigger:
            self.capture_started.set()
            assert interrupt.wait(1), "trigger wait was not interrupted"
            self._record("interrupted")
            raise CaptureInterrupted
        return {
            "channels_mv": {"A": np.zeros((1, 2))},
            "time_axis": np.array([0, 1e-6]),
            "actual_sample_rate": 1e6,
        }

    def close(self):
        self._record("close")


def test_failed_reconfiguration_and_rollback_use_the_same_worker():
    settings = get_default_scope_settings()
    source = FakeSource()
    manager = WaveformDataCaptureManager(settings, source=source)
    with patch("waveform_publisher.WaveformDataCaptureManager", return_value=manager):
        publisher = WaveformPublisher("localhost", 1883, "scope/waveform", settings)

    try:
        publisher.open_device()
        result = publisher.set_scope_settings({"num_samples": 42})
        assert result == {"success": False, "error": "rejected sample count"}
        assert publisher.current_scope_settings == settings
        manager.run_capture_cycle()
    finally:
        publisher.close_device()

    assert [(name, value) for name, value, _ in source.calls] == [
        ("open", None), ("settings", 42), ("configure", 42),
        ("settings", settings["sampling"]["num_samples"]),
        ("configure", settings["sampling"]["num_samples"]),
        ("capture", None), ("close", None),
    ]
    worker_ids = {thread_id for _, _, thread_id in source.calls}
    assert len(worker_ids) == 1 and threading.get_ident() not in worker_ids


def test_shutdown_interrupts_a_capture_waiting_for_trigger_before_closing():
    source = FakeSource()
    source.wait_for_trigger = True
    manager = WaveformDataCaptureManager(get_default_scope_settings(), source=source)
    manager.open_device()
    try:
        manager.start_continuous_capture()
        assert source.capture_started.wait(1)
    finally:
        manager.close_device()

    assert [name for name, _, _ in source.calls] == [
        "open", "capture", "interrupted", "close"
    ]
    assert not manager._worker.is_alive()
    assert manager.get_captured_data() is None
    with pytest.raises(RuntimeError, match="shutting down"):
        manager.start_continuous_capture()
