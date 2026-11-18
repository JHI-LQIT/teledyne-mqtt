"""Tests for routing MQTT and local reset requests to the accumulator."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from waveform_mqtt_handler import WaveformMQTTHandler
from waveform_postprocessor import WaveformPostprocessor
from waveform_pub import get_default_scope_settings
from waveform_publisher import WaveformPublisher


def test_mqtt_reset_subscribes_and_returns_current_settings():
    handler = WaveformMQTTHandler("localhost", 1883, "lqt/teledyne/waveform")
    handler.client.subscribe = Mock()
    handler.publish_settings_response = Mock()
    handler.get_settings_callback = Mock(return_value={"success": True})
    handler.reset_callback = Mock(return_value={"success": True, "current_settings": {}})

    handler.client.on_connect(handler.client, None, None, 0, None)
    handler.client.subscribe.assert_any_call("lqt/teledyne/cmnd/reset")

    for command in ("reset", "settings"):
        handler.client.on_message(
            handler.client, None,
            SimpleNamespace(topic=f"lqt/teledyne/cmnd/{command}", payload=b""),
        )

    handler.reset_callback.assert_called_once_with()
    handler.get_settings_callback.assert_called_once_with()
    assert [call.args[0] for call in handler.publish_settings_response.call_args_list] == [
        {"success": True, "current_settings": {}}, {"success": True}
    ]


def test_offline_gui_reset_waits_for_idle_and_clears_queued_frames():
    settings = get_default_scope_settings()
    config = {"channels": {
        "A": {"type": "simple_average", "window_size": 2},
        "B": {"type": "moving_average", "window_size": 2},
    }}
    settings["postprocessing"] = config
    with (
        patch("waveform_publisher.WaveformDataCaptureManager"),
        patch("waveform_publisher.WaveformPlotManager") as plot,
    ):
        publisher = WaveformPublisher("localhost", 1883, "scope/waveform", settings)

    publisher._running = True
    capture = publisher.data_capture
    processor = WaveformPostprocessor(config)
    capture.reset_averaging.side_effect = processor.reset
    raw = {"A": np.array([[1.0, 2.0]]), "B": np.array([[1.0, 2.0]])}
    processor.process_waveforms(raw)
    publisher._last_capture_data = raw
    publisher._new_data_available = True
    capture.get_captured_data.side_effect = [("data", raw), None]
    capture.stop_continuous_capture.side_effect = (
        lambda: assert_batch_pending(processor)
    )

    plot.call_args.kwargs["reset_callback"]()

    assert processor.simple_average_counts["A"] == 0
    assert not processor.moving_average_buffers["B"]
    assert processor.config == config
    assert publisher._last_capture_data is None
    assert not publisher._new_data_available
    capture.start_continuous_capture.assert_called_once_with()
    capture.configure_device.assert_not_called()


def assert_batch_pending(processor):
    assert processor.simple_average_counts["A"] == 1


def test_online_gui_reset_preserves_mqtt_command():
    with (
        patch("waveform_publisher.WaveformDataCaptureManager"),
        patch("waveform_publisher.WaveformPlotManager") as plot,
    ):
        publisher = WaveformPublisher(
            "localhost", 1883, "scope/waveform", get_default_scope_settings()
        )
    handler = publisher.mqtt_handler
    handler.connected = True
    handler.client.publish = Mock(return_value=SimpleNamespace(rc=0))

    plot.call_args.kwargs["reset_callback"]()

    handler.client.publish.assert_called_once_with("scope/cmnd/reset", payload="", qos=1)
    publisher.data_capture.stop_continuous_capture.assert_not_called()


def test_waveform_larger_than_broker_limit_is_rejected_before_queueing(caplog):
    from paho.mqtt.packettypes import PacketTypes
    from paho.mqtt.properties import Properties

    handler = WaveformMQTTHandler("localhost", 1883, "scope/waveform")
    handler.client.subscribe = Mock()
    handler.client.publish = Mock()
    properties = Properties(PacketTypes.CONNACK)
    properties.MaximumPacketSize = 1024
    handler._on_connect(handler.client, None, None, 0, properties)

    assert not handler.submit_waveform(bytes(1024))
    handler.client.publish.assert_not_called()
    assert "Downsample Ratio" in caplog.text
    assert handler.connected

    pending = Mock(rc=0)
    pending.is_published.return_value = False
    handler.client.publish.return_value = pending
    assert handler.submit_waveform(bytes(100))
    assert not handler.submit_waveform(bytes(100))
    handler.client.publish.assert_called_once()
    pending.is_published.return_value = True
    assert handler.submit_waveform(bytes(100))
    assert handler.client.publish.call_count == 2
