"""League tables: parsing, caching, scrolling and rotation placement."""

from __future__ import annotations

import time

import pytest

from scoreboard.config import Config
from scoreboard.display import layouts
from scoreboard.display.layouts import RenderContext, table_scroll_offset, zone_color
from scoreboard.httpclient import FetchError
from scoreboard.models import Standings, StandingsGroup
from scoreboard.providers.espn import EspnProvider
from scoreboard.rotation import Layout, ScreenKind, build_playlist
from scoreboard.samples import sample_card_games
from scoreboard.standings import StandingsCache

from conftest import load_fixture


@pytest.fixture
def epl_group(provider):
    return provider.parse_standings("epl", load_fixture("espn_epl_standings.json"))[0]


@pytest.fixture
def nfl_groups(provider):
    return provider.parse_standings("nfl", load_fixture("espn_nfl_standings.json"))


# -- parsing ---------------------------------------------------------------

def test_a_single_table_is_parsed(epl_group):
    assert epl_group.name == "English Premier League"
    assert len(epl_group.rows) == 9
    top = epl_group.rows[0]
    assert (top.abbreviation, top.rank, top.points) == ("ARS", 1, 19)
    assert (top.wins, top.draws, top.losses, top.played) == (6, 1, 1, 8)
    assert top.goal_difference == 11


def test_conference_children_become_separate_tables(nfl_groups):
    assert [group.name for group in nfl_groups] == [
        "American Football Conference", "National Football Conference"]
    assert [row.abbreviation for row in nfl_groups[0].rows] == ["BUF", "KC"]


def test_soccer_shows_draws_and_us_leagues_do_not(epl_group, nfl_groups):
    assert epl_group.rows[1].record == "6-0-2", "soccer always shows draws"
    assert nfl_groups[0].rows[0].record == "5-1", "a 0-tie NFL record is W-L"


def test_the_primary_column_is_what_the_league_ranks_on(epl_group, nfl_groups):
    assert epl_group.rows[0].primary_value == "19", "soccer ranks on points"
    assert nfl_groups[0].rows[0].primary_value == "5-1", "not games behind, which is 0"


def test_qualification_and_relegation_notes_are_kept(epl_group):
    by_abbr = {row.abbreviation: row for row in epl_group.rows}
    assert by_abbr["ARS"].note == "UEFA Champions League"
    assert by_abbr["MUN"].note == "UEFA Europa League"
    assert by_abbr["LEE"].note == "Relegation"
    assert by_abbr["CHE"].note is None


def test_rows_are_ordered_by_rank(epl_group):
    ranks = [row.rank for row in epl_group.rows]
    assert ranks == sorted(ranks)


def test_unreadable_payloads_yield_no_tables(provider):
    assert provider.parse_standings("epl", {"nope": True}) == []
    assert provider.parse_standings("epl", None) == []
    assert provider.parse_standings("epl", {"standings": {"entries": "not a list"}}) == []


def test_entries_without_a_team_id_are_skipped(provider):
    payload = {"standings": {"entries": [
        {"team": {"abbreviation": "???"}, "stats": []},
        {"team": {"id": "9", "abbreviation": "GB"}, "stats": [
            {"name": "wins", "value": 4}, {"name": "losses", "value": 2}]},
    ]}}
    groups = provider.parse_standings("nfl", payload)
    assert [row.abbreviation for row in groups[0].rows] == ["GB"]


def test_missing_rank_falls_back_to_payload_order(provider):
    payload = {"standings": {"entries": [
        {"team": {"id": "1", "abbreviation": "AAA"}, "stats": [{"name": "points", "value": 9}]},
        {"team": {"id": "2", "abbreviation": "BBB"}, "stats": [{"name": "points", "value": 7}]},
    ]}}
    rows = provider.parse_standings("epl", payload)[0].rows
    assert [(row.abbreviation, row.rank) for row in rows] == [("AAA", 1), ("BBB", 2)]


