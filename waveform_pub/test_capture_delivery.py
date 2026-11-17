"""Tests for capture delivery with and without the Qt preview."""

from threading import Event, Thread, get_ident
from unittest.mock import Mock, patch

import pytest

from waveform_publisher import WaveformPublisher


@pytest.mark.parametrize("preview_enabled", [False, True])
def test_delivery_publishes_each_capture_and_coalesces_preview(preview_enabled):
    with patch("waveform_publisher.WaveformDataCaptureManager"):
        publisher = WaveformPublisher("localhost", 1883, "wave", {})
    publisher.plot_manager = Mock()
    publisher._running = True
    publisher._preview_enabled = preview_enabled
    first = {"time_axis": [0, 1]}
    second = {"time_axis": [0, 2]}
    publisher.data_capture.get_captured_data.side_effect = [
        ("data", first), ("data", second)
    ]

    finished = Event()
    published = []
    qt_thread = get_ident()

    def publish(capture, settings, generation):
        published.append((capture, get_ident()))
        if capture is second:
            publisher._delivery_stop.set()
            finished.set()

    publisher._publish_capture = publish
    worker = Thread(target=publisher._deliver_captures, args=(0.01,))
    worker.start()
    try:
        assert finished.wait(2)
    finally:
        publisher._delivery_stop.set()
        worker.join(2)

    assert published == [(first, worker.ident), (second, worker.ident)]
    assert worker.ident != qt_thread
    publisher.plot_manager.display_capture.assert_not_called()
    publisher._update_preview()
    if preview_enabled:
        publisher.plot_manager.display_capture.assert_called_once_with(second)
    else:
        publisher.plot_manager.display_capture.assert_not_called()


def test_delivery_discards_payload_encoded_before_settings_change():
    with patch("waveform_publisher.WaveformDataCaptureManager"):
        publisher = WaveformPublisher("localhost", 1883, "wave", {})
    publisher._running = True
    publisher.data_capture.get_captured_data.return_value = None

    def encode(capture, *, sender_payload, scope_settings, submit):
        publisher._discard_queued_captures()
        return submit(b"stale")

    with patch.object(publisher.mqtt_handler, "publish_waveform", side_effect=encode):
        with patch.object(publisher.mqtt_handler, "submit_waveform") as submit:
            assert not publisher._publish_capture({}, {}, publisher._capture_generation)
            submit.assert_not_called()
