"""The configuration API the web UI drives."""

from __future__ import annotations

import json

import pytest

from scoreboard.config import ConfigStore
from scoreboard.display.fonts import FontRegistry
from scoreboard.models import LeagueSnapshot, TeamInfo
from scoreboard.state import AppState
from scoreboard.web import Services, create_app


class StubRunner:
    def __init__(self):
        self.advanced = 0
        self.tests = []
        self.cleared = 0

    def force_next(self):
        self.advanced += 1

    def show_test_screen(self, name, seconds=15.0):
        from scoreboard.runner import TEST_SCREENS
        if name not in TEST_SCREENS:
            return False
        self.tests.append((name, seconds))
        return True

    def clear_override(self):
        self.cleared += 1


class StubScheduler:
    def __init__(self):
        self.refreshes = 0

    def request_refresh(self):
        self.refreshes += 1


class StubCatalog:
    def __init__(self, teams):
        self.teams = teams

    def search(self, league, query="", limit=40):
        needle = query.lower()
        return [team for team in self.teams
                if team.league == league and needle in team.display_name.lower()][:limit]

    def is_loaded(self, league):
        return any(team.league == league for team in self.teams)

    def lookup(self, league, team_id):
        for team in self.teams:
            if team.league == league and team.team_id == team_id:
                return team
        return None


@pytest.fixture
def services(tmp_path):
    store = ConfigStore(str(tmp_path / "config.json"))
    store.load()
    catalog = StubCatalog([
        TeamInfo("nfl", "16", "MIN", "Minnesota Vikings", color="#4f2683"),
        TeamInfo("nfl", "9", "GB", "Green Bay Packers", color="#204e32"),
        TeamInfo("nhl", "3", "NYR", "New York Rangers"),
    ])
    return Services(
        config_store=store, state=AppState(), runner=StubRunner(),
        scheduler=StubScheduler(), teams=catalog, logos=None,
        fonts=FontRegistry(cache_dir=str(tmp_path / "fonts")), version="test",
    )


@pytest.fixture
def client(services):
    app = create_app(services)
    app.config.update(TESTING=True)
    return app.test_client()


# -- status ----------------------------------------------------------------