def test_a_provider_without_standings_says_so():
    from scoreboard.providers.base import ProviderError, SportsProvider

    class Bare(SportsProvider):
        name = "bare"

        def supported_leagues(self):
            return ["nfl"]

        def fetch_scoreboard(self, league_id):
            raise NotImplementedError

        def fetch_teams(self, league_id):
            return []

    with pytest.raises(ProviderError):
        Bare().fetch_standings("nfl")


# -- caching ---------------------------------------------------------------

class StubProvider(EspnProvider):
    def __init__(self, fixture="espn_epl_standings.json", error=None):
        self.fixture = fixture
        self.error = error
        self.calls = 0

    def fetch_standings(self, league_id):
        self.calls += 1
        if self.error:
            raise self.error
        return Standings(league=league_id,
                         groups=self.parse_standings(league_id, load_fixture(self.fixture)))


def test_tables_persist_across_a_restart(tmp_path):
    StandingsCache(StubProvider(), str(tmp_path)).refresh("epl")

    offline = StandingsCache(StubProvider(error=FetchError("offline")), str(tmp_path))
    table = offline.get("epl", refresh_if_stale=False)
    assert table is not None and table.row_count == 9
    assert table.groups[0].rows[0].note == "UEFA Champions League"
    assert table.stale is True, "a table off disk is flagged as not fresh"


def test_a_failed_refresh_cools_down(tmp_path):
    provider = StubProvider(error=FetchError("offline"))
    cache = StandingsCache(provider, str(tmp_path))
    with pytest.raises(FetchError):
        cache.refresh("epl")
    cache.refresh_async("epl")
    time.sleep(0.2)
    assert provider.calls == 1, "must not hammer a failing endpoint"


def test_an_empty_table_does_not_replace_a_good_one(tmp_path):
    cache = StandingsCache(StubProvider(), str(tmp_path))
    cache.refresh("epl")
    cache.provider = StubProvider(fixture="espn_malformed.json")
    cache.refresh("epl")
    assert cache.get("epl", refresh_if_stale=False).row_count == 9


def test_unreadable_cache_is_ignored(tmp_path):
    (tmp_path / "epl.json").write_text("{{{ not json")
    cache = StandingsCache(StubProvider(error=FetchError("x")), str(tmp_path))
    assert cache.get("epl", refresh_if_stale=False) is None


# -- scrolling -------------------------------------------------------------

def test_a_short_table_does_not_scroll():
    assert table_scroll_offset(0.0, 16, 25) == 0
    assert table_scroll_offset(99.0, 16, 25) == 0


def test_a_long_table_pauses_then_scrolls_then_stops():
    content, view = 160, 25
    assert table_scroll_offset(0.0, content, view) == 0
    assert table_scroll_offset(1.0, content, view) == 0, "pauses so the top is readable"
    middle = table_scroll_offset(8.0, content, view)
    assert 0 < middle < content - view
    assert table_scroll_offset(999.0, content, view) == content - view, "holds at the end"


def test_scrolling_is_monotonic():
    offsets = [table_scroll_offset(phase / 2, 200, 25) for phase in range(60)]
    assert offsets == sorted(offsets)


@pytest.mark.parametrize("note,expected", [
    ("UEFA Champions League", True),
    ("UEFA Europa League", True),
    ("Relegation", True),
    ("Clinched Playoff Berth", True),
    (None, False),
    ("", False),
    ("Something else entirely", False),
])
def test_zone_colours_are_recognised(note, expected):
    assert (zone_color(note) is not None) is expected


# -- layout ----------------------------------------------------------------

@pytest.mark.parametrize("chain", [1, 2, 3])
def test_the_table_renders_at_every_chain_length(fonts, epl_group, chain):
    context = RenderContext(fonts=fonts, logos=None, show_logos=False,
                            width=64 * chain, height=32)
    image = layouts.render_standings(epl_group, context)
    assert image.size == (64 * chain, 32)


