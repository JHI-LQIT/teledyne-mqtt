"""
Waveform Postprocessing Module

Provides postprocessing functions for captured waveform data including
simple averaging and moving average filters.
"""

import logging
from collections import deque

import numpy as np

logger = logging.getLogger(__name__)


class WaveformPostprocessor:
    """Handles postprocessing of waveform data with various averaging methods"""

    def __init__(self, postprocessing_config):
        """Initialize postprocessor with configuration

        Args:
            postprocessing_config (dict): Postprocessing configuration dictionary
                Format: {
                    "show_original": bool,
                    "channels": {
                        "A": {"type": "none"|"simple_average"|"moving_average", "window_size": int},
                        ...
                    }
                }
        """
        self.config = postprocessing_config
        self.show_original = postprocessing_config.get("show_original", False)

        # Storage for averaging buffers (per channel)
        self.simple_average_buffers = {}  # Channel -> list of waveforms
        self.simple_average_counts = {}  # Channel -> current count
        self.moving_average_buffers = {}  # Channel -> deque of waveforms

        self.reset()

    def update_config(self, postprocessing_config):
        """Update postprocessing configuration and reset buffers

        Args:
            postprocessing_config (dict): New postprocessing configuration
        """
        logger.info("Updating postprocessing configuration")
        self.config = postprocessing_config
        self.show_original = postprocessing_config.get("show_original", False)

        self.reset()

    def reset(self):
        """Clear accumulated averages using the active configuration."""
        self.simple_average_buffers.clear()
        self.simple_average_counts.clear()
        self.moving_average_buffers.clear()

        for channel_name, ch_config in self.config.get(
            "channels", {}
        ).items():
            if ch_config["type"] == "simple_average":
                self.simple_average_buffers[channel_name] = []
                self.simple_average_counts[channel_name] = 0
            elif ch_config["type"] == "moving_average":
                window_size = ch_config.get("window_size", 10)
                self.moving_average_buffers[channel_name] = deque(maxlen=window_size)

    def process_waveforms(self, channels_mv):
        """Process waveform data according to configuration

        Args:
            channels_mv (dict): Channel data dictionary with channel enums as keys
                Format: {channel_enum: np.array of shape (num_segments, num_samples)}

        Returns:
            tuple: (processed_channels_mv, original_channels_mv, processing_info)
                - processed_channels_mv: Dictionary with processed waveforms (or None if no update)
                - original_channels_mv: Original waveforms if show_original is True, else None
                - processing_info: Dictionary with processing metadata per channel
        """
        if channels_mv is None:
            return None, None, {}

        processed_channels = {}
        original_channels = {} if self.show_original else None
        processing_info = {}

        for channel_enum, data in channels_mv.items():
            # Get channel name from enum (e.g., CHANNEL.A -> "A")
            if hasattr(channel_enum, "name"):
                channel_name = channel_enum.name
            else:
                channel_name = str(channel_enum).split(".")[-1]

            # Get config for this channel
            ch_config = self.config.get("channels", {}).get(channel_name, {})
            postprocess_type = ch_config.get("type", "none")

            # Store original if needed
            if original_channels is not None and postprocess_type != "simple_average":
                original_channels[channel_enum] = data.copy()

            # Apply postprocessing based on type
            if postprocess_type == "none":
                # No postprocessing - pass through
                processed_channels[channel_enum] = data
                processing_info[channel_enum] = {"type": "none", "ready": True}

            elif postprocess_type == "simple_average":
                averaged, info = self._apply_simple_average(channel_name, data, ch_config)
                if averaged is not None:
                    processed_channels[channel_enum] = averaged
                processing_info[channel_enum] = info

            elif postprocess_type == "moving_average":
                result = self._apply_moving_average(
                    channel_name, channel_enum, data, ch_config
                )
                # Moving average always returns a result (even with partial window)
                processed_channels[channel_enum] = result[0]
                processing_info[channel_enum] = result[1]

        # Return None for processed channels if no channels are ready
        if not processed_channels:
            return None, original_channels, processing_info

        return processed_channels, original_channels, processing_info

    def _apply_simple_average(self, channel_name, data, config):
        """Return a completed batch average, if any, and its progress."""
        window_size = config.get("window_size", 10)

        # Add current waveform to buffer
        self.simple_average_buffers[channel_name].append(data.copy())
        self.simple_average_counts[channel_name] += 1

        count = self.simple_average_counts[channel_name]
        ready = count >= window_size
        averaged = None
        if ready:
            averaged = np.mean(self.simple_average_buffers[channel_name], axis=0)
            self.simple_average_buffers[channel_name].clear()
            self.simple_average_counts[channel_name] = 0

        return averaged, {
            "type": "simple_average",
            "window_size": window_size,
            "current_samples": count,
            "ready": ready,
        }

    def _apply_moving_average(self, channel_name, channel_enum, data, config):
        """Apply moving average (continuously updated average over last N waveforms)

        Args:
            channel_name (str): Channel name (e.g., "A")
            channel_enum: Channel enum value
            data (np.array): Waveform data
            config (dict): Channel postprocessing config

        Returns:
            tuple: (averaged_data, info) - always returns result with available samples
        """
        window_size = config.get("window_size", 10)

        # Add current waveform to circular buffer
        self.moving_average_buffers[channel_name].append(data.copy())

        # Compute moving average with available samples (even if less than window_size)
        buffer = list(self.moving_average_buffers[channel_name])
        averaged = np.mean(buffer, axis=0)

        current_samples = len(buffer)
        info = {
            "type": "moving_average",
            "window_size": window_size,
            "current_samples": current_samples,
            "ready": True,
            "partial": current_samples < window_size,
        }
        return averaged, info

    def should_show_original(self):
        """Check if original waveforms should be displayed

        Returns:
            bool: True if original waveforms should be shown
        """
        return self.show_original

    def get_channel_config(self, channel_enum):
        """Get postprocessing config for a specific channel

        Args:
            channel_enum: Channel enum value

        Returns:
            dict: Channel postprocessing configuration
        """
        if hasattr(channel_enum, "name"):
            channel_name = channel_enum.name
        else:
            channel_name = str(channel_enum).split(".")[-1]

        return self.config.get("channels", {}).get(
            channel_name, {"type": "none", "window_size": 10}
        )


def is_simple_average_pending(processing_info):
    """Return True while any configured simple average is still accumulating."""
    return any(
        info.get("type") == "simple_average" and not info.get("ready", False)
        for info in (processing_info or {}).values()
    )
