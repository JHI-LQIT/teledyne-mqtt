# Teledyne MQTT

Live analog waveform preview and MQTT publishing for the **Teledyne LeCroy T3DSO1204**. The app reads the scope over Ethernet using VXI-11 with PyVISA-py. It includes per-channel moving and batch averaging, optional sample averaging, and MQTT settings and reset commands.

## Run

Use Python 3.11 or later. Create a virtual environment and install the dependencies.

```sh
python -m venv venv
# Linux
source venv/bin/activate
# Windows PowerShell: .\venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

1. Connect the scope and computer to the same LAN. Find the scope's IP address in its network menu.
2. On the scope, enable **C1** and select volts as the channel unit. Set the probe attenuation, vertical scale, coupling, timebase, and trigger source, slope, and level on the scope. Use ordinary time-domain acquisition with sequence, history playback, and roll modes disabled.
3. In `config.yaml`, set `scope.host` to the scope IP address and `mqtt.broker` to your MQTT 5 broker. Or supply `--host` and use the default broker at `localhost:1883`.

```sh
python waveform_pub/waveform_pub.py --host 192.168.1.50
```

Use `-c /path/to/config.yaml` to select a different configuration. The environment variables `TELEDYNE_HOST`, `MQTT_BROKER`, and `MQTT_PORT` override YAML values. On Linux, `./start.sh` uses the virtual environment and `python install_desktop_launcher.py` installs a desktop entry. On Windows, use `start.bat`.

For a connection-free scope demo, run `python waveform_pub/waveform_pub.py --simulate`. Simulation uses the configured broker under `lqt/teledyne/sim/` by default. The GUI also works while the broker is unavailable.

## Acquisition controls

| Setting | Behavior |
| --- | --- |
| Channels | Select the analog traces to download. Enable those traces on the scope first. |
| Maximum Points | Maximum number of downloaded samples per channel, from the beginning of the record. Defaults to 100,000. |
| Transfer Stride | Download every Nth sample. Defaults to 1. Increase it to cover more of a long record with fewer points. |
| Auto | The scope can acquire without a valid trigger. |
| Normal | The app arms one sweep per waveform and waits for a valid trigger. |
| Downsample Ratio | Average adjacent downloaded samples on the computer. Use 0 to keep every point. The averaged sample is timed at the center of its group. |
| Publishing | Select which acquired channels to send to MQTT. |

The app stops the scope between captures to read all selected channels from the same acquisition. It leaves the scope stopped on exit. The scope reports the actual sample rate, scale, and offset. The app applies the scope's trigger delay to the time axis and uses the host UTC time when a completed sweep is detected for the packet timestamp. See the [T3DSO1000/2000 programming manual](https://cdn.teledynelecroy.com/files/manuals/tds031000-2000_programming_manual.pdf).

## MQTT

| Default topic | Function |
| --- | --- |
| `lqt/teledyne/waveform` | Binary waveform, QoS 1 |
| `lqt/teledyne/config` | Retained JSON configuration, QoS 1 |
| `lqt/teledyne/cmnd/settings` | Empty payload reads settings. A JSON object updates them. |
| `lqt/teledyne/response/settings` | JSON result of a settings or reset command |
| `lqt/teledyne/cmnd/reset` | Clear waveform averaging buffers |
| `lqt/teledyne/cmnd/waveform` | Request the newest available waveform |

For example, this settings patch selects C1 and C2 and downloads at most 20,000 points per channel with a stride of four.

```json
{"sampling":{"num_samples":20000,"transfer_stride":4},"channels":[{"channel":"A"},{"channel":"B"}]}
```

The channel IDs `A`, `B`, `C`, and `D` map to scope inputs `C1`–`C4`. Their lowercase IDs appear in waveform packets. The GUI persists changes to `waveform_pub/scope_settings.json`. MQTT changes apply for the running session.

The binary payload uses the PicoScope MQTT `WAVE` format. A little-endian `<4sddQII` header carries the magic bytes, start time, sample interval, UTC timestamp in microseconds, channel count, and sample count per channel. Each channel name follows as a one-byte length and UTF-8 name. Float64 samples follow, interleaved by channel. MQTT 5 user properties announce `sample_unit=mV` and `time_unit=s`.

## Development

```sh
python -m pip install pytest
python -m pytest -q
```

Protocol tests exercise the acquisition worker through a simulated VISA session and decode the resulting MQTT waveform packet.

Adapted from [JHI-LQIT/picoscope-mqtt](https://github.com/JHI-LQIT/picoscope-mqtt) at `f1786c4`, with MQTT 5 waveform units from [PR #22](https://github.com/JHI-LQIT/picoscope-mqtt/pull/22). The original MIT license is included.
