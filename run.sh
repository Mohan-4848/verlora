#!/usr/bin/env bash
# Start the WhatsApp order agent + store dashboard.  Usage: ./run.sh [port]
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "${1:-8000}" --reload
