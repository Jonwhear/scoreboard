#!/usr/bin/env bash
#
# Install (or re-install) the sports scoreboard.
#
# The script is idempotent and inspects before it acts. It does not touch
# the existing rpi-rgb-led-matrix installation, and it will not change
# anything outside the project directory without telling you first and
# asking for confirmation.
#
#   ./scripts/install.sh              inspect, create venv, install deps
#   ./scripts/install.sh --service    also install and enable the systemd unit
#   ./scripts/install.sh --service --yes   ... without the confirmation prompt
#
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$PROJECT_DIR/.venv"
SERVICE_NAME="sports-scoreboard"
SERVICE_SRC="$PROJECT_DIR/systemd/$SERVICE_NAME.service"
SERVICE_DST="/etc/systemd/system/$SERVICE_NAME.service"

INSTALL_SERVICE=0
ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    --service) INSTALL_SERVICE=1 ;;
    --yes|-y)  ASSUME_YES=1 ;;
    --help|-h) sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
ok()    { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn()  { printf '  \033[33m!\033[0m %s\n' "$*"; }
fail()  { printf '  \033[31m✗\033[0m %s\n' "$*"; }

# Who should own and run this? Never root, even when invoked with sudo.
RUN_USER="${SUDO_USER:-$(id -un)}"
if [ "$RUN_USER" = "root" ]; then
  RUN_USER="$(stat -c '%U' "$PROJECT_DIR")"
fi

# ---------------------------------------------------------------- inspect
bold "Inspecting this system"
echo "  project directory : $PROJECT_DIR"
echo "  service user      : $RUN_USER"

if [ -r /etc/os-release ]; then
  . /etc/os-release
  echo "  operating system  : ${PRETTY_NAME:-unknown} ($(uname -m))"
fi
if [ -r /proc/device-tree/model ]; then
  echo "  hardware          : $(tr -d '\0' < /proc/device-tree/model)"
else
  warn "No /proc/device-tree/model: this does not look like a Raspberry Pi."
  warn "The app will still run with --preview, but not drive LED panels."
fi

PYTHON="$(command -v python3 || true)"
if [ -z "$PYTHON" ]; then
  fail "python3 not found. Install it with: sudo apt install python3 python3-venv"
  exit 1
fi
PY_VERSION="$("$PYTHON" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
echo "  python            : $PY_VERSION ($PYTHON)"
"$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' || {
  fail "Python 3.9 or newer is required."; exit 1; }
"$PYTHON" -c 'import venv' 2>/dev/null || {
  fail "The venv module is missing. Install it with: sudo apt install python3-venv"; exit 1; }

# rpi-rgb-led-matrix: find it, do not touch it.
MATRIX_DIR=""
for candidate in "$HOME/rpi-rgb-led-matrix" "/home/$RUN_USER/rpi-rgb-led-matrix" \
                 "$PROJECT_DIR/../rpi-rgb-led-matrix" "/opt/rpi-rgb-led-matrix"; do
  if [ -d "$candidate" ]; then MATRIX_DIR="$(cd "$candidate" && pwd)"; break; fi
done
if [ -n "$MATRIX_DIR" ]; then
  ok "rpi-rgb-led-matrix: $MATRIX_DIR (left untouched)"
  if [ -x "$MATRIX_DIR/examples-api-use/demo" ]; then
    ok "demo binary present: $MATRIX_DIR/examples-api-use/demo"
  else
    warn "No compiled demo binary under $MATRIX_DIR/examples-api-use/"
  fi
  FONT_COUNT="$(find "$MATRIX_DIR/fonts" -name '*.bdf' 2>/dev/null | wc -l)"
  if [ "$FONT_COUNT" -gt 0 ]; then
    ok "$FONT_COUNT BDF fonts available in $MATRIX_DIR/fonts"
  else
    warn "No BDF fonts found; the app will fall back to a TrueType face."
  fi
else
  warn "rpi-rgb-led-matrix not found in the usual places."
  warn "Preview mode will work; hardware output will not."
fi

if "$PYTHON" -c 'import rgbmatrix' 2>/dev/null; then
  ok "rgbmatrix bindings importable by the system python"
else
  warn "The system python cannot import 'rgbmatrix'."
  warn "Build them with: cd $MATRIX_DIR/bindings/python && sudo make install-python"
fi

