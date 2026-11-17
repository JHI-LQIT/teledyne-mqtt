"""
Waveform Publisher orchestrator for Teledyne data publishing
"""

import logging
from functools import partial
from threading import Event, RLock, Thread
from time import monotonic

from pyqtgraph.Qt import QtCore
from scope_settings import prepare_settings_update, save_scope_settings
from waveform_data_capture import WaveformDataCaptureManager
from waveform_mqtt_handler import WaveformMQTTHandler
from waveform_plot_manager import WaveformPlotManager
from waveform_postprocessor import is_simple_average_pending
from waveform_settings_dialog import MQTTSettingsDialog, ScopeSettingsDialog

logger = logging.getLogger(__name__)


class WaveformPublisher:
    """
    Orchestrates waveform publishing using separate components for:
    - MQTT publishing (WaveformMQTTHandler)
    - Data capture (WaveformDataCaptureManager)
    - Plotting (WaveformPlotManager)
    """

    def __init__(
        self,
        mqtt_broker,
        mqtt_port,
        mqtt_topic,
        scope_settings,
        config_topic=None,
        *,
        source=None,
        settings_topics=None,
    ):
        """Initialize waveform publisher with component managers

        Args:
            mqtt_broker (str): MQTT broker hostname
            mqtt_port (int): MQTT broker port
            mqtt_topic (str): MQTT topic for publishing waveform data
            scope_settings (dict): Scope configuration settings
            config_topic (str): MQTT topic for publishing configuration changes
        """
        # Initialize component managers
        self.mqtt_handler = WaveformMQTTHandler(
            mqtt_broker, mqtt_port, mqtt_topic, scope_settings, config_topic,
            settings_topics=settings_topics,
        )
        self.data_capture = WaveformDataCaptureManager(scope_settings, source=source)
        self.plot_manager = WaveformPlotManager(
            settings_actions={
                "MQTT Settings": (
                    self._open_mqtt_settings_dialog,
                    "Configure the MQTT broker, port, and topics for the next restart.",
                ),
                "Scope Settings": (
                    partial(self._open_settings_dialog, "scope"),
                    "Select scope channels, transfer size, and trigger sweep mode.",
                ),
                "Postprocessing": (
                    partial(self._open_settings_dialog, "postprocessing"),
                    "Configure downsampling, waveform averaging, and the original trace overlay.",
                ),
                "Publishing": (
                    partial(self._open_settings_dialog, "publishing"),
                    "Choose which acquired channels are published to MQTT.",
                ),
            },
            reset_callback=self._request_averaging_reset,
        )

        # Set up callbacks
        self.mqtt_handler.connected_callback = self._publish_applied_config
        self.mqtt_handler.publish_waveform_callback = self.publish_waveform
        self.mqtt_handler.get_settings_callback = self.get_scope_settings
        self.mqtt_handler.set_settings_callback = self.set_scope_settings
        self.mqtt_handler.reset_callback = self.reset_averaging

        # Core state
        self._running = False
        self._last_capture_data = None
        self._new_data_available = False

        self.preview_timer = None
        self._preview_enabled = False
        self._pending_preview = None
        self._capture_error = None
        self._capture_generation = 0
        self._delivery_stop = Event()
        self._delivery_thread = None

        # Settings dialog
        self.settings_dialogs = {}
        self.current_scope_settings = scope_settings
        self._settings_lock = RLock()
        self._closing = False

    def publish_waveform(self, sender_payload=None):
        """Publish waveform on demand (for MQTT request)

        Args:
            sender_payload: Payload from the requester

        Returns:
            bool: True if successful
        """
        with self._settings_lock:
            if not self._running or not self._new_data_available:
                return False
            capture = self._last_capture_data
            generation = self._capture_generation
            settings = self.current_scope_settings
            self._new_data_available = False

        return self._publish_capture(capture, settings, generation, sender_payload)

    def connect_mqtt(self):
        """Connect to MQTT broker

        Returns:
            bool: True if successful, False otherwise
        """
        return self.mqtt_handler.connect()

    def _publish_applied_config(self):
        with self._settings_lock:
            if not self._closing:
                self.mqtt_handler.publish_config(self.current_scope_settings)

    def open_device(self):
        """Open and configure Teledyne device"""
        self.data_capture.open_device()

    def _start_delivery(self, data_poll_interval_ms, enable_plotting):
        self.plot_manager._ensure_qt_app()
        self._preview_enabled = enable_plotting
        if self.preview_timer is None:
            self.preview_timer = QtCore.QTimer()
            self.preview_timer.timeout.connect(self._update_preview)
        self.preview_timer.start(50)

        self._delivery_stop.clear()
        self._delivery_thread = Thread(
            target=self._deliver_captures,
            args=(max(1, data_poll_interval_ms) / 1000,),
            name="waveform-delivery",
        )
        self._delivery_thread.start()

    def _deliver_captures(self, poll_interval):
        while not self._delivery_stop.is_set():
            poll_started = monotonic()
            with self._settings_lock:
                if not self._running:
                    return
                event = self.data_capture.get_captured_data()
                if event is not None:
                    event_type, payload = event
                    if event_type == "error":
                        self._capture_error = payload
                        return
                    if event_type == "data":
                        self._last_capture_data = payload
                        self._new_data_available = True
                        # Keep a completed average until Qt has rendered it.
                        if self._preview_enabled and (
                            self._pending_preview is None
                            or is_simple_average_pending(
                                self._pending_preview.get("processing_info")
                            )
                            or not is_simple_average_pending(
                                payload.get("processing_info")
                            )
                        ):
                            self._pending_preview = payload
                        generation = self._capture_generation
                        settings = self.current_scope_settings

            if event is not None and event_type == "data":
                self._publish_capture(payload, settings, generation)
            elapsed = monotonic() - poll_started
            self._delivery_stop.wait(max(0, poll_interval - elapsed))

    def _publish_capture(self, capture, settings, generation, sender_payload=None):
        def submit(payload):
            # Encoding can overlap settings changes. Check the generation at
            # submission so old waveforms cannot follow the new configuration.
            with self._settings_lock:
                if not self._running or generation != self._capture_generation:
                    return False
                return self.mqtt_handler.submit_waveform(payload)

        return self.mqtt_handler.publish_waveform(
            capture, sender_payload=sender_payload, scope_settings=settings, submit=submit
        )

    def _update_preview(self):
        # A settings command can hold this lock while waiting for the scope.
        # Skip this redraw rather than waiting on the Qt event thread.
        if not self._settings_lock.acquire(blocking=False):
            return
        try:
            error = self._capture_error
            capture = self._pending_preview
            self._capture_error = None
            self._pending_preview = None
        finally:
            self._settings_lock.release()

        if error is not None:
            self._on_capture_error(error)
        elif capture is not None:
            self.plot_manager.display_capture(capture)

    def _on_capture_error(self, error_message):
        """Handle capture error

        Args:
            error_message (str): Error message from capture
        """
        logger.error(f"Data capture error: {error_message}")
        self._stop_publishing()
        if self.plot_manager.status_bar is not None:
            self.plot_manager.status_bar.showMessage(f"Capture stopped: {error_message}")
        app = self.plot_manager.get_qt_app()
        if not self._preview_enabled and app is not None:
            app.exit(1)

    def setup_plotting(self, title="Teledyne MQTT", title_suffix=None):
        """Setup plotting window

        Args:
            title (str): Window title
            title_suffix (str): Optional error message to display in title

        Returns:
            pg.PlotWidget: Main plot widget
        """
        if title_suffix:
            title = f"{title} — {title_suffix}"
        return self.plot_manager.setup_plots(title)

    def plot_single_capture(self, segment_index=0):
        """Capture and plot a single measurement

        Args:
            segment_index (int): Which segment to plot
        """
        try:
            # Run single capture cycle
            capture_data = self.data_capture.run_capture_cycle()

            self.plot_manager.display_capture(capture_data, segment_index)

            return capture_data

        except Exception as e:
            logger.error(f"Error in single capture: {e}")
            return None

    def run_continuous_publishing(
        self,
        data_poll_interval_ms=10,
        enable_plotting=False,
        title_suffix=None,
    ):
        """Run continuous waveform publishing using Qt event loop

        Args:
            data_poll_interval_ms (int): Data polling interval in milliseconds
            enable_plotting (bool): Whether to enable real-time plotting
            title_suffix (str): Optional error message to display in window title
        """
        logger.info("Starting continuous waveform publishing...")
        logger.info(
            f"Config: poll_interval={data_poll_interval_ms}ms, plotting={enable_plotting}"
        )
        logger.info("Press Ctrl+C to stop or close the plot window")

        # Set running flag
        try:
            # Start data capture in background thread
            logger.info("Starting continuous data capture...")
            with self._settings_lock:
                self.data_capture.start_continuous_capture()
                self._running = True
            logger.info("Data capture thread started")

            # Setup plot window if plotting is enabled (this creates Qt app)
            if enable_plotting:
                logger.info("Setting up plotting window...")
                self.setup_plotting(title_suffix=title_suffix)
                logger.info("Plotting window setup complete")

                # Handle window close
                qt_app = self.plot_manager.get_qt_app()
                if qt_app is not None:
                    qt_app.aboutToQuit.connect(self._stop_publishing)
                    logger.info("Connected window close handler")

            self._start_delivery(data_poll_interval_ms, enable_plotting)

            logger.info("Publishing started. Use Ctrl+C or close plot window to stop.")

            # Start Qt event loop - this will block until app.quit() is called
            logger.info("Starting Qt event loop...")
            return self.plot_manager.exec_app()

        except KeyboardInterrupt:
            logger.info("Stopping publishing...")
        finally:
            self._stop_publishing()

    def _stop_publishing(self, *, stop_capture=True):
        """Stop the continuous publishing"""
        self._delivery_stop.set()
        if self.preview_timer is not None:
            self.preview_timer.stop()

        with self._settings_lock:
            self._running = False
            self._capture_generation += 1
            self._pending_preview = None
            self._capture_error = None
            self._new_data_available = False

        if self._delivery_thread is not None:
            self._delivery_thread.join()
            self._delivery_thread = None

        if stop_capture:
            with self._settings_lock:
                self.data_capture.stop_continuous_capture()

    def stop_publishing(self):
        """Stop continuous publishing and quit Qt application"""
        self._stop_publishing()
        self.plot_manager.quit_app()

    def close_device(self):
        """Clean up and close all components"""
        with self._settings_lock:
            self._closing = True
        self._stop_publishing(stop_capture=False)

        try:
            self.data_capture.close_device()
        finally:
            self.mqtt_handler.disconnect()
            self.plot_manager.close_plots()

        logger.info("All components closed successfully")

    def get_current_data(self):
        """Get the most recent capture data

        Returns:
            dict: Current data from data capture manager
        """
        return self.data_capture.get_current_data()

    def get_scope_settings(self):
        """Get current scope settings

        Returns:
            dict: Current scope settings with serializable values
        """
        try:
            import copy

            with self._settings_lock:
                settings = copy.deepcopy(self.current_scope_settings)
            settings_serializable = settings

            logger.info("Settings retrieved successfully")
            return {"success": True, "current_settings": settings_serializable}
        except Exception as e:
            logger.error(f"Error getting scope settings: {e}")
            return {"success": False, "error": str(e)}

    def set_scope_settings(self, new_settings, *, persist=False):
        """Apply a GUI snapshot or MQTT patch through the same capture boundary."""
        with self._settings_lock:
            if self._closing:
                return {"success": False, "error": "The publisher is shutting down"}
            try:
                previous = self.current_scope_settings
                candidate, response = prepare_settings_update(
                    previous, new_settings
                )
                reconfigure = self._requires_device_reconfiguration(candidate, previous)
                if candidate != previous:
                    self._apply_scope_settings(candidate, previous, reconfigure)
                    self.mqtt_handler.publish_config(candidate)
            except Exception as exc:
                logger.exception("Failed to apply scope settings")
                return {"success": False, "error": str(exc)}

            message = (
                "Settings updated and device reconfigured"
                if reconfigure and not self.data_capture._use_synthetic_data
                else "Settings updated (no reconfiguration needed)"
            )
            result = {"success": True, "message": message, "updated_settings": response}
            if persist:
                try:
                    save_scope_settings(self.current_scope_settings)
                except OSError as exc:
                    logger.exception("Settings applied but could not be saved")
                    result["warning"] = f"Settings applied but could not be saved: {exc}"
            return result

    def _apply_scope_settings(self, candidate, previous, reconfigure):
        capture = self.data_capture
        hardware_change = reconfigure and not capture._use_synthetic_data
        was_capturing = self._running
        try:
            capture.stop_continuous_capture()
        except TimeoutError:
            if was_capturing:
                capture.start_continuous_capture()
            raise

        try:
            capture.update_settings(candidate)
            if hardware_change:
                capture.configure_device()
        except Exception:
            try:
                capture.update_settings(previous)
                if hardware_change:
                    capture.configure_device()
            except Exception as recovery_error:
                self._running = False
                raise RuntimeError(
                    "Previous scope configuration could not be restored. "
                    f"Capture stopped: {recovery_error}"
                ) from recovery_error
            if was_capturing:
                capture.start_continuous_capture()
            raise

        # Discard frames captured with the old configuration before switching
        # the MQTT channel filter to the new settings.
        self._discard_queued_captures()
        self.current_scope_settings = candidate
        self.mqtt_handler.update_settings(candidate)
        if was_capturing:
            capture.start_continuous_capture()

    def _discard_queued_captures(self):
        self._capture_generation += 1
        self._pending_preview = None
        while self.data_capture.get_captured_data() is not None:
            pass
        self._last_capture_data = None
        self._new_data_available = False

    def _request_averaging_reset(self):
        if self.mqtt_handler.is_connected() and self.mqtt_handler.publish_reset_command():
            return

        result = self.reset_averaging()
        if not result.get("success"):
            from pyqtgraph.Qt import QtWidgets

            QtWidgets.QMessageBox.warning(
                self.plot_manager.main_plot_window, "Reset Averaging", result["error"]
            )

    def reset_averaging(self):
        """Reset the accumulator between capture cycles without changing settings."""
        with self._settings_lock:
            if self._closing:
                return {"success": False, "error": "The publisher is shutting down"}

            capture = self.data_capture
            try:
                try:
                    capture.stop_continuous_capture()
                    capture.reset_averaging()
                    self._discard_queued_captures()
                finally:
                    if self._running:
                        capture.start_continuous_capture()
            except Exception as exc:
                logger.exception("Failed to reset waveform averaging")
                return {"success": False, "error": str(exc)}
            return self.get_scope_settings()

    @staticmethod
    def _requires_device_reconfiguration(new_settings, old_settings):
        return any(
            new_settings.get(key) != old_settings.get(key)
            for key in ("sampling", "trigger_settings", "channels")
        )

    def _open_mqtt_settings_dialog(self):
        from config_loader import get_config

        dialog = MQTTSettingsDialog(get_config(), self.plot_manager.main_plot_window)
        dialog.exec()
        dialog.deleteLater()

    def _open_settings_dialog(self, section="scope"):
        """Open a dialog backed by the current applied settings."""
        dialog = self.settings_dialogs.get(section)
        if dialog is None:
            dialog = ScopeSettingsDialog(
                parent=self.plot_manager.main_plot_window,
                initial_settings=self.current_scope_settings,
                section=section,
            )
            dialog.settings_applied.connect(partial(self._on_settings_applied, dialog=dialog))
            self.settings_dialogs[section] = dialog
        elif not dialog.isVisible():
            dialog.refresh_settings(self.current_scope_settings)

        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _on_settings_applied(self, new_settings, *, dialog=None):
        """Persist the edited section together with the latest applied settings."""
        if dialog is None:
            result = self.set_scope_settings(new_settings, persist=True)
        else:
            with self._settings_lock:
                candidate = {**self.current_scope_settings, **new_settings}
                result = self.set_scope_settings(candidate, persist=True)
                applied_settings = self.current_scope_settings
            if result.get("error"):
                dialog.refresh_settings(applied_settings)
        message = result.get("error") or result.get("warning")
        if message:
            from pyqtgraph.Qt import QtWidgets

            title = dialog.windowTitle() if dialog is not None else "Scope Settings"
            QtWidgets.QMessageBox.warning(dialog, title, message)
