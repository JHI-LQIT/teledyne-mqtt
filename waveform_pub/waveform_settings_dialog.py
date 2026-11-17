"""Qt settings dialogs for Teledyne MQTT."""

import logging
import os

import pyqtgraph as pg
from PyQt5.QtCore import QSettings
from pyqtgraph.parametertree import Parameter, ParameterTree
from pyqtgraph.Qt import QtCore, QtWidgets

from scope_settings import (
    CHANNELS,
    MAX_POINTS,
    get_default_scope_settings,
    load_scope_settings,
)

logger = logging.getLogger(__name__)


class ScopeSettingsDialog(QtWidgets.QDialog):
    """Dialog for configuring scope settings with ParameterTree"""

    # Signal emitted when settings are applied
    settings_applied = QtCore.pyqtSignal(dict)

    SECTIONS = {
        "scope": {
            "Sampling": "sampling",
            "Trigger Settings": "trigger_settings",
            "Channels": "channels",
        },
        "postprocessing": {"Postprocessing": "postprocessing"},
        "publishing": {"Publishing": "publishing"},
    }

    def __init__(self, parent=None, initial_settings=None, *, section="scope"):
        """Initialize settings dialog

        Args:
            parent: Parent widget
            initial_settings: Initial settings dictionary
        """
        super().__init__(parent)
        self.section = section
        self.setWindowTitle(f"{section.capitalize()} Settings")
        self.geometry_key = (
            "settingsDialog/geometry"
            if section == "scope"
            else f"{section}SettingsDialog/geometry"
        )
        self.resize(500, 650)

        # QSettings for persistent window geometry
        self.settings_storage = QSettings("Teledyne", "WaveformPublisher")

        # Restore window geometry from settings
        geometry = self.settings_storage.value(self.geometry_key)
        if geometry:
            self.restoreGeometry(geometry)

        # Store current settings
        self.current_settings = (
            initial_settings or load_scope_settings() or get_default_scope_settings()
        )

        # Store value mappings for reverse lookup (MUST be before _create_parameters)
        self._create_value_mappings()

        # Create parameter tree with settings loaded
        self.params = None
        self.tree = ParameterTree()
        self.refresh_settings(self.current_settings)

        # Create buttons
        button_layout = QtWidgets.QHBoxLayout()

        self.load_button = QtWidgets.QPushButton("Load")
        self.load_button.setToolTip("Load and apply the saved settings for this dialog.")
        self.load_button.clicked.connect(self._on_load)

        self.reset_button = QtWidgets.QPushButton("Reset to Defaults")
        self.reset_button.setToolTip("Restore the settings in this dialog to their defaults.")
        self.reset_button.clicked.connect(self._on_reset)

        self.close_button = QtWidgets.QPushButton("Close")
        self.close_button.setToolTip("Close this dialog. Changes apply as you edit.")
        self.close_button.clicked.connect(self.reject)

        button_layout.addWidget(self.load_button)
        button_layout.addWidget(self.reset_button)
        button_layout.addStretch()
        button_layout.addWidget(self.close_button)

        # Create layout
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.tree)
        layout.addLayout(button_layout)

    def closeEvent(self, a0):
        """Handle close event and save geometry

        Args:
            a0: Close event
        """
        # Save window geometry
        self.settings_storage.setValue(self.geometry_key, self.saveGeometry())
        logger.debug("Settings dialog geometry saved")
        if a0:
            a0.accept()

    def _create_value_mappings(self):
        """Create value mappings for reverse lookup"""
        # Postprocessing type mappings
        self.postprocess_type_map = {
            "None": "none",
            "Simple Average": "simple_average",
            "Moving Average": "moving_average",
        }
        self.postprocess_type_reverse = {
            v: k for k, v in self.postprocess_type_map.items()
        }

    def _create_parameters_from_settings(self, settings):
        """Create parameter tree structure with values from settings

        Args:
            settings: Settings dictionary to load values from

        Returns:
            Parameter: Root parameter with all values set
        """
        sampling = settings["sampling"]
        trigger = settings["trigger_settings"]
        selected = {entry["channel"] for entry in settings["channels"]}
        channel_names = CHANNELS

        # Build postprocessing parameters from settings
        postprocessing = settings.get("postprocessing", {})

        down_sample_ratio = postprocessing.get("down_sample_ratio", 0)
        try:
            down_sample_ratio = max(0, int(down_sample_ratio))
        except (TypeError, ValueError):
            down_sample_ratio = 0

        # Build per-channel postprocessing settings
        postprocess_channel_params = []
        for ch_name in channel_names:
            ch_postprocess = postprocessing.get("channels", {}).get(ch_name, {})

            # Get postprocessing type display name
            postprocess_type = ch_postprocess.get("type", "none")
            postprocess_type_display = self.postprocess_type_reverse.get(
                postprocess_type, "None"
            )

            postprocess_channel_params.append(
                {
                    "name": f"{CHANNELS[ch_name]} ({ch_name})",
                    "type": "group",
                    "children": [
                        {
                            "name": "Type",
                            "tip": "Choose no averaging, a fixed batch average, or a rolling average for this channel.",
                            "type": "list",
                            "limits": ["None", "Simple Average", "Moving Average"],
                            "value": postprocess_type_display,
                        },
                        {
                            "name": "Window Size",
                            "type": "int",
                            "value": ch_postprocess.get("window_size", 10),
                            "limits": [2, 1000],
                            "tip": "Number of captured waveforms in each batch or rolling average.",
                        },
                    ],
                }
            )

        # Build publishing parameters from settings
        publishing = settings.get("publishing", {})

        # Build per-channel publishing settings
        publishing_channel_params = []
        for ch_name in channel_names:
            ch_publishing = publishing.get("channels", {}).get(ch_name, {})
            publish_value = ch_publishing.get("publish", True)

            publishing_channel_params.append(
                {
                    "name": f"{CHANNELS[ch_name]} ({ch_name})",
                    "type": "group",
                    "children": [
                        {
                            "name": "Publish",
                            "type": "bool",
                            "value": publish_value,
                            "tip": "Include this channel in MQTT waveform messages when it is acquired.",
                        },
                    ],
                }
            )

        params = [
            {
                "name": "Sampling",
                "type": "group",
                "children": [
                    {
                        "name": "Follow Scope",
                        "type": "bool",
                        "value": sampling.get("follow_scope", False),
                        "tip": "Download the full record for each enabled scope channel and use its display scale.",
                    },
                    {
                        "name": "Maximum Points",
                        "type": "int",
                        "value": sampling["num_samples"],
                        "limits": [1, MAX_POINTS],
                        "tip": "Limit the number of points downloaded per channel.",
                    },
                    {
                        "name": "Transfer Stride",
                        "type": "int",
                        "value": sampling["transfer_stride"],
                        "limits": [1, MAX_POINTS],
                        "tip": "Download every Nth acquired point. Set the sample rate on the scope.",
                    },
                ],
            },
            {
                "name": "Trigger Settings",
                "type": "group",
                "children": [{
                    "name": "Sweep Mode",
                    "type": "list",
                    "limits": {"Auto": "auto", "Normal": "normal"},
                    "value": trigger["mode"],
                    "tip": "Normal waits for a valid trigger. Set the trigger source and level on the scope.",
                }],
            },
            {
                "name": "Channels",
                "type": "group",
                "children": [
                    {
                        "name": trace,
                        "type": "bool",
                        "value": name in selected,
                        "tip": "Download this analog input. Enable the trace on the scope first.",
                    }
                    for name, trace in CHANNELS.items()
                ],
            },
            {
                "name": "Publishing",
                "type": "group",
                "children": [
                    {
                        "name": "Channels",
                        "type": "group",
                        "children": publishing_channel_params,
                    },
                ],
            },
            {
                "name": "Postprocessing",
                "type": "group",
                "children": [
                    {
                        "name": "Downsample Ratio",
                        "type": "int",
                        "value": down_sample_ratio,
                        "limits": [0, 1000000],
                        "tip": "Average each group of N adjacent samples into one point. Use 0 to keep every sample.",
                    },
                    {
                        "name": "Show Original Waveforms",
                        "type": "bool",
                        "value": postprocessing.get("show_original", False),
                        "tip": "Overlay the original waveform alongside the processed trace in the plot.",
                    },
                    {
                        "name": "Channels",
                        "type": "group",
                        "children": postprocess_channel_params,
                    },
                ],
            },
        ]

        visible_groups = self.SECTIONS[self.section]
        return Parameter.create(
            name="params",
            type="group",
            children=[param for param in params if param["name"] in visible_groups],
        )

    def _params_to_settings(self):
        if self.section == "postprocessing":
            return self._postprocessing_from_params()
        if self.section == "publishing":
            return self._publishing_from_params()
        return self._scope_from_params()

    def _scope_from_params(self):
        return {
            "sampling": {
                "follow_scope": self.params.child("Sampling", "Follow Scope").value(),
                "num_samples": self.params.child("Sampling", "Maximum Points").value(),
                "transfer_stride": self.params.child("Sampling", "Transfer Stride").value(),
            },
            "trigger_settings": {
                "mode": self.params.child("Trigger Settings", "Sweep Mode").value(),
            },
            "channels": [
                {"channel": name}
                for name, trace in CHANNELS.items()
                if self.params.child("Channels", trace).value()
            ],
        }

    def _postprocessing_from_params(self):
        settings = {}
        # Postprocessing
        down_sample_ratio = self.params.child(
            "Postprocessing", "Downsample Ratio"
        ).value()
        try:
            down_sample_ratio = max(0, int(down_sample_ratio))
        except (TypeError, ValueError):
            down_sample_ratio = 0

        settings["postprocessing"] = {
            "down_sample_ratio": down_sample_ratio,
            "show_original": self.params.child(
                "Postprocessing", "Show Original Waveforms"
            ).value(),
            "channels": {},
        }

        # Per-channel postprocessing settings
        for ch_name in ("A", "B", "C", "D"):
            ch_postprocess = self.params.child(
                "Postprocessing", "Channels", f"{CHANNELS[ch_name]} ({ch_name})"
            )
            postprocess_type_display = ch_postprocess.child("Type").value()
            if (
                not postprocess_type_display
                or postprocess_type_display not in self.postprocess_type_map
            ):
                logger.warning(
                    f"Invalid postprocessing type for Channel {ch_name}: '{postprocess_type_display}', using default 'None'"
                )
                postprocess_type_display = "None"
            postprocess_type_enum = self.postprocess_type_map[postprocess_type_display]

            settings["postprocessing"]["channels"][ch_name] = {
                "type": postprocess_type_enum,
                "window_size": ch_postprocess.child("Window Size").value(),
            }

        return settings

    def _publishing_from_params(self):
        channels = {}
        for channel in ("A", "B", "C", "D"):
            publish = self.params.child(
                "Publishing", "Channels", f"{CHANNELS[channel]} ({channel})", "Publish"
            ).value()
            channels[channel] = {"publish": publish}
        return {"publishing": {"channels": channels}}

    def refresh_settings(self, settings):
        if self.params is not None:
            self.params.sigTreeStateChanged.disconnect(self._on_parameter_changed)
        self.current_settings = settings
        self.params = self._create_parameters_from_settings(settings)
        self.tree.setParameters(self.params, showTop=False)
        items = QtWidgets.QTreeWidgetItemIterator(self.tree)
        while (item := items.value()) is not None:
            tip = item.param.opts.get("tip")
            if tip:
                item.setToolTip(0, tip)
                item.setToolTip(1, tip)
                item.displayLabel.setToolTip(tip)
                item.defaultBtn.setToolTip("Restore this field to its default value.")
            items += 1
        self._update_sampling_controls()
        self.params.sigTreeStateChanged.connect(self._on_parameter_changed)

    def _update_sampling_controls(self):
        if self.section == "scope":
            sampling = self.params.child("Sampling")
            manual = not sampling.child("Follow Scope").value()
            for field in ("Maximum Points", "Transfer Stride"):
                sampling.child(field).setOpts(enabled=manual)
            for channel in self.params.child("Channels").children():
                channel.setOpts(enabled=manual)

    def _apply_section(self, settings):
        keys = self.SECTIONS[self.section].values()
        self.settings_applied.emit({key: settings[key] for key in keys if key in settings})

    def _on_parameter_changed(self, param, changes):
        """Submit edited settings for application

        Args:
            param: Parameter that changed
            changes: List of changes
        """
        if not any(change == "value" for _, change, _ in changes):
            return
        try:
            self._update_sampling_controls()
            self._apply_section(self._params_to_settings())
        except Exception:
            logger.exception("Error submitting settings")

    def _on_load(self):
        """Handle load button click"""
        try:
            settings = load_scope_settings()
            if settings:
                self.refresh_settings(settings)
                self._apply_section(settings)
                logger.info("Settings loaded successfully")
            else:
                QtWidgets.QMessageBox.warning(
                    self, "No Settings", "No saved settings file found."
                )
        except Exception as e:
            logger.error(f"Error loading settings: {e}")
            QtWidgets.QMessageBox.critical(
                self, "Error", f"Failed to load settings:\n{str(e)}"
            )

    def _on_reset(self):
        """Handle reset button click"""
        reply = QtWidgets.QMessageBox.question(
            self,
            "Reset Settings",
            f"Reset {self.section} settings to defaults?",
            QtWidgets.QMessageBox.StandardButton.Yes,
            QtWidgets.QMessageBox.StandardButton.No,
        )

        if reply == QtWidgets.QMessageBox.StandardButton.Yes:
            defaults = get_default_scope_settings()
            self.refresh_settings(defaults)
            self._apply_section(defaults)
            logger.info("Settings reset to defaults")

    def get_settings(self):
        """Get current settings

        Returns:
            dict: Current settings dictionary
        """
        return self._params_to_settings()


