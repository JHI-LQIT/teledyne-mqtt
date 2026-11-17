"""Regression coverage for the shared GUI and MQTT apply path."""

from copy import deepcopy
from unittest.mock import Mock, patch

import pytest

from waveform_pub import get_default_scope_settings
from waveform_publisher import WaveformPublisher


@pytest.fixture
def publisher():
    settings = get_default_scope_settings()
    with (
        patch("waveform_publisher.WaveformDataCaptureManager") as capture_class,
        patch("waveform_publisher.WaveformMQTTHandler"),
        patch("waveform_publisher.WaveformPlotManager"),
    ):
        instance = WaveformPublisher("localhost", 1883, "scope/waveform", settings)

    capture = capture_class.return_value
    capture._use_synthetic_data = False
    capture.get_captured_data.return_value = None
    instance._running = True
    return instance


def test_gui_snapshot_and_flat_mqtt_update_apply_same_settings(publisher):
    candidate = deepcopy(publisher.current_scope_settings)
    candidate["sampling"]["num_samples"] = 200
    with (
        patch("waveform_publisher.save_scope_settings") as save,
        patch("pyqtgraph.Qt.QtWidgets.QMessageBox.warning") as warning,
    ):
        publisher._on_settings_applied(candidate)

    warning.assert_not_called()
    gui_settings = publisher.current_scope_settings
    save.assert_called_once_with(gui_settings)
    publisher.data_capture.configure_device.assert_called_once()

    publisher.set_scope_settings({"num_samples": 300})
    result = publisher.set_scope_settings({"num_samples": 200})
    assert result["updated_settings"] == {"num_samples": 200}
    assert publisher.current_scope_settings == gui_settings


def test_software_only_patch_waits_for_idle_without_reconfiguring(publisher):
    operations = Mock()
    operations.attach_mock(publisher.data_capture.stop_continuous_capture, "stop")
    operations.attach_mock(publisher.data_capture.update_settings, "update")
    operations.attach_mock(publisher.data_capture.start_continuous_capture, "start")

    result = publisher.set_scope_settings(
        {"publishing": {"channels": {"A": {"publish": False}}}}
    )

    assert result["success"]
    assert [call[0] for call in operations.mock_calls] == ["stop", "update", "start"]
    publisher.data_capture.configure_device.assert_not_called()


def test_failed_configuration_restores_previous_settings_before_resuming(publisher):
    previous = publisher.current_scope_settings
    capture = publisher.data_capture
    capture.configure_device.side_effect = [RuntimeError("device rejected settings"), None]

    result = publisher.set_scope_settings({"num_samples": 200})

    assert result == {"success": False, "error": "device rejected settings"}
    assert publisher.current_scope_settings == previous
    assert capture.configure_device.call_count == 2
    capture.update_settings.assert_called_with(previous)
    capture.start_continuous_capture.assert_called_once()
    publisher.mqtt_handler.publish_config.assert_not_called()


def test_failed_recovery_leaves_capture_stopped(publisher):
    capture = publisher.data_capture
    capture.configure_device.side_effect = RuntimeError("device unavailable")

    result = publisher.set_scope_settings({"num_samples": 200})

    assert not result["success"]
    assert "could not be restored" in result["error"]
    assert not publisher._running
    capture.start_continuous_capture.assert_not_called()
    publisher.mqtt_handler.publish_config.assert_not_called()


def test_invalid_trigger_direction_leaves_capture_untouched(publisher):
    result = publisher.set_scope_settings({"trigger_direction": "invalid"})
    assert not result["success"]
    publisher.data_capture.stop_continuous_capture.assert_not_called()


def test_scope_dialog_round_trips_teledyne_settings(monkeypatch):
    from pyqtgraph.Qt import QtWidgets
    from waveform_settings_dialog import ScopeSettingsDialog

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = get_default_scope_settings()
    for section in ("scope", "postprocessing", "publishing"):
        dialog = ScopeSettingsDialog(initial_settings=settings, section=section)
        assert dialog._params_to_settings() == {
            name: settings[name] for name in dialog.SECTIONS[section].values()
        }
        if section == "scope":
            toggled = []
            dialog.settings_applied.connect(toggled.append)
            dialog.params.child("Sampling", "Follow Scope").setValue(True)
            assert len(toggled) == 1
            assert toggled[0]["sampling"]["follow_scope"] is True
            assert not dialog.params.child("Sampling", "Maximum Points").opts["enabled"]
            assert not dialog.params.child("Sampling", "Transfer Stride").opts["enabled"]
            assert not dialog.params.child("Channels", "C1").opts["enabled"]
        dialog.deleteLater()
    app.processEvents()
