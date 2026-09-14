"""Background sports-data polling.

One thread walks the enabled leagues, normalizes what it gets and publishes
it into :class:`~scoreboard.state.AppState`.  Polling frequency adapts to
what is actually happening, so we are quick during a favourite team's game
and quiet at 3am.

The display never waits on this thread, and this thread never waits on the
display: a slow or failing endpoint can only ever make the data stale.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from .config import Config, ConfigStore
from .logos import LogoCache
from .models import Game, LeagueSnapshot
from .providers.base import SportsProvider
from .state import AppState

log = logging.getLogger(__name__)

#: Poll faster when a game is about to start.
KICKOFF_WINDOW = timedelta(minutes=10)


class DataScheduler(threading.Thread):
    """Polls each enabled league on an adaptive interval."""

    def __init__(
        self,
        provider: SportsProvider,
        state: AppState,
        config_store: ConfigStore,
        logos: Optional[LogoCache] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        super().__init__(name="data-scheduler", daemon=True)
        self.provider = provider
        self.state = state
        self.config_store = config_store
        self.logos = logos
        self._stop_event = stop_event or threading.Event()
        self._wake = threading.Event()
        self._last_poll: Dict[str, float] = {}

    # -- control ---------------------------------------------------------

    def request_refresh(self) -> None:
        """Ask for a poll as soon as the minimum interval allows."""
        self._wake.set()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake.set()

    # -- main loop -------------------------------------------------------

    def run(self) -> None:
        log.info("Data scheduler started (provider=%s)", self.provider.name)
        while not self._stop_event.is_set():
            interval = 60.0
            try:
                interval = self.poll_once()
            except Exception:
                # The loop must survive anything a provider can throw.
                log.exception("Unexpected error during poll; retrying shortly")
                interval = 30.0
            self._wake.clear()
            self._wake.wait(timeout=max(1.0, interval))
        log.info("Data scheduler stopped")

    def poll_once(self) -> float:
        """Poll every due league and return seconds until the next pass."""
        config = self.config_store.config
        leagues = list(config.sports.enabled_leagues)
        self.state.forget_leagues(leagues)
        if not leagues:
            log.warning("No leagues enabled; nothing to poll")
            return float(config.polling.idle_seconds)

        now = time.monotonic()
        minimum = config.polling.min_interval_seconds
        for league in leagues:
            if now - self._last_poll.get(league, 0.0) < minimum:
                continue
            snapshot = self._fetch(league)
            self._last_poll[league] = time.monotonic()
            self.state.update_snapshot(snapshot)
            self._prefetch_logos(snapshot, config)
            if self._stop_event.is_set():
                break

        return self.next_interval(self.state.games(), config)

    def _fetch(self, league: str) -> LeagueSnapshot:
        try:
            return self.provider.fetch_scoreboard(league)
        except Exception as exc:
            log.exception("Provider raised while fetching %s", league)
            return LeagueSnapshot(league=league, error=str(exc))

    def _prefetch_logos(self, snapshot: LeagueSnapshot, config: Config) -> None:
        if self.logos is None or not config.display.show_logos:
            return
        for game in snapshot.games:
            for team in (game.home, game.away):
                self.logos.prefetch(team.league, team.team_id, team.logo_url)

    # -- interval policy -------------------------------------------------

    @staticmethod
    def next_interval(games: List[Game], config: Config,
                      now: Optional[datetime] = None) -> float:
        """How long to wait before the next pass, based on what is on."""
        now = now or datetime.now(timezone.utc)
        favorites = config.sports.favorite_keys
        polling = config.polling

        live = [game for game in games if game.is_live]
        if any(game.involves_any(favorites) for game in live):
            return float(polling.favorite_live_seconds)
        if live:
            return float(polling.live_seconds)
        starting_soon = any(
            game.is_upcoming and now <= game.start_time <= now + KICKOFF_WINDOW
            for game in games
        )
        if starting_soon:
            return float(polling.live_seconds)
        return float(polling.idle_seconds)