class MQTTSettingsDialog(QtWidgets.QDialog):
    """Edit the broker and topics used on the next application start."""

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("MQTT Settings")
        self.config = config
        waveform = config.get_waveform_config()
        self.broker = QtWidgets.QLineEdit(config.get_mqtt_broker())
        self.port = QtWidgets.QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(config.get_mqtt_port())
        self.topic = QtWidgets.QLineEdit(waveform.get("topic", "lqt/teledyne/waveform"))
        self.config_topic = QtWidgets.QLineEdit(waveform.get("config_topic") or "")
        self.config_topic.setPlaceholderText("Optional")

        layout = QtWidgets.QFormLayout(self)
        for label, widget, tip in (
            ("Broker", self.broker, "Hostname or IP address of the MQTT broker."),
            ("Port", self.port, "TCP port used to connect to the MQTT broker."),
            ("Waveform topic", self.topic, "MQTT topic for binary waveform messages."),
            (
                "Configuration topic",
                self.config_topic,
                "MQTT topic for applied scope settings. Leave blank to disable configuration announcements.",
            ),
        ):
            widget.setToolTip(tip)
            layout.addRow(label, widget)
            layout.labelForField(widget).setToolTip(tip)
        for variable, widget in (("MQTT_BROKER", self.broker), ("MQTT_PORT", self.port)):
            if os.getenv(variable):
                widget.setReadOnly(True)
                tip = f"{widget.toolTip()} Set by the {variable} environment variable."
                widget.setToolTip(tip)
                layout.labelForField(widget).setToolTip(tip)

        layout.addRow(QtWidgets.QLabel("Saved changes take effect after restarting the app."))
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.button(QtWidgets.QDialogButtonBox.Save).setToolTip(
            "Save MQTT settings to YAML for the next application start."
        )
        buttons.button(QtWidgets.QDialogButtonBox.Cancel).setToolTip(
            "Close this dialog and discard unsaved MQTT edits."
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _save(self):
        broker = self.broker.text().strip()
        topic = self.topic.text().strip()
        config_topic = self.config_topic.text().strip()
        if not broker or not topic or any(
            char in topic or char in config_topic for char in ("#", "+", chr(0))
        ):
            QtWidgets.QMessageBox.warning(
                self, "MQTT Settings", "Enter a broker and valid MQTT publish topics."
            )
            return
        try:
            self.config.save_waveform_mqtt(
                broker=None if self.broker.isReadOnly() else broker,
                port=None if self.port.isReadOnly() else self.port.value(),
                topic=topic,
                config_topic=config_topic or None,
            )
        except (OSError, ValueError) as exc:
            QtWidgets.QMessageBox.warning(self, "MQTT Settings", str(exc))
            return
        self.accept()
