"""Configuration model, validation and safe persistence.

Design notes
------------
* Every section is a dataclass with explicit defaults, so a missing or
  partial ``config.json`` still yields a fully populated object.
* Parsing is *lenient*: unknown keys are ignored and out-of-range values are
  clamped rather than raising, because a bad value in one field must never
  stop the scoreboard from starting.
* Writes are atomic (temp file + ``fsync`` + ``os.replace``) and the previous
  good file is kept as ``config.json.bak``, so a power cut cannot leave us
  without a usable configuration.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime
from typing import Any, Callable, Dict, List, Optional, Tuple

from .leagues import filter_known, normalize_league_id
from .models import team_key

log = logging.getLogger(__name__)

GPIO_MAPPINGS = ("adafruit-hat", "adafruit-hat-pwm", "regular", "regular-pi1", "classic",
                 "classic-pi1")
LAYOUT_MODES = ("auto", "featured", "cards")


def _clamp(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    return int(_clamp(value, low, high, default))


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
    return default


def _parse_hhmm(value: Any, default: str) -> str:
    if value is None or value == "":
        return default
    text = str(value).strip()
    parts = text.split(":")
    if len(parts) == 2:
        try:
            hour, minute = int(parts[0]), int(parts[1])
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return f"{hour:02d}:{minute:02d}"
        except ValueError:
            pass
    log.warning("Invalid time %r, using %s", value, default)
    return default


@dataclass
class DisplayConfig:
    """Physical matrix geometry and driver tuning.

    ``cols`` is the width of *one* panel; ``chain_length`` panels are wired
    in series, so the logical canvas is ``cols * chain_length`` wide.
    """

    rows: int = 32
    cols: int = 64
    chain_length: int = 2
    parallel: int = 1
    gpio_mapping: str = "adafruit-hat"
    slowdown_gpio: int = 4
    brightness: int = 50
    pwm_bits: int = 11
    pwm_lsb_nanoseconds: int = 130
    limit_refresh_rate_hz: int = 0
    disable_hardware_pulsing: bool = True
    #: Ask the library to print the achieved refresh rate. Invaluable when
    #: chasing flicker: below roughly 100 Hz the panels visibly shimmer.
    show_refresh_rate: bool = False
    show_logos: bool = True
    #: Unix user the process drops to once the matrix has been initialized.
    #: Empty means "stay as the current user" (used in preview mode).
    run_as_user: str = ""

    @property
    def width(self) -> int:
        return self.cols * self.chain_length

    @property
    def height(self) -> int:
        return self.rows * self.parallel

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DisplayConfig":
        data = data or {}
        mapping = str(data.get("gpio_mapping", cls.gpio_mapping) or "").strip()
        if mapping not in GPIO_MAPPINGS:
            log.warning("Unknown gpio_mapping %r; falling back to adafruit-hat", mapping)
            mapping = "adafruit-hat"
        return cls(
            rows=_clamp_int(data.get("rows"), 8, 64, 32),
            cols=_clamp_int(data.get("cols"), 8, 256, 64),
            chain_length=_clamp_int(data.get("chain_length"), 1, 8, 2),
            parallel=_clamp_int(data.get("parallel"), 1, 3, 1),
            gpio_mapping=mapping,
            slowdown_gpio=_clamp_int(data.get("slowdown_gpio"), 0, 5, 4),
            brightness=_clamp_int(data.get("brightness"), 1, 100, 50),
            pwm_bits=_clamp_int(data.get("pwm_bits"), 1, 11, 11),
            pwm_lsb_nanoseconds=_clamp_int(data.get("pwm_lsb_nanoseconds"), 50, 3000, 130),
            limit_refresh_rate_hz=_clamp_int(data.get("limit_refresh_rate_hz"), 0, 400, 0),
            disable_hardware_pulsing=_as_bool(data.get("disable_hardware_pulsing"), True),
            show_refresh_rate=_as_bool(data.get("show_refresh_rate"), False),
            show_logos=_as_bool(data.get("show_logos"), True),
            run_as_user=str(data.get("run_as_user", "") or ""),
        )


@dataclass(frozen=True)
class FavoriteTeam:
    """A favourite stored by stable ``league`` + ``team_id``.

    The name fields are a cache for the web UI so it can render the list
    without hitting the network; matching always uses the id.
    """

    league: str
    team_id: str
    abbreviation: str = ""
    display_name: str = ""

    @property
    def key(self) -> str:
        return team_key(self.league, self.team_id)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional["FavoriteTeam"]:
        if not isinstance(data, dict):
            return None
        league = normalize_league_id(str(data.get("league", "")))
        team_id = str(data.get("team_id", "") or data.get("id", "") or "").strip()
        if not league or not team_id:
            log.warning("Skipping malformed favourite team entry: %r", data)
            return None
        return cls(
            league=league,
            team_id=team_id,
            abbreviation=str(data.get("abbreviation", "") or ""),
            display_name=str(data.get("display_name", "") or ""),
        )


@dataclass
class SportsConfig:
    enabled_leagues: List[str] = field(default_factory=lambda: ["nfl", "mlb", "nhl", "nba"])
    favorite_teams: List[FavoriteTeam] = field(default_factory=list)

    @property
    def favorite_keys(self) -> frozenset:
        return frozenset(fav.key for fav in self.favorite_teams)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SportsConfig":
        data = data or {}
        leagues = filter_known(data.get("enabled_leagues") or [])
        if not leagues:
            log.warning("No valid enabled_leagues configured; defaulting to NFL/MLB/NHL/NBA")
            leagues = ["nfl", "mlb", "nhl", "nba"]
        favorites: List[FavoriteTeam] = []
        seen = set()
        for raw in data.get("favorite_teams") or []:
            fav = FavoriteTeam.from_dict(raw)
            if fav and fav.key not in seen:
                seen.add(fav.key)
                favorites.append(fav)
        return cls(enabled_leagues=leagues, favorite_teams=favorites)


@dataclass
class RotationConfig:
    screen_seconds: int = 8
    favorites_only: bool = False
    show_nonfavorite_live: bool = True
    show_recent_finals: bool = True
    show_upcoming: bool = True
    layout_mode: str = "auto"
    #: A final stays eligible for this many hours after the game ends.
    final_window_hours: int = 12
    #: An upcoming game becomes eligible this many hours before kickoff.
    upcoming_window_hours: int = 36
    #: Hard cap on playlist length so a busy Saturday cannot make the
    #: rotation take 20 minutes to come back around.
    max_screens: int = 12

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RotationConfig":
        data = data or {}
        mode = str(data.get("layout_mode", "auto") or "auto").strip().lower()
        if mode not in LAYOUT_MODES:
            log.warning("Unknown layout_mode %r; using 'auto'", mode)
            mode = "auto"
        return cls(
            screen_seconds=_clamp_int(data.get("screen_seconds"), 3, 120, 8),
            favorites_only=_as_bool(data.get("favorites_only"), False),
            show_nonfavorite_live=_as_bool(data.get("show_nonfavorite_live"), True),
            show_recent_finals=_as_bool(data.get("show_recent_finals"), True),
            show_upcoming=_as_bool(data.get("show_upcoming"), True),
            layout_mode=mode,
            final_window_hours=_clamp_int(data.get("final_window_hours"), 1, 72, 12),
            upcoming_window_hours=_clamp_int(data.get("upcoming_window_hours"), 1, 336, 36),
            max_screens=_clamp_int(data.get("max_screens"), 1, 60, 12),
        )


@dataclass
class SleepConfig:
    """Overnight blanking window.  ``start`` may be later than ``end``."""

    enabled: bool = False
    start: str = "01:00"
    end: str = "07:00"

    def is_sleeping(self, now: Optional[datetime] = None) -> bool:
        if not self.enabled:
            return False
        now = now or datetime.now()
        current = now.time()
        start = _to_time(self.start)
        end = _to_time(self.end)
        if start == end:
            return False
        if start < end:
            return start <= current < end
        return current >= start or current < end  # window crosses midnight

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SleepConfig":
        data = data or {}
        return cls(
            enabled=_as_bool(data.get("enabled"), False),
            start=_parse_hhmm(data.get("start"), "01:00"),
            end=_parse_hhmm(data.get("end"), "07:00"),
        )


def _to_time(value: str) -> dtime:
    hour, minute = value.split(":")
    return dtime(int(hour), int(minute))


@dataclass
class PollingConfig:
    """Adaptive polling intervals, in seconds."""

    favorite_live_seconds: int = 15
    live_seconds: int = 25
    idle_seconds: int = 180
    #: Minimum gap between two requests to the same league endpoint.
    min_interval_seconds: int = 10
    http_timeout_seconds: float = 8.0
    http_retries: int = 3

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PollingConfig":
        data = data or {}
        return cls(
            favorite_live_seconds=_clamp_int(data.get("favorite_live_seconds"), 10, 300, 15),
            live_seconds=_clamp_int(data.get("live_seconds"), 10, 600, 25),
            idle_seconds=_clamp_int(data.get("idle_seconds"), 30, 3600, 180),
            min_interval_seconds=_clamp_int(data.get("min_interval_seconds"), 5, 120, 10),
            http_timeout_seconds=_clamp(data.get("http_timeout_seconds"), 1.0, 60.0, 8.0),
            http_retries=_clamp_int(data.get("http_retries"), 0, 6, 3),
        )


@dataclass
class WebConfig:
    host: str = "0.0.0.0"
    port: int = 8080

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WebConfig":
        data = data or {}
        return cls(
            host=str(data.get("host", "0.0.0.0") or "0.0.0.0"),
            port=_clamp_int(data.get("port"), 1, 65535, 8080),
        )


@dataclass
class Config:
    """Root configuration object."""

    display: DisplayConfig = field(default_factory=DisplayConfig)
    sports: SportsConfig = field(default_factory=SportsConfig)
    rotation: RotationConfig = field(default_factory=RotationConfig)
    sleep: SleepConfig = field(default_factory=SleepConfig)
    polling: PollingConfig = field(default_factory=PollingConfig)
    web: WebConfig = field(default_factory=WebConfig)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Config":
        data = data if isinstance(data, dict) else {}
        return cls(
            display=DisplayConfig.from_dict(data.get("display")),
            sports=SportsConfig.from_dict(data.get("sports")),
            rotation=RotationConfig.from_dict(data.get("rotation")),
            sleep=SleepConfig.from_dict(data.get("sleep")),
            polling=PollingConfig.from_dict(data.get("polling")),
            web=WebConfig.from_dict(data.get("web")),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "display": asdict(self.display),
            "sports": {
                "enabled_leagues": list(self.sports.enabled_leagues),
                "favorite_teams": [asdict(fav) for fav in self.sports.favorite_teams],
            },
            "rotation": asdict(self.rotation),
            "sleep": asdict(self.sleep),
            "polling": asdict(self.polling),
            "web": asdict(self.web),
        }

    def copy(self) -> "Config":
        return Config.from_dict(self.to_dict())


def default_config() -> Config:
    return Config()


def deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """Merge ``patch`` into a copy of ``base``, recursing into dicts."""
    result = dict(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class ConfigStore:
    """Thread-safe load/save of ``config.json`` with backup and listeners."""

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(path)
        self.backup_path = self.path + ".bak"
        self._lock = threading.RLock()
        self._config = default_config()
        self._listeners: List[Callable[[Config], None]] = []
        self._loaded_from: str = "defaults"

    # -- accessors -------------------------------------------------------

    @property
    def config(self) -> Config:
        """The current configuration (treat the returned object as read-only)."""
        with self._lock:
            return self._config

    @property
    def source(self) -> str:
        """Where the live configuration came from: file/backup/defaults."""
        with self._lock:
            return self._loaded_from

    def add_listener(self, callback: Callable[[Config], None]) -> None:
        """Register a callback invoked (outside the lock) after every change."""
        with self._lock:
            self._listeners.append(callback)

    # -- load/save -------------------------------------------------------

    def load(self) -> Config:
        """Load config, falling back to the backup and then to defaults.

        A malformed file is never deleted or overwritten in place: it is
        preserved under a timestamped ``.corrupt-*`` name so it can be
        inspected later.
        """
        with self._lock:
            raw, origin = self._read_first_usable()
            self._config = Config.from_dict(raw)
            self._loaded_from = origin
            config = self._config
        log.info(
            "Loaded configuration from %s (leagues=%s, favourites=%d)",
            origin,
            ",".join(config.sports.enabled_leagues),
            len(config.sports.favorite_teams),
        )
        return config

    def _read_first_usable(self) -> Tuple[Dict[str, Any], str]:
        for candidate, origin in ((self.path, "file"), (self.backup_path, "backup")):
            if not os.path.exists(candidate):
                continue
            try:
                with open(candidate, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if not isinstance(data, dict):
                    raise ValueError("top-level JSON value is not an object")
                return data, origin
            except (OSError, ValueError) as exc:
                log.error("Configuration at %s is unusable: %s", candidate, exc)
                if candidate == self.path:
                    self._quarantine(candidate)
        log.warning("No usable configuration found; starting from defaults")
        return {}, "defaults"

    def _quarantine(self, path: str) -> None:
        """Move a corrupt config aside so we never silently destroy it."""
        target = f"{path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
        try:
            shutil.move(path, target)
            log.error("Moved corrupt configuration to %s", target)
        except OSError as exc:
            log.error("Could not quarantine corrupt configuration %s: %s", path, exc)

    def save(self, config: Optional[Config] = None) -> Config:
        """Persist ``config`` atomically and notify listeners."""
        with self._lock:
            if config is not None:
                self._config = config
            to_write = self._config
            payload = json.dumps(to_write.to_dict(), indent=2, sort_keys=False) + "\n"
            self._atomic_write(payload)
            self._loaded_from = "file"
        self._notify(to_write)
        return to_write

    def update(self, patch: Dict[str, Any]) -> Config:
        """Apply a partial update (nested dict), validate, persist, notify."""
        with self._lock:
            merged = deep_merge(self._config.to_dict(), patch or {})
            self._config = Config.from_dict(merged)
            to_write = self._config
            payload = json.dumps(to_write.to_dict(), indent=2) + "\n"
            self._atomic_write(payload)
            self._loaded_from = "file"
        log.info("Configuration updated: %s", ", ".join(sorted((patch or {}).keys())) or "(no-op)")
        self._notify(to_write)
        return to_write

    def _atomic_write(self, payload: str) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        if os.path.exists(self.path):
            try:
                shutil.copy2(self.path, self.backup_path)
            except OSError as exc:  # non-fatal: we still want to save
                log.warning("Could not refresh config backup: %s", exc)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=directory, prefix=".config-", suffix=".tmp", delete=False
        )
        temp_path = handle.name
        try:
            with handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            # NamedTemporaryFile creates 0600 files; if the service started as
            # root the unprivileged account must still be able to read this.
            os.chmod(temp_path, 0o644)
            os.replace(temp_path, self.path)
            self._fsync_dir(directory)
        except OSError:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise

    @staticmethod
    def _fsync_dir(directory: str) -> None:
        try:
            fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError as exc:  # some filesystems refuse this; harmless
            log.debug("Directory fsync skipped for %s: %s", directory, exc)
        finally:
            os.close(fd)

    def _notify(self, config: Config) -> None:
        for listener in list(self._listeners):
            try:
                listener(config)
            except Exception:  # a bad listener must not break config saves
                log.exception("Configuration listener %r failed", listener)
