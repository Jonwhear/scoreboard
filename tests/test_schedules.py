"""Favourite-team schedules: fetching, caching, merging, rotation and layout."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from scoreboard.config import Config
from scoreboard.display import layouts
from scoreboard.display.layouts import RenderContext
from scoreboard.httpclient import FetchError, FetchResult
from scoreboard.models import GameState, TeamSchedule
from scoreboard.providers.base import ProviderError, SportsProvider
from scoreboard.providers.espn import EspnProvider
from scoreboard.rotation import Layout, Rotator, ScreenKind, build_playlist
from scoreboard.samples import sample_card_games, sample_schedule
from scoreboard.schedules import ScheduleCache, build_team_schedule

from conftest import load_fixture

#: Between the fixture's two finals and its five upcoming games.
NOW = datetime(2025, 10, 25, 12, 0, tzinfo=timezone.utc)


class RecordingClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get_json(self, url, params=None, cache_key=None, allow_stale=True):
        self.calls.append((url, params))
        return FetchResult(data=self.payload, fetched_at=datetime.now(timezone.utc))


@pytest.fixture
def season():
    client = RecordingClient(load_fixture("espn_nfl_team_schedule.json"))
    return EspnProvider(client=client).fetch_team_schedule("nfl", "8")


def make_config(favorites=("8",), **rotation):
    return Config.from_dict({
        "sports": {
            "enabled_leagues": ["nfl", "mlb", "nhl"],
            "favorite_teams": [{"league": "nfl", "team_id": team_id, "abbreviation": "DET"}
                               for team_id in favorites],
        },
        "rotation": rotation,
    })


# -- fetching --------------------------------------------------------------

def test_the_schedule_endpoint_is_per_team():
    client = RecordingClient(load_fixture("espn_nfl_team_schedule.json"))
    EspnProvider(client=client).fetch_team_schedule("nfl", "8")
    url, params = client.calls[0]
    assert url.endswith("/football/nfl/teams/8/schedule")
    assert not params


def test_soccer_asks_for_fixtures_not_results():
    client = RecordingClient({"events": []})
    EspnProvider(client=client).fetch_team_schedule("epl", "359")
    assert client.calls[0][1] == {"fixture": "true"}


def test_a_season_is_parsed_in_date_order(season):
    assert [game.game_id for game in season] == ["401", "402", "403", "404", "405",
                                                 "406", "407"]
    assert [game.state for game in season[:3]] == [GameState.POST, GameState.POST,
                                                   GameState.PRE]


def test_schedule_scores_are_objects_not_strings(season):
    first = season[0]
    assert (first.home.abbreviation, first.home.score) == ("DET", 34)
    assert first.away.score == 31


def test_schedule_broadcasts_are_read(season):
    assert [game.broadcast for game in season[2:5]] == ["FOX", "CBS", "NBC"]


def test_a_provider_without_schedules_says_so():
    class Bare(SportsProvider):
        def supported_leagues(self):
            return []

        def fetch_scoreboard(self, league_id):
            raise NotImplementedError

        def fetch_teams(self, league_id):
            return []

    with pytest.raises(ProviderError):
        Bare().fetch_team_schedule("nfl", "8")


# -- merging ---------------------------------------------------------------

def test_the_next_games_are_the_unplayed_ones(season):
    schedule = build_team_schedule("nfl", "8", season, [], limit=4, now=NOW)
    assert [game.game_id for game in schedule.games] == ["403", "404", "405", "406"]
    assert schedule.team.abbreviation == "DET"
    assert schedule.team.logo_url


def test_the_limit_is_respected(season):
    assert len(build_team_schedule("nfl", "8", season, [], limit=2, now=NOW).games) == 2
    assert len(build_team_schedule("nfl", "8", season, [], limit=10, now=NOW).games) == 5


def test_the_scoreboard_copy_of_a_game_wins(season):
    """Tonight's game turns live on the scoreboard before the schedule knows."""
    scheduled = season[2]
    live = replace(scheduled, state=GameState.IN, period=2,
                   home=replace(scheduled.home, score=7),
                   away=replace(scheduled.away, score=3))
    schedule = build_team_schedule("nfl", "8", season, [live], limit=4,
                                   now=scheduled.start_time + timedelta(hours=1))
    assert schedule.games[0].is_live and schedule.games[0].home.score == 7


