"""Software version, update checks and update/reboot requests.

The web UI runs unprivileged, so it never updates or reboots anything
itself. It reads the git checkout to say what is installed and what is
waiting upstream, and to act it writes a single word to
``var/control/request``. ``sports-scoreboard-control.path`` notices the
file and runs ``scripts/control.sh`` as root, which accepts only the words
in :data:`ACTIONS` and logs what it did to ``var/control/history.log``.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

#: Everything the web UI may ask for, and nothing else.
ACTIONS = ("update", "update-reboot", "reboot")

UNIT_DIR = "/etc/systemd/system"
CONTROL_UNIT = "sports-scoreboard-control.path"
UPDATE_UNIT = "sports-scoreboard-update.service"
GIT_TIMEOUT = 45


class SystemControl:
    """What is installed, what is available, and a way to ask for a change."""

    def __init__(self, project_dir: str, control_dir: str, unit_dir: str = UNIT_DIR) -> None:
        self.project_dir = project_dir
        self.control_dir = control_dir
        self.unit_dir = unit_dir

    # -- facts -------------------------------------------------------------

    @property
    def request_path(self) -> str:
        return os.path.join(self.control_dir, "request")

    @property
    def history_path(self) -> str:
        return os.path.join(self.control_dir, "history.log")

    def controls_installed(self) -> bool:
        """True once install.sh has set up the root helper the buttons need."""
        return os.path.exists(os.path.join(self.unit_dir, CONTROL_UNIT))

    def boot_update_enabled(self) -> bool:
        wants = os.path.join(self.unit_dir, "multi-user.target.wants", UPDATE_UNIT)
        return os.path.exists(wants)

    def pending(self) -> Optional[str]:
        """The request waiting to be picked up, if any."""
        try:
            with open(self.request_path, "r", encoding="utf-8") as handle:
                return handle.read().strip() or None
        except OSError:
            return None

    def history(self, lines: int = 20) -> List[str]:
        try:
            with open(self.history_path, "r", encoding="utf-8", errors="replace") as handle:
                return [line.rstrip("\n") for line in handle.readlines()[-lines:]]
        except OSError:
            return []

    def version(self) -> Dict[str, Any]:
        """The commit the Pi is running."""
        info: Dict[str, Any] = {"branch": None, "commit": None, "date": None,
                                "subject": None}
        branch = self._git("rev-parse", "--abbrev-ref", "HEAD")
        line = self._git("log", "-1", "--format=%h%x1f%cI%x1f%s")
        if branch:
            info["branch"] = branch
        if line and line.count("\x1f") == 2:
            info["commit"], info["date"], info["subject"] = line.split("\x1f")
        return info

    def describe(self) -> Dict[str, Any]:
        return {
            "version": self.version(),
            "controls_installed": self.controls_installed(),
            "boot_update_enabled": self.boot_update_enabled(),
            "pending": self.pending(),
            "history": self.history(),
        }

    # -- actions -------------------------------------------------------------

    def check(self) -> Dict[str, Any]:
        """Fetch from GitHub and list the commits the Pi does not have yet."""
        branch = self._git("rev-parse", "--abbrev-ref", "HEAD")
        if not branch or branch == "HEAD":
            return {"ok": False, "error": "not on a branch, so there is nothing to follow"}
        try:
            subprocess.run(
                ["git", "fetch", "--quiet", "origin", branch], cwd=self.project_dir,
                check=True, capture_output=True, text=True, timeout=GIT_TIMEOUT,
            )
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or exc.stdout or "").strip() or f"exit {exc.returncode}"
            return {"ok": False, "error": f"git fetch failed: {message}"}
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "error": f"git fetch failed: {exc}"}
        log_lines = self._git("log", "--format=%h%x1f%s", f"HEAD..origin/{branch}") or ""
        commits = [dict(zip(("commit", "subject"), line.split("\x1f", 1)))
                   for line in log_lines.splitlines() if "\x1f" in line]
        return {"ok": True, "branch": branch, "behind": len(commits), "commits": commits[:20]}

    def request(self, action: str) -> None:
        """Ask the root helper to update and/or reboot.  Raises ValueError."""
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action!r}; expected one of {', '.join(ACTIONS)}")
        os.makedirs(self.control_dir, exist_ok=True)
        # Write then rename, so the path unit never sees a half-written file.
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.control_dir,
                                             prefix=".request-", delete=False)
        with handle:
            handle.write(action + "\n")
        os.chmod(handle.name, 0o644)
        os.replace(handle.name, self.request_path)
        log.info("Requested %s from the web UI", action)

    # -- helpers -------------------------------------------------------------

    def _git(self, *args: str) -> Optional[str]:
        try:
            result = subprocess.run(
                ["git", *args], cwd=self.project_dir, capture_output=True, text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.debug("git %s failed: %s", " ".join(args), exc)
            return None
        if result.returncode != 0:
            log.debug("git %s failed: %s", " ".join(args), result.stderr.strip())
            return None
        return result.stdout.strip()
