"""Root only for as long as the matrix needs it.

rpi-rgb-led-matrix must be initialized as root: it maps ``/dev/mem`` and
asks for a real-time thread.  Nothing else in this application needs root,
so once the matrix is up we permanently drop to an unprivileged account.

We do this ourselves rather than using the library's ``drop_privileges``
option because the library drops to the ``daemon`` account, which cannot
write ``config.json`` or the logo cache.  Dropping to the account that owns
the installation keeps file ownership sane.
"""

from __future__ import annotations

import logging
import os
import pwd
from typing import Optional

log = logging.getLogger(__name__)


def is_root() -> bool:
    return os.geteuid() == 0


def current_username() -> str:
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:  # pragma: no cover - uid without a passwd entry
        return str(os.geteuid())


def resolve_target_user(configured: str = "") -> Optional[str]:
    """Pick the account to drop to.

    Preference order: the configured user, then the account behind ``sudo``,
    then the owner of this source tree.  Returns ``None`` when no sensible
    unprivileged account can be identified.
    """
    candidates = [
        configured.strip(),
        os.environ.get("SUDO_USER", "").strip(),
        _source_owner(),
    ]
    for candidate in candidates:
        if not candidate or candidate == "root":
            continue
        try:
            pwd.getpwnam(candidate)
        except KeyError:
            log.warning("Configured run_as_user %r does not exist; ignoring it", candidate)
            continue
        return candidate
    return None


def _source_owner() -> str:
    try:
        uid = os.stat(os.path.dirname(os.path.abspath(__file__))).st_uid
        return pwd.getpwuid(uid).pw_name
    except (OSError, KeyError):
        return ""


def drop_privileges(username: Optional[str]) -> bool:
    """Irreversibly become ``username``.  Returns True if we dropped."""
    if not is_root():
        log.debug("Not running as root; no privileges to drop")
        return False
    if not username:
        log.warning(
            "Still running as root: no unprivileged account was identified. "
            "Set display.run_as_user in config.json to drop privileges."
        )
        return False
    try:
        entry = pwd.getpwnam(username)
    except KeyError:
        log.error("Cannot drop privileges: user %r does not exist", username)
        return False

    try:
        os.initgroups(entry.pw_name, entry.pw_gid)
        os.setgid(entry.pw_gid)
        os.setuid(entry.pw_uid)
    except OSError as exc:
        log.error("Failed to drop privileges to %s: %s", username, exc)
        return False

    if os.geteuid() == 0:  # paranoia: never claim success while still root
        log.error("Privilege drop to %s did not take effect", username)
        return False

    os.environ["HOME"] = entry.pw_dir
    os.environ["USER"] = entry.pw_name
    os.environ["LOGNAME"] = entry.pw_name
    log.info("Dropped privileges to %s (uid=%d gid=%d)", username, entry.pw_uid, entry.pw_gid)
    return True
