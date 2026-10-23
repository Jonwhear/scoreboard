"""ESPN adapter.

This is the *only* module that knows anything about ESPN's JSON shape or
URLs.  Everything it returns is a :mod:`scoreboard.models` object.

Caveat: these are the undocumented endpoints ESPN's own site uses.  They are
not a supported public developer API and can change without notice, which is
exactly why this adapter sits behind :class:`~scoreboard.providers.base.SportsProvider`.
Parsing is therefore defensive: a malformed event is logged and skipped, not
allowed to take down the fetch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from ..httpclient import FetchError, HttpClient
from ..models import (Game, GameState, GameTeam, LeagueSnapshot, Standings,
                      StandingsGroup, StandingsRow, TeamInfo, normalize_color)
from .base import ProviderError, SportsProvider

log = logging.getLogger(__name__)

SITE_API = "https://site.api.espn.com/apis/site/v2/sports"
#: Standings live under a different API version to the scoreboard.
STANDINGS_API = "https://site.api.espn.com/apis/v2/sports"


@dataclass(frozen=True)
class _EspnLeague:
    """How one canonical league maps onto ESPN's URL space."""

    sport: str
    path: str
    scoreboard_params: Dict[str, Any] = None  # type: ignore[assignment]
    teams_params: Dict[str, Any] = None  # type: ignore[assignment]
    standings_params: Dict[str, Any] = None  # type: ignore[assignment]

    @property
    def base(self) -> str:
        return f"{SITE_API}/{self.sport}/{self.path}"

    @property
    def standings_base(self) -> str:
        return f"{STANDINGS_API}/{self.sport}/{self.path}"


#: Canonical league id -> ESPN endpoint description.
ESPN_LEAGUES: Dict[str, _EspnLeague] = {
    # level=3 asks for divisions rather than whole conferences: AFC and
    # NFC only matter for playoff seeding, the division is the race.
    "nfl": _EspnLeague("football", "nfl", standings_params={"level": 3}),
    # groups=80 is ESPN's FBS grouping; without it the scoreboard is a
    # narrow "top 25" selection rather than the full slate.
    "ncaaf": _EspnLeague("football", "college-football",
                         {"groups": 80, "limit": 200}, {"limit": 1000}),
    "mlb": _EspnLeague("baseball", "mlb"),
    "nhl": _EspnLeague("hockey", "nhl"),
    "nba": _EspnLeague("basketball", "nba"),
    # groups=50 is Division I.
    "ncaam": _EspnLeague("basketball", "mens-college-basketball",
                         {"groups": 50, "limit": 200}, {"limit": 1000}),
    "mls": _EspnLeague("soccer", "usa.1"),
    "epl": _EspnLeague("soccer", "eng.1"),
    "efl_championship": _EspnLeague("soccer", "eng.2"),
}


# ---------------------------------------------------------------------------
# small defensive helpers
# ---------------------------------------------------------------------------

def _dig(data: Any, *path: Any, default: Any = None) -> Any:
    """Walk nested dicts/lists, returning ``default`` at the first miss."""
    current = data
    for step in path:
        if isinstance(step, int):
            if not isinstance(current, (list, tuple)) or len(current) <= step:
                return default
            current = current[step]
        else:
            if not isinstance(current, dict) or step not in current:
                return default
            current = current[step]
    return default if current is None else current


