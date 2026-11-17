"""Exercise the T3DSO1204 wire format without an oscilloscope."""

import struct
import sys
import threading
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import numpy as np
import pytest
import pyvisa

from scope_settings import get_default_scope_settings, prepare_settings_update
from waveform_data_capture import WaveformDataCaptureManager
from waveform_mqtt_handler import WaveformMQTTHandler
from waveform_pub import main
from waveform_sources import CaptureInterrupted, SyntheticSource, TeledyneSource, _decode_dat2, list_usb_resources


USB_SCOPE = "USB0::0x1234::0x5678::SCOPE::INSTR"
USB_OTHER = "USB0::0x1234::0x5678::OTHER::INSTR"


@pytest.mark.parametrize("connection", ["ethernet", "usb", "usb-discovery"])
def test_visa_capture_to_mqtt_preserves_scaling_timing_and_units(connection):
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
    if connection == "ethernet":
        address = "TCPIP0::scope.local::inst0::INSTR"
        source = TeledyneSource(settings, "scope.local")
    else:
        address = USB_SCOPE
        source = TeledyneSource(
            settings,
            usb_resource=USB_SCOPE if connection == "usb" else None,
            visa_backend="@ivi",
        )
    if connection == "usb-discovery":
        resource_manager.list_resources.return_value = (USB_OTHER, USB_SCOPE)
        other_probe = MagicMock()
        other_probe.__enter__.return_value.query.return_value = "Other,Unrelated,123,1.0"
        scope_probe = MagicMock()
        scope_probe.__enter__.return_value.query.return_value = replies["*IDN?"]
        resource_manager.open_resource.side_effect = [other_probe, scope_probe, scope]
    else:
        resource_manager.open_resource.return_value = scope
    capture = WaveformDataCaptureManager(settings, source=source)
    with patch("waveform_sources.pyvisa.ResourceManager", return_value=resource_manager):
        try:
            capture.open_device()
            frame = capture.run_capture_cycle()
        finally:
            capture.close_device()

    if connection == "usb-discovery":
        resource_manager.list_resources.assert_called_once_with("USB?*::INSTR")
        assert [call.args[0] for call in resource_manager.open_resource.call_args_list] == [
            USB_OTHER, USB_SCOPE, USB_SCOPE
        ]
        other_probe.__exit__.assert_called_once()
        scope_probe.__exit__.assert_called_once()
    else:
        resource_manager.list_resources.assert_not_called()
        resource_manager.open_resource.assert_called_once()
    assert resource_manager.open_resource.call_args.args == (address,)
    assert resource_manager.open_resource.call_args.kwargs["read_termination"] is None
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


def test_usb_discovery_requires_explicit_selection_if_two_scopes_match():
    manager = Mock()
    manager.list_resources.return_value = (USB_SCOPE, USB_OTHER)
    probes = [MagicMock(), MagicMock()]
    for probe in probes:
        probe.__enter__.return_value.query.return_value = "Teledyne LeCroy,T3DSO1204,X,1.0"
    manager.open_resource.side_effect = probes
    source = TeledyneSource(get_default_scope_settings(), visa_backend="@ivi")
    with patch("waveform_sources.pyvisa.ResourceManager", return_value=manager):
        with pytest.raises(RuntimeError, match="Multiple T3DSO1204 scopes found.*--usb-resource"):
            source.open()
    for probe in probes:
        probe.__exit__.assert_called_once()
        probe.__enter__.return_value.write.assert_not_called()
    manager.close.assert_called_once()


def test_usb_discovery_explains_no_instruments():
    manager = Mock()
    manager.list_resources.side_effect = pyvisa.errors.VisaIOError(
        pyvisa.constants.StatusCode.error_resource_not_found
    )
    source = TeledyneSource(get_default_scope_settings(), visa_backend="@ivi")
    with patch("waveform_sources.pyvisa.ResourceManager", return_value=manager):
        with pytest.raises(RuntimeError, match="No T3DSO1204 found over USB.*--list-usb"):
            source.open()
    manager.open_resource.assert_not_called()
    manager.close.assert_called_once()


