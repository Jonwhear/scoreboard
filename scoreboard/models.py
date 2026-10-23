"""Provider-independent data models.

Nothing in this module knows that ESPN exists.  Providers translate their
own payloads into these types; the renderer, rotation engine and web UI
consume only these types.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")


class GameState(str, Enum):
    """Coarse lifecycle state of a game, normalized across providers."""

    PRE = "pre"
    IN = "in"
    POST = "post"

    @classmethod
    def parse(cls, value: Any) -> "GameState":
        text = str(value or "").strip().lower()
        if text in ("in", "live", "inprogress", "in_progress"):
            return cls.IN
        if text in ("post", "final", "complete", "completed"):
            return cls.POST
        return cls.PRE


def normalize_color(value: Any) -> Optional[str]:
    """Return ``"#rrggbb"`` for a usable colour, else ``None``."""
    if not value:
        return None
    text = str(value).strip()
    if not _HEX_RE.match(text):
        return None
    return "#" + text.lstrip("#").lower()


def team_key(league: str, team_id: str) -> str:
    """Stable identity for a team: canonical league id + provider team id."""
    return f"{league}:{team_id}"


@dataclass(frozen=True)
class TeamInfo:
    """An entry in a league's team catalog (used for favourite selection)."""

    league: str
    team_id: str
    abbreviation: str
    display_name: str
    short_name: str = ""
    location: str = ""
    logo_url: Optional[str] = None
    color: Optional[str] = None
    alt_color: Optional[str] = None

    @property
    def key(self) -> str:
        return team_key(self.league, self.team_id)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "league": self.league,
            "team_id": self.team_id,
            "abbreviation": self.abbreviation,
            "display_name": self.display_name,
            "short_name": self.short_name,
            "location": self.location,
            "logo_url": self.logo_url,
            "color": self.color,
            "alt_color": self.alt_color,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TeamInfo":
        return cls(
            league=str(data.get("league", "")),
            team_id=str(data.get("team_id", "")),
            abbreviation=str(data.get("abbreviation", "")),
            display_name=str(data.get("display_name", "")),
            short_name=str(data.get("short_name", "") or ""),
            location=str(data.get("location", "") or ""),
            logo_url=data.get("logo_url"),
            color=normalize_color(data.get("color")),
            alt_color=normalize_color(data.get("alt_color")),
        )


@dataclass
class GameTeam:
    """One side of a game: team identity plus its game-specific state."""

    league: str
    team_id: str
    abbreviation: str
    display_name: str
    short_name: str = ""
    location: str = ""
    score: Optional[int] = None
    logo_url: Optional[str] = None
    color: Optional[str] = None
    alt_color: Optional[str] = None
    record: Optional[str] = None
    rank: Optional[int] = None
    winner: bool = False
    has_possession: bool = False

    @property
    def key(self) -> str:
        return team_key(self.league, self.team_id)

    @property
    def score_text(self) -> str:
        return "-" if self.score is None else str(self.score)

    @property
    def label(self) -> str:
        """Best short label for the LED matrix: abbreviation, else initials."""
        if self.abbreviation:
            return self.abbreviation.upper()
        if self.short_name:
            return self.short_name[:4].upper()
        return (self.display_name or "???")[:4].upper()

    def to_info(self) -> TeamInfo:
        return TeamInfo(
            league=self.league,
            team_id=self.team_id,
            abbreviation=self.abbreviation,
            display_name=self.display_name,
            short_name=self.short_name,
            location=self.location,
            logo_url=self.logo_url,
            color=self.color,
            alt_color=self.alt_color,
        )

    def to_dict(self) -> Dict[str, Any]:
        data = self.to_info().to_dict()
        data.update(
            {
                "score": self.score,
                "record": self.record,
                "rank": self.rank,
                "winner": self.winner,
                "has_possession": self.has_possession,
            }
        )
        return data


