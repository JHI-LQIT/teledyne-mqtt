"""
Plot Manager for PyQtGraph-based real-time waveform plotting
"""

import logging
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PyQt5.QtCore import QSettings
from pyqtgraph.Qt import QtCore, QtGui, QtWidgets

from acquisition_status import build_acquisition_status
from waveform_postprocessor import is_simple_average_pending

logger = logging.getLogger(__name__)


class WaveformPlotManager:
    """Manages PyQtGraph plotting for real-time waveform visualization"""

    def __init__(self, settings_actions=None, reset_callback=None):
        """Initialize plot manager

        Args:
            settings_actions: Mapping from button labels to (callback, tooltip) pairs
        """
        # Qt Application
        self.qt_app = None

        # Main plotting widgets
        self.main_plot_window = None
        self.plot_widget = None

        # Plot curves for efficient updates
        self.plot_curves = {}
        self.original_plot_curves = {}  # Curves for original waveforms
        self._update_counter = (
            0  # Counter for managing auto-range like PyQtGraph example
        )

        # Store last postprocessed data to keep it visible between updates
        self._last_processed_channels = None
        self._last_processing_info = None
        self._scope_grid = None

        # Settings callback
        self.settings_actions = settings_actions or {}
        self.reset_callback = reset_callback
        self.settings_buttons = {}

        # Status bar
        self.status_bar = None

        # QSettings for persistent window geometry
        self.settings = QSettings("Teledyne", "WaveformPublisher")

    def _ensure_qt_app(self):
        """Ensure QApplication exists"""
        if self.qt_app is None:
            self.qt_app = pg.mkQApp("org.jhi_lqit.TeledyneMQTT")
            self.qt_app.setDesktopFileName("org.jhi_lqit.TeledyneMQTT")
            icon_path = Path(__file__).resolve().parent / "assets" / "oscilloscope.svg"
            self.qt_app.setWindowIcon(QtGui.QIcon(str(icon_path)))
        return self.qt_app

    def get_qt_app(self):
        """Get Qt application instance

        Returns:
            QApplication: Qt application instance
        """
        return self.qt_app

    @staticmethod
    def _set_waveform_axis_units(plot):
        # Captures store millivolts and seconds. Format voltage ticks in
        # volts before PyQtGraph chooses an SI prefix for either axis.
        plot.getAxis("left").enableAutoSIPrefix(True)
        plot.getAxis("left").setScale(1e-3)
        plot.setLabel("left", "Voltage", "V")
        plot.setLabel("bottom", "Time", "s")

    def setup_plots(self, title="Waveform MQTT Publisher"):
        """Setup pyqtgraph plot window for waveforms

        Args:
            title (str): Window title

        Returns:
            pg.PlotWidget: Main plot widget for waveforms
        """
        if self.plot_widget is None:
            self._ensure_qt_app()  # Ensure QApplication exists first

            # Create main window widget
            self.main_plot_window = QtWidgets.QWidget()
            self.main_plot_window.setWindowTitle(title)
            self.main_plot_window.resize(900, 500)

            # Create main layout
            main_layout = QtWidgets.QVBoxLayout(self.main_plot_window)

            # Create toolbar with settings button
            toolbar_layout = QtWidgets.QHBoxLayout()

            for label, (callback, tooltip) in self.settings_actions.items():
                button = QtWidgets.QPushButton(label)
                button.setToolTip(tooltip)
                button.clicked.connect(callback)
                toolbar_layout.addWidget(button)
                self.settings_buttons[label] = button

            if self.reset_callback is not None:
                self.reset_button = QtWidgets.QPushButton("↻ Reset")
                self.reset_button.setToolTip("Clear accumulated waveform averages and start averaging again.")
                self.reset_button.clicked.connect(self.reset_callback)
                toolbar_layout.addWidget(self.reset_button)
            toolbar_layout.addStretch()

            main_layout.addLayout(toolbar_layout)

            # Create graphics layout widget for plots
            plot_layout_widget = pg.GraphicsLayoutWidget()
            main_layout.addWidget(plot_layout_widget)

            # Create waveform plot
            self.plot_widget = plot_layout_widget.addPlot(title="Channel Waveforms")
            self._set_waveform_axis_units(self.plot_widget)
            self.plot_widget.addLegend()
            self.plot_widget.showGrid(x=True, y=True)

            # Create status bar
            self.status_bar = QtWidgets.QStatusBar()
            self.status_bar.showMessage("Ready")
            main_layout.addWidget(self.status_bar)

            # Restore window geometry from settings
            geometry = self.settings.value("mainWindow/geometry")
            if geometry:
                self.main_plot_window.restoreGeometry(geometry)

            # Override closeEvent to save geometry
            original_close_event = self.main_plot_window.closeEvent

            def closeEvent(a0):
                self._on_main_window_close(a0)

            self.main_plot_window.closeEvent = closeEvent

            # Show the window
            self.main_plot_window.show()

        return self.plot_widget

    def update_waveform_plots(
        self,
        channels_mv,
        time_axis,
        segment_index=0,
        processed_channels_mv=None,
        original_channels_mv=None,
        processing_info=None,
        show_original=False,
        scope_scales=None,
    ):
        """Update waveform plots using curve.setData()

        Args:
            channels_mv (dict): Channel data dictionary (raw data, used if no processed data)
            time_axis (np.array): Time axis data
            segment_index (int): Which segment to plot
            processed_channels_mv (dict): Processed channel data (if postprocessing applied)
            original_channels_mv (dict): Original channel data (if showing originals)
            processing_info (dict): Processing metadata per channel
            show_original (bool): Whether to show original waveforms
            scope_scales (dict): Optional per-channel volts/div and position for scope view
        """
        logger.debug(
            f"update_waveform_plots called: channels={len(channels_mv) if channels_mv else None}, "
            f"processed={len(processed_channels_mv) if processed_channels_mv else None}, "
            f"time_axis_len={len(time_axis) if time_axis is not None else None}, segment={segment_index}"
        )

        if channels_mv:
            logger.debug(f"Raw channels_mv keys: {list(channels_mv.keys())}")

        if channels_mv is None or time_axis is None:
            logger.warning("No data available to plot")
            return

        if not isinstance(channels_mv, dict):
            logger.warning("channels_mv is not in expected dictionary format")
            return

        logger.debug(
            f"update_waveform_plots: plot_curves exist={len(self.plot_curves)}, plot_widget={self.plot_widget is not None}"
        )

        # Determine which data to plot for postprocessed curves
        # Use new processed data if available, otherwise keep last processed data
        if processed_channels_mv is not None:
            self._last_processed_channels = processed_channels_mv
            self._last_processing_info = processing_info
            logger.debug("Storing new processed channels for display")

        # Use last processed data if we have it, otherwise fall back to raw
        # But filter to only include channels present in current raw data
        if self._last_processed_channels is not None and channels_mv is not None:
            # Filter cached processed data to only include currently active channels
            filtered_processed = {
                ch: data
                for ch, data in self._last_processed_channels.items()
                if ch in channels_mv
            }
            # If filtering removed all channels, clear the cache
            if not filtered_processed:
                self._last_processed_channels = None
                self._last_processing_info = None
                data_to_plot = channels_mv
            else:
                # Update cache with filtered data
                self._last_processed_channels = filtered_processed
                data_to_plot = filtered_processed
        else:
            data_to_plot = (
                self._last_processed_channels
                if self._last_processed_channels is not None
                else channels_mv
            )

        current_processing_info = (
            self._last_processing_info
            if self._last_processing_info is not None
            else processing_info
        )

        if data_to_plot is None:
            logger.warning("No data available to plot (neither processed nor raw)")
            return

        logger.debug(f"data_to_plot keys: {list(data_to_plot.keys())}")
        logger.debug(f"Current plot_curves keys: {list(self.plot_curves.keys())}")

        # Remove curves for channels that are no longer in the data
        # This handles cases where channels are disabled after being enabled
        if self.plot_widget is not None:
            channels_to_remove = []
            for channel in self.plot_curves.keys():
                if channel not in data_to_plot:
                    channels_to_remove.append(channel)

            for channel in channels_to_remove:
                curve = self.plot_curves[channel]
                self.plot_widget.removeItem(curve)
                # Remove from legend if it exists
                if (
                    hasattr(self.plot_widget, "legend")
                    and self.plot_widget.legend is not None
                ):
                    try:
                        self.plot_widget.legend.removeItem(curve)
                    except Exception:
                        pass  # Item might not be in legend
                del self.plot_curves[channel]
                logger.debug(f"Removed curve for disabled channel {channel}")

            # Also remove original curves for disabled channels
            original_channels_to_remove = []
            for channel in self.original_plot_curves.keys():
                if channel not in data_to_plot:
                    original_channels_to_remove.append(channel)

            for channel in original_channels_to_remove:
                curve = self.original_plot_curves[channel]
                self.plot_widget.removeItem(curve)
                # Remove from legend if it exists
                if (
                    hasattr(self.plot_widget, "legend")
                    and self.plot_widget.legend is not None
                ):
                    try:
                        self.plot_widget.legend.removeItem(curve)
                    except Exception:
                        pass  # Item might not be in legend
                del self.original_plot_curves[channel]
                logger.debug(f"Removed original curve for disabled channel {channel}")

        # Check if we need to create curves for new channels
        # This handles cases where channels are added after initial setup
        need_new_curves = False
        if self.plot_widget is not None:
            for channel in data_to_plot.keys():
                if channel not in self.plot_curves:
                    need_new_curves = True
                    break

        # Initialize curves if this is the first time or if new channels were added
        if (not self.plot_curves or need_new_curves) and self.plot_widget is not None:
            if need_new_curves and self.plot_curves:
                logger.debug("update_waveform_plots: Adding curves for new channels")
            else:
                logger.debug("update_waveform_plots: Creating new curves")
            colors = [
                pg.mkPen(color="r", width=1),
                pg.mkPen(color="g", width=1),
                pg.mkPen(color="b", width=1),
                pg.mkPen(color="c", width=1),
                pg.mkPen(color="m", width=1),
                pg.mkPen(color="y", width=1),
            ]
            # Store mapping of channel to color index for original curves
            channel_color_map = {}
            # Start color index from number of existing curves to maintain color continuity
            color_index = len(self.plot_curves)

            # Create curves once for each channel in data_to_plot
            for channel, data in data_to_plot.items():
                # Skip if curve already exists for this channel
                if channel in self.plot_curves:
                    # Update the color map for existing channels
                    # Find the color index by counting existing curves before this one
                    existing_channel_list = list(self.plot_curves.keys())
                    if channel in existing_channel_list:
                        channel_color_map[channel] = existing_channel_list.index(
                            channel
                        )
                    continue

                if hasattr(data, "shape"):
                    channel_name = (
                        f"Channel {channel.name}"
                        if hasattr(channel, "name")
                        else f"Channel {channel}"
                    )

                    # Check if postprocessing is applied to this channel
                    if current_processing_info and channel in current_processing_info:
                        info = current_processing_info[channel]
                        if info.get("type") != "none" and info.get("ready"):
                            postprocess_type = info["type"].replace("_", " ").title()
                            window_size = info.get("window_size", "?")
                            channel_name += f" ({postprocess_type}, N={window_size})"

                    curve = self.plot_widget.plot(
                        pen=colors[color_index % len(colors)], name=channel_name
                    )
                    self.plot_curves[channel] = curve
                    channel_color_map[channel] = color_index % len(colors)
                    color_index += 1

            # Create curves for original waveforms if requested and postprocessing is applied
            if show_original and original_channels_mv:
                for channel, data in original_channels_mv.items():
                    # Skip if original curve already exists
                    if channel in self.original_plot_curves:
                        continue

                    if hasattr(data, "shape") and channel in data_to_plot:
                        # Only show original if this channel has postprocessing enabled
                        if (
                            current_processing_info
                            and channel in current_processing_info
                        ):
                            info = current_processing_info[channel]
                            if info.get("type") == "none":
                                # Skip original curve for channels without postprocessing
                                continue

                        channel_name = (
                            f"Channel {channel.name} (Original)"
                            if hasattr(channel, "name")
                            else f"Channel {channel} (Original)"
                        )
                        # Use same color as processed curve but with dashed style and lower alpha
                        if channel in channel_color_map:
                            base_pen = colors[channel_color_map[channel]]
                            color = base_pen.color()
                            # Set alpha to 128 (50% transparency)
                            color.setAlpha(128)
                            orig_pen = pg.mkPen(
                                color=color,
                                width=1,
                                # style=pg.QtCore.Qt.PenStyle.DashLine,
                            )
                        else:
                            # Fallback if color mapping not found
                            orig_pen = pg.mkPen(
                                color="gray",
                                width=1,
                                # style=pg.QtCore.Qt.PenStyle.DashLine,
                            )
                        curve = self.plot_widget.plot(pen=orig_pen, name=channel_name)
                        self.original_plot_curves[channel] = curve

            logger.debug(
                f"update_waveform_plots: Created {len(self.plot_curves)} curves + {len(self.original_plot_curves)} original curves"
            )

        # Update existing curves with new data
        logger.debug(f"update_waveform_plots: Updating {len(data_to_plot)} channels")
        for channel, data in data_to_plot.items():
            logger.debug(
                f"update_waveform_plots: Processing channel {channel}, shape={data.shape if hasattr(data, 'shape') else 'no shape'}, segment_index={segment_index}"
            )
            if hasattr(data, "shape") and segment_index < data.shape[0]:
                if channel in self.plot_curves:
                    # Get data for the specified segment
                    y_data = self._plot_values(data[segment_index, :], channel, scope_scales)
                    x_data = time_axis

                    # Update curve label if postprocessing info has changed
                    if current_processing_info and channel in current_processing_info:
                        info = current_processing_info[channel]
                        channel_name = (
                            f"Channel {channel.name}"
                            if hasattr(channel, "name")
                            else f"Channel {channel}"
                        )
                        if info.get("type") != "none" and info.get("ready"):
                            postprocess_type = info["type"].replace("_", " ").title()
                            window_size = info.get("window_size", "?")
                            channel_name += f" ({postprocess_type}, N={window_size})"

                        # Update curve name in legend
                        curve = self.plot_curves[channel]
                        if (
                            hasattr(curve, "opts")
                            and curve.opts.get("name") != channel_name
                        ):
                            curve.opts["name"] = channel_name
                            # Update legend if it exists
                            if (
                                self.plot_widget is not None
                                and hasattr(self.plot_widget, "legend")
                                and self.plot_widget.legend is not None
                            ):
                                self.plot_widget.legend.removeItem(curve)
                                self.plot_widget.legend.addItem(curve, channel_name)

                    logger.debug(
                        f"update_waveform_plots: Setting data for {channel}: x_len={len(x_data)}, y_len={len(y_data)}"
                    )
                    # Update curve data using setData
                    self.plot_curves[channel].setData(x_data, y_data)
                    logger.debug(
                        f"update_waveform_plots: setData completed for {channel}"
                    )
                else:
                    logger.warning(
                        f"update_waveform_plots: Channel {channel} not in plot_curves!"
                    )
            else:
                logger.warning(
                    f"update_waveform_plots: Skipping channel {channel} - shape issue or segment out of range"
                )

        # Update original waveform curves if requested
        if show_original and original_channels_mv:
            logger.debug(
                f"update_waveform_plots: Updating {len(original_channels_mv)} original channels"
            )
            for channel, data in original_channels_mv.items():
                if (
                    hasattr(data, "shape")
                    and segment_index < data.shape[0]
                    and channel in self.original_plot_curves
                ):
                    y_data = self._plot_values(data[segment_index, :], channel, scope_scales)
                    x_data = time_axis
                    self.original_plot_curves[channel].setData(x_data, y_data)
                    logger.debug(
                        f"update_waveform_plots: Updated original curve for {channel}"
                    )

        if self._update_counter == 0 and self.plot_widget is not None:
            # Fit the full data before enabling display optimizations. They use
            # the current view and can hide the first trace in the default range.
            self.plot_widget.autoRange()
            self.plot_widget.enableAutoRange("xy", False)
            self.plot_widget.setDownsampling(auto=True, mode="peak")
            self.plot_widget.setClipToView(True)

        self._update_counter += 1

    def plot_all_segments(self, channels_mv, time_axis):
        """Plot all segments in separate subplots

        Args:
            channels_mv (dict): Channel data dictionary
            time_axis (np.array): Time axis data
        """
        if channels_mv is None or time_axis is None:
            logger.warning("No data available to plot")
            return

        self._ensure_qt_app()

        # Get first channel to determine number of segments
        first_channel = next(iter(channels_mv.keys()))
        num_segments = channels_mv[first_channel].shape[0]

        # Create a window with subplots for all segments
        win = pg.GraphicsLayoutWidget(show=True, title="All Segments")
        win.resize(800, 650)

        colors = ["r", "g", "b", "c", "m", "y"]

        for segment_idx in range(num_segments):
            plot = win.addPlot(title=f"Segment {segment_idx}")
            self._set_waveform_axis_units(plot)
            plot.addLegend()
            plot.setDownsampling(auto=True, mode="peak")
            plot.setClipToView(True)

            color_idx = 0
            for channel, data in channels_mv.items():
                channel_name = (
                    f"Channel {channel.name}"
                    if hasattr(channel, "name")
                    else f"Channel {channel}"
                )
                plot.plot(
                    time_axis,
                    data[segment_idx, :],
                    pen=pg.mkPen(color=colors[color_idx % len(colors)], width=1),
                    name=channel_name,
                )
                color_idx += 1

            # Move to next row after every 2 plots
            if (segment_idx + 1) % 2 == 0:
                win.nextRow()

    def _on_main_window_close(self, a0):
        """Handle main window close event and save geometry

        Args:
            a0: Close event
        """
        if self.main_plot_window is not None:
            # Save window geometry
            self.settings.setValue(
                "mainWindow/geometry", self.main_plot_window.saveGeometry()
            )
            logger.debug("Main window geometry saved")
        a0.accept()

    def close_plots(self):
        """Close all plot windows"""
        if self.main_plot_window is not None:
            self.main_plot_window.close()
            self.main_plot_window = None
            self.plot_widget = None
            self.plot_curves = {}
            self.original_plot_curves = {}
            self._update_counter = 0
            self._scope_grid = None

    def exec_app(self):
        """Execute Qt application event loop

        Returns:
            int: Application exit code
        """
        if self.qt_app is not None:
            return self.qt_app.exec()
        return 0

    def quit_app(self):
        """Quit Qt application"""
        if self.qt_app is not None:
            self.qt_app.quit()

    def update_status_bar(self, capture_data):
        """Display acquisition and averaging status for a capture."""
        if self.status_bar is not None:
            self.status_bar.showMessage(build_acquisition_status(capture_data))

    def reset_plot_view(self):
        """Reset plot view to auto-range for both axes (call when settings change)"""
        if self.plot_widget is not None:
            # Remove existing curves from the plot widget
            for channel, curve in self.plot_curves.items():
                self.plot_widget.removeItem(curve)
                logger.debug(f"Removed curve for channel {channel}")

            # Remove original curves
            for channel, curve in self.original_plot_curves.items():
                self.plot_widget.removeItem(curve)
                logger.debug(f"Removed original curve for channel {channel}")

            # Clear existing curves so they're recreated with correct channel keys
            self.plot_curves.clear()
            self.original_plot_curves.clear()
            logger.debug("Plot curves cleared - will be recreated on next update")

            # Clear stored postprocessed data
            self._last_processed_channels = None
            self._last_processing_info = None

            # Prepare the next data set for a full-range fit.
            self.plot_widget.setDownsampling(auto=False)
            self.plot_widget.setClipToView(False)
            # Re-enable auto-range for next update to adjust to new data dimensions
            self.plot_widget.enableAutoRange("xy", True)
            self._update_counter = 0  # Reset counter so auto-range works on next update
            logger.debug("Plot view reset - auto-range enabled, counter reset")

    @staticmethod
    def _plot_values(values_mv, channel, scope_scales):
        if scope_scales is None:
            return values_mv
        settings = scope_scales[channel]
        # Restore the scope's screen position while keeping physical voltages in MQTT.
        return (values_mv * 1e-3 + settings["offset_volts"]) / settings["volts_per_div"]

    def display_capture(self, capture_data, segment_index=0):
        """Render a capture, retaining the previous plot while averaging."""
        processing_info = capture_data.get("processing_info") or {}
        if not is_simple_average_pending(processing_info):
            scope_scales = None
            grid = None
            if capture_data.get("follow_scope"):
                scope_scales = {
                    settings["channel"]: settings for settings in capture_data["channel_settings"]
                }
                grid = (
                    capture_data["seconds_per_div"],
                    capture_data["trigger_delay_s"],
                    capture_data["raw_sample_count"],
                    capture_data["actual_sample_rate"],
                )
            if grid != self._scope_grid and self.plot_widget is not None:
                self.reset_plot_view()
                if grid is None:
                    self._set_waveform_axis_units(self.plot_widget)
                else:
                    axis = self.plot_widget.getAxis("left")
                    axis.enableAutoSIPrefix(False)
                    axis.setScale(1)
                    self.plot_widget.setLabel("left", "Vertical position", "div")
                self._scope_grid = grid

            self.update_waveform_plots(
                channels_mv=capture_data["channels_mv"],
                time_axis=capture_data["time_axis"],
                segment_index=segment_index,
                processed_channels_mv=capture_data.get("processed_channels_mv") or {},
                original_channels_mv=capture_data.get("original_channels_mv") or {},
                processing_info=processing_info,
                show_original=capture_data.get("show_original", False),
                scope_scales=scope_scales,
            )
        self.update_status_bar(capture_data)
