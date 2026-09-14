"""Team catalog: cached per-league team lists used for favourite selection.

Catalogs change rarely, so they are fetched at most once a week and stored
on disk.  Lookups never block on the network: a cold catalog is refreshed in
a background thread and the UI simply shows "loading" until it lands.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from typing import Dict, List, Optional

from .httpclient import FetchError
from .models import TeamInfo, team_key
from .providers.base import ProviderError, SportsProvider

log = logging.getLogger(__name__)

DEFAULT_MAX_AGE_SECONDS = 7 * 24 * 3600
#: Do not hammer the provider for a catalog that just failed to download.
FAILURE_COOLDOWN_SECONDS = 300


class TeamCatalog:
    """Disk-cached team lists, refreshed lazily in the background."""

    def __init__(
        self,
        provider: SportsProvider,
        cache_dir: str,
        max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    ) -> None:
        self.provider = provider
        self.cache_dir = cache_dir
        self.max_age_seconds = max_age_seconds
        self._lock = threading.RLock()
        self._teams: Dict[str, List[TeamInfo]] = {}
        self._fetched_at: Dict[str, float] = {}
        self._refreshing: set = set()
        self._failed_at: Dict[str, float] = {}
        os.makedirs(cache_dir, exist_ok=True)

    # -- queries ---------------------------------------------------------

    def get(self, league_id: str, refresh_if_stale: bool = True) -> List[TeamInfo]:
        """Return the cached catalog, scheduling a refresh when needed."""
        with self._lock:
            if league_id not in self._teams:
                self._load_from_disk(league_id)
            teams = list(self._teams.get(league_id, []))
            age = time.time() - self._fetched_at.get(league_id, 0.0)
        if refresh_if_stale and (not teams or age > self.max_age_seconds):
            self.refresh_async(league_id)
        return teams

    def is_loaded(self, league_id: str) -> bool:
        with self._lock:
            return bool(self._teams.get(league_id))

    def search(self, league_id: str, query: str = "", limit: int = 40) -> List[TeamInfo]:
        """Case-insensitive search over name/abbreviation/location."""
        teams = self.get(league_id)
        needle = (query or "").strip().lower()
        if not needle:
            return teams[:limit]

        def score(team: TeamInfo) -> Optional[int]:
            fields = (
                team.abbreviation.lower(),
                team.display_name.lower(),
                team.short_name.lower(),
                team.location.lower(),
            )
            if fields[0] == needle:
                return 0
            for rank, value in enumerate(fields):
                if value.startswith(needle):
                    return 1 + rank
            for value in fields:
                if needle in value:
                    return 10
            return None

        scored = [(score(team), team) for team in teams]
        matches = [(rank, team) for rank, team in scored if rank is not None]
        matches.sort(key=lambda item: (item[0], item[1].display_name.lower()))
        return [team for _, team in matches[:limit]]

    def lookup(self, league_id: str, team_id: str) -> Optional[TeamInfo]:
        wanted = team_key(league_id, team_id)
        for team in self.get(league_id, refresh_if_stale=False):
            if team.key == wanted:
                return team
        return None

    # -- refresh ---------------------------------------------------------

    def refresh_async(self, league_id: str) -> None:
        """Kick off a background refresh unless one is already running."""
        with self._lock:
            if league_id in self._refreshing:
                return
            if time.time() - self._failed_at.get(league_id, 0.0) < FAILURE_COOLDOWN_SECONDS:
                return  # still cooling down from a recent failure
            self._refreshing.add(league_id)
        thread = threading.Thread(
            target=self._refresh_worker, args=(league_id,),
            name=f"teams-{league_id}", daemon=True,
        )
        thread.start()

    def _refresh_worker(self, league_id: str) -> None:
        try:
            self.refresh(league_id)
        except (FetchError, ProviderError) as exc:
            # Expected when the Pi is offline: one line, no traceback.
            log.warning("Could not refresh %s team catalog: %s", league_id, exc)
        except Exception:
            log.exception("Unexpected error refreshing the %s team catalog", league_id)
        finally:
            with self._lock:
                self._refreshing.discard(league_id)

    def refresh(self, league_id: str) -> List[TeamInfo]:
        """Fetch a catalog now and persist it.  Raises on provider failure."""
        try:
            teams = self.provider.fetch_teams(league_id)
        except Exception:
            with self._lock:
                self._failed_at[league_id] = time.time()
            raise
        with self._lock:
            self._failed_at.pop(league_id, None)
        if not teams:
            log.warning("%s team catalog came back empty; keeping previous copy", league_id)
            return self.get(league_id, refresh_if_stale=False)
        # Sort here rather than trusting each provider to do it, so the
        # picker in the web UI is always alphabetical.
        teams = sorted(teams, key=lambda team: team.display_name.lower())
        with self._lock:
            self._teams[league_id] = teams
            self._fetched_at[league_id] = time.time()
            self._save_to_disk(league_id, teams)
        log.info("Refreshed %s team catalog (%d teams)", league_id, len(teams))
        return teams

    # -- persistence -----------------------------------------------------

    def _path(self, league_id: str) -> str:
        return os.path.join(self.cache_dir, f"{league_id}.json")

    def _load_from_disk(self, league_id: str) -> None:
        path = self._path(league_id)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                envelope = json.load(handle)
            teams = [TeamInfo.from_dict(item) for item in envelope.get("teams", [])]
            self._teams[league_id] = [team for team in teams if team.team_id]
            self._fetched_at[league_id] = float(envelope.get("fetched_at", 0.0))
            log.debug("Loaded %d cached %s teams", len(self._teams[league_id]), league_id)
        except FileNotFoundError:
            self._teams.setdefault(league_id, [])
        except (OSError, ValueError, TypeError) as exc:
            log.warning("Ignoring unreadable team cache %s: %s", path, exc)
            self._teams.setdefault(league_id, [])

    def _save_to_disk(self, league_id: str, teams: List[TeamInfo]) -> None:
        envelope = {
            "league": league_id,
            "fetched_at": self._fetched_at.get(league_id, time.time()),
            "teams": [team.to_dict() for team in teams],
        }
        try:
            handle = tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.cache_dir, prefix=f".{league_id}-", delete=False
            )
            with handle:
                json.dump(envelope, handle)
            os.replace(handle.name, self._path(league_id))
        except OSError as exc:
            log.warning("Could not persist %s team catalog: %s", league_id, exc)
