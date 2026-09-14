"""Priority and rotation engine.

Given the current set of games and the user's configuration, produce an
ordered playlist of screens.  The rules are deliberately concrete -- this is
a small ordered pipeline, not a generic rules engine.

Priority order (highest first):

1. live games involving a favourite team
2. other live games in enabled leagues
3. upcoming favourite-team games
4. recently completed favourite-team games
5. other enabled games (upcoming, then finals)
6. the idle screen
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import FrozenSet, Iterable, List, Optional, Sequence

from .config import Config
from .models import Game

log = logging.getLogger(__name__)

GAMES_PER_CARD_SCREEN = 3


class ScreenKind(str, Enum):
    FAVORITE_LIVE = "favorite_live"
    LIVE = "live"
    FAVORITE_UPCOMING = "favorite_upcoming"
    FAVORITE_FINAL = "favorite_final"
    UPCOMING = "upcoming"
    FINAL = "final"
    IDLE = "idle"


class Layout(str, Enum):
    """Which renderer draws the screen."""

    FEATURED = "featured"   # layout A: one game across all three panels
    CARDS = "cards"         # layout B: one game per 64x32 panel
    UPCOMING = "upcoming"   # layout C
    FINAL = "final"         # layout D
    IDLE = "idle"           # layout E


#: Lower sorts first.
_KIND_PRIORITY = {
    ScreenKind.FAVORITE_LIVE: 0,
    ScreenKind.LIVE: 1,
    ScreenKind.FAVORITE_UPCOMING: 2,
    ScreenKind.FAVORITE_FINAL: 3,
    ScreenKind.UPCOMING: 4,
    ScreenKind.FINAL: 5,
    ScreenKind.IDLE: 9,
}


@dataclass
class ScreenItem:
    """One screen in the rotation."""

    kind: ScreenKind
    layout: Layout
    games: List[Game] = field(default_factory=list)
    title: str = ""
    detail: str = ""

    @property
    def priority(self) -> int:
        return _KIND_PRIORITY[self.kind]

    @property
    def key(self) -> str:
        """Stable identity, so rotation survives a data refresh."""
        return f"{self.kind.value}|{self.layout.value}|" + ",".join(
            game.key for game in self.games
        )


def _chunk(items: Sequence[Game], size: int) -> List[List[Game]]:
    return [list(items[i:i + size]) for i in range(0, len(items), size)]


def _sort_live(games: Iterable[Game]) -> List[Game]:
    """Closer games and later periods first; ties broken deterministically."""
    return sorted(
        games,
        key=lambda game: (game.score_margin, -(game.period or 0), game.start_time, game.key),
    )


def _sort_upcoming(games: Iterable[Game]) -> List[Game]:
    return sorted(games, key=lambda game: (game.start_time, game.key))


def _sort_finals(games: Iterable[Game]) -> List[Game]:
    return sorted(games, key=lambda game: (game.start_time, game.key), reverse=True)


def _featured_title(game: Game) -> str:
    return f"{game.away.label} @ {game.home.label}"


def build_playlist(
    games: Sequence[Game],
    config: Config,
    now: Optional[datetime] = None,
    favorite_keys: Optional[FrozenSet[str]] = None,
) -> List[ScreenItem]:
    """Build the ordered playlist of screens for the current game set."""
    now = now or datetime.now(timezone.utc)
    rotation = config.rotation
    favorites = favorite_keys if favorite_keys is not None else config.sports.favorite_keys
    enabled = set(config.sports.enabled_leagues)

    eligible = [game for game in games if game.league in enabled]
    fav_games = [game for game in eligible if game.involves_any(favorites)]
    other_games = [game for game in eligible if not game.involves_any(favorites)]
    if rotation.favorites_only:
        other_games = []

    final_cutoff = now - timedelta(hours=rotation.final_window_hours)
    upcoming_cutoff = now + timedelta(hours=rotation.upcoming_window_hours)

    def recent_final(game: Game) -> bool:
        return game.is_final and game.start_time >= final_cutoff

    def soon(game: Game) -> bool:
        return game.is_upcoming and game.start_time <= upcoming_cutoff

    screens: List[ScreenItem] = []

    # 1. favourite live games. In "auto" and "featured" these each get the
    #    full width -- they are the point of the whole thing. "cards" is an
    #    explicit request for three-up everywhere, so it is honoured here too.
    fav_live = _sort_live(g for g in fav_games if g.is_live)
    if rotation.layout_mode == "cards":
        screens.extend(
            ScreenItem(ScreenKind.FAVORITE_LIVE, Layout.CARDS, chunk,
                       title=_chunk_title(chunk), detail="favourite live")
            for chunk in _chunk(fav_live, GAMES_PER_CARD_SCREEN)
        )
    else:
        screens.extend(
            ScreenItem(ScreenKind.FAVORITE_LIVE, Layout.FEATURED, [game],
                       title=_featured_title(game), detail="favourite live")
            for game in fav_live
        )

    # 2. other live games
    if rotation.show_nonfavorite_live and not rotation.favorites_only:
        live_others = _sort_live(g for g in other_games if g.is_live)
        screens.extend(
            _grouped(live_others, ScreenKind.LIVE, rotation.layout_mode, "live")
        )

    # 3. upcoming favourite games
    if rotation.show_upcoming:
        upcoming_favs = _sort_upcoming(g for g in fav_games if soon(g))
        screens.extend(
            _grouped(upcoming_favs, ScreenKind.FAVORITE_UPCOMING, rotation.layout_mode,
                     "favourite upcoming", group_layout=Layout.UPCOMING,
                     single_layout=Layout.UPCOMING)
        )

    # 4. recent favourite finals
    if rotation.show_recent_finals:
        final_favs = _sort_finals(g for g in fav_games if recent_final(g))
        screens.extend(
            _grouped(final_favs, ScreenKind.FAVORITE_FINAL, rotation.layout_mode,
                     "favourite final", group_layout=Layout.FINAL,
                     single_layout=Layout.FINAL)
        )

    # 5. everything else in enabled leagues
    if rotation.show_upcoming:
        screens.extend(
            _grouped(_sort_upcoming(g for g in other_games if soon(g)),
                     ScreenKind.UPCOMING, rotation.layout_mode, "upcoming",
                     group_layout=Layout.UPCOMING, single_layout=Layout.UPCOMING)
        )
    if rotation.show_recent_finals:
        screens.extend(
            _grouped(_sort_finals(g for g in other_games if recent_final(g)),
                     ScreenKind.FINAL, rotation.layout_mode, "final",
                     group_layout=Layout.FINAL, single_layout=Layout.FINAL)
        )

    screens.sort(key=lambda screen: screen.priority)
    if rotation.max_screens and len(screens) > rotation.max_screens:
        log.debug("Trimming playlist from %d to %d screens", len(screens), rotation.max_screens)
        screens = screens[: rotation.max_screens]

    if not screens:
        screens.append(ScreenItem(ScreenKind.IDLE, Layout.IDLE, [], title="Idle"))
    return screens


def _grouped(
    games: Sequence[Game],
    kind: ScreenKind,
    layout_mode: str,
    detail: str,
    group_layout: Layout = Layout.CARDS,
    single_layout: Layout = Layout.FEATURED,
) -> List[ScreenItem]:
    """Turn a list of games into screens honouring the layout preference."""
    games = list(games)
    if not games:
        return []
    if layout_mode == "featured":
        return [
            ScreenItem(kind, Layout.FEATURED, [game], title=_featured_title(game), detail=detail)
            for game in games
        ]
    if layout_mode == "cards":
        return [
            ScreenItem(kind, group_layout, chunk,
                       title=_chunk_title(chunk), detail=detail)
            for chunk in _chunk(games, GAMES_PER_CARD_SCREEN)
        ]
    # auto: a lone game earns the full width, otherwise pack three per screen.
    if len(games) == 1:
        return [ScreenItem(kind, single_layout, list(games),
                           title=_featured_title(games[0]), detail=detail)]
    return [
        ScreenItem(kind, group_layout, chunk, title=_chunk_title(chunk), detail=detail)
        for chunk in _chunk(games, GAMES_PER_CARD_SCREEN)
    ]


def _chunk_title(chunk: Sequence[Game]) -> str:
    return ", ".join(f"{game.away.label}@{game.home.label}" for game in chunk)


class Rotator:
    """Walks a playlist, advancing on a timer or on demand.

    The playlist is rebuilt on every data refresh; the rotator keeps its
    place by screen key so a refresh does not restart the rotation.
    """

    def __init__(self, screen_seconds: int = 8) -> None:
        self.screen_seconds = screen_seconds
        self._playlist: List[ScreenItem] = []
        self._index = 0
        self._shown_at: Optional[float] = None

    @property
    def playlist(self) -> List[ScreenItem]:
        return list(self._playlist)

    @property
    def index(self) -> int:
        return self._index

    def set_playlist(self, screens: List[ScreenItem]) -> None:
        """Replace the playlist, preserving the current screen if it survives."""
        current_key = self.current().key if self._playlist else None
        self._playlist = screens or []
        if current_key is not None:
            for position, screen in enumerate(self._playlist):
                if screen.key == current_key:
                    self._index = position
                    return
        self._index = min(self._index, max(0, len(self._playlist) - 1))

    def current(self) -> Optional[ScreenItem]:
        if not self._playlist:
            return None
        self._index %= len(self._playlist)
        return self._playlist[self._index]

    def tick(self, monotonic_now: float) -> ScreenItem:
        """Return the screen to display now, advancing when its time is up."""
        if not self._playlist:
            self._playlist = [ScreenItem(ScreenKind.IDLE, Layout.IDLE, [], title="Idle")]
            self._index = 0
            self._shown_at = monotonic_now
        if self._shown_at is None:
            self._shown_at = monotonic_now
        elif monotonic_now - self._shown_at >= max(1, self.screen_seconds):
            self.advance(monotonic_now)
        return self.current()  # type: ignore[return-value]

    def advance(self, monotonic_now: Optional[float] = None) -> Optional[ScreenItem]:
        """Move to the next screen immediately."""
        if self._playlist:
            self._index = (self._index + 1) % len(self._playlist)
        if monotonic_now is not None:
            self._shown_at = monotonic_now
        else:
            self._shown_at = None
        return self.current()
