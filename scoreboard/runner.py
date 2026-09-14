"""The display loop.

Renders the current screen to a 192x32 image, hands it to whichever
:class:`~scoreboard.display.base.Display` backend is in use, and publishes
the same frame for the web preview.  This thread owns the rotation state.

It is deliberately tolerant: a layout that raises, a logo that will not
load, or a provider that has gone quiet must all degrade to "keep drawing
something sensible", never to a stopped display.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional, Tuple

from PIL import Image

from .config import ConfigStore
from .display.base import Display
from .display.fonts import FontRegistry
from .display.layouts import RenderContext, render_screen, render_test_pattern, render_message
from .display import layouts
from .logos import LogoCache
from .models import Game
from .rotation import Rotator, build_playlist
from .samples import sample_card_games, sample_live_game, sample_upcoming_games
from .state import AppState, ScreenInfo

log = logging.getLogger(__name__)

FRAME_INTERVAL = 0.2            # 5 fps is ample for scoreboard content
PREVIEW_INTERVAL = 0.5          # how often the browser preview is re-encoded
PLAYLIST_INTERVAL = 2.0         # how often the playlist is rebuilt

#: Test screens the web UI can request.
TEST_SCREENS = ("clock", "sample_game", "sample_cards", "sample_upcoming", "test_pattern")


class DisplayRunner(threading.Thread):
    """Owns rotation, rendering and the display backend."""

    def __init__(
        self,
        display: Display,
        state: AppState,
        config_store: ConfigStore,
        fonts: FontRegistry,
        logos: Optional[LogoCache] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        super().__init__(name="display-loop", daemon=True)
        self.display = display
        self.state = state
        self.config_store = config_store
        self.fonts = fonts
        self.logos = logos
        self._stop_event = stop_event or threading.Event()
        self._lock = threading.Lock()
        self._rotator = Rotator(config_store.config.rotation.screen_seconds)
        self._override: Optional[Tuple[str, float]] = None
        self._force_next = False
        # None means "never done" -- important because the loop's first tick
        # can legitimately arrive at monotonic time 0.0.
        self._last_preview: Optional[float] = None
        self._last_playlist: Optional[float] = None
        self._last_brightness: Optional[int] = None
        self._was_sleeping = False
        self._last_screen_key = ""

    # -- control (called from Flask threads) ------------------------------

    def force_next(self) -> None:
        with self._lock:
            self._force_next = True
            self._override = None

    def show_test_screen(self, name: str, seconds: float = 15.0) -> bool:
        """Temporarily show a built-in test screen.  False if unknown."""
        if name not in TEST_SCREENS:
            return False
        with self._lock:
            self._override = (name, time.monotonic() + max(1.0, seconds))
        log.info("Showing test screen %r for %.0fs", name, seconds)
        return True

    def clear_override(self) -> None:
        with self._lock:
            self._override = None

    def stop(self) -> None:
        self._stop_event.set()

    # -- main loop --------------------------------------------------------

    def run(self) -> None:
        log.info("Display loop started (backend=%s)", self.display.backend_name)
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                self._tick(started)
            except Exception:
                log.exception("Display loop iteration failed; continuing")
                time.sleep(1.0)
            elapsed = time.monotonic() - started
            self._stop_event.wait(max(0.0, FRAME_INTERVAL - elapsed))
        try:
            self.display.clear()
        except Exception:
            log.exception("Failed to clear the display on shutdown")
        log.info("Display loop stopped")

    def _tick(self, monotonic_now: float) -> None:
        config = self.config_store.config
        self._apply_brightness(config.display.brightness)

        if config.sleep.is_sleeping():
            self._render_sleeping(config)
            return
        if self._was_sleeping:
            log.info("Sleep window ended; resuming display")
            self._was_sleeping = False
            self.state.set_sleeping(False)

        context = self._context(config)
        override = self._take_override(monotonic_now)
        if override is not None:
            image, info = self._render_override(override, context)
        else:
            image, info = self._render_rotation(config, context, monotonic_now)

        self._publish(image, info, monotonic_now)

    # -- rendering --------------------------------------------------------

    def _context(self, config) -> RenderContext:
        return RenderContext(
            fonts=self.fonts,
            logos=self.logos,
            show_logos=config.display.show_logos,
            stale=self.state.any_stale,
            width=config.display.width,
            height=config.display.height,
        )

    def _render_rotation(
        self, config, context: RenderContext, monotonic_now: float
    ) -> Tuple[Image.Image, ScreenInfo]:
        self._rotator.screen_seconds = config.rotation.screen_seconds
        if self._last_playlist is None or monotonic_now - self._last_playlist >= PLAYLIST_INTERVAL:
            games = self.state.games()
            self._rotator.set_playlist(build_playlist(games, config))
            self._last_playlist = monotonic_now

        with self._lock:
            force = self._force_next
            self._force_next = False
        if force:
            self._rotator.advance(monotonic_now)

        screen = self._rotator.tick(monotonic_now)
        image = render_screen(
            screen,
            context,
            next_game=self._next_favorite_game(config),
            online=self.state.online,
        )
        info = ScreenInfo(
            kind=screen.kind.value,
            layout=screen.layout.value,
            title=screen.title or "Idle",
            detail=screen.detail,
            index=self._rotator.index + 1,
            total=len(self._rotator.playlist),
        )
        return image, info

    def _render_override(
        self, name: str, context: RenderContext
    ) -> Tuple[Image.Image, ScreenInfo]:
        if name == "test_pattern":
            image = render_test_pattern(context)
        elif name == "clock":
            image = layouts.render_idle(context, online=self.state.online)
        elif name == "sample_game":
            image = layouts.render_featured(sample_live_game(), context)
        elif name == "sample_cards":
            image = layouts.render_cards(sample_card_games(), context)
        elif name == "sample_upcoming":
            image = layouts.render_upcoming(sample_upcoming_games(), context)
        else:  # pragma: no cover - guarded by show_test_screen
            image = render_message(context, "SCOREBOARD", name)
        return image, ScreenInfo(kind="test", layout=name, title=f"Test: {name}")

    def _render_sleeping(self, config) -> None:
        if not self._was_sleeping:
            log.info(
                "Entering sleep window (%s-%s); blanking display",
                config.sleep.start, config.sleep.end,
            )
            self._was_sleeping = True
            self.state.set_sleeping(True)
            self.state.set_screen(ScreenInfo(kind="sleep", layout="sleep", title="Asleep",
                                             detail=f"{config.sleep.start}-{config.sleep.end}"))
        blank = Image.new("RGB", (config.display.width, config.display.height), (0, 0, 0))
        try:
            self.display.show(blank)
        except Exception:
            log.exception("Could not blank the display")
        now = time.monotonic()
        if self._last_preview is None or now - self._last_preview >= PREVIEW_INTERVAL:
            self._last_preview = now
            self._publish_preview(blank)

    def _publish(self, image: Image.Image, info: ScreenInfo, monotonic_now: float) -> None:
        try:
            self.display.show(image)
        except Exception:
            log.exception("Display backend rejected a frame")

        key = f"{info.kind}|{info.layout}|{info.title}"
        if key != self._last_screen_key:
            self._last_screen_key = key
            log.info("Screen %d/%d: %s [%s]", info.index, info.total, info.title, info.layout)
        self.state.set_screen(info)

        if self._last_preview is None or monotonic_now - self._last_preview >= PREVIEW_INTERVAL:
            self._last_preview = monotonic_now
            self._publish_preview(image)

    def _publish_preview(self, image: Image.Image) -> None:
        from .display.preview import to_png_bytes

        try:
            self.state.set_frame_png(to_png_bytes(image))
        except Exception:
            log.exception("Could not encode the preview frame")

    # -- helpers ----------------------------------------------------------

    def _apply_brightness(self, brightness: int) -> None:
        if brightness == self._last_brightness:
            return
        self._last_brightness = brightness
        try:
            self.display.set_brightness(brightness)
        except Exception:
            log.exception("Could not apply brightness %s", brightness)

    def _take_override(self, monotonic_now: float) -> Optional[str]:
        with self._lock:
            if self._override is None:
                return None
            name, expires = self._override
            if monotonic_now >= expires:
                self._override = None
                log.debug("Test screen %r expired", name)
                return None
            return name

    def _next_favorite_game(self, config) -> Optional[Game]:
        favorites = config.sports.favorite_keys
        if not favorites:
            return None
        now = datetime.now(timezone.utc)
        upcoming = [
            game for game in self.state.games()
            if game.is_upcoming and game.start_time >= now and game.involves_any(favorites)
        ]
        if not upcoming:
            return None
        return min(upcoming, key=lambda game: (game.start_time, game.key))
