"""Run the Teledyne waveform monitor and MQTT publisher."""

import argparse
import logging
import os
import sys
from pathlib import Path

from scope_settings import get_default_scope_settings, load_scope_settings
from waveform_publisher import WaveformPublisher
from waveform_sources import SyntheticSource, TeledyneSource

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config_loader import get_config, load_config

logger = logging.getLogger(__name__)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Teledyne T3DSO1204 MQTT waveform publisher")
    parser.add_argument("-c", "--config", help="Path to config.yaml")
    parser.add_argument("--host", help="Override the oscilloscope LAN address")
    parser.add_argument("--simulate", action="store_true", help="Use generated waveforms")
    args = parser.parse_args()

    config = load_config(args.config) if args.config else get_config()
    host = args.host or os.getenv("TELEDYNE_HOST") or config.get_full_config().get("scope", {}).get("host")
    if not host and not args.simulate:
        parser.error("Set scope.host in config.yaml or pass --host. Use --simulate to run without a scope.")
    try:
        settings = load_scope_settings() or get_default_scope_settings()
    except (OSError, ValueError) as error:
        parser.error(f"Could not load scope settings: {error}")
    waveform_config = config.get_waveform_config()
    source = SyntheticSource(settings) if args.simulate else TeledyneSource(settings, host)
    topic = waveform_config.get("topic", "lqt/teledyne/waveform")
    config_topic = waveform_config.get("config_topic")
    settings_topics = config.get_settings_topics()
    if args.simulate:
        base = topic.rpartition("/")[0]
        topic = f"{base}/sim/waveform"
        config_topic = f"{base}/sim/config" if config_topic else None
        settings_topics = {}
    publisher = WaveformPublisher(
        config.get_mqtt_broker(), config.get_mqtt_port(), topic, settings, config_topic,
        source=source, settings_topics=settings_topics,
    )

    try:
        publisher.open_device()
        if not publisher.connect_mqtt():
            logger.warning("Continuing without an MQTT connection")
        status = publisher.run_continuous_publishing(
            data_poll_interval_ms=10,
            enable_plotting=True,
            title_suffix="Simulation" if args.simulate else None,
        )
        return status or 0
    except Exception as error:
        logger.error("%s", error)
        return 1
    finally:
        publisher.close_device()


if __name__ == "__main__":
    sys.exit(main())
