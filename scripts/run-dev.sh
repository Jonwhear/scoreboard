#!/usr/bin/env bash
#
# Run the scoreboard in preview mode: no GPIO, no root, no LED panels.
# The web UI (including the live framebuffer preview) works exactly as it
# does on the Pi, which makes this the easiest way to work on layouts.
#
#   ./scripts/run-dev.sh              serve on :8080
#   ./scripts/run-dev.sh --port 9000  any extra args are passed through
#
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$PROJECT_DIR/.venv/bin/python"
[ -x "$PY" ] || { echo "No virtualenv yet. Run ./scripts/install.sh first." >&2; exit 1; }
cd "$PROJECT_DIR"
exec "$PY" -m scoreboard.app --preview --log-level "${LOG_LEVEL:-INFO}" "$@"
