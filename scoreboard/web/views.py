"""HTML routes (there is only one page)."""

from __future__ import annotations

from flask import Blueprint, current_app, render_template

bp = Blueprint("views", __name__)


@bp.get("/")
def index():
    services = current_app.extensions["scoreboard"]
    return render_template("index.html", version=services.version)


@bp.get("/healthz")
def healthz():
    return {"ok": True}
