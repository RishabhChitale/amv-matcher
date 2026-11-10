#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then python3 -m venv .venv; fi
.venv/bin/pip install -q -r requirements.txt
URL=http://127.0.0.1:8765
(sleep 2 && (open "$URL" 2>/dev/null || true)) &
exec .venv/bin/python -m uvicorn amv_matcher.server:app --host 127.0.0.1 --port 8765