def test_index_page_renders(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"Scoreboard" in response.data
    assert b"preview" in response.data


def test_status_reports_matrix_configuration(client, services):
    payload = client.get("/api/status").get_json()
    display = services.config_store.config.display
    assert payload["matrix"]["chain_length"] == display.chain_length
    assert payload["matrix"]["canvas"] == f"{display.width}x{display.height}"
    assert payload["matrix"]["cols"] == 64 and payload["matrix"]["rows"] == 32
    assert payload["matrix"]["gpio_mapping"] == "adafruit-hat"
    assert "uptime_seconds" in payload and "screen" in payload
    assert payload["version"] == "test"


def test_status_includes_league_health(client, services, nfl_games):
    services.state.update_snapshot(LeagueSnapshot("nfl", games=nfl_games))
    payload = client.get("/api/status").get_json()
    assert payload["leagues"][0]["game_count"] == 3
    assert payload["online"] is True


def test_games_endpoint(client, services, nfl_games):
    services.state.update_snapshot(LeagueSnapshot("nfl", games=nfl_games))
    games = client.get("/api/games").get_json()["games"]
    assert len(games) == 3
    assert {"home", "away", "state", "league"} <= set(games[0])


# -- configuration ---------------------------------------------------------

def test_brightness_can_be_changed(client, services):
    response = client.post("/api/config", json={"display": {"brightness": 80}})
    assert response.status_code == 200
    assert services.config_store.config.display.brightness == 80
    assert client.get("/api/config").get_json()["display"]["brightness"] == 80


def test_rotation_settings_can_be_changed(client, services):
    client.post("/api/config", json={"rotation": {
        "screen_seconds": 20, "layout_mode": "cards", "favorites_only": True}})
    rotation = services.config_store.config.rotation
    assert (rotation.screen_seconds, rotation.layout_mode, rotation.favorites_only) \
        == (20, "cards", True)


def test_enabled_leagues_can_be_changed(client, services):
    client.post("/api/config", json={"sports": {"enabled_leagues": ["nhl", "epl"]}})
    assert services.config_store.config.sports.enabled_leagues == ["nhl", "epl"]


def test_config_changes_are_persisted_to_disk(client, services):
    client.post("/api/config", json={"display": {"brightness": 12}})
    with open(services.config_store.path) as handle:
        assert json.load(handle)["display"]["brightness"] == 12


def test_invalid_config_values_are_clamped_not_rejected(client, services):
    response = client.post("/api/config", json={"display": {"brightness": 10_000}})
    assert response.status_code == 200
    assert services.config_store.config.display.brightness == 100


def test_non_object_config_is_rejected(client):
    assert client.post("/api/config", json=["nope"]).status_code == 400


def test_config_change_triggers_a_data_refresh(client, services):
    client.post("/api/config", json={"sports": {"enabled_leagues": ["nhl"]}})
    assert services.scheduler.refreshes >= 1


def test_leagues_endpoint_lists_everything_with_enabled_flags(client):
    leagues = client.get("/api/leagues").get_json()["leagues"]
    ids = {league["id"] for league in leagues}
    assert {"nfl", "ncaaf", "mlb", "nhl", "nba", "ncaam", "mls", "epl",
            "efl_championship"} == ids
    assert {league["id"] for league in leagues if league["enabled"]} == \
        {"nfl", "mlb", "nhl", "nba"}


# -- favourites ------------------------------------------------------------

def test_teams_can_be_searched(client):
    payload = client.get("/api/teams?league=nfl&q=pack").get_json()
    assert [team["abbreviation"] for team in payload["teams"]] == ["GB"]
    assert payload["loading"] is False


def test_team_search_rejects_unknown_leagues(client):
    assert client.get("/api/teams?league=quidditch").status_code == 400
    assert client.get("/api/teams").status_code == 400


def test_favourites_can_be_added_and_removed(client, services):
    added = client.post("/api/favorites", json={"league": "nfl", "team_id": "16"})
    assert added.status_code == 200
    favourites = services.config_store.config.sports.favorite_teams
    assert [favourite.key for favourite in favourites] == ["nfl:16"]
    assert favourites[0].display_name == "Minnesota Vikings", "filled in from the catalog"

    listed = client.get("/api/favorites").get_json()["favorites"]
    assert listed[0]["color"] == "#4f2683"

    client.delete("/api/favorites/nfl/16")
    assert services.config_store.config.sports.favorite_teams == []


def test_adding_a_favourite_enables_its_league(client, services):
    client.post("/api/favorites", json={"league": "epl", "team_id": "359",
                                        "display_name": "Arsenal"})
    assert "epl" in services.config_store.config.sports.enabled_leagues


def test_adding_the_same_favourite_twice_is_a_no_op(client, services):
    client.post("/api/favorites", json={"league": "nfl", "team_id": "16"})
    response = client.post("/api/favorites", json={"league": "nfl", "team_id": "16"})
    assert response.get_json()["unchanged"] is True
    assert len(services.config_store.config.sports.favorite_teams) == 1


def test_favourite_requires_league_and_id(client):
    assert client.post("/api/favorites", json={"league": "nfl"}).status_code == 400
    assert client.post("/api/favorites", json={"team_id": "1"}).status_code == 400


def test_removing_an_unknown_favourite_is_harmless(client):
    assert client.delete("/api/favorites/nfl/99999").get_json()["unchanged"] is True


# -- preview and actions ---------------------------------------------------

def test_preview_returns_the_current_framebuffer(client, services, render_context):
    from scoreboard.display.layouts import render_idle
    from scoreboard.display.preview import to_png_bytes

    assert client.get("/api/preview.png").status_code == 503, "no frame yet"
    services.state.set_frame_png(to_png_bytes(render_idle(render_context)))

    response = client.get("/api/preview.png")
    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert response.headers["Cache-Control"] == "no-store, max-age=0"

    from PIL import Image
    import io
    display = services.config_store.config.display
    assert Image.open(io.BytesIO(response.data)).size == (display.width, display.height)


def test_force_next_screen(client, services):
    assert client.post("/api/actions/next").status_code == 200
    assert services.runner.advanced == 1


def test_manual_refresh(client, services):
    before = services.scheduler.refreshes
    client.post("/api/actions/refresh")
    assert services.scheduler.refreshes == before + 1


def test_test_screens(client, services):
    assert client.post("/api/actions/test", json={"screen": "clock"}).status_code == 200
    assert services.runner.tests[-1][0] == "clock"
    assert client.post("/api/actions/test", json={"screen": "clear"}).status_code == 200
    assert services.runner.cleared == 1
    assert client.post("/api/actions/test", json={"screen": "nope"}).status_code == 400


def test_actions_fail_cleanly_without_a_runner(tmp_path, services):
    services.runner = None
    services.scheduler = None
    app = create_app(services)
    client = app.test_client()
    assert client.post("/api/actions/next").status_code == 503
    assert client.post("/api/actions/refresh").status_code == 503


def test_league_errors_are_summarised_for_the_status_page(client, services):
    from scoreboard.models import LeagueSnapshot

    services.state.update_snapshot(LeagueSnapshot(
        "nfl", error=("GET https://site.api.espn.com/apis/site/v2/sports/football/nfl/"
                      "scoreboard failed after 4 attempts: HTTPSConnectionPool("
                      "host='site.api.espn.com', port=443): Max retries exceeded")))
    league = client.get("/api/status").get_json()["leagues"][0]
    assert league["error_short"] == "unreachable"
    assert len(league["error"]) > 80, "the full detail is still available"


def test_cached_data_is_summarised_as_such(client, services):
    from scoreboard.models import LeagueSnapshot

    services.state.update_snapshot(LeagueSnapshot(
        "nfl", games=[], stale=True, error="cached response (upstream unavailable)"))
    league = client.get("/api/status").get_json()["leagues"][0]
    assert league["error_short"] == "using cached data"
