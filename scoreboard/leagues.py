"""Canonical league registry.

The rest of the application refers to leagues *only* by the canonical ids
defined here (``"nfl"``, ``"ncaaf"``, ...).  Provider-specific details --
such as ESPN's ``sport/league`` URL segments -- live inside the provider
adapters, never in application code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional


@dataclass(frozen=True)
class League:
    """A league the scoreboard knows how to display."""

    id: str
    name: str
    short_name: str
    sport: str
    #: Scoring is low enough that an extra digit is never needed.
    low_scoring: bool = False
    #: Period label used by the layouts, e.g. "Q" for quarters.
    period_label: str = "P"
    #: Some leagues (soccer) draw; others cannot.
    allows_draw: bool = False
    aliases: List[str] = field(default_factory=list)


_LEAGUE_LIST: List[League] = [
    League("nfl", "National Football League", "NFL", "football", period_label="Q"),
    League("ncaaf", "NCAA Football", "NCAAF", "football", period_label="Q",
           aliases=["college-football", "cfb"]),
    League("mlb", "Major League Baseball", "MLB", "baseball", low_scoring=True,
           period_label="INN"),
    League("nhl", "National Hockey League", "NHL", "hockey", low_scoring=True,
           period_label="P"),
    League("nba", "National Basketball Association", "NBA", "basketball",
           period_label="Q"),
    League("ncaam", "NCAA Men's Basketball", "NCAAM", "basketball",
           period_label="H", aliases=["mens-college-basketball", "cbb"]),
    League("mls", "Major League Soccer", "MLS", "soccer", low_scoring=True,
           period_label="H", allows_draw=True),
    League("epl", "English Premier League", "EPL", "soccer", low_scoring=True,
           period_label="H", allows_draw=True, aliases=["premier-league"]),
    League("efl_championship", "EFL Championship", "EFLC", "soccer",
           low_scoring=True, period_label="H", allows_draw=True,
           aliases=["championship", "efl"]),
]

LEAGUES: Dict[str, League] = {lg.id: lg for lg in _LEAGUE_LIST}

_ALIASES: Dict[str, str] = {}
for _lg in _LEAGUE_LIST:
    _ALIASES[_lg.id] = _lg.id
    _ALIASES[_lg.short_name.lower()] = _lg.id
    for _alias in _lg.aliases:
        _ALIASES[_alias] = _lg.id


def all_leagues() -> List[League]:
    """Every supported league, in display order."""
    return list(_LEAGUE_LIST)


def league_ids() -> List[str]:
    """Canonical ids of every supported league."""
    return [lg.id for lg in _LEAGUE_LIST]


def get_league(league_id: str) -> Optional[League]:
    """Look up a league by canonical id (returns ``None`` if unknown)."""
    return LEAGUES.get(league_id)


def normalize_league_id(value: str) -> Optional[str]:
    """Resolve a user-supplied league name/alias to a canonical id."""
    if not value:
        return None
    return _ALIASES.get(str(value).strip().lower().replace(" ", "-"))


def filter_known(league_ids_in: Iterable[str]) -> List[str]:
    """Drop unknown ids, normalize the rest, and de-duplicate preserving order."""
    seen: Dict[str, None] = {}
    for raw in league_ids_in or []:
        canonical = normalize_league_id(raw)
        if canonical:
            seen.setdefault(canonical, None)
    return list(seen)
