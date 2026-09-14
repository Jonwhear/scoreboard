#!/usr/bin/env bash
#
# Prove the panels are wired and configured correctly, before blaming the
# application for anything.
#
#   ./scripts/test-matrix.sh            the app's own panel test pattern
#   ./scripts/test-matrix.sh demo       hzeller's demo, same options
#   ./scripts/test-matrix.sh samples    render layout PNGs, no hardware
#
# The test pattern draws a numbered, coloured border around each 64x32 panel
# plus a grey ramp. If a border is broken, wraps onto the wrong panel, or the
# numbers are out of order, the problem is chain length / rows / cols / GPIO
# mapping -- not the scoreboard software. If the pattern is correct at first
# and degrades after a few minutes, suspect power and wiring (see README).
#
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$PROJECT_DIR/.venv/bin/python"
MODE="${1:-pattern}"

# Read one display setting out of config.json, with a fallback.
get() {
  CONFIG_PATH="$PROJECT_DIR/config/config.json" KEY="$1" FALLBACK="$2" "$PY" - <<'PY'
import json, os
try:
    with open(os.environ["CONFIG_PATH"]) as fh:
        print(json.load(fh)["display"][os.environ["KEY"]])
except Exception:
    print(os.environ["FALLBACK"])
PY
}

ROWS="$(get rows 32)"
COLS="$(get cols 64)"
CHAIN="$(get chain_length 3)"
MAPPING="$(get gpio_mapping adafruit-hat)"
SLOWDOWN="$(get slowdown_gpio 4)"

case "$MODE" in
  samples)
    exec "$PY" "$PROJECT_DIR/scripts/render_samples.py" \
        "${2:-$PROJECT_DIR/preview-samples}" --scale "${3:-6}"
    ;;
  demo)
    MATRIX_DIR=""
    for candidate in "$HOME/rpi-rgb-led-matrix" "$PROJECT_DIR/../rpi-rgb-led-matrix" \
                     "/opt/rpi-rgb-led-matrix"; do
      [ -d "$candidate" ] && { MATRIX_DIR="$(cd "$candidate" && pwd)"; break; }
    done
    [ -n "$MATRIX_DIR" ] || { echo "rpi-rgb-led-matrix not found" >&2; exit 1; }
    echo "Running hzeller's demo with this project's matrix settings."
    echo "Press Ctrl-C to stop."
    exec sudo "$MATRIX_DIR/examples-api-use/demo" -D 0 \
        --led-rows="$ROWS" --led-cols="$COLS" --led-chain="$CHAIN" \
        --led-gpio-mapping="$MAPPING" --led-slowdown-gpio="$SLOWDOWN"
    ;;
  pattern)
    echo "Matrix settings: rows=$ROWS cols=$COLS chain=$CHAIN mapping=$MAPPING slowdown=$SLOWDOWN"
    echo "Showing the panel test pattern for 30 seconds (Ctrl-C to stop)."
    exec sudo "$PY" "$PROJECT_DIR/scripts/show_pattern.py" --seconds 30
    ;;
  *)
    echo "Usage: $0 [pattern|demo|samples]" >&2
    exit 2
    ;;
esac
