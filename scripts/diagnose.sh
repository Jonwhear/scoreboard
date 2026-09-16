#!/usr/bin/env bash
#
# Gather everything relevant to a flickering panel in one go, so the cause
# can be identified rather than guessed at.
#
#   ./scripts/diagnose.sh
#
# The three questions this answers:
#   1. Is the Pi itself being under-volted or thermally throttled?
#      (that throttles the CPU, which destabilises the refresh thread)
#   2. Is the refresh thread competing with other processes for CPU?
#   3. Are the two big stability settings actually in effect?
#
set -uo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }

bold "Power and thermals"
if command -v vcgencmd >/dev/null 2>&1; then
  THROTTLED="$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)"
  VOLTS="$(vcgencmd measure_volts core 2>/dev/null | cut -d= -f2)"
  TEMP="$(vcgencmd measure_temp 2>/dev/null | cut -d= -f2)"
  info "get_throttled = $THROTTLED   core = $VOLTS   temp = $TEMP"
  VALUE=$((THROTTLED))
  if [ "$VALUE" -eq 0 ]; then
    ok "The Pi itself has never been under-volted or throttled."
    info "So a flicker is NOT the Pi's own supply. (This says nothing about"
    info "the separate 5V supply feeding the LED panels -- measure that at"
    info "the far panel's terminals with a meter, under a white image.)"
  else
    [ $((VALUE & 0x1))     -ne 0 ] && bad  "UNDER-VOLTAGE RIGHT NOW."
    [ $((VALUE & 0x4))     -ne 0 ] && bad  "CPU is currently throttled."
    [ $((VALUE & 0x2))     -ne 0 ] && warn "ARM frequency currently capped."
    [ $((VALUE & 0x10000)) -ne 0 ] && warn "Under-voltage has occurred since boot."
    [ $((VALUE & 0x40000)) -ne 0 ] && warn "Throttling has occurred since boot."
    [ $((VALUE & 0x80000)) -ne 0 ] && warn "Soft temperature limit hit since boot."
    info ""
    info "An under-volted Pi throttles its CPU, which destabilises the panel"
    info "refresh thread and looks exactly like flicker. Give the Pi its own"
    info "adequate supply, separate from the panels."
  fi
else
  warn "vcgencmd not found; cannot check for under-voltage."
fi

bold "CPU contention (the refresh thread needs a core to itself)"
CMDLINE="$(cat /boot/cmdline.txt 2>/dev/null || cat /boot/firmware/cmdline.txt 2>/dev/null || echo '')"
if echo "$CMDLINE" | grep -q "isolcpus"; then
  ok "isolcpus is set: $(echo "$CMDLINE" | tr ' ' '\n' | grep isolcpus)"
else
  warn "isolcpus is NOT set. The library asks for this at startup."
  info "Append ' isolcpus=3' to the SINGLE line in /boot/cmdline.txt, reboot."
fi

LOAD="$(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null)"
info "load average: $LOAD   (cores: $(nproc 2>/dev/null || echo '?'))"

DESKTOP=""
pgrep -x lxsession >/dev/null 2>&1 && DESKTOP="LXDE desktop"
pgrep -x chromium-browse >/dev/null 2>&1 || pgrep -x chromium >/dev/null 2>&1 \
  && DESKTOP="${DESKTOP:+$DESKTOP + }Chromium"
if [ -n "$DESKTOP" ]; then
  warn "Running: $DESKTOP"
  info "A browser on a Pi 4 steals a lot of CPU and makes the refresh rate"
  info "jitter. Close it (and ideally test from SSH with the desktop off)"
  info "before concluding the problem is the power supply."
else
  ok "No desktop browser competing for CPU."
fi
echo "    top CPU consumers:"
ps -eo pcpu,comm --sort=-pcpu 2>/dev/null | head -5 | sed 's/^/      /'

bold "Timing settings in effect"
CONFIG="$PROJECT_DIR/config/config.json"
if [ -f "$CONFIG" ]; then
  "$PROJECT_DIR/.venv/bin/python" - "$CONFIG" <<'PY'
import json, sys
with open(sys.argv[1]) as handle:
    display = json.load(handle)["display"]
print(f"    pwm_bits                 = {display.get('pwm_bits')}")
print(f"    pwm_lsb_nanoseconds      = {display.get('pwm_lsb_nanoseconds')}")
print(f"    slowdown_gpio            = {display.get('slowdown_gpio')}")
print(f"    limit_refresh_rate_hz    = {display.get('limit_refresh_rate_hz') or 'unlimited'}")
print(f"    disable_hardware_pulsing = {display.get('disable_hardware_pulsing')}")
PY
else
  warn "No config/config.json yet."
fi

if lsmod 2>/dev/null | grep -q "^snd_bcm2835"; then
  warn "The onboard sound module (snd_bcm2835) is loaded."
  info "It shares hardware with the precise PWM timer. While it is loaded you"
  info "must keep disable_hardware_pulsing=true, which gives softer timing."
  info "Blacklisting it and enabling hardware pulsing is the single biggest"
  info "stability win. See the Flicker section of README.md."
else
  ok "snd_bcm2835 is not loaded -- hardware pulsing is available."
  info "Try: sudo .venv/bin/python scripts/tune-matrix.py --hardware-pulsing"
fi

bold "Suggested next step"
echo "    sudo .venv/bin/python scripts/tune-matrix.py --pattern white"
echo "    ...and watch the 'lowest' figure. A wide gap between the current"
echo "    rate and the lowest means jitter (CPU contention or soft timing);"
echo "    a steady rate that still flickers means power."
echo
