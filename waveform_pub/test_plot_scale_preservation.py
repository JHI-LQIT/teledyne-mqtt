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


def test_first_frame_fits_before_the_plot_range_is_locked(monkeypatch):
    import numpy as np

    from waveform_plot_manager import WaveformPlotManager

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    manager = WaveformPlotManager()
    plot = manager.setup_plots()
    try:
        times = np.linspace(-150e-6, 50e-6, 1000)
        wave = np.sin(np.linspace(0, 20, times.size))
        channels = {
            name: (amplitude * wave)[None, :]
            for name, amplitude in zip("ABCD", (2000, 3, 500, 1))
        }

        # The worker can deliver data before the widget gets its next paint.
        manager.update_waveform_plots(channels, times)
        manager.get_qt_app().processEvents()

        (xmin, xmax), (ymin, ymax) = plot.viewRange()
        assert xmin <= times[0] < times[-1] <= xmax
        assert ymin <= channels["A"].min() < channels["A"].max() <= ymax
        for curve in manager.plot_curves.values():
            assert curve.getData()[0].size > 0

        plot.setRange(xRange=(-5e-6, 5e-6), yRange=(-100, 100), padding=0)
        zoom = plot.viewRange()
        manager.update_waveform_plots(channels, times)
        manager.get_qt_app().processEvents()
        np.testing.assert_allclose(plot.viewRange(), zoom)
    finally:
        manager.close_plots()