def test_a_game_the_scoreboard_says_is_over_drops_off(season):
    finished = replace(season[2], state=GameState.POST)
    schedule = build_team_schedule("nfl", "8", season, [finished], limit=4,
                                   now=season[2].start_time + timedelta(hours=4))
    assert schedule.games[0].game_id == "404"


def test_a_team_with_nothing_coming_has_no_schedule(season):
    later = datetime(2026, 3, 1, tzinfo=timezone.utc)
    assert build_team_schedule("nfl", "8", season, [], limit=4, now=later) is None
    assert build_team_schedule("nfl", "8", None, [], limit=4, now=NOW) is None


def test_other_teams_games_are_ignored(season):
    other = sample_card_games()
    schedule = build_team_schedule("nfl", "8", season, other, limit=10, now=NOW)
    assert all(game.involves_any(("nfl:8",)) for game in schedule.games)


# -- caching ---------------------------------------------------------------

class StubProvider(SportsProvider):
    def __init__(self, games=None, error=None):
        self.games = games or []
        self.error = error
        self.calls = 0

    def supported_leagues(self):
        return ["nfl"]

    def fetch_scoreboard(self, league_id):
        raise NotImplementedError

    def fetch_teams(self, league_id):
        return []

    def fetch_team_schedule(self, league_id, team_id):
        self.calls += 1
        if self.error:
            raise self.error
        return list(self.games)


