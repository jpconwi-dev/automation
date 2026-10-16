#!/usr/bin/env bash
# Installs the base app. AI extras are optional: ./scripts/setup.sh --ai
set -euo pipefail
cd "$(dirname "$0")/.."
command -v ffmpeg >/dev/null || echo "WARNING: ffmpeg not found. Install it (apt install ffmpeg / brew install ffmpeg)."
python3 -m venv backend/.venv
backend/.venv/bin/pip install -q -r backend/requirements.txt
[ "${1:-}" = "--ai" ] && backend/.venv/bin/pip install -r backend/requirements-ai.txt
[ -f backend/.env ] || cp backend/.env.example backend/.env
(cd frontend && npm install --no-audit --no-fund)
echo "Done. Next: ollama pull \$(grep OLLAMA_MODEL backend/.env | cut -d= -f2) then ./scripts/dev.sh"