def test_scrolled_rows_never_paint_over_the_header(fonts, epl_group):
    """Arithmetic clipping used to let a half-scrolled row bleed upward."""
    still = RenderContext(fonts=fonts, logos=None, show_logos=False,
                          width=128, height=32, phase=0.0)
    baseline = layouts.render_standings(epl_group, still).crop((0, 0, 128, 6)).tobytes()
    for phase in (2.0, 3.5, 6.0, 9.0, 14.0):
        context = RenderContext(fonts=fonts, logos=None, show_logos=False,
                                width=128, height=32, phase=phase)
        header = layouts.render_standings(epl_group, context).crop((0, 0, 128, 6))
        assert header.tobytes() == baseline, f"header changed at phase {phase}"


def test_the_table_actually_moves(fonts, epl_group):
    frames = []
    for phase in (0.0, 6.0, 12.0):
        context = RenderContext(fonts=fonts, logos=None, show_logos=False,
                                width=128, height=32, phase=phase)
        frames.append(layouts.render_standings(epl_group, context).tobytes())
    assert len(set(frames)) == 3, "each phase should show a different window"


def test_an_empty_table_still_renders(fonts):
    context = RenderContext(fonts=fonts, logos=None, show_logos=False, width=128, height=32)
    image = layouts.render_standings(StandingsGroup("epl", "Empty", []), context)
    assert image.size == (128, 32)


def test_render_screen_dispatches_a_table(fonts, epl_group):
    from scoreboard.rotation import ScreenItem

    context = RenderContext(fonts=fonts, logos=None, show_logos=False, width=128, height=32)
    screen = ScreenItem(ScreenKind.STANDINGS, Layout.STANDINGS, [],
                        title="EPL", payload=epl_group)
    assert layouts.render_screen(screen, context).size == (128, 32)


# -- rotation --------------------------------------------------------------

def make_config(**rotation):
    return Config.from_dict({
        "sports": {"enabled_leagues": ["nfl", "mlb", "nhl", "epl"]},
        "rotation": rotation,
    })


def test_tables_are_absent_unless_switched_on(epl_group):
    tables = {"epl": Standings(league="epl", groups=[epl_group])}
    playlist = build_playlist(sample_card_games(), make_config(), standings=tables)
    assert not any(screen.kind is ScreenKind.STANDINGS for screen in playlist)


def test_tables_appear_after_the_games(epl_group):
    tables = {"epl": Standings(league="epl", groups=[epl_group])}
    playlist = build_playlist(sample_card_games(), make_config(show_standings=True),
                              standings=tables)
    kinds = [screen.kind for screen in playlist]
    assert ScreenKind.STANDINGS in kinds
    assert kinds.index(ScreenKind.STANDINGS) == len(kinds) - 1


def test_a_table_gets_its_own_longer_dwell(epl_group):
    tables = {"epl": Standings(league="epl", groups=[epl_group])}
    playlist = build_playlist([], make_config(show_standings=True, standings_seconds=30),
                              standings=tables)
    table_screen = next(s for s in playlist if s.kind is ScreenKind.STANDINGS)
    assert table_screen.duration == 30.0


def test_standings_leagues_narrows_the_selection(epl_group, nfl_groups):
    tables = {"epl": Standings(league="epl", groups=[epl_group]),
              "nfl": Standings(league="nfl", groups=nfl_groups)}
    playlist = build_playlist([], make_config(show_standings=True,
                                              standings_leagues=["epl"]),
                              standings=tables)
    titles = [s.title for s in playlist if s.kind is ScreenKind.STANDINGS]
    assert titles == ["English Premier League"]


def test_a_locked_game_still_beats_the_tables(epl_group):
    games = sample_card_games()
    tables = {"epl": Standings(league="epl", groups=[epl_group])}
    config = make_config(show_standings=True, locked_game=games[0].key)
    playlist = build_playlist(games, config, standings=tables)
    assert len(playlist) == 1 and playlist[0].kind is ScreenKind.LOCKED
