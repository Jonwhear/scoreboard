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
