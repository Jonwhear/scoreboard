"""JSON API for the configuration UI.

Every handler is small and returns plain dictionaries.  Validation lives in
:mod:`scoreboard.config`, so a hostile or buggy client cannot write a
configuration the rest of the app would choke on.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from flask import Blueprint, Response, current_app, jsonify, request

from ..leagues import all_leagues, normalize_league_id
from ..models import team_key
from ..runner import TEST_SCREENS

log = logging.getLogger(__name__)

bp = Blueprint("api", __name__)


def services():
    return current_app.extensions["scoreboard"]


def _bad_request(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


# -- status ----------------------------------------------------------------

@bp.get("/status")
def status():
    svc = services()
    config = svc.config_store.config
    state = svc.state
    payload: Dict[str, Any] = state.to_dict()
    payload.update(
        {
            "version": svc.version,
            "config_source": svc.config_store.source,
            "config_path": svc.config_store.path,
            "matrix": {
                "rows": config.display.rows,
                "cols": config.display.cols,
                "chain_length": config.display.chain_length,
                "parallel": config.display.parallel,
                "gpio_mapping": config.display.gpio_mapping,
                "slowdown_gpio": config.display.slowdown_gpio,
                "brightness": config.display.brightness,
                "canvas": f"{config.display.width}x{config.display.height}",
            },
            "enabled_leagues": list(config.sports.enabled_leagues),
            "favorite_count": len(config.sports.favorite_teams),
            "sleep_window": (
                f"{config.sleep.start}-{config.sleep.end}" if config.sleep.enabled else None
            ),
        }
    )
    if svc.logos is not None:
        payload["logos"] = svc.logos.stats()
    if svc.fonts is not None:
        payload["fonts"] = svc.fonts.describe()
    return jsonify(payload)


@bp.get("/games")
def games():
    svc = services()
    return jsonify({"games": [game.to_dict() for game in sorted(
        svc.state.games(), key=lambda game: (game.league, game.start_time, game.game_id))]})


# -- configuration ---------------------------------------------------------

@bp.get("/config")
def get_config():
    return jsonify(services().config_store.config.to_dict())


@bp.post("/config")
def post_config():
    patch = request.get_json(silent=True)
    if not isinstance(patch, dict):
        return _bad_request("expected a JSON object")
    svc = services()
    try:
        config = svc.config_store.update(patch)
    except OSError as exc:
        log.error("Could not persist configuration: %s", exc)
        return _bad_request(f"could not save configuration: {exc}", 500)
    if svc.scheduler is not None:
        svc.scheduler.request_refresh()
    return jsonify({"ok": True, "config": config.to_dict()})


@bp.get("/leagues")
def leagues():
    enabled = set(services().config_store.config.sports.enabled_leagues)
    return jsonify(
        {
            "leagues": [
                {
                    "id": league.id,
                    "name": league.name,
                    "short_name": league.short_name,
                    "sport": league.sport,
                    "enabled": league.id in enabled,
                }
                for league in all_leagues()
            ]
        }
    )


# -- teams and favourites --------------------------------------------------

@bp.get("/teams")
def teams():
    svc = services()
    league = normalize_league_id(request.args.get("league", ""))
    if not league:
        return _bad_request("unknown or missing league")
    if svc.teams is None:
        return jsonify({"league": league, "teams": [], "loading": False})
    query = request.args.get("q", "")
    try:
        limit = max(1, min(200, int(request.args.get("limit", 40))))
    except ValueError:
        limit = 40
    matches = svc.teams.search(league, query, limit=limit)
    favorites = svc.config_store.config.sports.favorite_keys
    return jsonify(
        {
            "league": league,
            "loading": not svc.teams.is_loaded(league),
            "teams": [
                dict(team.to_dict(), favorite=team.key in favorites) for team in matches
            ],
        }
    )


@bp.get("/favorites")
def get_favorites():
    svc = services()
    config = svc.config_store.config
    result: List[Dict[str, Any]] = []
    for favorite in config.sports.favorite_teams:
        entry = {
            "league": favorite.league,
            "team_id": favorite.team_id,
            "abbreviation": favorite.abbreviation,
            "display_name": favorite.display_name,
            "key": favorite.key,
        }
        if svc.teams is not None:
            info = svc.teams.lookup(favorite.league, favorite.team_id)
            if info is not None:
                entry["abbreviation"] = info.abbreviation or entry["abbreviation"]
                entry["display_name"] = info.display_name or entry["display_name"]
                entry["logo_url"] = info.logo_url
                entry["color"] = info.color
        result.append(entry)
    return jsonify({"favorites": result})


@bp.post("/favorites")
def add_favorite():
    payload = request.get_json(silent=True) or {}
    league = normalize_league_id(str(payload.get("league", "")))
    team_id = str(payload.get("team_id", "")).strip()
    if not league or not team_id:
        return _bad_request("league and team_id are required")

    svc = services()
    config = svc.config_store.config
    wanted = team_key(league, team_id)
    if wanted in config.sports.favorite_keys:
        return jsonify({"ok": True, "unchanged": True})

    abbreviation = str(payload.get("abbreviation", "") or "")
    display_name = str(payload.get("display_name", "") or "")
    if svc.teams is not None and not display_name:
        info = svc.teams.lookup(league, team_id)
        if info is not None:
            abbreviation = abbreviation or info.abbreviation
            display_name = info.display_name

    favorites = [
        {
            "league": favorite.league,
            "team_id": favorite.team_id,
            "abbreviation": favorite.abbreviation,
            "display_name": favorite.display_name,
        }
        for favorite in config.sports.favorite_teams
    ]
    favorites.append(
        {
            "league": league,
            "team_id": team_id,
            "abbreviation": abbreviation,
            "display_name": display_name,
        }
    )
    patch: Dict[str, Any] = {"sports": {"favorite_teams": favorites}}
    # Adding a favourite in a league that is switched off is almost always a
    # mistake, so switch the league on too.
    if league not in config.sports.enabled_leagues:
        patch["sports"]["enabled_leagues"] = list(config.sports.enabled_leagues) + [league]
    svc.config_store.update(patch)
    if svc.scheduler is not None:
        svc.scheduler.request_refresh()
    log.info("Added favourite %s", wanted)
    return jsonify({"ok": True})


@bp.delete("/favorites/<league>/<team_id>")
def remove_favorite(league: str, team_id: str):
    svc = services()
    canonical = normalize_league_id(league)
    if not canonical:
        return _bad_request("unknown league")
    config = svc.config_store.config
    target = team_key(canonical, team_id)
    remaining = [
        {
            "league": favorite.league,
            "team_id": favorite.team_id,
            "abbreviation": favorite.abbreviation,
            "display_name": favorite.display_name,
        }
        for favorite in config.sports.favorite_teams
        if favorite.key != target
    ]
    if len(remaining) == len(config.sports.favorite_teams):
        return jsonify({"ok": True, "unchanged": True})
    svc.config_store.update({"sports": {"favorite_teams": remaining}})
    log.info("Removed favourite %s", target)
    return jsonify({"ok": True})


# -- preview and actions ---------------------------------------------------

@bp.get("/preview.png")
def preview_png():
    svc = services()
    png = svc.state.frame_png
    if png is None:
        return _bad_request("no frame has been rendered yet", 503)
    return Response(png, mimetype="image/png")


@bp.post("/actions/next")
def action_next():
    svc = services()
    if svc.runner is None:
        return _bad_request("display loop is not running", 503)
    svc.runner.force_next()
    return jsonify({"ok": True})


@bp.post("/actions/refresh")
def action_refresh():
    svc = services()
    if svc.scheduler is None:
        return _bad_request("data scheduler is not running", 503)
    svc.scheduler.request_refresh()
    return jsonify({"ok": True})


@bp.post("/actions/test")
def action_test():
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("screen", "")).strip()
    svc = services()
    if svc.runner is None:
        return _bad_request("display loop is not running", 503)
    if name == "clear":
        svc.runner.clear_override()
        return jsonify({"ok": True})
    try:
        seconds = float(payload.get("seconds", 20))
    except (TypeError, ValueError):
        seconds = 20.0
    if not svc.runner.show_test_screen(name, seconds):
        return _bad_request(f"unknown test screen {name!r}; expected one of "
                            + ", ".join(TEST_SCREENS))
    return jsonify({"ok": True})
