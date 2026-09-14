"""Shared application state.

One small, explicitly locked object is the only mutable state shared between
the data thread, the display thread and the Flask request threads.  Readers
get copies, so no caller can mutate state accidentally.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .models import Game, LeagueSnapshot


@dataclass
class LeagueStatus:
    """Health of the most recent poll of one league."""

    league: str
    last_success: Optional[datetime] = None
    last_attempt: Optional[datetime] = None
    stale: bool = False
    error: Optional[str] = None
    game_count: int = 0

    @property
    def error_short(self) -> Optional[str]:
        """A one-line version of ``error``, for the status page.

        Transport errors stringify into a paragraph of nested exception
        text; the journal keeps the detail, the UI just needs the gist.
        """
        if not self.error:
            return None
        text = self.error.strip()
        lowered = text.lower()
        if "cached response" in lowered:
            return "using cached data"
        if "timed out" in lowered or "timeout" in lowered:
            return "timed out"
        if "not supported" in lowered:
            return text
        if any(marker in lowered for marker in
               ("connection", "proxy", "resolve", "unreachable", "max retries")):
            return "unreachable"
        first = text.split(":")[0].strip()
        return (first[:70] + "…") if len(first) > 70 else first

    def to_dict(self) -> Dict[str, Any]:
        return {
            "league": self.league,
            "last_success": self.last_success.isoformat() if self.last_success else None,
            "last_attempt": self.last_attempt.isoformat() if self.last_attempt else None,
            "stale": self.stale,
            "error": self.error,
            "error_short": self.error_short,
            "game_count": self.game_count,
        }


@dataclass
class ScreenInfo:
    """A human-readable description of what the matrix is showing."""

    kind: str = "startup"
    layout: str = "idle"
    title: str = "Starting up"
    detail: str = ""
    index: int = 0
    total: int = 0
    since: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "layout": self.layout,
            "title": self.title,
            "detail": self.detail,
            "index": self.index,
            "total": self.total,
            "since": self.since.isoformat(),
        }


class AppState:
    """Thread-safe container for everything the UI and display need."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._started_at = time.monotonic()
        self._start_wall = datetime.now(timezone.utc)
        self._snapshots: Dict[str, LeagueSnapshot] = {}
        self._status: Dict[str, LeagueStatus] = {}
        self._screen = ScreenInfo()
        self._last_frame_png: Optional[bytes] = None
        self._last_frame_at: Optional[datetime] = None
        self._display_backend = "none"
        self._display_error: Optional[str] = None
        self._sleeping = False
        self._last_success: Optional[datetime] = None

    # -- sports data -----------------------------------------------------

    def update_snapshot(self, snapshot: LeagueSnapshot) -> None:
        """Record a poll result.  A failed poll keeps the previous games."""
        with self._lock:
            status = self._status.setdefault(snapshot.league, LeagueStatus(snapshot.league))
            status.last_attempt = datetime.now(timezone.utc)
            status.error = snapshot.error
            status.stale = snapshot.stale
            if snapshot.ok or snapshot.games:
                self._snapshots[snapshot.league] = snapshot
                status.game_count = len(snapshot.games)
                if snapshot.ok:
                    status.last_success = snapshot.fetched_at
                    self._last_success = snapshot.fetched_at
            # A failure with no cached games leaves the last snapshot in place.

    def forget_leagues(self, keep: List[str]) -> None:
        """Drop data for leagues no longer enabled."""
        with self._lock:
            for league in list(self._snapshots):
                if league not in keep:
                    self._snapshots.pop(league, None)
                    self._status.pop(league, None)

    def games(self) -> List[Game]:
        with self._lock:
            games: List[Game] = []
            for snapshot in self._snapshots.values():
                games.extend(snapshot.games)
        return games

    def league_statuses(self) -> List[LeagueStatus]:
        with self._lock:
            return [
                LeagueStatus(
                    league=status.league,
                    last_success=status.last_success,
                    last_attempt=status.last_attempt,
                    stale=status.stale,
                    error=status.error,
                    game_count=status.game_count,
                )
                for status in self._status.values()
            ]

    @property
    def last_success(self) -> Optional[datetime]:
        with self._lock:
            return self._last_success

    @property
    def any_stale(self) -> bool:
        with self._lock:
            return any(status.stale or status.error for status in self._status.values())

    @property
    def online(self) -> bool:
        """True when at least one league polled cleanly on its last attempt."""
        with self._lock:
            if not self._status:
                return False
            return any(status.error is None and status.last_success for status in self._status.values())

    # -- display ---------------------------------------------------------

    def set_screen(self, screen: ScreenInfo) -> None:
        with self._lock:
            self._screen = screen

    @property
    def screen(self) -> ScreenInfo:
        with self._lock:
            return self._screen

    def set_frame_png(self, png: bytes) -> None:
        with self._lock:
            self._last_frame_png = png
            self._last_frame_at = datetime.now(timezone.utc)

    @property
    def frame_png(self) -> Optional[bytes]:
        with self._lock:
            return self._last_frame_png

    @property
    def frame_at(self) -> Optional[datetime]:
        with self._lock:
            return self._last_frame_at

    def set_display_backend(self, backend: str, error: Optional[str] = None) -> None:
        with self._lock:
            self._display_backend = backend
            self._display_error = error

    def set_sleeping(self, sleeping: bool) -> None:
        with self._lock:
            self._sleeping = sleeping

    # -- misc ------------------------------------------------------------

    @property
    def uptime_seconds(self) -> float:
        return time.monotonic() - self._started_at

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "started_at": self._start_wall.isoformat(),
                "uptime_seconds": round(self.uptime_seconds, 1),
                "display_backend": self._display_backend,
                "display_error": self._display_error,
                "sleeping": self._sleeping,
                "online": self.online,
                "stale": self.any_stale,
                "last_success": self._last_success.isoformat() if self._last_success else None,
                "frame_at": self._last_frame_at.isoformat() if self._last_frame_at else None,
                "screen": self._screen.to_dict(),
                "leagues": [status.to_dict() for status in self._status.values()],
            }
