#!/bin/sh
# Ein kompletter Experimentlauf: Referenz-Cache auffrischen, dann messen.
# torch/torchaudio angepinnt, sonst hebt der Overlay torch ueber die Version,
# gegen die dots-tts gebaut ist (beobachtet: 2.13.0+cu130 statt 2.8.0+cu128).
set -eu
cd "$(dirname "$0")/.."

UV_WITH='--with resemblyzer --with setuptools<81 --with torch==2.8.0 --with torchaudio==2.8.0'

# shellcheck disable=SC2086
uv run $UV_WITH python forschung/prepare.py
# shellcheck disable=SC2086
uv run $UV_WITH python forschung/experiment.py