@dataclass
class Game:
    """A single normalized game/event."""

    league: str
    game_id: str
    start_time: datetime
    state: GameState
    home: GameTeam
    away: GameTeam
    status_detail: str = ""
    status_short: str = ""
    period: Optional[int] = None
    clock: Optional[str] = None
    venue: Optional[str] = None
    broadcast: Optional[str] = None
    #: Sport-specific live extras that we trust (outs/balls/strikes, down &
    #: distance, ...).  Layouts treat every key as optional.
    situation: Dict[str, Any] = field(default_factory=dict)
    note: Optional[str] = None
    last_updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # -- derived helpers -------------------------------------------------

    @property
    def key(self) -> str:
        return f"{self.league}:{self.game_id}"

    @property
    def is_live(self) -> bool:
        return self.state is GameState.IN

    @property
    def is_final(self) -> bool:
        return self.state is GameState.POST

    @property
    def is_upcoming(self) -> bool:
        return self.state is GameState.PRE

    @property
    def team_keys(self) -> List[str]:
        return [self.home.key, self.away.key]

    def involves_any(self, keys: Any) -> bool:
        """True when either side is in ``keys`` (a set/list of team keys)."""
        if not keys:
            return False
        return self.home.key in keys or self.away.key in keys

    def favorite_side(self, keys: Any) -> Optional[GameTeam]:
        """Return the favourite team taking part, preferring the home side."""
        if not keys:
            return None
        if self.home.key in keys:
            return self.home
        if self.away.key in keys:
            return self.away
        return None

    @property
    def total_score(self) -> int:
        return (self.home.score or 0) + (self.away.score or 0)

    @property
    def score_margin(self) -> int:
        return abs((self.home.score or 0) - (self.away.score or 0))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "league": self.league,
            "game_id": self.game_id,
            "start_time": self.start_time.isoformat(),
            "state": self.state.value,
            "status_detail": self.status_detail,
            "status_short": self.status_short,
            "period": self.period,
            "clock": self.clock,
            "venue": self.venue,
            "broadcast": self.broadcast,
            "situation": dict(self.situation),
            "note": self.note,
            "home": self.home.to_dict(),
            "away": self.away.to_dict(),
            "last_updated": self.last_updated.isoformat(),
        }


@dataclass
class LeagueSnapshot:
    """The result of one polling pass over a single league."""

    league: str
    games: List[Game] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    #: ``True`` when this snapshot was served from cache after a failure.
    stale: bool = False
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class StandingsRow:
    """One team's line in a league table."""

    league: str
    team_id: str
    abbreviation: str
    display_name: str
    rank: Optional[int] = None
    played: Optional[int] = None
    wins: Optional[int] = None
    losses: Optional[int] = None
    draws: Optional[int] = None
    #: Points for soccer; for the US leagues this is left unset and the
    #: record is what matters.
    points: Optional[int] = None
    goal_difference: Optional[int] = None
    #: Games behind the leader, where the league thinks in those terms.
    games_behind: Optional[str] = None
    logo_url: Optional[str] = None
    color: Optional[str] = None
    #: What the league says this position earns, e.g. "UEFA Champions
    #: League" or "Relegation". Soccer tables are mostly meaningless
    #: without it.
    note: Optional[str] = None
    note_color: Optional[str] = None

    @property
    def key(self) -> str:
        return team_key(self.league, self.team_id)

    @property
    def record(self) -> str:
        """``W-L`` or ``W-D-L``, in the form this league actually uses.

        Soccer always shows draws; the US leagues only mention a tie when
        there has been one, so an NFL team reads 5-1 rather than 5-0-1.
        """
        from .leagues import get_league

        league = get_league(self.league)
        show_draws = self.draws is not None and (
            (league is not None and league.allows_draw) or self.draws > 0
        )
        parts = [self.wins, self.draws, self.losses] if show_draws \
            else [self.wins, self.losses]
        if any(value is None for value in parts):
            return ""
        return "-".join(str(value) for value in parts)

    @property
    def primary_value(self) -> str:
        """The column this league is actually ranked on.

        Points where a league keeps them (soccer, NHL), otherwise the
        win-loss record. Games behind is a distant third: it reads "0" for
        every division leader, which tells you nothing.
        """
        if self.points is not None:
            return str(self.points)
        record = self.record
        if record:
            return record
        return self.games_behind or ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "league": self.league,
            "team_id": self.team_id,
            "abbreviation": self.abbreviation,
            "display_name": self.display_name,
            "rank": self.rank,
            "played": self.played,
            "wins": self.wins,
            "losses": self.losses,
            "draws": self.draws,
            "points": self.points,
            "goal_difference": self.goal_difference,
            "games_behind": self.games_behind,
            "logo_url": self.logo_url,
            "color": self.color,
            "note": self.note,
            "note_color": self.note_color,
            "record": self.record,
        }


@dataclass
class StandingsGroup:
    """A table: a whole league, or one conference/division/group of it."""

    league: str
    name: str
    rows: List[StandingsRow] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "league": self.league,
            "name": self.name,
            "rows": [row.to_dict() for row in self.rows],
        }


@dataclass
class Standings:
    """Every table a league publishes, plus when we fetched it."""

    league: str
    groups: List[StandingsGroup] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    stale: bool = False
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.groups)

    @property
    def row_count(self) -> int:
        return sum(len(group.rows) for group in self.groups)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "league": self.league,
            "groups": [group.to_dict() for group in self.groups],
            "fetched_at": self.fetched_at.isoformat(),
            "stale": self.stale,
            "error": self.error,
        }


@dataclass
class TeamSchedule:
    """One team's coming games, for the favourite-team schedule screen."""

    team: GameTeam
    games: List[Game] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.team.key
