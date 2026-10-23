#!/usr/bin/env bash
#
# Carry out an update or reboot the web UI asked for.
#
# The web UI runs unprivileged, so it cannot reboot the Pi or restart the
# service itself. Instead it writes one word to var/control/request; the
# sports-scoreboard-control.path unit notices and runs this script as root.
#
# Only these words are understood -- the file is never executed or
# interpreted beyond that:
#
#   update          pull the latest code, then restart the scoreboard
#   update-reboot   pull the latest code, then reboot the Pi
#   reboot          reboot the Pi
#
# Everything it does is appended to var/control/history.log, which is what
# the web UI shows, and to the journal (sports-scoreboard-control).
#
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTROL_DIR="$PROJECT_DIR/var/control"
REQUEST="$CONTROL_DIR/request"
HISTORY="$CONTROL_DIR/history.log"
RUN_USER="${SCOREBOARD_USER:-$(stat -c '%U' "$PROJECT_DIR")}"
UNIT_DIR="${SCOREBOARD_UNIT_DIR:-/etc/systemd/system}"
SERVICE="sports-scoreboard"

[ -f "$REQUEST" ] || exit 0
VERB="$(head -c 32 "$REQUEST" | tr -cd 'a-z-')"
rm -f "$REQUEST"

say() {
  local line
  line="$(date '+%Y-%m-%d %H:%M:%S')  $*"
  printf '%s\n' "$line"
  printf '%s\n' "$line" >> "$HISTORY"
}

finish_history() {
  # Keep the log short and readable by the (unprivileged) web UI.
  if [ -f "$HISTORY" ]; then
    tail -n 200 "$HISTORY" > "$HISTORY.tmp" && mv "$HISTORY.tmp" "$HISTORY"
    chown "$RUN_USER" "$HISTORY" 2>/dev/null || true
    chmod 644 "$HISTORY" 2>/dev/null || true
  fi
}
trap finish_history EXIT

update() {
  # A `sudo git pull` at some point leaves root-owned objects that the
  # unprivileged updater cannot write past. Hand the checkout back.
  if [ -n "$(find "$PROJECT_DIR/.git" ! -user "$RUN_USER" -print -quit 2>/dev/null)" ]; then
    say "Returning ownership of .git to $RUN_USER (something ran git as root)."
    chown -R "$RUN_USER" "$PROJECT_DIR/.git"
  fi

  say "Updating from git..."
  runuser -u "$RUN_USER" -- env SCOREBOARD_UPDATE_FORCE=1 SCOREBOARD_NETWORK_WAIT=30 \
      "$PROJECT_DIR/scripts/auto-update.sh" 2>&1 |
    while IFS= read -r line; do say "  $line"; done
  refresh_units
}

refresh_units() {
  # An update may change the systemd units themselves. Re-render the ones
  # that are installed, so the change takes effect without a manual
  # re-install; never install a unit that was not there before.
  local changed=0 template name target rendered
  for template in "$PROJECT_DIR"/systemd/*.service "$PROJECT_DIR"/systemd/*.path; do
    [ -f "$template" ] || continue
    name="$(basename "$template")"
    target="$UNIT_DIR/$name"
    [ -f "$target" ] || continue
    rendered="$(sed -e "s|__INSTALL_DIR__|$PROJECT_DIR|g" -e "s|__RUN_USER__|$RUN_USER|g" \
                    "$template")"
    if [ "$rendered" != "$(cat "$target")" ]; then
      printf '%s\n' "$rendered" > "$target"
      chmod 644 "$target"
      say "  Updated $name"
      changed=1
    fi
  done
  if [ "$changed" -eq 1 ]; then
    systemctl daemon-reload
  fi
}

case "$VERB" in
  update)
    say "Update requested from the web UI."
    update
    say "Restarting the scoreboard."
    systemctl restart "$SERVICE"
    ;;
  update-reboot)
    say "Update and reboot requested from the web UI."
    update
    say "Rebooting."
    finish_history
    sync
    systemctl reboot
    ;;
  reboot)
    say "Reboot requested from the web UI."
    finish_history
    sync
    systemctl reboot
    ;;
  *)
    say "Ignored an unrecognised request: '${VERB}'."
    ;;
esac
exit 0
