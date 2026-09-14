"""ESPN payloads must become provider-independent Game objects."""

from __future__ import annotations

from datetime import timezone

import pytest

from scoreboard.models import GameState


def test_parses_every_well_formed_event(nfl_games):
    # The fixture has three good events and three broken ones.
    assert len(nfl_games) == 3
    assert {game.game_id for game in nfl_games} == {"401671800", "401671801", "401671802"}


def test_malformed_events_are_skipped_not_fatal(provider, nfl_payload):
    games = provider.parse_scoreboard("nfl", nfl_payload)
    assert all(game.home.team_id and game.away.team_id for game in games)


def test_live_game_fields(nfl_games):
    game = next(g for g in nfl_games if g.game_id == "401671800")
    assert game.league == "nfl"
    assert game.state is GameState.IN and game.is_live
    assert (game.away.abbreviation, game.away.score) == ("MIN", 24)
    assert (game.home.abbreviation, game.home.score) == ("GB", 17)
    assert game.home.display_name == "Green Bay Packers"
    assert game.period == 3 and game.clock == "4:23"
    assert game.status_short == "4:23 - 3rd"
    assert game.venue == "Lambeau Field"
    assert game.broadcast == "FOX"
    assert game.away.record == "1-0"
    assert game.start_time.tzinfo is timezone.utc
    assert game.last_updated is not None


def test_team_colours_are_normalized(nfl_games):
    game = next(g for g in nfl_games if g.game_id == "401671800")
    assert game.away.color == "#4f2683"       # gains the leading '#'
    assert game.away.alt_color == "#ffc62f"


def test_logo_urls_are_extracted(nfl_games):
    game = nfl_games[0]
    assert game.home.logo_url and game.home.logo_url.startswith("https://")


def test_possession_is_attributed_to_one_side(nfl_games):
    game = next(g for g in nfl_games if g.game_id == "401671800")
    assert game.away.has_possession is True     # situation.possession == "16"
    assert game.home.has_possession is False


def test_pre_and_post_states(nfl_games):
    upcoming = next(g for g in nfl_games if g.game_id == "401671801")
    final = next(g for g in nfl_games if g.game_id == "401671802")
    assert upcoming.is_upcoming and upcoming.away.score == 0
    assert final.is_final and final.home.winner is True and final.away.winner is False
    assert final.broadcast == "NBC"            # falls back to geoBroadcasts


def test_situation_is_sport_specific(provider, mlb_payload, nfl_games):
    mlb = provider.parse_scoreboard("mlb", mlb_payload)[0]
    assert mlb.situation == {"balls": 2, "strikes": 1, "outs": 1,
                             "bases": [True, False, True]}
    nfl = next(g for g in nfl_games if g.is_live)
    assert nfl.situation == {"down_distance": "2nd & 7"}


def test_situation_is_empty_for_non_live_games(nfl_games):
    for game in nfl_games:
        if not game.is_live:
            assert game.situation == {}


def test_unknown_payload_shape_yields_no_games(provider):
    assert provider.parse_scoreboard("nfl", {"nope": True}) == []
    assert provider.parse_scoreboard("nfl", None) == []
    assert provider.parse_scoreboard("nfl", {"events": "not-a-list"}) == []


def test_team_catalog_parsing(provider, teams_payload):
    teams = provider.parse_teams("nfl", teams_payload)
    assert len(teams) == 6                     # the id-less entry is dropped
    by_abbr = {team.abbreviation: team for team in teams}
    assert by_abbr["GB"].team_id == "9"
    assert by_abbr["GB"].key == "nfl:9"
    assert by_abbr["GB"].color == "#204e32"


def test_games_are_sorted_deterministically(provider, nfl_payload):
    first = provider.parse_scoreboard("nfl", nfl_payload)
    second = provider.parse_scoreboard("nfl", nfl_payload)
    assert [g.key for g in first] == [g.key for g in second]


@pytest.mark.parametrize("league", ["nfl", "ncaaf", "mlb", "nhl", "nba", "ncaam",
                                    "mls", "epl", "efl_championship"])
def test_every_supported_league_has_an_endpoint(league):
    from scoreboard.providers.espn import ESPN_LEAGUES

    endpoint = ESPN_LEAGUES[league]
    assert endpoint.base.startswith("https://site.api.espn.com/")
