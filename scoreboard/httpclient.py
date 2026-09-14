"""Small, well-behaved HTTP helper shared by providers and the logo cache.

Responsibilities:
* sane connect/read timeouts so a hung endpoint never stalls a thread forever
* bounded retries with exponential backoff and jitter
* a descriptive User-Agent
* an on-disk cache of the last successful JSON response per key, so the
  scoreboard keeps working (with visibly stale data) across outages
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests

from . import __version__

log = logging.getLogger(__name__)

USER_AGENT = (
    f"sports-scoreboard/{__version__} "
    "(Raspberry Pi LED matrix scoreboard; +https://github.com/jonwhear/scoreboard)"
)


class FetchError(RuntimeError):
    """Raised when a request could not be completed after all retries."""


@dataclass
class FetchResult:
    """A JSON payload plus where it came from."""

    data: Any
    fetched_at: datetime
    from_cache: bool = False

    @property
    def stale(self) -> bool:
        return self.from_cache


class HttpClient:
    """A thin ``requests`` wrapper with retries and a last-known-good cache."""

    def __init__(
        self,
        cache_dir: Optional[str] = None,
        timeout: float = 8.0,
        retries: int = 3,
        backoff_base: float = 0.8,
        user_agent: str = USER_AGENT,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.retries = retries
        self.backoff_base = backoff_base
        #: Set during shutdown so backoff sleeps are interrupted instead of
        #: holding the service open for the full retry budget.
        self.cancel_event = cancel_event or threading.Event()
        self._lock = threading.Lock()
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "application/json, text/plain, */*",
                "Accept-Encoding": "gzip, deflate",
            }
        )
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)

    # -- public API ------------------------------------------------------

    def get_json(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        cache_key: Optional[str] = None,
        allow_stale: bool = True,
    ) -> FetchResult:
        """Fetch and decode JSON, falling back to the cached copy on failure.

        Raises :class:`FetchError` only when the request fails *and* no
        cached copy is available (or ``allow_stale`` is False).
        """
        try:
            payload = self._get_with_retries(url, params)
        except FetchError as exc:
            if allow_stale and cache_key:
                cached = self._read_cache(cache_key)
                if cached is not None:
                    log.warning("%s -- serving cached copy from %s", exc, cached.fetched_at)
                    return cached
            raise
        result = FetchResult(data=payload, fetched_at=datetime.now(timezone.utc))
        if cache_key:
            self._write_cache(cache_key, result)
        return result

    def get_bytes(self, url: str) -> bytes:
        """Fetch raw bytes (used for logo artwork)."""
        last_error: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            if self.cancel_event.is_set():
                raise FetchError(f"GET {url} cancelled during shutdown")
            try:
                response = self._session.get(url, timeout=(self.timeout, self.timeout))
                response.raise_for_status()
                return response.content
            except requests.RequestException as exc:
                last_error = exc
                self._sleep_backoff(attempt, url, exc)
        raise FetchError(f"GET {url} failed after {self.retries + 1} attempts: {last_error}")

    def close(self) -> None:
        self.cancel_event.set()
        try:
            self._session.close()
        except Exception:  # pragma: no cover - best effort
            log.debug("Ignoring error while closing HTTP session", exc_info=True)

    # -- internals -------------------------------------------------------

    def _get_with_retries(self, url: str, params: Optional[Dict[str, Any]]) -> Any:
        last_error: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            if self.cancel_event.is_set():
                raise FetchError(f"GET {url} cancelled during shutdown")
            try:
                response = self._session.get(
                    url, params=params, timeout=(self.timeout, self.timeout)
                )
                if response.status_code >= 500:
                    raise requests.HTTPError(
                        f"server error {response.status_code}", response=response
                    )
                response.raise_for_status()
                return response.json()
            except ValueError as exc:  # JSON decode error: retrying rarely helps
                raise FetchError(f"GET {url} returned undecodable JSON: {exc}") from exc
            except requests.RequestException as exc:
                last_error = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status is not None and 400 <= status < 500 and status != 429:
                    raise FetchError(f"GET {url} failed with HTTP {status}") from exc
                self._sleep_backoff(attempt, url, exc)
        raise FetchError(f"GET {url} failed after {self.retries + 1} attempts: {last_error}")

    def _sleep_backoff(self, attempt: int, url: str, exc: Exception) -> None:
        """Wait before the next attempt.

        Individual attempts log at DEBUG: when the Pi is offline every league
        would otherwise emit several WARNINGs per poll and bury the journal.
        The caller logs one message when all attempts are exhausted.
        """
        if attempt >= self.retries:
            return
        delay = self.backoff_base * (2 ** attempt) + random.uniform(0, 0.3)
        log.debug(
            "GET %s failed (attempt %d/%d): %s -- retrying in %.1fs",
            url, attempt + 1, self.retries + 1, exc, delay,
        )
        self.cancel_event.wait(delay)

    def _cache_path(self, cache_key: str) -> str:
        digest = hashlib.sha1(cache_key.encode("utf-8")).hexdigest()[:16]
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in cache_key)[:48]
        return os.path.join(self.cache_dir or "", f"{safe}-{digest}.json")

    def _read_cache(self, cache_key: str) -> Optional[FetchResult]:
        if not self.cache_dir:
            return None
        path = self._cache_path(cache_key)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                envelope = json.load(handle)
            fetched_at = datetime.fromisoformat(envelope["fetched_at"])
            return FetchResult(data=envelope["data"], fetched_at=fetched_at, from_cache=True)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError) as exc:
            log.warning("Ignoring unreadable response cache %s: %s", path, exc)
            return None

    def _write_cache(self, cache_key: str, result: FetchResult) -> None:
        if not self.cache_dir:
            return
        path = self._cache_path(cache_key)
        envelope = {"fetched_at": result.fetched_at.isoformat(), "data": result.data}
        with self._lock:
            try:
                handle = tempfile.NamedTemporaryFile(
                    "w", encoding="utf-8", dir=self.cache_dir, prefix=".resp-", delete=False
                )
                with handle:
                    json.dump(envelope, handle)
                os.replace(handle.name, path)
            except OSError as exc:
                log.warning("Could not write response cache %s: %s", path, exc)
