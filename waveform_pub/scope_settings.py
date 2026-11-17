"""Settings shared by the acquisition worker, GUI, and MQTT interface."""

import json
import os
from copy import deepcopy
from pathlib import Path
from tempfile import NamedTemporaryFile

CHANNELS = {"A": "C1", "B": "C2", "C": "C3", "D": "C4"}
MAX_POINTS = 1_000_000


def get_default_scope_settings():
    return {
        "sampling": {"follow_scope": False, "num_samples": 100_000, "transfer_stride": 1},
        "trigger_settings": {"mode": "auto"},
        "channels": [{"channel": "A"}],
        "postprocessing": {
            "down_sample_ratio": 0,
            "show_original": False,
            "channels": {name: {"type": "none", "window_size": 10} for name in CHANNELS},
        },
        "publishing": {"channels": {name: {"publish": True} for name in CHANNELS}},
    }


def get_settings_file_path():
    return Path(__file__).with_name("scope_settings.json")


def save_scope_settings(settings):
    path = get_settings_file_path()
    with NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as temporary:
        temporary_path = Path(temporary.name)
        try:
            json.dump(settings, temporary, indent=4, allow_nan=False)
            temporary.close()
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)


def load_scope_settings():
    path = get_settings_file_path()
    if not path.is_file():
        return None
    with path.open() as stream:
        settings = json.load(stream)
    return prepare_settings_update(get_default_scope_settings(), settings)[0]


def _merge_settings(base, patch):
    result = deepcopy(base)
    for key, value in patch.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = _merge_settings(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _integer(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")


def _check_keys(section, allowed, name):
    if not isinstance(section, dict) or set(section) - set(allowed):
        raise ValueError(f"Invalid {name} fields. Allowed fields: {', '.join(allowed)}")


def prepare_settings_update(current, update):
    """Apply a partial settings update, validating it before touching hardware."""
    defaults = get_default_scope_settings()
    _check_keys(update, (*defaults, "num_samples"), "settings")
    patch = deepcopy(update)
    if "num_samples" in patch:
        points = patch.pop("num_samples")
        sampling = patch.setdefault("sampling", {})
        _check_keys(sampling, defaults["sampling"], "sampling")
        sampling["num_samples"] = points
    for name, value in patch.items():
        if name != "channels":
            _check_keys(value, defaults[name], name)

    candidate = _merge_settings(_merge_settings(defaults, current), patch)
    sampling = candidate["sampling"]
    if type(sampling["follow_scope"]) is not bool:
        raise ValueError("follow_scope must be a boolean")
    _integer(sampling["num_samples"], "num_samples", 1, MAX_POINTS)
    _integer(sampling["transfer_stride"], "transfer_stride", 1, MAX_POINTS)

    if candidate["trigger_settings"]["mode"] not in ("auto", "normal"):
        raise ValueError("Trigger mode must be auto or normal")

    channels = candidate["channels"]
    if not isinstance(channels, list) or not channels:
        raise ValueError("Select at least one analog channel")
    names = []
    for channel in channels:
        _check_keys(channel, ("channel",), "channel")
        name = channel.get("channel")
        if name not in CHANNELS or name in names:
            raise ValueError("Channels must be distinct and named A, B, C, or D")
        names.append(name)

    post = candidate["postprocessing"]
    _integer(post["down_sample_ratio"], "down_sample_ratio", 0, MAX_POINTS)
    if type(post["show_original"]) is not bool:
        raise ValueError("show_original must be a boolean")
    _check_keys(post["channels"], CHANNELS, "postprocessing channels")
    for channel in post["channels"].values():
        _check_keys(channel, ("type", "window_size"), "postprocessing channel")
        channel.setdefault("type", "none")
        if channel["type"] not in ("none", "simple_average", "moving_average"):
            raise ValueError("Invalid waveform averaging type")
        _integer(channel.get("window_size", 10), "window_size", 2, 1000)

    publishing = candidate["publishing"]["channels"]
    _check_keys(publishing, CHANNELS, "publishing channels")
    for channel in publishing.values():
        _check_keys(channel, ("publish",), "publishing channel")
        if type(channel.get("publish", True)) is not bool:
            raise ValueError("publish must be a boolean")

    response = {"num_samples": candidate["sampling"]["num_samples"]} if update.keys() == {"num_samples"} else candidate
    return candidate, response