def _as_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_str(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _parse_datetime(value: Any) -> datetime:
    """Parse ESPN's ISO-8601 timestamps (``2024-09-08T17:00Z``)."""
    text = _as_str(value)
    if text:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            log.debug("Unparseable event date %r", value)
    return datetime.now(timezone.utc)


#: The letter ESPN puts beside a US-league team late in a season, spelled
#: out so the layouts can treat it like a soccer table's zone note.
_CLINCHERS = {
    "z": "Clinched first-round bye",
    "*": "Clinched home field",
    "y": "Clinched division",
    "x": "Clinched playoff berth",
    "e": "Eliminated",
}


class MalformedEvent(ValueError):
    """One event could not be understood; skip it and keep going."""


# ---------------------------------------------------------------------------
# provider
# ---------------------------------------------------------------------------

class EspnProvider(SportsProvider):
    """Reads ESPN's public site API and normalizes it."""

    name = "espn"

    def __init__(self, client: Optional[HttpClient] = None, cache_dir: Optional[str] = None,
                 timeout: float = 8.0, retries: int = 3) -> None:
        self.client = client or HttpClient(cache_dir=cache_dir, timeout=timeout, retries=retries)
        #: Consecutive failures per league, so a long outage logs once at
        #: ERROR and then stays quiet until things recover.
        self._failure_streak: Dict[str, int] = {}

    # -- interface -------------------------------------------------------

    def supported_leagues(self) -> List[str]:
        return list(ESPN_LEAGUES)

    def fetch_scoreboard(self, league_id: str) -> LeagueSnapshot:
        endpoint = ESPN_LEAGUES.get(league_id)
        if endpoint is None:
            return LeagueSnapshot(league=league_id, error=f"league {league_id!r} not supported")
        url = f"{endpoint.base}/scoreboard"
        params = dict(endpoint.scoreboard_params or {})
        try:
            result = self.client.get_json(url, params=params, cache_key=f"scoreboard-{league_id}")
        except FetchError as exc:
            streak = self._failure_streak.get(league_id, 0) + 1
            self._failure_streak[league_id] = streak
            if streak == 1:
                log.error("Scoreboard fetch failed for %s: %s", league_id, exc)
            elif streak % 20 == 0:
                log.error("Scoreboard fetch still failing for %s (%d consecutive): %s",
                          league_id, streak, exc)
            else:
                log.debug("Scoreboard fetch failed for %s (%d consecutive)", league_id, streak)
            return LeagueSnapshot(league=league_id, error=str(exc))

        if self._failure_streak.pop(league_id, 0):
            log.info("%s: sports data is reachable again", league_id)
        games = self.parse_scoreboard(league_id, result.data)
        snapshot = LeagueSnapshot(
            league=league_id,
            games=games,
            fetched_at=result.fetched_at,
            stale=result.from_cache,
            error="cached response (upstream unavailable)" if result.from_cache else None,
        )
        log.info(
            "%s: %d game(s)%s", league_id, len(games), " [stale cache]" if result.from_cache else ""
        )
        return snapshot

    def fetch_teams(self, league_id: str) -> List[TeamInfo]:
        endpoint = ESPN_LEAGUES.get(league_id)
        if endpoint is None:
            raise ProviderError(f"league {league_id!r} not supported")
        url = f"{endpoint.base}/teams"
        params = dict(endpoint.teams_params or {"limit": 1000})
        result = self.client.get_json(url, params=params, cache_key=f"teams-{league_id}")
        return self.parse_teams(league_id, result.data)

    def fetch_standings(self, league_id: str) -> Standings:
        endpoint = ESPN_LEAGUES.get(league_id)
        if endpoint is None:
            return Standings(league=league_id, error=f"league {league_id!r} not supported")
        url = f"{endpoint.standings_base}/standings"
        params = dict(endpoint.standings_params or {})
        try:
            result = self.client.get_json(url, params=params or None,
                                          cache_key=f"standings-{league_id}")
        except FetchError as exc:
            log.warning("Standings fetch failed for %s: %s", league_id, exc)
            return Standings(league=league_id, error=str(exc))

        groups = self.parse_standings(league_id, result.data)
        standings = Standings(
            league=league_id,
            groups=groups,
            fetched_at=result.fetched_at,
            stale=result.from_cache,
            error=None if groups else "no standings in response",
        )
        log.info("%s: standings for %d team(s) in %d table(s)%s",
                 league_id, standings.row_count, len(groups),
                 " [stale cache]" if result.from_cache else "")
        return standings

    def fetch_team_schedule(self, league_id: str, team_id: str) -> List[Game]:
        endpoint = ESPN_LEAGUES.get(league_id)
        if endpoint is None:
            raise ProviderError(f"league {league_id!r} not supported")
        url = f"{endpoint.base}/teams/{team_id}/schedule"
        # Soccer schedules list results unless asked for the fixtures.
        params = {"fixture": "true"} if endpoint.sport == "soccer" else None
        result = self.client.get_json(url, params=params,
                                      cache_key=f"schedule-{league_id}-{team_id}")
        games = self.parse_scoreboard(league_id, result.data)
        log.info("%s: schedule for team %s has %d game(s)%s", league_id, team_id,
                 len(games), " [stale cache]" if result.from_cache else "")
        return games

    # -- parsing (pure functions over already-decoded JSON) ---------------

    def parse_standings(self, league_id: str, payload: Any) -> List[StandingsGroup]:
        """Normalize a standings payload.

        ESPN returns a single table, a list of conference / group children,
        or -- when asked for divisions -- conferences whose children are the
        divisions. Walk the tree and make a table of every innermost level,
        skipping anything we cannot read rather than losing the whole set
        to one odd entry.
        """
        groups: List[StandingsGroup] = []
        self._collect_standings(league_id, payload, groups, depth=0)
        if not groups:
            log.warning("%s: no usable standings entries in response", league_id)
        return groups

    def _collect_standings(
        self, league_id: str, node: Any, groups: List[StandingsGroup], depth: int
    ) -> None:
        if not isinstance(node, dict) or depth > 4:
            return
        children = _dig(node, "children", default=None)
        if isinstance(children, list) and children:
            for child in children:
                self._collect_standings(league_id, child, groups, depth + 1)
            return
        entries = _dig(node, "standings", "entries", default=[])
        name = _as_str(_dig(node, "name")) or _as_str(_dig(node, "abbreviation"))
        if not name and depth == 0:
            name = league_id.upper()
        group = self._parse_standings_group(league_id, name, entries)
        if group.rows:
            groups.append(group)

    def _parse_standings_group(
        self, league_id: str, name: str, entries: Any
    ) -> StandingsGroup:
        rows: List[StandingsRow] = []
        if not isinstance(entries, list):
            return StandingsGroup(league=league_id, name=name)
        for entry in entries:
            try:
                row = self._parse_standings_row(league_id, entry)
            except Exception:
                log.exception("%s: skipping unreadable standings entry", league_id)
                continue
            if row is not None:
                rows.append(row)
        # Trust an explicit rank where there is one; otherwise keep the
        # order ESPN sent, which is already the table order.
        if rows and all(row.rank for row in rows):
            rows.sort(key=lambda row: row.rank or 0)
        # The rank shown is the position in *this* table. A division's
        # entries carry conference-wide playoff seeds (1, 6, 9, 14), which
        # order the division correctly but read as nonsense beside it.
        if [row.rank for row in rows] != list(range(1, len(rows) + 1)):
            for position, row in enumerate(rows, start=1):
                row.rank = position
        return StandingsGroup(league=league_id, name=name, rows=rows)

    def _parse_standings_row(self, league_id: str, entry: Any) -> Optional[StandingsRow]:
        if not isinstance(entry, dict):
            return None
        raw_team = _dig(entry, "team", default={})
        team_id = _as_str(_dig(raw_team, "id"))
        if not team_id:
            return None

        stats = {}
        for stat in _dig(entry, "stats", default=[]) or []:
            if isinstance(stat, dict):
                stat_name = _as_str(stat.get("name")) or _as_str(stat.get("abbreviation"))
                if stat_name:
                    stats[stat_name] = stat

        def number(*names: str) -> Optional[int]:
            for stat_name in names:
                stat = stats.get(stat_name)
                if stat is None:
                    continue
                value = _as_int(stat.get("value"))
                if value is None:
                    value = _as_int(stat.get("displayValue"))
                if value is not None:
                    return value
            return None

        def text(*names: str) -> Optional[str]:
            for stat_name in names:
                stat = stats.get(stat_name)
                if stat is not None:
                    display = _as_str(stat.get("displayValue"))
                    if display:
                        return display
            return None

        abbreviation = _as_str(raw_team.get("abbreviation")).upper()
        display_name = _as_str(raw_team.get("displayName")) or _as_str(raw_team.get("name"))
        if not abbreviation:
            abbreviation = (_as_str(raw_team.get("shortDisplayName")) or display_name)[:4].upper()

        return StandingsRow(
            league=league_id,
            team_id=team_id,
            abbreviation=abbreviation,
            display_name=display_name,
            rank=number("rank", "playoffSeed"),
            played=number("gamesPlayed"),
            wins=number("wins"),
            losses=number("losses"),
            # ESPN calls a draw a "tie"; soccer tables need it, the US
            # leagues mostly do not have one.
            draws=number("ties", "draws"),
            points=number("points"),
            goal_difference=number("pointDifferential", "goalDifferential",
                                   "differential"),
            games_behind=text("gamesBehind"),
            logo_url=self._logo_url(raw_team),
            color=normalize_color(raw_team.get("color")),
            note=(_as_str(_dig(entry, "note", "description"))
                  or _CLINCHERS.get(_as_str(text("clincher")).lower()) or None),
            note_color=normalize_color(_dig(entry, "note", "color")),
        )

    def parse_scoreboard(self, league_id: str, payload: Any) -> List[Game]:
        """Normalize a scoreboard payload, skipping events we cannot read."""
        events = _dig(payload, "events", default=[])
        if not isinstance(events, list):
            log.warning("%s: scoreboard payload had no usable 'events' list", league_id)
            return []
        games: List[Game] = []
        skipped = 0
        for event in events:
            try:
                games.append(self._parse_event(league_id, event))
            except MalformedEvent as exc:
                skipped += 1
                log.warning("%s: skipping malformed event: %s", league_id, exc)
            except Exception:  # never let one odd event kill the whole refresh
                skipped += 1
                log.exception("%s: unexpected error parsing event", league_id)
        if skipped:
            log.warning("%s: skipped %d of %d events", league_id, skipped, len(events))
        games.sort(key=lambda game: (game.start_time, game.game_id))
        return games

    def parse_teams(self, league_id: str, payload: Any) -> List[TeamInfo]:
        """Normalize a ``/teams`` payload into a catalog."""
        entries = _dig(payload, "sports", 0, "leagues", 0, "teams", default=[])
        if not isinstance(entries, list):
            log.warning("%s: teams payload had no usable team list", league_id)
            return []
        teams: List[TeamInfo] = []
        for entry in entries:
            raw = _dig(entry, "team", default=entry)
            team_id = _as_str(_dig(raw, "id"))
            if not team_id:
                continue
            teams.append(
                TeamInfo(
                    league=league_id,
                    team_id=team_id,
                    abbreviation=_as_str(_dig(raw, "abbreviation")).upper(),
                    display_name=_as_str(_dig(raw, "displayName")) or _as_str(_dig(raw, "name")),
                    short_name=_as_str(_dig(raw, "shortDisplayName")),
                    location=_as_str(_dig(raw, "location")),
                    logo_url=self._logo_url(raw),
                    color=normalize_color(_dig(raw, "color")),
                    alt_color=normalize_color(_dig(raw, "alternateColor")),
                )
            )
        teams.sort(key=lambda team: team.display_name.lower())
        log.info("%s: team catalog has %d entries", league_id, len(teams))
        return teams

    # -- event/competitor parsing ----------------------------------------

    def _parse_event(self, league_id: str, event: Any) -> Game:
        if not isinstance(event, dict):
            raise MalformedEvent("event is not an object")
        game_id = _as_str(event.get("id"))
        if not game_id:
            raise MalformedEvent("event has no id")

        competition = _dig(event, "competitions", 0, default={})
        competitors = _dig(competition, "competitors", default=[])
        if not isinstance(competitors, list) or len(competitors) < 2:
            raise MalformedEvent(f"event {game_id} has fewer than two competitors")

        home_raw = self._find_side(competitors, "home")
        away_raw = self._find_side(competitors, "away")
        if home_raw is None or away_raw is None:
            # Neutral-site/soccer payloads occasionally omit homeAway; fall
            # back to ESPN's ordering rather than dropping the game.
            home_raw, away_raw = competitors[0], competitors[1]

        status = _dig(competition, "status", default=_dig(event, "status", default={}))
        state = GameState.parse(_dig(status, "type", "state"))
        possession_id = _as_str(_dig(competition, "situation", "possession"))

        home = self._parse_competitor(league_id, home_raw, possession_id)
        away = self._parse_competitor(league_id, away_raw, possession_id)

        return Game(
            league=league_id,
            game_id=game_id,
            start_time=_parse_datetime(event.get("date") or _dig(competition, "date")),
            state=state,
            home=home,
            away=away,
            status_detail=_as_str(_dig(status, "type", "detail")),
            status_short=_as_str(_dig(status, "type", "shortDetail")),
            period=_as_int(_dig(status, "period")),
            clock=_as_str(_dig(status, "displayClock")) or None,
            venue=_as_str(_dig(competition, "venue", "fullName")) or None,
            broadcast=self._broadcast(competition),
            situation=self._situation(league_id, competition, state),
            note=_as_str(_dig(competition, "notes", 0, "headline")) or None,
            last_updated=datetime.now(timezone.utc),
        )

    @staticmethod
    def _find_side(competitors: Sequence[Any], side: str) -> Optional[Dict[str, Any]]:
        for competitor in competitors:
            if isinstance(competitor, dict) and _as_str(competitor.get("homeAway")).lower() == side:
                return competitor
        return None

    def _parse_competitor(
        self, league_id: str, competitor: Any, possession_id: str
    ) -> GameTeam:
        if not isinstance(competitor, dict):
            raise MalformedEvent("competitor is not an object")
        raw = _dig(competitor, "team", default={})
        team_id = _as_str(raw.get("id")) or _as_str(competitor.get("id"))
        if not team_id:
            raise MalformedEvent("competitor has no team id")
        abbreviation = _as_str(raw.get("abbreviation")).upper()
        display_name = _as_str(raw.get("displayName")) or _as_str(raw.get("name"))
        if not abbreviation:
            abbreviation = (_as_str(raw.get("shortDisplayName")) or display_name)[:4].upper()
        return GameTeam(
            league=league_id,
            team_id=team_id,
            abbreviation=abbreviation,
            display_name=display_name,
            short_name=_as_str(raw.get("shortDisplayName")),
            location=_as_str(raw.get("location")),
            score=self._score(competitor.get("score")),
            logo_url=self._logo_url(raw),
            color=normalize_color(raw.get("color")),
            alt_color=normalize_color(raw.get("alternateColor")),
            record=self._record(competitor),
            rank=_as_int(competitor.get("curatedRank", {}).get("current")
                         if isinstance(competitor.get("curatedRank"), dict) else None),
            winner=bool(competitor.get("winner")),
            has_possession=bool(possession_id) and possession_id == team_id,
        )

    @staticmethod
    def _score(raw: Any) -> Optional[int]:
        """A score is a string on the scoreboard but an object in schedules."""
        if isinstance(raw, dict):
            value = _as_int(raw.get("displayValue"))
            if value is None and isinstance(raw.get("value"), (int, float)):
                value = int(raw["value"])
            return value
        return _as_int(raw)

    @staticmethod
    def _logo_url(raw: Any) -> Optional[str]:
        direct = _as_str(_dig(raw, "logo"))
        if direct:
            return direct
        for logo in _dig(raw, "logos", default=[]) or []:
            href = _as_str(_dig(logo, "href"))
            if href:
                return href
        return None

    @staticmethod
    def _record(competitor: Any) -> Optional[str]:
        for record in _dig(competitor, "records", default=[]) or []:
            if not isinstance(record, dict):
                continue
            if _as_str(record.get("type")).lower() in ("total", "overall", ""):
                summary = _as_str(record.get("summary"))
                if summary:
                    return summary
        summary = _as_str(_dig(competitor, "records", 0, "summary"))
        return summary or None

    @staticmethod
    def _broadcast(competition: Any) -> Optional[str]:
        for broadcast in _dig(competition, "broadcasts", default=[]) or []:
            names = _dig(broadcast, "names", default=[])
            if isinstance(names, list) and names:
                return _as_str(names[0]) or None
            # The team-schedule endpoint describes broadcasts differently.
            media = _as_str(_dig(broadcast, "media", "shortName"))
            if media:
                return media
        for geo in _dig(competition, "geoBroadcasts", default=[]) or []:
            name = _as_str(_dig(geo, "media", "shortName"))
            if name:
                return name
        return None

    @staticmethod
    def _situation(league_id: str, competition: Any, state: GameState) -> Dict[str, Any]:
        """Extract only the live extras we trust for this sport."""
        if state is not GameState.IN:
            return {}
        raw = _dig(competition, "situation", default={})
        if not isinstance(raw, dict):
            return {}
        situation: Dict[str, Any] = {}
        if league_id in ("nfl", "ncaaf"):
            text = _as_str(raw.get("shortDownDistanceText")) or _as_str(raw.get("downDistanceText"))
            if text:
                situation["down_distance"] = text
            if raw.get("isRedZone"):
                situation["red_zone"] = True
        elif league_id == "mlb":
            for source, target in (("balls", "balls"), ("strikes", "strikes"), ("outs", "outs")):
                value = _as_int(raw.get(source))
                if value is not None:
                    situation[target] = value
            bases = [bool(raw.get(key)) for key in ("onFirst", "onSecond", "onThird")]
            if any(bases):
                situation["bases"] = bases
        return situation
