"""Synthetic games used by the web UI's test buttons and by the tests.

Having realistic sample data in one place means layouts can be exercised
with no network, no hardware and no live games in season.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List

from .models import Game, GameState, GameTeam


def _team(league: str, team_id: str, abbr: str, name: str, score, color: str,
          alt: str = "#ffffff", **kwargs) -> GameTeam:
    return GameTeam(
        league=league, team_id=team_id, abbreviation=abbr, display_name=name,
        short_name=name.split()[-1], score=score, color=color, alt_color=alt,
        logo_url=f"https://a.espncdn.com/i/teamlogos/{league}/500/{abbr.lower()}.png",
        **kwargs,
    )


def sample_live_game(now: datetime = None) -> Game:
    now = now or datetime.now(timezone.utc)
    return Game(
        league="nfl", game_id="sample-live", start_time=now - timedelta(hours=1),
        state=GameState.IN,
        away=_team("nfl", "16", "MIN", "Minnesota Vikings", 24, "#4f2683", "#ffc62f",
                   record="3-1", has_possession=True),
        home=_team("nfl", "9", "GB", "Green Bay Packers", 17, "#204e32", "#ffb612",
                   record="2-2"),
        status_detail="4:23 - 3rd Quarter", status_short="4:23 - 3rd",
        period=3, clock="4:23", venue="Lambeau Field", broadcast="FOX",
        situation={"down_distance": "2nd & 7"},
    )


def sample_card_games(now: datetime = None) -> List[Game]:
    now = now or datetime.now(timezone.utc)
    return [
        sample_live_game(now),
        Game(
            league="mlb", game_id="sample-mlb", start_time=now - timedelta(hours=2),
            state=GameState.IN,
            away=_team("mlb", "16", "CHC", "Chicago Cubs", 2, "#0e3386"),
            home=_team("mlb", "24", "STL", "St. Louis Cardinals", 4, "#c41e3a"),
            status_detail="Bottom 7th", status_short="Bot 7th", period=7,
            situation={"balls": 2, "strikes": 1, "outs": 1},
        ),
        Game(
            league="nhl", game_id="sample-nhl", start_time=now - timedelta(hours=3),
            state=GameState.POST,
            away=_team("nhl", "13", "NYR", "New York Rangers", 2, "#0038a8"),
            home=_team("nhl", "1", "BOS", "Boston Bruins", 3, "#fcb514", winner=True),
            status_detail="Final", status_short="Final", period=3,
        ),
    ]


def sample_upcoming_games(now: datetime = None) -> List[Game]:
    now = now or datetime.now(timezone.utc)
    return [
        Game(
            league="nfl", game_id="sample-up-1", start_time=now + timedelta(hours=20),
            state=GameState.PRE,
            away=_team("nfl", "12", "KC", "Kansas City Chiefs", None, "#e31837", record="3-1"),
            home=_team("nfl", "2", "BUF", "Buffalo Bills", None, "#00338d", record="3-1"),
            status_detail="Sun 4:25 PM", status_short="4:25 PM", broadcast="CBS",
        ),
        Game(
            league="nba", game_id="sample-up-2", start_time=now + timedelta(hours=26),
            state=GameState.PRE,
            away=_team("nba", "13", "LAL", "Los Angeles Lakers", None, "#552583", record="5-2"),
            home=_team("nba", "2", "BOS", "Boston Celtics", None, "#007a33", record="6-1"),
            status_detail="Mon 7:30 PM", status_short="7:30 PM", broadcast="TNT",
        ),
        Game(
            league="epl", game_id="sample-up-3", start_time=now + timedelta(hours=30),
            state=GameState.PRE,
            away=_team("epl", "360", "ARS", "Arsenal", None, "#ef0107", record="4-1-1"),
            home=_team("epl", "364", "LIV", "Liverpool", None, "#c8102e", record="5-1-0"),
            status_detail="Mon 11:30 AM", status_short="11:30 AM", broadcast="USA",
        ),
    ]


def sample_final_games(now: datetime = None) -> List[Game]:
    now = now or datetime.now(timezone.utc)
    return [
        Game(
            league="nfl", game_id="sample-fin-1", start_time=now - timedelta(hours=4),
            state=GameState.POST,
            away=_team("nfl", "6", "DAL", "Dallas Cowboys", 21, "#002a5c"),
            home=_team("nfl", "21", "PHI", "Philadelphia Eagles", 28, "#06424d", winner=True),
            status_detail="Final", status_short="Final", period=4,
        ),
        Game(
            league="ncaaf", game_id="sample-fin-2", start_time=now - timedelta(hours=6),
            state=GameState.POST,
            away=_team("ncaaf", "333", "ALA", "Alabama", 35, "#9e1b32", winner=True, rank=4),
            home=_team("ncaaf", "61", "UGA", "Georgia", 31, "#ba0c2f", rank=2),
            status_detail="Final", status_short="Final", period=4,
        ),
        Game(
            league="nhl", game_id="sample-fin-3", start_time=now - timedelta(hours=5),
            state=GameState.POST,
            away=_team("nhl", "10", "TOR", "Toronto Maple Leafs", 4, "#00205b", winner=True),
            home=_team("nhl", "14", "TBL", "Tampa Bay Lightning", 3, "#002868"),
            status_detail="Final/OT", status_short="Final/OT", period=4,
        ),
    ]


def sample_games(now: datetime = None) -> List[Game]:
    """A mixed slate covering every state."""
    now = now or datetime.now(timezone.utc)
    return sample_card_games(now) + sample_upcoming_games(now) + sample_final_games(now)
