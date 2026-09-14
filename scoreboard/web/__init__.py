"""The local web configuration UI.

Flask serves a single page plus a small JSON API.  Everything the UI can
change is written through :class:`~scoreboard.config.ConfigStore`, which
validates and persists atomically; the display and data threads pick the
new values up on their next iteration, so nothing needs restarting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from flask import Flask

log = logging.getLogger(__name__)


@dataclass
class Services:
    """Everything the web layer is allowed to touch."""

    config_store: object
    state: object
    runner: Optional[object] = None
    scheduler: Optional[object] = None
    teams: Optional[object] = None
    logos: Optional[object] = None
    fonts: Optional[object] = None
    version: str = "0"


def create_app(services: Services) -> Flask:
    """Build the Flask application."""
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config["JSON_SORT_KEYS"] = False
    app.extensions["scoreboard"] = services

    from .api import bp as api_bp
    from .views import bp as views_bp

    app.register_blueprint(views_bp)
    app.register_blueprint(api_bp, url_prefix="/api")

    @app.after_request
    def _no_store(response):
        # Status and preview must never be served from a phone's cache.
        if response.mimetype in ("application/json", "image/png"):
            response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    return app
