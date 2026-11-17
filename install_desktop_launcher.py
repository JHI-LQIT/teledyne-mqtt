"""Install the per-user Linux application launcher for this checkout."""

import os
from pathlib import Path
import sys


APP_ID = "org.jhi_lqit.TeledyneMQTT"


def desktop_value(value):
    """Escape a string for a desktop entry value."""
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")


def exec_argument(value):
    """Quote a literal argument in a desktop entry Exec command."""
    value = str(value).replace("%", "%%")
    for char in ('\\', '"', '`', '$'):
        value = value.replace(char, "\\" + char)
    return f'"{value}"'


def main():
    if not sys.platform.startswith("linux"):
        raise SystemExit("The desktop launcher installer is for Linux.")

    root = Path(__file__).resolve().parent
    python = root / "venv" / "bin" / "python"
    if not python.is_file():
        python = Path(sys.executable).absolute()
    entry_point = root / "waveform_pub" / "waveform_pub.py"
    icon = root / "waveform_pub" / "assets" / "oscilloscope.svg"
    command = " ".join(exec_argument(arg) for arg in (python, entry_point))
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    launcher = data_home / "applications" / f"{APP_ID}.desktop"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Teledyne MQTT\n"
        f"Exec={desktop_value(command)}\n"
        f"Path={desktop_value(str(root))}\n"
        f"Icon={desktop_value(str(icon))}\n"
        f"StartupWMClass={APP_ID}\n"
        "Terminal=false\n",
        encoding="utf-8",
    )
    print(f"Installed {launcher}")


if __name__ == "__main__":
    main()
