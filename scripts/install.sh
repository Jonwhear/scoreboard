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
"$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 7) else 1)' || {
  fail "Python 3.7 or newer is required. This system has $PY_VERSION."
  fail "Upgrade Raspberry Pi OS, or install a newer python3."
  exit 1; }
if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)'; then
  warn "Python $PY_VERSION is older than 3.9, so pip will install the last"
  warn "versions of Flask/Pillow/waitress that still support it. That is"
  warn "supported, but a 64-bit Raspberry Pi OS (Bookworm) image is faster"
  warn "and gets current security updates."
fi
# On Debian/Raspberry Pi OS 'import venv' succeeds even when the package that
# makes it usable is absent, so check ensurepip too -- that is the piece
# python3-venv actually provides.
if ! "$PYTHON" -c 'import venv, ensurepip' 2>/dev/null; then
  fail "The venv module is not usable. Install it with:"
  fail "  sudo apt install -y python3-venv"
  exit 1
fi

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
elif [ -n "$MATRIX_DIR" ]; then
  warn "The system python cannot import 'rgbmatrix'. Build the bindings with:"
  warn "  cd $MATRIX_DIR/bindings/python && sudo make install-python PYTHON=\$(which python3)"
else
  warn "The system python cannot import 'rgbmatrix' and rpi-rgb-led-matrix was not found."
  warn "Preview mode will work; driving the panels will not."
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

# The matrix bindings are built and installed system-wide by
# rpi-rgb-led-matrix's own makefile. Expose that existing build to the
# virtualenv with a .pth file rather than reinstalling or rebuilding
# anything -- the working installation is left exactly as it is.
if "$VENV/bin/python" -c 'import rgbmatrix' 2>/dev/null; then
  ok "rgbmatrix importable from the virtualenv"
elif "$PYTHON" -c 'import rgbmatrix' 2>/dev/null; then
  # Ask the system python where the package actually lives. The bindings are
  # a compiled extension, so they land in platlib, which is not always the
  # same directory as purelib on Debian/Raspberry Pi OS.
  RGB_SITE="$("$PYTHON" -c 'import os, rgbmatrix; print(os.path.dirname(os.path.dirname(os.path.abspath(rgbmatrix.__file__))))')"
  VENV_SITE="$("$VENV/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
  if [ -n "$RGB_SITE" ] && [ -d "$RGB_SITE" ]; then
    echo "$RGB_SITE" > "$VENV_SITE/rgbmatrix-system.pth"
    if "$VENV/bin/python" -c 'import rgbmatrix' 2>/dev/null; then
      ok "Linked the system rgbmatrix bindings ($RGB_SITE) into the virtualenv"
    else
      rm -f "$VENV_SITE/rgbmatrix-system.pth"
      fail "Found rgbmatrix at $RGB_SITE but the virtualenv still cannot import it."
      warn "The scoreboard will run in preview mode only until this is resolved."
      warn "Check that the bindings were built for $PY_VERSION."
    fi
  fi
else
  warn "rgbmatrix is not importable by either python; hardware output is unavailable."
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
  if [ "$RUN_USER" = "root" ]; then
    warn "The service user resolved to 'root', so privileges will NOT be dropped."
    warn "This usually means the project was cloned with sudo. Fix it with:"
    warn "  sudo chown -R <your-user> $PROJECT_DIR"
  fi
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
  ok "Wrote $SERVICE_DST"
  if ! systemctl daemon-reload; then
    fail "systemctl daemon-reload failed. The unit file is in place; run"
    fail "  sudo systemctl daemon-reload && sudo systemctl enable --now $SERVICE_NAME"
    exit 1
  fi
  if ! systemctl enable --now "$SERVICE_NAME"; then
    fail "Could not enable/start the service. Check: journalctl -u $SERVICE_NAME -n 50"
    exit 1
  fi
  ok "Service installed, enabled at boot, and started"
  systemctl --no-pager --lines=0 status "$SERVICE_NAME" || true
fi

bold "Done"
HOST_NAME="$(hostname 2>/dev/null || echo raspberrypi)"
HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<EOF

  Run it now (preview, no hardware):
      $VENV/bin/python -m scoreboard.app --preview

  Run it on the panels (needs root to initialize the matrix):
      sudo $VENV/bin/python -m scoreboard.app

  Web UI:
      http://${HOST_NAME}.local:${PORT}${HOST_IP:+
      http://${HOST_IP}:${PORT}}

  Install the service so it starts at boot:
      sudo $0 --service
EOF
