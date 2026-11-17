"""Tests for averaging progress and delivery of completed averages."""

from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from waveform_mqtt_handler import WaveformMQTTHandler
from waveform_plot_manager import WaveformPlotManager
from waveform_postprocessor import WaveformPostprocessor, is_simple_average_pending


def test_processor_reports_progress_and_hides_raw_original():
    processor = WaveformPostprocessor({
        "show_original": True,
        "channels": {"A": {"type": "simple_average", "window_size": 3}},
    })
    for count, value in enumerate((1.0, 3.0, 5.0), start=1):
        processed, original, info = processor.process_waveforms(
            {"A": np.array([[value, value + 2]])}
        )
        assert original == {}
        assert info["A"]["window_size"] == 3
        assert info["A"]["current_samples"] == count
        assert is_simple_average_pending(info) == (count < 3)
        if count < 3:
            assert processed is None

    np.testing.assert_allclose(processed["A"], [[3.0, 5.0]])


def test_only_completed_average_is_published_and_rendered():
    processor = WaveformPostprocessor({
        "show_original": True,
        "channels": {"A": {"type": "simple_average", "window_size": 2}},
    })
    handler = WaveformMQTTHandler(
        "localhost", 1883, "scope/waveform", {"channels": [{"channel": "A"}]}
    )
    handler.connected = True
    handler.client.publish = Mock(return_value=SimpleNamespace(rc=0))
    plot = WaveformPlotManager()
    plot.status_bar = Mock()
    plot.update_waveform_plots = Mock()

    for count in (1, 2):
        raw = {"A": np.array([[float(count), float(count + 2)]])}
        processed, original, info = processor.process_waveforms(raw)
        frame = {
            "channels_mv": raw,
            "time_axis": np.array([0.0, 1e-6]),
            "actual_sample_rate": 1e6,
            "synthetic": True,
            "processed_channels_mv": processed,
            "original_channels_mv": original,
            "processing_info": info,
            "show_original": True,
        }
        assert handler.publish_waveform(frame)
        plot.display_capture(frame)
        assert f"Averaging A: {count}/2" in plot.status_bar.showMessage.call_args.args[0]
        assert "Resolution: synthetic" in plot.status_bar.showMessage.call_args.args[0]
        if count == 1:
            handler.client.publish.assert_not_called()
            plot.update_waveform_plots.assert_not_called()

    handler.client.publish.assert_called_once()
    properties = handler.client.publish.call_args.kwargs["properties"]
    assert dict(properties.UserProperty) == {"sample_unit": "mV", "time_unit": "s"}
    plot.update_waveform_plots.assert_called_once()
    assert plot.update_waveform_plots.call_args.kwargs["original_channels_mv"] == {}
