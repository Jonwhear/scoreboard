"""Where the application keeps its files.

Static, version-controlled assets live inside the package; everything the
running application writes lives under ``var/`` at the project root, so the
package directory stays clean and a read-only install is possible.
"""

from __future__ import annotations

import os

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(PACKAGE_DIR)

CONFIG_DIR = os.path.join(PROJECT_ROOT, "config")
DEFAULT_CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

VAR_DIR = os.path.join(PROJECT_ROOT, "var")
LOGO_CACHE_DIR = os.path.join(VAR_DIR, "logos")
TEAM_CACHE_DIR = os.path.join(VAR_DIR, "teams")
HTTP_CACHE_DIR = os.path.join(VAR_DIR, "http")
FONT_CACHE_DIR = os.path.join(VAR_DIR, "fonts")

#: Drop extra .bdf files here to have them picked up automatically.
BUNDLED_FONT_DIR = os.path.join(PACKAGE_DIR, "assets", "fonts")


def config_path() -> str:
    """The config file, overridable with ``SCOREBOARD_CONFIG``."""
    return os.environ.get("SCOREBOARD_CONFIG") or DEFAULT_CONFIG_PATH


def ensure_directories() -> None:
    """Create the writable directories the app needs."""
    for directory in (CONFIG_DIR, VAR_DIR, LOGO_CACHE_DIR, TEAM_CACHE_DIR,
                      HTTP_CACHE_DIR, FONT_CACHE_DIR):
        os.makedirs(directory, exist_ok=True)
