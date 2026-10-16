#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
trap 'kill 0' EXIT
(cd backend && .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload) &
(cd frontend && npm run dev) &
wait
