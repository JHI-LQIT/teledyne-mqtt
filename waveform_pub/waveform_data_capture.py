"""Acquisition worker and shared waveform processing."""

import logging
import queue
import threading
from concurrent.futures import Future

from waveform_postprocessor import WaveformPostprocessor
from waveform_sources import CaptureInterrupted, SyntheticSource

logger = logging.getLogger(__name__)

CAPTURE_IDLE_TIMEOUT_SECONDS = 10.0


class WaveformDataCaptureThread(threading.Thread):
    """Run source operations and capture cycles in one thread."""

    def __init__(self, manager):
        super().__init__(name="waveform-acquisition", daemon=True)
        self.manager = manager
        self.commands = queue.Queue()
        self.data_queue = queue.Queue()
        self.interrupt = threading.Event()
        self._interrupt_lock = threading.Lock()
        self._pending_interrupts = 0
        self.capturing = False
        self.shutdown = False

    def enqueue(self, command, future, *, interrupt=False):
        with self._interrupt_lock:
            if interrupt:
                self._pending_interrupts += 1
                self.interrupt.set()
            self.commands.put((command, future, interrupt))

    def run(self):
        while not self.shutdown:
            try:
                command, future, interrupts_capture = self.commands.get(
                    timeout=0.01 if self.capturing else None
                )
            except queue.Empty:
                if not self.data_queue.empty():
                    continue
                try:
                    frame = self.manager._capture_once(self.interrupt)
                except CaptureInterrupted:
                    continue
                except Exception as exc:
                    logger.exception("Waveform capture failed")
                    self.data_queue.put(("error", str(exc)))
                    self.capturing = False
                else:
                    self.data_queue.put(("data", frame))
                continue

            if interrupts_capture:
                with self._interrupt_lock:
                    self._pending_interrupts -= 1
                    if not self._pending_interrupts:
                        self.interrupt.clear()

            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(command())
                except Exception as exc:
                    future.set_exception(exc)


class WaveformDataCaptureManager:
    """Submit acquisition commands and consume completed waveform snapshots."""

    def __init__(self, scope_settings, *, source):
        self.scope_settings = scope_settings
        self.source = source
        self._use_synthetic_data = isinstance(source, SyntheticSource)
        self._last_grid = None
        self._latest_capture = None
        self._lifecycle_lock = threading.Lock()
        self._worker = None
        self._close_future = None
        self.postprocessor = WaveformPostprocessor(
            scope_settings.get("postprocessing", {})
        )

    def _submit(self, command, *, interrupt=False):
        future = Future()
        with self._lifecycle_lock:
            if self._close_future is not None:
                raise RuntimeError("The acquisition worker is shutting down")
            if self._worker is None:
                self._worker = WaveformDataCaptureThread(self)
                self._worker.start()
            self._worker.enqueue(command, future, interrupt=interrupt)

        # Cancel a command that never reached the worker. Once started, wait for
        # its outcome so a caller cannot roll back while it is still executing.
        try:
            return future.result(timeout=CAPTURE_IDLE_TIMEOUT_SECONDS)
        except TimeoutError:
            if future.done() or not future.cancel():
                return future.result()
            raise TimeoutError("Timed out waiting for the active capture cycle") from None

    def update_settings(self, new_settings):
        self._submit(lambda: self._update_settings(new_settings))

    def _update_settings(self, new_settings):
        self.scope_settings = new_settings
        self.source.update_settings(new_settings)
        self.postprocessor.update_config(new_settings.get("postprocessing", {}))

    def reset_averaging(self):
        self._submit(self.postprocessor.reset)

    def open_device(self):
        self._submit(lambda: self.source.open())

    def configure_device(self):
        self._submit(lambda: self.source.configure())

    def run_capture_cycle(self):
        """Request one capture without accessing the source from the caller."""
        return self._submit(lambda: self._capture_once(self._worker.interrupt))

    def _capture_once(self, interrupt):
        frame = self.source.capture(interrupt)
        axis = frame["time_axis"]
        grid = (
            len(axis), axis[0], frame["actual_sample_rate"], frame.get("transfer_stride", 1),
            tuple(frame["channels_mv"]),
        )
        if grid != self._last_grid:
            self.postprocessor.reset()
            self._last_grid = grid
        ratio = min(
            len(axis), self.scope_settings.get("postprocessing", {}).get("down_sample_ratio", 0)
        )
        if ratio > 1:
            groups = len(axis) // ratio
            stop = groups * ratio
            frame["time_axis"] = axis[:stop].reshape(groups, ratio).mean(axis=-1)
            frame["channels_mv"] = {
                name: data[:, :stop].reshape(data.shape[0], groups, ratio).mean(axis=-1)
                for name, data in frame["channels_mv"].items()
            }
        frame["down_sample_ratio"] = max(1, ratio)
        processed, original, info = self.postprocessor.process_waveforms(
            frame["channels_mv"]
        )
        frame.update(
            processed_channels_mv=processed,
            original_channels_mv=original,
            processing_info=info,
            show_original=self.postprocessor.should_show_original(),
        )
        self._latest_capture = frame
        return frame

    def start_continuous_capture(self):
        self._submit(lambda: self._set_capturing(True))

    def stop_continuous_capture(self):
        with self._lifecycle_lock:
            if self._worker is None or self._close_future is not None:
                return
        self._submit(lambda: self._set_capturing(False), interrupt=True)

    def _set_capturing(self, capturing):
        self._worker.capturing = capturing

    def get_captured_data(self):
        worker = self._worker
        if worker is not None:
            try:
                return worker.data_queue.get_nowait()
            except queue.Empty:
                pass
        return None

    def close_device(self):
        """Interrupt a trigger wait and close the source on its owner thread."""
        with self._lifecycle_lock:
            worker = self._worker
            if worker is None:
                return
            if self._close_future is None:
                self._close_future = Future()
                worker.enqueue(self._close_source, self._close_future, interrupt=True)
            future = self._close_future

        try:
            future.result(timeout=CAPTURE_IDLE_TIMEOUT_SECONDS)
        finally:
            if future.done():
                worker.join(timeout=CAPTURE_IDLE_TIMEOUT_SECONDS)

    def _close_source(self):
        try:
            self.source.close()
        finally:
            self._worker.shutdown = True

    def get_current_data(self):
        """Return the most recent capture snapshot."""
        capture = self._latest_capture or {}
        return {
            key: capture.get(key)
            for key in ("channels_mv", "time_axis", "actual_sample_rate")
        }
