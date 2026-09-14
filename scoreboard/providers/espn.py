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
from ..models import Game, GameState, GameTeam, LeagueSnapshot, TeamInfo, normalize_color
from .base import ProviderError, SportsProvider

log = logging.getLogger(__name__)

SITE_API = "https://site.api.espn.com/apis/site/v2/sports"


@dataclass(frozen=True)
class _EspnLeague:
    """How one canonical league maps onto ESPN's URL space."""

    sport: str
    path: str
    scoreboard_params: Dict[str, Any] = None  # type: ignore[assignment]
    teams_params: Dict[str, Any] = None  # type: ignore[assignment]

    @property
    def base(self) -> str:
        return f"{SITE_API}/{self.sport}/{self.path}"


#: Canonical league id -> ESPN endpoint description.
ESPN_LEAGUES: Dict[str, _EspnLeague] = {
    "nfl": _EspnLeague("football", "nfl"),
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

    # -- parsing (pure functions over already-decoded JSON) ---------------

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
            score=_as_int(competitor.get("score")),
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
