"""Cached team schedules for the favourite-team schedule screen.

A schedule changes rarely -- a kick-off time moves, a game is added -- so
each favourite's is fetched every few hours. Like the standings and the
team catalog, a lookup never touches the network: a missing or old
schedule is refreshed in a background thread and whatever we already have
is returned meanwhile. Offline restarts are covered by the HTTP client's
own on-disk response cache, so nothing is persisted here.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

from .httpclient import FetchError
from .models import Game, GameTeam, TeamSchedule, team_key
from .providers.base import ProviderError, SportsProvider

log = logging.getLogger(__name__)

DEFAULT_MAX_AGE_SECONDS = 3 * 3600
FAILURE_COOLDOWN_SECONDS = 15 * 60
#: A game that started longer ago than this and is not marked final is
#: assumed to be stale schedule data, not a game still in progress.
LIVE_GRACE = timedelta(hours=6)


class ScheduleCache:
    """One season schedule per team, refreshed lazily in the background."""

    def __init__(
        self,
        provider: SportsProvider,
        max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    ) -> None:
        self.provider = provider
        self.max_age_seconds = max_age_seconds
        self._lock = threading.Lock()
        self._games: Dict[str, List[Game]] = {}
        self._fetched_at: Dict[str, float] = {}
        self._failed_at: Dict[str, float] = {}
        self._refreshing: set = set()

    def get(self, league_id: str, team_id: str,
            refresh_if_stale: bool = True) -> Optional[List[Game]]:
        """The cached schedule, scheduling a refresh when it is old or absent."""
        key = team_key(league_id, team_id)
        with self._lock:
            games = self._games.get(key)
            age = time.time() - self._fetched_at.get(key, 0.0)
        if refresh_if_stale and (games is None or age > self.max_age_seconds):
            self.refresh_async(league_id, team_id)
        return games

    def refresh_async(self, league_id: str, team_id: str) -> None:
        key = team_key(league_id, team_id)
        with self._lock:
            if key in self._refreshing:
                return
            if time.time() - self._failed_at.get(key, 0.0) < FAILURE_COOLDOWN_SECONDS:
                return
            self._refreshing.add(key)
        threading.Thread(
            target=self._refresh_worker, args=(league_id, team_id),
            name=f"schedule-{key}", daemon=True,
        ).start()

    def _refresh_worker(self, league_id: str, team_id: str) -> None:
        key = team_key(league_id, team_id)
        try:
            self.refresh(league_id, team_id)
        except (FetchError, ProviderError) as exc:
            log.warning("Could not refresh the %s schedule: %s", key, exc)
        except Exception:
            log.exception("Unexpected error refreshing the %s schedule", key)
        finally:
            with self._lock:
                self._refreshing.discard(key)

    def refresh(self, league_id: str, team_id: str) -> List[Game]:
        """Fetch one schedule now.  Raises on provider failure."""
        key = team_key(league_id, team_id)
        try:
            games = self.provider.fetch_team_schedule(league_id, team_id)
        except Exception:
            with self._lock:
                self._failed_at[key] = time.time()
            raise
        with self._lock:
            self._games[key] = games
            self._fetched_at[key] = time.time()
            self._failed_at.pop(key, None)
        return games


def build_team_schedule(
    league: str,
    team_id: str,
    scheduled: Optional[Iterable[Game]],
    live: Iterable[Game],
    limit: int,
    fallback: Optional[GameTeam] = None,
    now: Optional[datetime] = None,
) -> Optional[TeamSchedule]:
    """A favourite's next ``limit`` games, or ``None`` if it has none.

    The season schedule says what is coming; the scoreboard says what is
    happening now. Where both know a game, the scoreboard's copy wins, so
    tonight's game shows as live the moment it starts rather than whenever
    the schedule is next refreshed.
    """
    now = now or datetime.now(timezone.utc)
    key = team_key(league, team_id)
    merged: Dict[str, Game] = {}
    for game in scheduled or []:
        if game.involves_any((key,)):
            merged[game.key] = game
    for game in live:
        if game.involves_any((key,)):
            merged[game.key] = game

    coming = sorted(
        (game for game in merged.values()
         if not game.is_final and game.start_time >= now - LIVE_GRACE),
        key=lambda game: (game.start_time, game.key),
    )[:max(1, limit)]
    if not coming:
        return None
    team = next((side for game in coming for side in (game.home, game.away)
                 if side.key == key and side.logo_url), None)
    team = team or next(side for side in (coming[0].home, coming[0].away)
                        if side.key == key)
    if fallback is not None:
        team = GameTeam(
            league=league, team_id=team_id,
            abbreviation=team.abbreviation or fallback.abbreviation,
            display_name=team.display_name or fallback.display_name,
            short_name=team.short_name, logo_url=team.logo_url or fallback.logo_url,
            color=team.color or fallback.color, alt_color=team.alt_color,
        )
    return TeamSchedule(team=team, games=coming)
