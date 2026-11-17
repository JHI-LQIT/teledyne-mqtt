"""Tests for preserving the plot view when scope settings change."""

from unittest.mock import Mock

from waveform_publisher import WaveformPublisher


def test_applying_scope_settings_does_not_reset_plot_view():
    publisher = WaveformPublisher.__new__(WaveformPublisher)
    publisher.plot_manager = Mock()
    publisher.set_scope_settings = Mock(return_value={"success": True})

    new_settings = {"sampling": {"num_samples": 200}}
    publisher._on_settings_applied(new_settings)

    publisher.set_scope_settings.assert_called_once_with(new_settings, persist=True)
    publisher.plot_manager.reset_plot_view.assert_not_called()
