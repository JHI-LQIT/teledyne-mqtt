#!/usr/bin/env bash
set -eu
cd -- "$(dirname -- "$0")"
exec ./venv/bin/python waveform_pub/waveform_pub.py "$@"
