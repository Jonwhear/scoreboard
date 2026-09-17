"""End-to-end: provider → scheduler → state → rotation → render → web.

Everything except the LED hardware, wired together the way ``app.py`` wires
it, driven by the checked-in ESPN fixtures.
"""

from __future__ import annotations

import io
import threading
import time

import pytest
from PIL import Image

from scoreboard.config import ConfigStore
from scoreboard.display.base import Display
from scoreboard.display.fonts import FontRegistry
from scoreboard.logos import LogoCache
from scoreboard.providers.espn import EspnProvider
from scoreboard.runner import DisplayRunner
from scoreboard.scheduler import DataScheduler
from scoreboard.state import AppState
from scoreboard.teams import TeamCatalog
from scoreboard.web import Services, create_app

from conftest import load_fixture


class FixtureClient:
    """Stands in for HttpClient, serving the checked-in ESPN payloads."""

    def __init__(self):
        self.cancel_event = threading.Event()
        self.requests = []
        self.offline = False

    def get_json(self, url, params=None, cache_key=None, allow_stale=True):
        from scoreboard.httpclient import FetchError, FetchResult
        from datetime import datetime, timezone

        self.requests.append(url)
        if self.offline:
            raise FetchError("simulated outage")
        if "/teams" in url:
            payload = load_fixture("espn_nfl_teams.json")
        elif "baseball" in url:
            payload = load_fixture("espn_mlb_scoreboard.json")
        elif "football/nfl" in url:
            payload = load_fixture("espn_nfl_scoreboard.json")
        else:
            payload = {"events": []}
        return FetchResult(data=payload, fetched_at=datetime.now(timezone.utc))

    def get_bytes(self, url):
        raise RuntimeError("no network in tests")

    def close(self):
        pass


class CapturingDisplay(Display):
    backend_name = "capture"

    def __init__(self, width=128, height=32):
        super().__init__(width, height)
        self.frames = []

    def show(self, image):
        self.frames.append(image.copy())


@pytest.fixture
def stack(tmp_path):
    """The whole application graph, minus hardware and minus the network."""
    store = ConfigStore(str(tmp_path / "config.json"))
    store.load()
    store.update({
        "sports": {
            "enabled_leagues": ["nfl", "mlb"],
            "favorite_teams": [{"league": "nfl", "team_id": "16",
                                "abbreviation": "MIN",
                                "display_name": "Minnesota Vikings"}],
        },
        "rotation": {"screen_seconds": 3},
    })

    client = FixtureClient()
    provider = EspnProvider(client=client)
    state = AppState()
    logos = LogoCache(str(tmp_path / "logos"), client=client, enabled=False)
    catalog = TeamCatalog(provider, str(tmp_path / "teams"))
    scheduler = DataScheduler(provider, state, store, logos=logos)
    display = CapturingDisplay()
    fonts = FontRegistry(cache_dir=str(tmp_path / "fonts"))
    runner = DisplayRunner(display, state, store, fonts, logos=logos)

    app = create_app(Services(config_store=store, state=state, runner=runner,
                              scheduler=scheduler, teams=catalog, logos=logos,
                              fonts=fonts, version="itest"))
    app.config.update(TESTING=True)
    return {
        "store": store, "client": client, "provider": provider, "state": state,
        "scheduler": scheduler, "runner": runner, "display": display,
        "catalog": catalog, "web": app.test_client(),
    }


def test_poll_renders_and_serves_a_preview(stack):
    interval = stack["scheduler"].poll_once()
    assert interval == 15, "a favourite is live, so poll fast"

    games = stack["state"].games()
    assert len(games) == 4                       # 3 NFL + 1 MLB
    assert {game.league for game in games} == {"nfl", "mlb"}

    stack["runner"]._tick(time.monotonic())
    display = stack["display"]
    frame = display.frames[-1]
    assert frame.size == (display.width, display.height)

    # The favourite's live game must win the rotation.
    screen = stack["state"].screen
    assert screen.kind == "favorite_live"
    assert screen.layout == "featured"
    assert "MIN" in screen.title

    response = stack["web"].get("/api/preview.png")
    assert response.status_code == 200
    served = Image.open(io.BytesIO(response.data))
    assert served.size == (display.width, display.height)
    assert served.tobytes() == frame.convert("RGB").tobytes(), \
        "the browser preview must be the exact framebuffer sent to the LEDs"