def test_pyvisa_py_usb_enumeration_does_not_scan_lan():
    manager = Mock()
    usb_session = ModuleType("pyvisa_py.usb")
    usb_session.USBInstrSession = Mock()
    usb_session.USBInstrSession.list_resources.return_value = [USB_SCOPE]
    with patch.dict(sys.modules, {"pyvisa_py.usb": usb_session}):
        assert list_usb_resources(manager, "@py") == (USB_SCOPE,)
    manager.list_resources.assert_not_called()


def test_list_usb_command_lists_resources_and_closes_manager(capsys):
    manager = Mock()
    manager.list_resources.return_value = (USB_SCOPE,)
    with patch("waveform_pub.pyvisa.ResourceManager", return_value=manager):
        assert main(["--list-usb", "--visa-backend", "@ivi"]) == 0
    assert capsys.readouterr().out.strip() == USB_SCOPE
    manager.close.assert_called_once()


def test_follow_scope_downloads_full_record_and_downsamples_for_mqtt():
    settings, _ = prepare_settings_update(get_default_scope_settings(), {
        "sampling": {"follow_scope": True, "num_samples": 2, "transfer_stride": 5},
        "postprocessing": {"down_sample_ratio": 2},
    })
    scope = Mock()
    replies = {
        "*IDN?": "Teledyne LeCroy,T3DSO1204,123,1.0",
        "C1:TRA?": "OFF",
        "C2:TRA?": "ON",
        "C3:TRA?": "OFF",
        "C4:TRA?": "OFF",
        "C2:UNIT?": "V",
        "C2:VDIV?": "C2:VDIV 1.00E+00V",
        "C2:OFST?": "C2:OFST -1.00E-01V",
        "WFSU?": "WFSU SP,0,NP,0,FP,0",
        "INR?": "INR 1",
        "SARA?": "SARA 10Sa/s",
        "TDIV?": "TDIV 5.00E-02S",
        "TRDL?": "TRDL 0S",
        "SANU? C2": "SANU 7.00E+00pts",
    }
    scope.query.side_effect = replies.__getitem__
    scope.read_raw.return_value = b"C2:WF DAT2,#17\x00\x01\x02\x03\x04\x05\x06\n\n"
    manager = Mock()
    manager.open_resource.return_value = scope
    source = TeledyneSource(settings, usb_resource=USB_SCOPE, visa_backend="@ivi")
    capture = WaveformDataCaptureManager(settings, source=source)
    with patch("waveform_sources.pyvisa.ResourceManager", return_value=manager):
        try:
            capture.open_device()
            frame = capture.run_capture_cycle()
        finally:
            capture.close_device()

    scope.write.assert_any_call("WFSU SP,0,NP,0,FP,0")
    scope.write.assert_any_call("C2:WF? DAT2")
    assert frame["raw_sample_count"] == frame["scope_record_points"] == 7
    assert frame["transfer_stride"] == 1
    assert frame["down_sample_ratio"] == 2
    np.testing.assert_allclose(frame["time_axis"], [-0.3, -0.1, 0.1], atol=1e-12)
    np.testing.assert_allclose(frame["channels_mv"]["B"], [[120, 200, 280]])

    handler = WaveformMQTTHandler("localhost", 1883, "scope/waveform", settings)
    handler.connected = True
    handler.client.publish = Mock(return_value=SimpleNamespace(rc=0))
    assert handler.publish_waveform(frame)
    payload = handler.client.publish.call_args.args[1]
    _, _, dt, _, channels, samples = struct.unpack_from("<4sddQII", payload)
    assert (channels, samples, dt) == (1, 3, 0.2)
    assert payload[36:38] == b"\x01b"
    np.testing.assert_allclose(np.frombuffer(payload, dtype="<f8", offset=38), [120, 200, 280])

    settings["publishing"]["channels"]["B"]["publish"] = False
    handler.client.publish.reset_mock()
    assert handler.publish_waveform(frame)
    handler.client.publish.assert_not_called()
