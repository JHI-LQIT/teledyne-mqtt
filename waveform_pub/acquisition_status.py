"""Status-bar helpers for displaying Teledyne acquisition settings."""

def _format_sample_rate(sample_rate_hz):
    """Return a compact human-readable sample rate."""
    if sample_rate_hz is None:
        return "n/a"
    if sample_rate_hz >= 1e9:
        return f"{sample_rate_hz / 1e9:.3g} GSPS"
    if sample_rate_hz >= 1e6:
        return f"{sample_rate_hz / 1e6:.3g} MSPS"
    if sample_rate_hz >= 1e3:
        return f"{sample_rate_hz / 1e3:.3g} kSPS"
    return f"{sample_rate_hz:.3g} SPS"


def _format_duration(seconds):
    """Return a compact human-readable duration."""
    if seconds is None:
        return "n/a"
    if seconds >= 1:
        return f"{seconds:.3g} s"
    if seconds >= 1e-3:
        return f"{seconds * 1e3:.3g} ms"
    if seconds >= 1e-6:
        return f"{seconds * 1e6:.3g} us"
    return f"{seconds * 1e9:.3g} ns"


def _format_channel_name(channel):
    """Return a compact channel identifier."""
    if hasattr(channel, "name"):
        return channel.name
    name = str(channel).split(".")[-1]
    if name.lower().startswith("channel_"):
        name = name.split("_", 1)[1]
    return name.upper()


def _format_channels(channel_settings):
    """Format configured active channels and their ranges."""
    parts = []
    for channel_cfg in channel_settings:
        channel = _format_channel_name(channel_cfg.get("channel", "?"))
        vdiv = channel_cfg.get("volts_per_div")
        parts.append(channel if vdiv is None else f"{channel}: {vdiv:g} V/div")
    return ", ".join(parts) if parts else "none"


def build_acquisition_status(capture_data):
    """Format the status of the same capture that the preview displays."""
    actual_sample_rate = capture_data.get("actual_sample_rate")
    channel_settings = capture_data.get("channel_settings", [])
    raw_samples = capture_data.get("raw_sample_count", len(capture_data["time_axis"]))
    channels_mv = capture_data["channels_mv"]
    ratio = capture_data.get("down_sample_ratio", 1)
    resolution_bits = capture_data.get("resolution_bits")
    if capture_data.get("synthetic"):
        resolution = "synthetic"
    elif resolution_bits is not None:
        resolution = f"{resolution_bits}-bit"
    else:
        resolution = "n/a"

    displayed_samples = raw_samples
    if channels_mv:
        first_channel = next(iter(channels_mv.values()), None)
        if getattr(first_channel, "ndim", 0) >= 2:
            displayed_samples = int(first_channel.shape[1])

    ratio = max(1, int(ratio or 1))
    stride = capture_data.get("transfer_stride", 1)
    effective_rate = actual_sample_rate / (stride * ratio) if actual_sample_rate else None
    span_seconds = (
        displayed_samples / effective_rate
        if displayed_samples and effective_rate
        else None
    )

    if displayed_samples != raw_samples:
        samples_text = f"{displayed_samples:,} ({raw_samples:,} raw)"
    else:
        samples_text = f"{raw_samples:,}"

    parts = [
        f"Rate: {_format_sample_rate(actual_sample_rate)}",
        f"Resolution: {resolution}",
        f"Samples: {samples_text}",
        f"Span: {_format_duration(span_seconds)}",
        f"Channels: {_format_channels(channel_settings)}",
    ]
    if stride > 1:
        parts.append(f"Transfer stride: {stride}")
    if ratio > 1 or stride > 1:
        parts.append(
            f"Display rate: {_format_sample_rate(effective_rate)} ({ratio}x average)"
        )
    progress = [
        f"{_format_channel_name(channel)}: {info['current_samples']}/{info['window_size']}"
        for channel, info in (capture_data.get("processing_info") or {}).items()
        if info.get("type") == "simple_average"
    ]
    if progress:
        parts.append("Averaging " + ", ".join(progress))
    return " | ".join(parts)
