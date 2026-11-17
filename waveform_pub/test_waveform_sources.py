"""Exercise the T3DSO1204 wire format without an oscilloscope."""

import struct
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pytest

from scope_settings import get_default_scope_settings, prepare_settings_update
from waveform_data_capture import WaveformDataCaptureManager
from waveform_mqtt_handler import WaveformMQTTHandler
from waveform_sources import CaptureInterrupted, SyntheticSource, TeledyneSource, _decode_dat2


def test_visa_capture_to_mqtt_preserves_scaling_timing_and_units():
    settings, _ = prepare_settings_update(get_default_scope_settings(), {
        "sampling": {"num_samples": 4, "transfer_stride": 2},
        "trigger_settings": {"mode": "normal"},
        "channels": [{"channel": "A"}, {"channel": "D"}],
        "postprocessing": {"down_sample_ratio": 2},
    })
    scope = Mock()
    replies = {
        "*IDN?": "Teledyne LeCroy,T3DSO1204,123456,1.0\n",
        "WFSU?": "WFSU SP,2,NP,4,FP,0\n",
        "C1:TRA?": "C1:TRA ON\n",
        "C4:TRA?": "ON\n",
        "C1:UNIT?": "V\n",
        "C4:UNIT?": "C4:UNIT V\n",
        "SARA?": "SARA 500MSa/s\n",
        "TDIV?": "TDIV 5.00E-9S\n",
        "TRDL?": "2.0E-9\n",
        "C1:VDIV?": "C1:VDIV 5.00E-01V\n",
        "C1:OFST?": "C1:OFST -5.00E-01V\n",
        "C4:VDIV?": "200mV\n",
        "C4:OFST?": "0\n",
    }
    states = iter(["INR 1", "INR 8192", "INR 1"])
    scope.query.side_effect = lambda command: next(states) if command == "INR?" else replies[command]
    scope.read_raw.side_effect = [
        b"C1:WF ALL,#9000000004\x02\xfc\x0a\xff\n\n",
        b"DAT2,#14\x00\x01\x01\x00\n\n",
    ]
    resource_manager = Mock()
    resource_manager.open_resource.return_value = scope
    capture = WaveformDataCaptureManager(settings, source=TeledyneSource(settings, "scope.local"))
    with patch("waveform_sources.pyvisa.ResourceManager", return_value=resource_manager):
        try:
            capture.open_device()
            frame = capture.run_capture_cycle()
        finally:
            capture.close_device()

    resource_manager.open_resource.assert_called_once()
    assert resource_manager.open_resource.call_args.args == ("TCPIP0::scope.local::inst0::INSTR",)
    assert [call.args[0] for call in scope.write.call_args_list] == [
        "STOP", "WFSU SP,2,NP,4,FP,0", "TRMD SINGLE", "STOP",
        "C1:WF? DAT2", "C4:WF? DAT2", "STOP",
    ]
    np.testing.assert_allclose(frame["channels_mv"]["A"], [[490, 600]])
    np.testing.assert_allclose(frame["channels_mv"]["D"], [[4, 4]])
    np.testing.assert_allclose(frame["time_axis"], [-31e-9, -23e-9], atol=1e-15)
    assert frame["resolution_bits"] == 8
    assert frame["raw_sample_count"] == 4
    scope.close.assert_called_once()
    resource_manager.close.assert_called_once()

    handler = WaveformMQTTHandler("localhost", 1883, "scope/waveform", settings)
    handler.client.publish = Mock(return_value=SimpleNamespace(rc=0))
    handler.connected = True
    assert handler.publish_waveform(frame)
    publish = handler.client.publish.call_args
    payload = publish.args[1]
    magic, t0, dt, timestamp, channels, points = struct.unpack_from("<4sddQII", payload)
    assert (magic, timestamp, channels, points) == (b"WAVE", frame["timestamp_us"], 2, 2)
    assert t0 == pytest.approx(-31e-9, abs=1e-15)
    assert dt == pytest.approx(8e-9, abs=1e-15)
    assert payload[36:40] == b"\x01a\x01d"
    np.testing.assert_allclose(np.frombuffer(payload, dtype="<f8", offset=40), [490, 4, 600, 4])
    assert dict(publish.kwargs["properties"].UserProperty) == {"sample_unit": "mV", "time_unit": "s"}


@pytest.mark.parametrize("reply", [b"", b"#15abc", b"#0abc", b"#12abjunk", b"#13abc"])
def test_invalid_or_oversize_dat2_is_rejected(reply):
    with pytest.raises(ValueError):
        _decode_dat2(reply, 2)


def test_trigger_wait_can_be_cancelled_without_downloading_stale_data():
    source = TeledyneSource(get_default_scope_settings(), "scope.local")
    source.scope = Mock()
    interrupt = threading.Event()

    def reply(command):
        assert command == "INR?"
        if source.scope.query.call_count == 2:
            interrupt.set()
        return "0"

    source.scope.query.side_effect = reply
    with pytest.raises(CaptureInterrupted):
        source.capture(interrupt)
    assert [call.args[0] for call in source.scope.write.call_args_list] == ["TRMD AUTO", "STOP"]
    source.scope.read_raw.assert_not_called()


def test_simulation_uses_regular_acquisition_and_averaging():
    settings, _ = prepare_settings_update(get_default_scope_settings(), {
        "sampling": {"num_samples": 20},
        "postprocessing": {"channels": {"A": {"type": "simple_average", "window_size": 2}}},
    })
    capture = WaveformDataCaptureManager(settings, source=SyntheticSource(settings))
    try:
        capture.open_device()
        first = capture.run_capture_cycle()
        second = capture.run_capture_cycle()
    finally:
        capture.close_device()

    assert first["synthetic"]
    assert first["channels_mv"]["A"].shape == (1, 20)
    assert not first["processing_info"]["A"]["ready"]
    assert second["processing_info"]["A"]["ready"]
