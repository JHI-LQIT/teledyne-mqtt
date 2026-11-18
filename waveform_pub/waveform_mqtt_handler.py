"""
MQTT Handler for Teledyne waveform data publishing in binary format
"""

import json
import logging
import struct
import time
from collections.abc import Callable

import numpy as np
import paho.mqtt.client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

from waveform_postprocessor import is_simple_average_pending

logger = logging.getLogger(__name__)


class WaveformMQTTHandler:
    """Handles MQTT connection and publishing for waveform data in binary format"""

    def __init__(self, broker, port, topic, scope_settings=None, config_topic=None, *,
                 settings_topics=None):
        """Initialize MQTT handler

        Args:
            broker (str): MQTT broker hostname or IP
            port (int): MQTT broker port
            topic (str): MQTT topic for publishing waveform data
            scope_settings (dict): Scope settings including channel publish flags
            config_topic (str): MQTT topic for publishing configuration changes
        """
        self.broker = broker
        self.port = port
        self.topic = topic
        self.config_topic = config_topic
        base_topic = topic.rpartition("/")[0]
        topics = settings_topics or {}
        self.request_topic = f"{base_topic}/cmnd/waveform"
        self.reset_topic = f"{base_topic}/cmnd/reset"
        self.settings_topic = topics.get("command_topic") or f"{base_topic}/cmnd/settings"
        self.settings_response_topic = topics.get("response_topic") or f"{base_topic}/response/settings"
        self.scope_settings = scope_settings or {}
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5)
        self.client.max_queued_messages_set(16)
        self.client.max_inflight_messages_set(1)
        self.connected = False
        self._maximum_packet_size = 268435460
        self._pending_waveform = None
        self._last_size_warning = None

        self.connected_callback: Callable[[], None] | None = None
        self.publish_waveform_callback: Callable[[dict], bool] | None = None
        self.get_settings_callback: Callable[[], dict] | None = None
        self.reset_callback: Callable[[], dict] | None = None
        self.set_settings_callback: Callable[[dict], dict] | None = None

        # Setup callbacks
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        """MQTT connection callback"""
        if reason_code == 0:
            self._maximum_packet_size = getattr(properties, "MaximumPacketSize", 268435460)
            logger.info("MQTT broker maximum packet size: %s bytes", self._maximum_packet_size)
            # Subscribe to command topics
            self.client.subscribe(self.request_topic)
            self.client.subscribe(self.settings_topic)
            self.client.subscribe(self.reset_topic)
            self.connected = True
            logger.info(f"Connected to MQTT broker at {self.broker}:{self.port}")
            if self.connected_callback is not None:
                self.connected_callback()
        else:
            logger.error(f"Failed to connect to MQTT broker, reason {reason_code}")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        """MQTT disconnection callback"""
        self.connected = False
        logger.info(
            "Disconnected from MQTT broker: %s (server disconnect=%s)",
            reason_code, flags.is_disconnect_packet_from_server,
        )

    def connect(self):
        """Connect to MQTT broker"""
        try:
            self.client.connect(self.broker, self.port, 60)
            self.client.loop_start()
            return True
        except Exception as e:
            logger.error(f"Failed to connect to MQTT broker: {e}")
            return False

    def disconnect(self):
        """Disconnect from MQTT broker"""
        self.client.disconnect()
        self.client.loop_stop()

    def update_settings(self, scope_settings):
        """Update scope settings for channel filtering

        Args:
            scope_settings (dict): New scope settings
        """
        self.scope_settings = scope_settings
        logger.info("MQTT handler: Updated scope settings for channel filtering")

    def encode_binary_payload(self, t0, dt, channels, *, timestamp_us=None):
        """
        Encode waveform data into binary format.

        Args:
            t0 (float): Start time in seconds
            dt (float): Time step between samples in seconds
            channels (dict): Dictionary mapping channel names (str) to numpy arrays

        Returns:
            bytes: Binary encoded payload
        """
        # Get channel information
        channel_names = sorted(channels.keys())  # Sort for consistent ordering
        n_channels = len(channel_names)
        n_samples = len(channels[channel_names[0]])

        if timestamp_us is None:
            timestamp_us = time.time_ns() // 1000

        # Build header (36 bytes)
        header = struct.pack(
            "<4sddQII",
            b"WAVE",  # Magic number (4 bytes)
            t0,  # Start time (8 bytes, float64)
            dt,  # Time step (8 bytes, float64)
            timestamp_us,  # Timestamp (8 bytes, int64, microseconds)
            n_channels,  # Number of channels (4 bytes, uint32)
            n_samples,  # Number of samples (4 bytes, uint32)
        )

        # Build channel names section
        channel_names_bytes = b""
        for channel_name in channel_names:
            name_bytes = channel_name.encode("utf-8")
            name_length = len(name_bytes)
            if name_length > 255:
                raise ValueError(f"Channel name too long: {channel_name}")
            channel_names_bytes += struct.pack("<B", name_length) + name_bytes

        # Rows are samples and columns are channels in wire order.
        interleaved = np.empty((n_samples, n_channels), dtype="<f8")
        for column, channel_name in enumerate(channel_names):
            samples = channels[channel_name][:n_samples]
            if len(samples) != n_samples:
                raise ValueError(f"Too few samples for channel {channel_name}")
            interleaved[:, column] = samples

        return b"".join((header, channel_names_bytes, interleaved.tobytes()))

    def publish_waveform(
        self, capture_data, sender_payload=None, *, scope_settings=None, submit=None
    ):
        """Publish waveform data to MQTT in binary format

        Args:
            capture_data (dict): Capture data containing channels_mv, time_axis, etc.
            sender_payload: Optional payload from the requester

        Returns:
            bool: True if published successfully
        """
        if capture_data and is_simple_average_pending(capture_data.get("processing_info")):
            return True
        logger.debug(f"publish_waveform: Called, connected={self.connected}")

        if not self.connected:
            logger.warning("MQTT not connected, skipping publish")
            return False

        if capture_data is None or capture_data.get("channels_mv") is None:
            logger.warning(
                f"No capture data to publish: capture_data={capture_data is not None}, channels_mv={capture_data.get('channels_mv') if capture_data else None}"
            )
            return False

        try:
            # Extract data from capture_data
            # Use processed channels if available, otherwise use raw channels
            channels_mv = capture_data.get("processed_channels_mv")
            if channels_mv is None:
                # Postprocessing not ready yet, use raw channels
                channels_mv = capture_data["channels_mv"]
                logger.debug(
                    "Using raw channels_mv (postprocessing not ready or not configured)"
                )
            else:
                logger.debug("Using postprocessed channels_mv")

            time_axis = capture_data["time_axis"]
            logger.debug(
                f"publish_waveform: Got {len(channels_mv)} channels, time_axis length={len(time_axis)}"
            )

            # Convert channel format (from dict with channel keys to dict with string keys)
            channels = {}
            for channel_key, data in channels_mv.items():
                # Convert channel enum to string name (e.g., "a", "b")
                if hasattr(channel_key, "name"):
                    channel_name = channel_key.name.lower()
                else:
                    channel_name = str(channel_key).split(".")[-1].lower()
                # Get first segment if multiple segments
                if len(data.shape) > 1:
                    channels[channel_name] = data[0, :]
                    logger.debug(
                        f"publish_waveform: Channel {channel_name} shape={data.shape}, using segment 0"
                    )
                else:
                    channels[channel_name] = data
                    logger.debug(
                        f"publish_waveform: Channel {channel_name} shape={data.shape}"
                    )

            logger.debug(
                f"publish_waveform: Converted to {len(channels)} channels: {list(channels.keys())}"
            )

            # Filter channels based on publish flag in scope_settings
            channels_to_publish = {}
            settings = self.scope_settings if scope_settings is None else scope_settings
            channel_configs = (
                capture_data["channel_settings"]
                if capture_data.get("follow_scope")
                else settings.get("channels", [])
            )
            publishing_config = settings.get("publishing", {})
            publishing_channels = publishing_config.get("channels", {})

            logger.debug(
                f"publish_waveform: Filtering {len(channel_configs)} channel configs"
            )

            for channel_cfg in channel_configs:
                # Get channel name from config
                channel_enum = channel_cfg.get("channel")
                if hasattr(channel_enum, "name"):
                    ch_name = channel_enum.name.lower()
                else:
                    ch_name = str(channel_enum).split(".")[-1].lower()

                # Strip 'channel_' prefix if present
                if ch_name.startswith("channel_"):
                    ch_name = ch_name.replace("channel_", "")

                # Also get uppercase version for publishing config lookup
                ch_name_upper = ch_name.upper()

                # Check if this channel should be published (from publishing section)
                ch_publish_config = publishing_channels.get(ch_name_upper, {})
                should_publish = ch_publish_config.get("publish", True)

                logger.debug(
                    f"publish_waveform: Config channel '{ch_name}' - publish={should_publish}, exists in channels={ch_name in channels}"
                )

                if should_publish and ch_name in channels:
                    channels_to_publish[ch_name] = channels[ch_name]
                    logger.debug(
                        f"publish_waveform: Including channel {ch_name} (publish=True)"
                    )
                elif ch_name in channels:
                    logger.debug(
                        f"publish_waveform: Excluding channel {ch_name} (publish=False)"
                    )
                elif should_publish:
                    logger.warning(
                        f"publish_waveform: Channel {ch_name} marked for publish but not found in captured channels {list(channels.keys())}"
                    )

            # If no channels should be published, skip
            if not channels_to_publish:
                logger.debug(
                    "No channels configured for publishing, skipping MQTT publish"
                )
                return True  # Return True to indicate no error, just nothing to publish

            logger.debug(
                f"publish_waveform: Publishing {len(channels_to_publish)} of {len(channels)} channels: {list(channels_to_publish.keys())}"
            )

            # Calculate t0 and dt from time_axis
            t0 = float(time_axis[0])
            dt = (
                capture_data.get("transfer_stride", 1)
                * capture_data.get("down_sample_ratio", 1)
                / capture_data["actual_sample_rate"]
            )

            logger.debug(f"publish_waveform: t0={t0}, dt={dt}")

            # Encode to binary format
            logger.debug(f"publish_waveform: Encoding binary payload...")
            payload = self.encode_binary_payload(
                t0, dt, channels_to_publish, timestamp_us=capture_data.get("timestamp_us")
            )
            logger.debug(f"publish_waveform: Encoded {len(payload)} bytes")

            submit = self.submit_waveform if submit is None else submit
            return submit(payload)

        except Exception as e:
            logger.error(f"Error publishing waveform to MQTT: {e}", exc_info=True)
            return False

    def submit_waveform(self, payload):
        """Queue one waveform within the broker's packet and delivery limits."""
        if not self.connected:
            return False
        properties = Properties(PacketTypes.PUBLISH)
        properties.UserProperty = [("sample_unit", "mV"), ("time_unit", "s")]
        # QoS 1 adds a two-byte packet identifier. Properties.pack() includes
        # the property-length field, and the fixed header uses a variable length.
        remaining = 2 + len(self.topic.encode("utf-8")) + 2 + len(properties.pack()) + len(payload)
        length_bytes = max(1, (remaining.bit_length() + 6) // 7)
        packet_size = 1 + length_bytes + remaining
        if remaining > 268435455 or packet_size > self._maximum_packet_size:
            signature = (len(payload), self._maximum_packet_size)
            if signature != self._last_size_warning:
                logger.error(
                    "Waveform MQTT packet is %s bytes, exceeding the broker limit of %s bytes. "
                    "Increase Postprocessing > Downsample Ratio or the broker max_packet_size. "
                    "This waveform was skipped.",
                    packet_size, self._maximum_packet_size,
                )
                self._last_size_warning = signature
            return False
        self._last_size_warning = None

        # Avoid accumulating large frames while the broker is still acknowledging
        # an earlier frame. Leave queue space for configuration and control replies.
        if self._pending_waveform is not None and not self._pending_waveform.is_published():
            return False
        result = self.client.publish(self.topic, payload, qos=1, properties=properties)
        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            self._pending_waveform = result
            logger.debug("Queued %s bytes of waveform data to %s", len(payload), self.topic)
            return True
        logger.error("Failed to publish to MQTT, return code: %s", result.rc)
        return False

    def publish_reset_command(self):
        """Send the same reset command available to external MQTT clients."""
        result = self.client.publish(self.reset_topic, payload="", qos=1)
        return result.rc == mqtt.MQTT_ERR_SUCCESS

    def publish_settings_response(self, settings_data):
        """Publish settings response to MQTT

        Args:
            settings_data (dict): Settings data to publish
        """
        if not self.connected:
            logger.warning("MQTT not connected, skipping settings response")
            return False

        payload = {
            "timestamp": time.time(),
            "settings": settings_data,
        }

        try:
            result = self.client.publish(
                self.settings_response_topic, json.dumps(payload)
            )
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                logger.debug(f"Published settings response to MQTT: {settings_data}")
                return True
            else:
                logger.error(
                    f"Failed to publish settings to MQTT, return code: {result.rc}"
                )
                return False
        except Exception as e:
            logger.error(f"Error publishing settings to MQTT: {e}")
            return False

    def publish_config(self, config_data):
        """Publish complete configuration to MQTT

        Args:
            config_data (dict): Complete configuration data to publish

        Returns:
            bool: True if published successfully, False otherwise
        """
        if not self.connected:
            logger.warning("MQTT not connected, skipping config publication")
            return False

        if not self.config_topic:
            logger.debug("No config topic configured, skipping config publication")
            return False

        payload = {
            "timestamp": time.time(),
            "config": config_data,
        }

        try:
            result = self.client.publish(
                self.config_topic, json.dumps(payload, indent=2), qos=1, retain=True
            )
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                logger.info(
                    f"Published configuration to {self.config_topic} (retained)"
                )
                return True
            else:
                logger.error(
                    f"Failed to publish config to MQTT, return code: {result.rc}"
                )
                return False
        except Exception as e:
            logger.error(f"Error publishing config to MQTT: {e}", exc_info=True)
            return False

    def is_connected(self):
        """Check if MQTT client is connected

        Returns:
            bool: True if connected, False otherwise
        """
        return self.connected

    def _on_message(self, client, userdata, msg):
        """MQTT message received callback"""
        logger.debug(f"Received MQTT message on topic {msg.topic}: {msg.payload}")

        if msg.topic == self.reset_topic:
            if self.reset_callback is not None:
                self.publish_settings_response(self.reset_callback())
        elif msg.topic == self.request_topic:
            try:
                sender_payload = json.loads(msg.payload) if msg.payload else {}
            except json.JSONDecodeError:
                sender_payload = {}
            logger.debug(
                f"Waveform publish command received. Payload: {sender_payload}"
            )
            if self.publish_waveform_callback is not None:
                self.publish_waveform_callback(sender_payload)

        elif msg.topic == self.settings_topic:
            try:
                if msg.payload:
                    # Set settings if payload contains data
                    settings_payload = json.loads(msg.payload)
                    logger.debug(f"Settings update received: {settings_payload}")
                    if self.set_settings_callback is not None:
                        result = self.set_settings_callback(settings_payload)
                        self.publish_settings_response(result)
                else:
                    # Get settings if payload is empty
                    logger.debug("Settings request received")
                    if self.get_settings_callback is not None:
                        current_settings = self.get_settings_callback()
                        self.publish_settings_response(current_settings)
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse settings JSON: {e}")
                error_response = {"error": f"Invalid JSON: {e}"}
                self.publish_settings_response(error_response)
            except Exception as e:
                logger.error(f"Error handling settings command: {e}")
                error_response = {"error": str(e)}
                self.publish_settings_response(error_response)