def test_status_reflects_the_live_state(stack):
    stack["scheduler"].poll_once()
    stack["runner"]._tick(time.monotonic())
    status = stack["web"].get("/api/status").get_json()
    assert status["online"] is True and status["stale"] is False
    assert status["screen"]["kind"] == "favorite_live"
    assert {league["league"] for league in status["leagues"]} == {"nfl", "mlb"}
    assert sum(league["game_count"] for league in status["leagues"]) == 4


def test_going_offline_keeps_the_last_good_data(stack):
    stack["scheduler"].poll_once()
    stack["runner"]._tick(time.monotonic())
    assert stack["state"].any_stale is False

    stack["client"].offline = True
    stack["scheduler"]._last_poll.clear()
    interval = stack["scheduler"].poll_once()

    assert len(stack["state"].games()) == 4, "cached games must survive"
    assert stack["state"].any_stale is True
    assert interval > 0

    display = stack["display"]
    stack["runner"]._tick(time.monotonic() + 10)
    assert display.frames[-1].size == (display.width, display.height)
    assert stack["web"].get("/api/status").get_json()["stale"] is True


def test_recovery_after_an_outage(stack):
    stack["client"].offline = True
    stack["scheduler"].poll_once()
    assert stack["state"].any_stale is True

    stack["client"].offline = False
    stack["scheduler"]._last_poll.clear()
    stack["scheduler"].poll_once()
    assert stack["state"].any_stale is False
    assert len(stack["state"].games()) == 4


def test_changing_settings_through_the_api_changes_the_display(stack):
    stack["scheduler"].poll_once()
    stack["runner"]._tick(time.monotonic())
    assert stack["state"].screen.layout == "featured"

    stack["web"].post("/api/config", json={"rotation": {"layout_mode": "cards"}})
    stack["runner"]._last_playlist = None            # force a rebuild
    stack["runner"]._tick(time.monotonic() + 1)
    assert stack["state"].screen.layout == "cards"


def test_removing_a_favourite_changes_the_priority(stack):
    stack["scheduler"].poll_once()
    stack["runner"]._tick(time.monotonic())
    assert stack["state"].screen.kind == "favorite_live"

    stack["web"].delete("/api/favorites/nfl/16")
    stack["runner"]._last_playlist = None
    stack["runner"]._tick(time.monotonic() + 1)
    assert stack["state"].screen.kind != "favorite_live"


def test_favourites_selected_through_the_api_are_used(stack):
    stack["catalog"].refresh("nfl")
    teams = stack["web"].get("/api/teams?league=nfl&q=packers").get_json()["teams"]
    assert teams and teams[0]["team_id"] == "9"

    stack["web"].post("/api/favorites", json={"league": "nfl", "team_id": "9"})
    assert "nfl:9" in stack["store"].config.sports.favorite_keys

    stack["scheduler"].poll_once()
    stack["runner"]._last_playlist = None
    stack["runner"]._tick(time.monotonic())
    assert stack["state"].screen.kind == "favorite_live"


def test_sleep_schedule_blanks_the_panels(stack):
    stack["scheduler"].poll_once()
    stack["web"].post("/api/config", json={
        "sleep": {"enabled": True, "start": "00:00", "end": "23:59"}})
    stack["runner"]._tick(time.monotonic())
    assert stack["display"].frames[-1].getextrema() == ((0, 0), (0, 0), (0, 0))
    assert stack["web"].get("/api/status").get_json()["sleeping"] is True


def test_the_rotation_cycles_through_screens(stack):
    stack["scheduler"].poll_once()
    seen = set()
    base = time.monotonic()
    for step in range(8):
        stack["runner"]._tick(base + step * 3.1)
        seen.add(stack["state"].screen.title)
    assert len(seen) > 1, "several eligible screens should rotate"


def test_full_loop_runs_in_threads_and_shuts_down(stack):
    scheduler, runner = stack["scheduler"], stack["runner"]
    runner.start()
    scheduler.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            if stack["state"].games() and stack["display"].frames:
                break
            time.sleep(0.05)
        assert stack["state"].games(), "the scheduler should have fetched"
        assert stack["display"].frames, "the runner should have drawn"
        assert stack["state"].frame_png is not None
    finally:
        scheduler.stop()
        runner.stop()
        runner.join(timeout=5)
        scheduler.join(timeout=5)
    assert not runner.is_alive() and not scheduler.is_alive()
