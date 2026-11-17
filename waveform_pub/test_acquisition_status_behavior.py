"""Tests for formatting status directly from a captured waveform."""

import numpy as np

from acquisition_status import build_acquisition_status


def test_status_uses_capture_timing_and_resolution():
    status = build_acquisition_status({
        "actual_sample_rate": 2e6,
        "resolution_bits": 12,
        "raw_sample_count": 1000,
        "down_sample_ratio": 4,
        "channel_settings": [{"channel": "A", "volts_per_div": 0.5}],
        "channels_mv": {"A": np.zeros((1, 250))},
        "time_axis": np.arange(250),
    })

    assert "Rate: 2 MSPS" in status
    assert "Resolution: 12-bit" in status
    assert "Samples: 250 (1,000 raw)" in status
    assert "Span: 500 us" in status
    assert "A: 0.5 V/div" in status
    assert "Display rate: 500 kSPS (4x average)" in status
