"""Cached league tables.

Standings move at most once a day, so they are fetched rarely and cached
on disk. Like the team catalog, lookups never block on the network: a cold
or stale table is refreshed in a background thread and whatever we already
have is returned meanwhile.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .httpclient import FetchError
from .models import Standings, StandingsGroup, StandingsRow
from .providers.base import ProviderError, SportsProvider

log = logging.getLogger(__name__)

DEFAULT_MAX_AGE_SECONDS = 30 * 60
FAILURE_COOLDOWN_SECONDS = 10 * 60


class StandingsCache:
    """Disk-cached league tables, refreshed lazily in the background."""

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
        self._tables: Dict[str, Standings] = {}
        self._fetched_at: Dict[str, float] = {}
        self._failed_at: Dict[str, float] = {}
        self._refreshing: set = set()
        os.makedirs(cache_dir, exist_ok=True)

    # -- queries ---------------------------------------------------------

    def get(self, league_id: str, refresh_if_stale: bool = True) -> Optional[Standings]:
        """The cached table, scheduling a refresh when it is old or absent."""
        with self._lock:
            if league_id not in self._tables:
                self._load_from_disk(league_id)
            table = self._tables.get(league_id)
            age = time.time() - self._fetched_at.get(league_id, 0.0)
        if refresh_if_stale and (table is None or age > self.max_age_seconds):
            self.refresh_async(league_id)
        return table

    def is_loaded(self, league_id: str) -> bool:
        with self._lock:
            table = self._tables.get(league_id)
        return table is not None and table.row_count > 0

    def leagues(self) -> List[str]:
        with self._lock:
            return sorted(self._tables)

    # -- refresh ---------------------------------------------------------

    def refresh_async(self, league_id: str) -> None:
        with self._lock:
            if league_id in self._refreshing:
                return
            if time.time() - self._failed_at.get(league_id, 0.0) < FAILURE_COOLDOWN_SECONDS:
                return
            self._refreshing.add(league_id)
        threading.Thread(
            target=self._refresh_worker, args=(league_id,),
            name=f"standings-{league_id}", daemon=True,
        ).start()

    def _refresh_worker(self, league_id: str) -> None:
        try:
            self.refresh(league_id)
        except (FetchError, ProviderError) as exc:
            log.warning("Could not refresh %s standings: %s", league_id, exc)
        except Exception:
            log.exception("Unexpected error refreshing %s standings", league_id)
        finally:
            with self._lock:
                self._refreshing.discard(league_id)

    def refresh(self, league_id: str) -> Optional[Standings]:
        """Fetch a table now and persist it.  Raises on provider failure."""
        try:
            table = self.provider.fetch_standings(league_id)
        except Exception:
            with self._lock:
                self._failed_at[league_id] = time.time()
            raise
        if not table.ok:
            with self._lock:
                self._failed_at[league_id] = time.time()
            log.warning("%s standings came back empty (%s); keeping previous copy",
                        league_id, table.error)
            return self.get(league_id, refresh_if_stale=False)
        with self._lock:
            self._tables[league_id] = table
            self._fetched_at[league_id] = time.time()
            self._failed_at.pop(league_id, None)
            self._save_to_disk(league_id, table)
        log.info("Refreshed %s standings (%d teams)", league_id, table.row_count)
        return table

    # -- persistence -----------------------------------------------------

    def _path(self, league_id: str) -> str:
        return os.path.join(self.cache_dir, f"{league_id}.json")

    def _load_from_disk(self, league_id: str) -> None:
        path = self._path(league_id)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                envelope = json.load(handle)
            table = _standings_from_dict(envelope["standings"])
            table.stale = True        # it came off disk, not off the wire
            self._tables[league_id] = table
            self._fetched_at[league_id] = float(envelope.get("fetched_at", 0.0))
            log.debug("Loaded cached %s standings (%d teams)", league_id, table.row_count)
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError, KeyError) as exc:
            log.warning("Ignoring unreadable standings cache %s: %s", path, exc)

    def _save_to_disk(self, league_id: str, table: Standings) -> None:
        envelope = {"fetched_at": self._fetched_at.get(league_id, time.time()),
                    "standings": table.to_dict()}
        try:
            handle = tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.cache_dir,
                prefix=f".{league_id}-", delete=False)
            with handle:
                json.dump(envelope, handle)
            os.replace(handle.name, self._path(league_id))
        except OSError as exc:
            log.warning("Could not persist %s standings: %s", league_id, exc)


def _standings_from_dict(data: Dict) -> Standings:
    groups = []
    for raw_group in data.get("groups", []):
        rows = [
            StandingsRow(
                league=row.get("league", ""),
                team_id=str(row.get("team_id", "")),
                abbreviation=row.get("abbreviation", ""),
                display_name=row.get("display_name", ""),
                rank=row.get("rank"),
                played=row.get("played"),
                wins=row.get("wins"),
                losses=row.get("losses"),
                draws=row.get("draws"),
                points=row.get("points"),
                goal_difference=row.get("goal_difference"),
                games_behind=row.get("games_behind"),
                logo_url=row.get("logo_url"),
                color=row.get("color"),
                note=row.get("note"),
                note_color=row.get("note_color"),
            )
            for row in raw_group.get("rows", [])
            if row.get("team_id")
        ]
        groups.append(StandingsGroup(league=raw_group.get("league", ""),
                                     name=raw_group.get("name", ""), rows=rows))
    try:
        fetched_at = datetime.fromisoformat(data["fetched_at"])
    except (KeyError, ValueError, TypeError):
        fetched_at = datetime.now(timezone.utc)
    return Standings(league=data.get("league", ""), groups=groups, fetched_at=fetched_at)
