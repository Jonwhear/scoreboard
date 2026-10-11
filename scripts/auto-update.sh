#!/usr/bin/env bash
#
# Pull the latest code at boot, then hand over to the scoreboard service.
#
# Run by sports-scoreboard-update.service, which is ordered Before= the
# scoreboard itself, so the Pi always starts on the newest commit without
# anyone attaching a screen.
#
# The point of this script is that a bad commit must not leave you with a
# dark board and no way to see why. So:
#
#   * the commit that is running now is recorded before anything changes
#   * after updating, the app is asked to start up once, headless, with a
#     timeout; if it cannot, the update is rolled back
#   * a rollback is recorded so the next boot does not try the same commit
#     again and again
#   * no network, or no new commits, is a no-op, not a failure
#
# It never touches config/config.json or var/ -- those are gitignored, so
# favourites and settings survive every update.
#
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR" || exit 0

STATE_DIR="$PROJECT_DIR/var"
LAST_GOOD="$STATE_DIR/last-good-commit"
BLOCKED="$STATE_DIR/blocked-commit"
VENV_PY="$PROJECT_DIR/.venv/bin/python"
STARTUP_TIMEOUT="${SCOREBOARD_UPDATE_TIMEOUT:-90}"

log() { printf '%s\n' "$*"; }

mkdir -p "$STATE_DIR" 2>/dev/null || true

if [ ! -d .git ]; then
  log "Not a git checkout; nothing to update."
  exit 0
fi
if [ ! -x "$VENV_PY" ]; then
  log "No virtualenv yet; skipping update (run scripts/install.sh)."
  exit 0
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '')"
if [ -z "$BRANCH" ] || [ "$BRANCH" = "HEAD" ]; then
  log "Detached HEAD; refusing to update automatically."
  exit 0
fi

CURRENT="$(git rev-parse HEAD)"
log "On $BRANCH at ${CURRENT:0:8}"

# A dirty tree means someone edited on the Pi. Never clobber that.
if ! git diff --quiet || ! git diff --cached --quiet; then
  log "Working tree has local changes; leaving them alone."
  exit 0
fi

if ! timeout 60 git fetch --quiet origin "$BRANCH" 2>/dev/null; then
  log "Could not reach the remote (offline?); continuing on the current commit."
  exit 0
fi

TARGET="$(git rev-parse "origin/$BRANCH" 2>/dev/null || echo "$CURRENT")"
if [ "$TARGET" = "$CURRENT" ]; then
  log "Already up to date."
  echo "$CURRENT" > "$LAST_GOOD"
  exit 0
fi

if [ -f "$BLOCKED" ] && [ "$(cat "$BLOCKED")" = "$TARGET" ]; then
  log "Commit ${TARGET:0:8} already failed to start once; staying on ${CURRENT:0:8}."
  exit 0
fi

log "Updating ${CURRENT:0:8} -> ${TARGET:0:8}"
echo "$CURRENT" > "$LAST_GOOD"

if ! git merge --ff-only --quiet "$TARGET" 2>/dev/null; then
  log "Cannot fast-forward (branch rewritten?); staying put."
  exit 0
fi

# Only reinstall when the dependency list actually moved: pip is slow on a Pi.
if ! git diff --quiet "$CURRENT" "$TARGET" -- requirements.txt; then
  log "requirements.txt changed; installing dependencies."
  if ! "$VENV_PY" -m pip install --quiet -r requirements.txt; then
    log "Dependency install failed; rolling back."
    git reset --hard --quiet "$CURRENT"
    echo "$TARGET" > "$BLOCKED"
    exit 0
  fi
fi

# Prove the new commit can actually start before letting the service have
# it. --check-only initialises everything the service would, without
# touching the matrix, and exits.
log "Checking that ${TARGET:0:8} starts..."
CHECK_LOG="$STATE_DIR/last-update-check.log"
if timeout "$STARTUP_TIMEOUT" "$VENV_PY" -m scoreboard.app --check-only \
      > "$CHECK_LOG" 2>&1; then
  log "Update to ${TARGET:0:8} looks good."
  echo "$TARGET" > "$LAST_GOOD"
  rm -f "$BLOCKED"
else
  STATUS=$?
  log "Commit ${TARGET:0:8} failed its startup check (exit $STATUS); rolling back to ${CURRENT:0:8}."
  # Put the reason in the journal: on a headless Pi this log is the only
  # way to find out why an update was refused.
  log "--- startup check output (also in $CHECK_LOG) ---"
  tail -n 15 "$CHECK_LOG" 2>/dev/null | while IFS= read -r line; do log "    $line"; done
  log "--- end ---"
  git reset --hard --quiet "$CURRENT"
  echo "$TARGET" > "$BLOCKED"
fi
exit 0
