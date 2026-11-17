"""
Configuration loader for MQTT and application settings.

Loads configuration from config.yaml file with support for environment variable overrides.
"""

import logging
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

import yaml

logger = logging.getLogger(__name__)


class ConfigLoader:
    """Loads and manages application configuration from YAML files."""

    def __init__(self, config_file: str | None = None):
        """Initialize config loader with optional custom config file path.

        Args:
            config_file (str, optional): Path to config.yaml file. If not provided,
                                        searches in current directory and parent directories.
        """
        self.config = {}
        self.config_file = config_file or self._find_config_file()

        if self.config_file and os.path.exists(self.config_file):
            self._load_config()
        else:
            logger.warning("Config file not found, using defaults")
            self._load_defaults()

    def _find_config_file(self) -> str | None:
        """Search for config.yaml in current and parent directories.

        Returns:
            str: Path to config.yaml if found, None otherwise
        """
        # Search in current directory and parent directories
        search_paths = [
            Path.cwd() / "config.yaml",
            Path.cwd().parent / "config.yaml",
            Path(__file__).parent / "config.yaml",
            Path(__file__).parent.parent / "config.yaml",
        ]

        for path in search_paths:
            if path.exists():
                logger.info(f"Found config file: {path}")
                return str(path)

        return None

    def _load_config(self):
        """Load configuration from YAML file."""
        try:
            with open(self.config_file, "r") as f:
                self.config = yaml.safe_load(f) or {}
            logger.info(f"Loaded configuration from {self.config_file}")
        except Exception as e:
            logger.error(f"Error loading config file {self.config_file}: {e}")
            self._load_defaults()

    def _load_defaults(self):
        """Load default configuration values."""
        self.config = {
            "mqtt": {
                "broker": "localhost",
                "port": 1883,
                "waveform": {
                    "topic": "lqt/teledyne/waveform",
                },
            }
        }
        logger.info("Using default configuration")

    def get_mqtt_broker(self) -> str:
        """Get MQTT broker hostname from configuration.

        Returns:
            str: MQTT broker hostname or IP address
        """
        return os.getenv("MQTT_BROKER") or self.config.get("mqtt", {}).get(
            "broker", "localhost"
        )

    def get_mqtt_port(self) -> int:
        """Get MQTT broker port from configuration.

        Returns:
            int: MQTT broker port number
        """
        port = os.getenv("MQTT_PORT") or self.config.get("mqtt", {}).get("port", 1883)
        return int(port)

    def get_waveform_config(self) -> dict:
        """Get waveform publisher configuration."""
        defaults = {
            "topic": "lqt/teledyne/waveform",
        }
        return self.config.get("mqtt", {}).get("waveform", defaults)

    def save_waveform_mqtt(self, *, broker, port, topic, config_topic):
        """Update waveform connection settings in the selected YAML file."""
        path = Path(self.config_file or Path.cwd() / "config.yaml")
        try:
            with path.open() as stream:
                config = yaml.safe_load(stream)
        except FileNotFoundError:
            config = {}
        except yaml.YAMLError as exc:
            raise ValueError(f"Cannot update {path}: {exc}") from exc
        if config is None:
            config = {}
        if not isinstance(config, dict):
            raise ValueError(f"Expected a mapping in {path}")
        mqtt_config = config.setdefault("mqtt", {})
        if not isinstance(mqtt_config, dict):
            raise ValueError(f"Expected an mqtt mapping in {path}")
        waveform = mqtt_config.setdefault("waveform", {})
        if not isinstance(waveform, dict):
            raise ValueError(f"Expected an mqtt.waveform mapping in {path}")
        if broker is not None:
            mqtt_config["broker"] = broker
        if port is not None:
            mqtt_config["port"] = port
        waveform.update(topic=topic, config_topic=config_topic)

        temp_path = None
        try:
            with NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
                temp_path = Path(stream.name)
                yaml.safe_dump(config, stream, sort_keys=False)
            temp_path.replace(path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        self.config_file = str(path)
        self.config = config

    def get_settings_topics(self) -> dict:
        """Get settings command/response topics.

        Returns:
            dict: Configuration dictionary with keys: command_topic, response_topic
        """
        return self.config.get("mqtt", {}).get("settings", {})

    def get_full_config(self) -> dict:
        """Get the full configuration dictionary.

        Returns:
            dict: Complete configuration
        """
        return self.config


# Global config instance
_config_instance = None


def get_config() -> ConfigLoader:
    """Get or create the global config instance (singleton pattern).

    Returns:
        ConfigLoader: Global configuration instance
    """
    global _config_instance
    if _config_instance is None:
        _config_instance = ConfigLoader()
    return _config_instance


def load_config(config_file: str | None = None) -> ConfigLoader:
    """Load configuration from file.

    Args:
        config_file (str, optional): Path to config.yaml file.

    Returns:
        ConfigLoader: Configuration instance
    """
    global _config_instance
    _config_instance = ConfigLoader(config_file)
    return _config_instance
