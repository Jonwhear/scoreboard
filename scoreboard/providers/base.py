"""The provider interface.

Implementations must not leak their upstream schema: every public method
returns objects from :mod:`scoreboard.models`.
"""

from __future__ import annotations

import abc
from typing import List

from ..models import Game, LeagueSnapshot, Standings, TeamInfo


class ProviderError(RuntimeError):
    """A provider could not satisfy a request."""


class SportsProvider(abc.ABC):
    """Fetches scores and team catalogs for canonical league ids."""

    #: Short identifier used in logs and the web UI.
    name: str = "provider"

    @abc.abstractmethod
    def supported_leagues(self) -> List[str]:
        """Canonical league ids this provider can serve."""

    @abc.abstractmethod
    def fetch_scoreboard(self, league_id: str) -> LeagueSnapshot:
        """Return the current games for one league.

        Implementations should return a snapshot with ``error`` set (and
        ``stale=True`` when cached data was used) rather than raising, so a
        single bad league never stops the others.
        """

    @abc.abstractmethod
    def fetch_teams(self, league_id: str) -> List[TeamInfo]:
        """Return the full team catalog for one league."""

    def fetch_standings(self, league_id: str) -> Standings:
        """Return the league table.

        Deliberately not abstract: a provider that has no standings feed is
        still a perfectly good provider, and the display simply skips the
        table screens. Override it where the data exists.
        """
        raise ProviderError(f"{self.name} does not provide standings")

    def fetch_team_schedule(self, league_id: str, team_id: str) -> List[Game]:
        """Return one team's games for the season, past and future.

        Not abstract for the same reason as standings: the schedule screen
        is simply absent for a provider that cannot supply one.
        """
        raise ProviderError(f"{self.name} does not provide team schedules")
