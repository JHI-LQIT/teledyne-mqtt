"""Run the Teledyne waveform monitor and MQTT publisher."""

import argparse
import logging
import os
import sys
from contextlib import closing
from pathlib import Path

import pyvisa

from scope_settings import get_default_scope_settings, load_scope_settings
from waveform_publisher import WaveformPublisher
from waveform_sources import (
    SyntheticSource,
    TeledyneSource,
    list_usb_resources,
    usb_visa_backend,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config_loader import get_config, load_config

logger = logging.getLogger(__name__)


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Teledyne T3DSO1204 MQTT waveform publisher")
    parser.add_argument("-c", "--config", help="Path to config.yaml")
    connection = parser.add_mutually_exclusive_group()
    connection.add_argument("--host", help="Connect over Ethernet to this address")
    connection.add_argument("--usb", action="store_true", help="Connect over USB")
    connection.add_argument("--usb-resource", help="Connect to a specific USB VISA address")
    parser.add_argument("--visa-backend", choices=("@py", "@ivi"), help="Override the VISA backend")
    parser.add_argument("--list-usb", action="store_true", help="List USBTMC VISA addresses and exit")
    parser.add_argument("--simulate", action="store_true", help="Use generated waveforms")
    args = parser.parse_args(argv)

    config = load_config(args.config) if args.config else get_config()
    scope_config = config.get_full_config().get("scope") or {}
    backend = args.visa_backend or scope_config.get("visa_backend")
    if args.list_usb:
        backend = usb_visa_backend(backend)
        try:
            with closing(pyvisa.ResourceManager(backend)) as manager:
                resources = list_usb_resources(manager, backend)
        except Exception as error:
            logger.error("Could not list USB instruments: %s. See the USB setup in the README.", error)
            return 1
        if not resources:
            print("No USBTMC instruments found. Check the cable, USBTMC mode, and VISA setup.")
            return 1
        print("\n".join(resources))
        return 0

    try:
        settings = load_scope_settings() or get_default_scope_settings()
    except (OSError, ValueError) as error:
        parser.error(f"Could not load scope settings: {error}")
    waveform_config = config.get_waveform_config()
    if args.simulate:
        source = SyntheticSource(settings)
    else:
        host = args.host or os.getenv("TELEDYNE_HOST") or scope_config.get("host")
        transport = scope_config.get("transport") or ("lan" if host else "usb")
        if args.host:
            transport = "lan"
        if args.usb or args.usb_resource:
            transport = "usb"
        try:
            if transport == "usb":
                address = args.usb_resource or scope_config.get("usb_resource")
                source = TeledyneSource(settings, usb_resource=address, visa_backend=backend)
            elif transport == "lan" and host:
                source = TeledyneSource(settings, host, visa_backend=backend)
            else:
                raise ValueError("Choose scope.transport usb or lan. LAN requires scope.host or --host.")
        except ValueError as error:
            parser.error(str(error))
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