def wait_for(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_a_lookup_never_blocks_and_fills_in_the_background(season):
    cache = ScheduleCache(StubProvider(season))
    assert cache.get("nfl", "8") is None
    assert wait_for(lambda: cache.get("nfl", "8", refresh_if_stale=False))
    assert len(cache.get("nfl", "8")) == 7


def test_a_fresh_schedule_is_not_refetched(season):
    provider = StubProvider(season)
    cache = ScheduleCache(provider)
    cache.refresh("nfl", "8")
    cache.get("nfl", "8")
    time.sleep(0.05)
    assert provider.calls == 1


def test_a_failed_fetch_cools_down():
    provider = StubProvider(error=FetchError("offline"))
    cache = ScheduleCache(provider)
    with pytest.raises(FetchError):
        cache.refresh("nfl", "8")
    cache.get("nfl", "8")
    time.sleep(0.05)
    assert provider.calls == 1, "no retry inside the cooldown"


# -- rotation --------------------------------------------------------------

@pytest.fixture
def det_schedule(season):
    return build_team_schedule("nfl", "8", season, [], limit=4, now=NOW)


def test_a_favourite_gets_a_schedule_screen(det_schedule):
    playlist = build_playlist([], make_config(), schedules=[det_schedule], now=NOW)
    screens = [s for s in playlist if s.kind is ScreenKind.FAVORITE_SCHEDULE]
    assert len(screens) == 1
    assert screens[0].layout is Layout.SCHEDULE and screens[0].title == "DET schedule"


def test_only_favourites_get_one(det_schedule):
    playlist = build_playlist([], make_config(favorites=("12",)),
                              schedules=[det_schedule], now=NOW)
    assert not any(s.kind is ScreenKind.FAVORITE_SCHEDULE for s in playlist)


def test_the_schedule_screen_can_be_switched_off(det_schedule):
    playlist = build_playlist([], make_config(show_schedule=False),
                              schedules=[det_schedule], now=NOW)
    assert not any(s.kind is ScreenKind.FAVORITE_SCHEDULE for s in playlist)


def test_a_disabled_league_has_no_schedule_screen(det_schedule):
    config = Config.from_dict({
        "sports": {"enabled_leagues": ["mlb"],
                   "favorite_teams": [{"league": "nfl", "team_id": "8"}]}})
    playlist = build_playlist([], config, schedules=[det_schedule], now=NOW)
    assert not any(s.kind is ScreenKind.FAVORITE_SCHEDULE for s in playlist)


def test_schedules_follow_your_games_and_precede_everyone_elses(det_schedule):
    games = sample_card_games(NOW)
    config = make_config(favorites=("8", "16"))  # MIN is live in the sample
    kinds = [s.kind for s in build_playlist(games, config, schedules=[det_schedule],
                                            now=NOW)]
    position = kinds.index(ScreenKind.FAVORITE_SCHEDULE)
    assert kinds.index(ScreenKind.FAVORITE_LIVE) < position
    assert all(kind not in (ScreenKind.UPCOMING, ScreenKind.FINAL)
               for kind in kinds[:position])


def test_each_team_keeps_its_place_across_a_rebuild(season, det_schedule):
    other = TeamSchedule(team=replace(det_schedule.team, team_id="16",
                                      abbreviation="MIN"),
                         games=det_schedule.games)
    config = make_config(favorites=("8", "16"))
    rotator = Rotator()
    rotator.set_playlist(build_playlist([], config, schedules=[det_schedule, other],
                                        now=NOW))
    rotator.tick(0.0)
    rotator.advance(1.0)
    assert rotator.current().title == "MIN schedule"
    rotator.set_playlist(build_playlist([], config, schedules=[det_schedule, other],
                                        now=NOW))
    assert rotator.current().title == "MIN schedule"


# -- layout ----------------------------------------------------------------

@pytest.mark.parametrize("chain", [1, 2, 3])
def test_the_schedule_renders_at_every_chain_length(fonts, chain):
    context = RenderContext(fonts=fonts, logos=None, show_logos=False,
                            width=64 * chain, height=32)
    assert layouts.render_schedule(sample_schedule(), context).size == (64 * chain, 32)


def test_four_games_fit_without_scrolling(fonts):
    schedule = sample_schedule()
    frames = {layouts.render_schedule(schedule, RenderContext(
        fonts=fonts, logos=None, show_logos=False, width=128, height=32,
        phase=phase, duration=8.0)).tobytes() for phase in (0.0, 4.0, 7.9)}
    assert len(schedule.games) == 4 and len(frames) == 1


def test_more_games_than_fit_scroll_past(fonts, season):
    schedule = build_team_schedule("nfl", "8", season, [], limit=5, now=NOW)
    frames = [layouts.render_schedule(schedule, RenderContext(
        fonts=fonts, logos=None, show_logos=False, width=128, height=32,
        phase=phase, duration=8.0, now=NOW)).tobytes() for phase in (0.0, 8.0)]
    assert frames[0] != frames[1]


def test_a_live_game_shows_the_score(fonts):
    schedule = sample_schedule()
    first = schedule.games[0]
    live = replace(first, state=GameState.IN, home=replace(first.home, score=21),
                   away=replace(first.away, score=17))
    context = RenderContext(fonts=fonts, logos=None, show_logos=False, width=128, height=32)
    still = layouts.render_schedule(schedule, context)
    playing = layouts.render_schedule(TeamSchedule(schedule.team,
                                                   [live] + schedule.games[1:]), context)
    assert still.tobytes() != playing.tobytes()
    assert any(pixel == layouts.LIVE for pixel in playing.getdata())


def test_render_screen_dispatches_a_schedule(fonts):
    from scoreboard.rotation import ScreenItem

    screen = ScreenItem(ScreenKind.FAVORITE_SCHEDULE, Layout.SCHEDULE, [],
                        payload=sample_schedule())
    context = RenderContext(fonts=fonts, logos=None, show_logos=False, width=128, height=32)
    image = layouts.render_screen(screen, context)
    assert image.size == (128, 32)
    assert image.tobytes() != layouts.render_idle(context).tobytes()