PORT="$(PROJECT_DIR="$PROJECT_DIR" "$PYTHON" - <<'PY' 2>/dev/null || echo 8080
import json, os, sys
path = os.path.join(os.environ.get("PROJECT_DIR", "."), "config", "config.json")
try:
    with open(path) as fh:
        print(json.load(fh)["web"]["port"])
except Exception:
    print(8080)
PY
)"
if command -v ss >/dev/null && ss -ltn "sport = :$PORT" 2>/dev/null | grep -q LISTEN; then
  warn "Port $PORT is already in use. Change web.port in config/config.json."
else
  ok "Port $PORT is free"
fi

# ------------------------------------------------------------ virtualenv
bold "Python environment"
if [ -x "$VENV/bin/python" ]; then
  ok "Reusing existing virtualenv at $VENV"
else
  "$PYTHON" -m venv "$VENV"
  ok "Created virtualenv at $VENV"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -r "$PROJECT_DIR/requirements.txt"
ok "Dependencies installed"

# The matrix bindings are installed system-wide by rpi-rgb-led-matrix's
# makefile; expose them to the venv rather than reinstalling anything.
if ! "$VENV/bin/python" -c 'import rgbmatrix' 2>/dev/null; then
  SYS_SITE="$("$PYTHON" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
  if "$PYTHON" -c 'import rgbmatrix' 2>/dev/null; then
    VENV_SITE="$("$VENV/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
    echo "$SYS_SITE" > "$VENV_SITE/rgbmatrix-system.pth"
    if "$VENV/bin/python" -c 'import rgbmatrix' 2>/dev/null; then
      ok "Linked the system rgbmatrix bindings into the virtualenv"
    else
      warn "Could not expose rgbmatrix to the virtualenv; preview mode only."
      rm -f "$VENV_SITE/rgbmatrix-system.pth"
    fi
  fi
else
  ok "rgbmatrix importable from the virtualenv"
fi

# ---------------------------------------------------------------- config
bold "Configuration"
mkdir -p "$PROJECT_DIR/config" "$PROJECT_DIR/var"
if [ -f "$PROJECT_DIR/config/config.json" ]; then
  ok "Keeping the existing config/config.json"
else
  cp "$PROJECT_DIR/config/config.example.json" "$PROJECT_DIR/config/config.json"
  ok "Created config/config.json from the example"
fi
if [ "$(id -u)" -eq 0 ] && [ "$RUN_USER" != "root" ]; then
  chown -R "$RUN_USER" "$PROJECT_DIR/config" "$PROJECT_DIR/var" "$VENV"
  ok "config/ and var/ are owned by $RUN_USER"
fi

# --------------------------------------------------------------- service
if [ "$INSTALL_SERVICE" -eq 1 ]; then
  bold "systemd service"
  echo "  This will make the following system-level changes:"
  echo "    1. write $SERVICE_DST"
  echo "       (starts as root, drops privileges to '$RUN_USER' after the matrix inits)"
  echo "    2. run: systemctl daemon-reload"
  echo "    3. run: systemctl enable --now $SERVICE_NAME"
  echo "  Nothing else on the system is modified."
  if [ "$ASSUME_YES" -ne 1 ]; then
    read -r -p "  Proceed? [y/N] " reply
    case "$reply" in [yY]*) ;; *) echo "  Skipped."; exit 0 ;; esac
  fi
  if [ "$(id -u)" -ne 0 ]; then
    fail "Installing the service needs root. Re-run with: sudo $0 --service"
    exit 1
  fi
  sed -e "s|__INSTALL_DIR__|$PROJECT_DIR|g" -e "s|__RUN_USER__|$RUN_USER|g" \
      "$SERVICE_SRC" > "$SERVICE_DST"
  chmod 644 "$SERVICE_DST"
  systemctl daemon-reload
  systemctl enable --now "$SERVICE_NAME"
  ok "Service installed and started"
  systemctl --no-pager --lines=0 status "$SERVICE_NAME" || true
fi

bold "Done"
cat <<EOF

  Run it now (preview, no hardware):
      $VENV/bin/python -m scoreboard.app --preview

  Run it on the panels (needs root to initialize the matrix):
      sudo $VENV/bin/python -m scoreboard.app

  Web UI:
      http://\$(hostname).local:${PORT}    or   http://\$(hostname -I | awk '{print \$1}'):${PORT}

  Install the service:
      sudo $0 --service
EOF
