#!/bin/sh
# Dev launcher for the desktop app (macOS/Linux).
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then python3 -m venv .venv; fi
.venv/bin/pip install -q -r requirements.txt
exec .venv/bin/python app_main.py "$@"
