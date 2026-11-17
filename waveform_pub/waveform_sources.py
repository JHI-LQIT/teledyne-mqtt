"""Raw waveform sources for the Teledyne T3DSO1204 and simulation."""

import logging
import math
import re
import time

import numpy as np
import pyvisa

from scope_settings import CHANNELS

logger = logging.getLogger(__name__)
_CAPTURE_READY = 1
_VOLTAGE_CODES_PER_DIVISION = 25
_HORIZONTAL_DIVISIONS = 14
_PREFIXES = {"": 1, "n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}
_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?"


class CaptureInterrupted(Exception):
    """A capture stopped before delivering a waveform."""


def _parse_quantity(reply, unit, *, positive=False):
    """Parse T3DSO1000(A) scientific notation, with or without the SCPI header."""
    fields = reply.split()
    token = fields[-1] if fields else ""
    match = re.fullmatch(rf"({_NUMBER})([numkKMG]?)(?i:{re.escape(unit)})?", token)
    if match:
        value = float(match[1]) * _PREFIXES[match[2]]
        if math.isfinite(value) and (not positive or value > 0):
            return value
    raise ValueError(f"Unexpected scope quantity for {unit}: {reply!r}")


def _decode_dat2(reply, max_points):
    """Read the announced byte count even if the ADC data contains LF bytes."""
    marker = reply.find(b"#", 0, 64)
    if marker < 0 or len(reply) < marker + 2:
        raise ValueError("Scope did not return a DAT2 binary block")
    count_digit = reply[marker + 1] - ord("0")
    start = marker + 2 + count_digit
    count_bytes = reply[marker + 2 : start]
    if not 1 <= count_digit <= 9 or len(count_bytes) != count_digit or not count_bytes.isdigit():
        raise ValueError("Invalid DAT2 length field")
    count = int(count_bytes)
    end = start + count
    if not 0 < count <= max_points or end > len(reply):
        raise ValueError(f"Invalid or truncated DAT2 data ({count} points, limit {max_points})")
    if reply[end:] not in (b"", b"\n", b"\n\n", b"\r\n"):
        raise ValueError("Unexpected bytes after the DAT2 block")
    return np.frombuffer(reply, dtype=np.uint8, count=count, offset=start)


class TeledyneSource:
    """Acquire coherent analog frames over a VXI-11 LAN session."""

    def __init__(self, scope_settings, host):
        self.scope_settings = scope_settings
        self.host = host
        self.manager = None
        self.scope = None
        self._configured = False

    def update_settings(self, settings):
        self.scope_settings = settings

    def open(self):
        self.manager = pyvisa.ResourceManager("@py")
        try:
            self.scope = self.manager.open_resource(
                f"TCPIP0::{self.host}::inst0::INSTR",
                open_timeout=3000,
                timeout=3000,
                write_termination="\n",
                read_termination=None,
                chunk_size=256 * 1024,
            )
            identity = self.scope.query("*IDN?").strip()
            fields = identity.split(",")
            if len(fields) < 2 or fields[1].strip().upper() != "T3DSO1204":
                raise RuntimeError(f"Expected a T3DSO1204, received {identity!r}")
            logger.info("Connected to %s", identity)
            self.configure()
        except Exception:
            try:
                self.close()
            except Exception:
                logger.warning("Could not close the failed scope connection", exc_info=True)
            raise

    def _selected_channels(self):
        return [channel["channel"] for channel in self.scope_settings["channels"]]

    def configure(self):
        for name in self._selected_channels():
            trace = CHANNELS[name]
            if self.scope.query(f"{trace}:TRA?").split()[-1].upper() != "ON":
                raise ValueError(f"Enable {trace} on the oscilloscope before selecting it")
            self._check_voltage_unit(trace)
        sampling = self.scope_settings["sampling"]
        stride = sampling["transfer_stride"]
        scope_stride = stride if stride > 1 else 0
        self.scope.write("STOP")
        self._configured = True
        self.scope.write(f"WFSU SP,{scope_stride},NP,{sampling['num_samples']},FP,0")
        # Ensure the command was accepted before publishing a waveform with this time grid.
        actual = self.scope.query("WFSU?")
        fields = dict(re.findall(r"\b(SP|NP|FP)\s*,\s*(\d+)", actual.upper()))
        if (
            int(fields.get("SP", -1)) not in ({0, 1} if stride == 1 else {stride})
            or int(fields.get("NP", -1)) != sampling["num_samples"]
            or fields.get("FP") != "0"
        ):
            raise ValueError(f"Scope rejected the waveform transfer settings: {actual.strip()!r}")

    def _check_voltage_unit(self, trace):
        if self.scope.query(f"{trace}:UNIT?").split()[-1].upper() != "V":
            raise ValueError(f"Set {trace} to volts on the oscilloscope")

    @staticmethod
    def _check_interrupt(interrupt):
        if interrupt is not None and interrupt.is_set():
            raise CaptureInterrupted

    def _wait_for_capture(self, interrupt):
        while True:
            self._check_interrupt(interrupt)
            if int(self.scope.query("INR?").split()[-1]) & _CAPTURE_READY:
                return time.time_ns() // 1000
            if interrupt is None:
                time.sleep(0.025)
            elif interrupt.wait(0.025):
                raise CaptureInterrupted

    def capture(self, interrupt=None):
        self._check_interrupt(interrupt)
        mode = self.scope_settings["trigger_settings"]["mode"]
        self.scope.query("INR?")  # Clear completion from the previous sweep.
        self.scope.write("TRMD AUTO" if mode == "auto" else "TRMD SINGLE")
        try:
            timestamp_us = self._wait_for_capture(interrupt)
        finally:
            self.scope.write("STOP")
        self._check_interrupt(interrupt)

        rate = _parse_quantity(self.scope.query("SARA?"), "Sa/s", positive=True)
        scale = _parse_quantity(self.scope.query("TDIV?"), "S", positive=True)
        delay = _parse_quantity(self.scope.query("TRDL?"), "S")
        sampling = self.scope_settings["sampling"]
        voltages = {}
        channel_settings = []
        for name in self._selected_channels():
            self._check_interrupt(interrupt)
            trace = CHANNELS[name]
            self._check_voltage_unit(trace)
            vdiv = _parse_quantity(self.scope.query(f"{trace}:VDIV?"), "V", positive=True)
            offset = _parse_quantity(self.scope.query(f"{trace}:OFST?"), "V")
            self.scope.write(f"{trace}:WF? DAT2")
            codes = _decode_dat2(self.scope.read_raw(), sampling["num_samples"])
            # The T3DSO1000(A) DAT2 manual specifies subtracting 255 for codes >127.
            values = codes.astype(np.float64)
            values[codes > 127] -= 255
            voltages[name] = ((values * (vdiv / _VOLTAGE_CODES_PER_DIVISION) - offset) * 1000)[None, :]
            channel_settings.append({"channel": name, "volts_per_div": vdiv})
        lengths = {samples.shape[-1] for samples in voltages.values()}
        if len(lengths) != 1:
            raise ValueError("Analog channels returned different waveform lengths")
        points = lengths.pop()
        stride = sampling["transfer_stride"]
        start = delay - scale * (_HORIZONTAL_DIVISIONS / 2)
        return {
            "channels_mv": voltages,
            "time_axis": start + np.arange(points) * (stride / rate),
            "actual_sample_rate": rate,
            "transfer_stride": stride,
            "raw_sample_count": points,
            "resolution_bits": 8,
            "channel_settings": channel_settings,
            "timestamp_us": timestamp_us,
        }

    def close(self):
        try:
            if self.scope is not None:
                try:
                    if self._configured:
                        self.scope.write("STOP")
                except pyvisa.errors.VisaIOError:
                    logger.warning("Could not stop the oscilloscope before disconnecting", exc_info=True)
                finally:
                    self.scope.close()
        finally:
            self.scope = None
            self._configured = False
            if self.manager is not None:
                self.manager.close()
                self.manager = None


class SyntheticSource:
    """Generate analog channel data for an explicit simulation session."""

    def __init__(self, scope_settings):
        self.update_settings(scope_settings)

    def update_settings(self, settings):
        self.scope_settings = settings

    def open(self):
        pass

    def configure(self):
        pass

    def close(self):
        pass

    def capture(self, interrupt=None):
        if interrupt is None:
            time.sleep(0.05)
        elif interrupt.wait(0.05):
            raise CaptureInterrupted
        sampling = self.scope_settings["sampling"]
        rate = 1_000_000
        stride = sampling["transfer_stride"]
        points = sampling["num_samples"]
        axis = np.arange(points) * (stride / rate)
        channels = self.scope_settings["channels"]
        return {
            "channels_mv": {
                channel["channel"]: (50 * np.sin(2 * np.pi * 1000 * axis + i))[None, :]
                for i, channel in enumerate(channels)
            },
            "time_axis": axis,
            "actual_sample_rate": rate,
            "transfer_stride": stride,
            "raw_sample_count": points,
            "synthetic": True,
            "channel_settings": channels,
            "timestamp_us": time.time_ns() // 1000,
        }
