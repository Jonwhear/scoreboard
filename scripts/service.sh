#!/usr/bin/env bash
#
# Thin wrapper over systemctl/journalctl for the scoreboard service, so the
# common operations are one short command.
#
#   ./scripts/service.sh start|stop|restart|status|enable|disable
#   ./scripts/service.sh logs        follow the journal
#   ./scripts/service.sh logs 200    last 200 lines, no follow
#   ./scripts/service.sh errors      warnings and errors only
#
set -euo pipefail
UNIT="sports-scoreboard"
ACTION="${1:-status}"

case "$ACTION" in
  start|stop|restart|enable|disable)
    sudo systemctl "$ACTION" "$UNIT"
    sudo systemctl --no-pager --lines=0 status "$UNIT" || true
    ;;
  status)
    systemctl --no-pager status "$UNIT" || true
    ;;
  logs)
    if [ -n "${2:-}" ]; then
      journalctl -u "$UNIT" -n "$2" --no-pager
    else
      journalctl -u "$UNIT" -f
    fi
    ;;
  errors)
    journalctl -u "$UNIT" -p warning -n "${2:-100}" --no-pager
    ;;
  *)
    sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
    exit 2
    ;;
esac
