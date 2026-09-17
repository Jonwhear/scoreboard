"""Favourite matching, priority ordering and rotation behaviour."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scoreboard.config import Config
from scoreboard.rotation import (Layout, Rotator, ScreenItem, ScreenKind,
                                 build_playlist)
from scoreboard.samples import (sample_card_games, sample_final_games,
                                sample_live_game, sample_upcoming_games)


def make_config(**rotation):
    favorites = rotation.pop("favorites", [])
    leagues = rotation.pop("leagues", ["nfl", "mlb", "nhl", "nba", "ncaaf", "epl"])
    return Config.from_dict({
        "sports": {"enabled_leagues": leagues, "favorite_teams": favorites},
        "rotation": rotation,
    })


@pytest.fixture
def now():
    return datetime(2025, 10, 5, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def slate(now):
    return sample_card_games(now) + sample_upcoming_games(now) + sample_final_games(now)


# -- favourite matching ----------------------------------------------------

def test_favourite_matching_uses_league_and_id(now):
    game = sample_live_game(now)
    assert game.involves_any({"nfl:16"}) is True         # away team
    assert game.involves_any({"nfl:9"}) is True          # home team
    assert game.involves_any({"nba:16"}) is False        # same id, wrong league
    assert game.involves_any(set()) is False
    assert game.involves_any(None) is False


def test_favorite_side_prefers_the_home_team(now):
    game = sample_live_game(now)
    assert game.favorite_side({"nfl:16", "nfl:9"}).team_id == "9"
    assert game.favorite_side({"nfl:16"}).team_id == "16"
    assert game.favorite_side({"nhl:1"}) is None


# -- priority --------------------------------------------------------------

def test_favourite_live_game_comes_first(slate, now):
    config = make_config(favorites=[{"league": "nfl", "team_id": "16"}])
    playlist = build_playlist(slate, config, now)
    assert playlist[0].kind is ScreenKind.FAVORITE_LIVE
    assert playlist[0].layout is Layout.FEATURED
    assert playlist[0].games[0].away.abbreviation == "MIN"


def test_priority_order_is_the_documented_one(slate, now):
    config = make_config(favorites=[
        {"league": "nfl", "team_id": "16"},      # live game
        {"league": "nfl", "team_id": "12"},      # upcoming game
        {"league": "nfl", "team_id": "6"},       # recent final
    ])
    kinds = [screen.kind for screen in build_playlist(slate, config, now)]
    assert kinds == sorted(kinds, key=lambda kind: [
        ScreenKind.FAVORITE_LIVE, ScreenKind.LIVE, ScreenKind.FAVORITE_UPCOMING,
        ScreenKind.FAVORITE_FINAL, ScreenKind.UPCOMING, ScreenKind.FINAL,
        ScreenKind.IDLE].index(kind))
    assert ScreenKind.FAVORITE_LIVE in kinds
    assert ScreenKind.FAVORITE_UPCOMING in kinds
    assert ScreenKind.FAVORITE_FINAL in kinds


def test_empty_slate_yields_the_idle_screen(now):
    playlist = build_playlist([], make_config(), now)
    assert len(playlist) == 1
    assert playlist[0].kind is ScreenKind.IDLE and playlist[0].layout is Layout.IDLE


def test_disabled_leagues_are_excluded(slate, now):
    config = make_config(leagues=["nhl"])
    games = [game for screen in build_playlist(slate, config, now) for game in screen.games]
    assert games and all(game.league == "nhl" for game in games)


def test_favorites_only_hides_everything_else(slate, now):
    config = make_config(favorites=[{"league": "nfl", "team_id": "16"}], favorites_only=True)
    playlist = build_playlist(slate, config, now)
    for screen in playlist:
        for game in screen.games:
            assert game.involves_any({"nfl:16"})


def test_show_nonfavorite_live_toggle(slate, now):
    favorites = [{"league": "nfl", "team_id": "16"}]
    with_others = build_playlist(slate, make_config(favorites=favorites), now)
    without = build_playlist(
        slate, make_config(favorites=favorites, show_nonfavorite_live=False), now)
    assert any(screen.kind is ScreenKind.LIVE for screen in with_others)
    assert not any(screen.kind is ScreenKind.LIVE for screen in without)


def test_finals_toggle(slate, now):
    on = build_playlist(slate, make_config(), now)
    off = build_playlist(slate, make_config(show_recent_finals=False), now)
    assert any(screen.kind is ScreenKind.FINAL for screen in on)
    assert not any(screen.kind is ScreenKind.FINAL for screen in off)


def test_upcoming_toggle(slate, now):
    off = build_playlist(slate, make_config(show_upcoming=False), now)
    assert not any(screen.kind is ScreenKind.UPCOMING for screen in off)


def test_old_finals_drop_out_of_the_window(now):
    stale = sample_final_games(now - timedelta(hours=30))
    playlist = build_playlist(stale, make_config(final_window_hours=12), now)
    assert playlist[0].kind is ScreenKind.IDLE


def test_distant_games_drop_out_of_the_upcoming_window(now):
    far = sample_upcoming_games(now + timedelta(days=10))
    playlist = build_playlist(far, make_config(upcoming_window_hours=36), now)
    assert playlist[0].kind is ScreenKind.IDLE


def test_playlist_is_capped(slate, now):
    config = make_config(max_screens=2, layout_mode="featured")
    assert len(build_playlist(slate, config, now)) == 2


def test_layout_mode_featured_gives_one_game_per_screen(slate, now):
    playlist = build_playlist(slate, make_config(layout_mode="featured"), now)
    assert all(len(screen.games) == 1 for screen in playlist)
    assert all(screen.layout is Layout.FEATURED for screen in playlist)


@pytest.mark.parametrize("chain", [1, 2, 3])
def test_cards_pack_one_game_per_panel(now, chain):
    """A two-panel chain must show two per screen, not drop the third."""
    games = sample_upcoming_games(now) + sample_final_games(now)
    config = make_config(layout_mode="cards")
    config.display.chain_length = chain
    playlist = build_playlist(games, config, now)
    assert all(len(screen.games) <= chain for screen in playlist)
    assert any(len(screen.games) == chain for screen in playlist)
    shown = sum(len(screen.games) for screen in playlist)
    assert shown == len(games), "no game may be silently dropped"


def test_playlist_is_deterministic(slate, now):
    config = make_config(favorites=[{"league": "nfl", "team_id": "16"}])
    first = [screen.key for screen in build_playlist(slate, config, now)]
    second = [screen.key for screen in build_playlist(list(reversed(slate)), config, now)]
    assert first == second


# -- rotator ---------------------------------------------------------------

def screens(count):
    """Distinct screens, keyed by distinct games -- as in production."""
    base = sample_card_games() + sample_upcoming_games() + sample_final_games()
    return [
        ScreenItem(ScreenKind.LIVE, Layout.CARDS, [base[i % len(base)]], title=f"s{i}")
        for i in range(count)
    ]


def test_rotator_advances_on_the_timer():
    rotator = Rotator(screen_seconds=5)
    rotator.set_playlist(screens(3))
    assert rotator.tick(0.0).title == "s0"
    assert rotator.tick(2.0).title == "s0"
    assert rotator.tick(5.0).title == "s1"
    assert rotator.tick(10.0).title == "s2"
    assert rotator.tick(15.0).title == "s0", "wraps around"


def test_rotator_force_next():
    rotator = Rotator(screen_seconds=60)
    rotator.set_playlist(screens(3))
    rotator.tick(0.0)
    assert rotator.advance(1.0).title == "s1"


def test_rotator_keeps_its_place_across_a_refresh():
    rotator = Rotator(screen_seconds=5)
    original = screens(3)
    rotator.set_playlist(original)
    rotator.tick(0.0)
    rotator.tick(5.0)
    assert rotator.current().title == "s1"
    rotator.set_playlist(list(original))      # same screens, rebuilt
    assert rotator.current().title == "s1"


def test_rotator_survives_a_shrinking_playlist():
    rotator = Rotator(screen_seconds=5)
    rotator.set_playlist(screens(4))
    rotator.tick(0.0)
    rotator.tick(15.0)
    rotator.set_playlist(screens(1))
    assert rotator.current() is not None


def test_rotator_with_no_playlist_shows_idle():
    rotator = Rotator(screen_seconds=5)
    assert rotator.tick(0.0).kind is ScreenKind.IDLE


def test_cards_mode_applies_to_favourite_live_games_too(now):
    """The UI promises "always cards"; favourites must not silently opt out."""
    games = sample_card_games(now)
    favorites = [{"league": "nfl", "team_id": "16"}]
    auto = build_playlist(games, make_config(favorites=favorites), now)
    cards = build_playlist(games, make_config(favorites=favorites,
                                              layout_mode="cards"), now)
    assert auto[0].layout is Layout.FEATURED
    assert all(screen.layout is not Layout.FEATURED for screen in cards)
    assert cards[0].kind is ScreenKind.FAVORITE_LIVE, "priority is unchanged"


def test_featured_mode_still_features_favourites(now):
    games = sample_card_games(now)
    playlist = build_playlist(
        games, make_config(favorites=[{"league": "nfl", "team_id": "16"}],
                           layout_mode="featured"), now)
    assert playlist[0].kind is ScreenKind.FAVORITE_LIVE
    assert playlist[0].layout is Layout.FEATURED
