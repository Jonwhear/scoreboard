"""Team logo download, processing and caching.

Rules that matter for a 32-pixel-tall display:

* The render loop must never block on the network.  ``get`` returns whatever
  is already cached and queues a download for anything that is not; the
  layout draws the team abbreviation in the meantime.
* Artwork is downloaded once.  Both the original and each processed size are
  kept on disk, so the scoreboard keeps its logos across reboots and across
  an internet outage.
* Resizing preserves aspect ratio and transparency, and is done with a
  box filter then a hard alpha threshold, because soft edges turn to mush
  on an LED panel.
"""

from __future__ import annotations

import io
import logging
import os
import queue
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from PIL import Image

from .httpclient import FetchError, HttpClient

log = logging.getLogger(__name__)

RETRY_AFTER_SECONDS = 30 * 60
MAX_FAILURES = 4
GIVE_UP_SECONDS = 24 * 3600
MEMORY_CACHE_SIZE = 96


@dataclass
class _Failure:
    count: int = 0
    last_attempt: float = 0.0

    def should_retry(self, now: float) -> bool:
        if self.count >= MAX_FAILURES:
            return now - self.last_attempt > GIVE_UP_SECONDS
        return now - self.last_attempt > RETRY_AFTER_SECONDS


class LogoCache:
    """Disk- and memory-cached team logos, fetched off the render thread."""

    def __init__(self, cache_dir: str, client: Optional[HttpClient] = None,
                 enabled: bool = True) -> None:
        self.cache_dir = cache_dir
        self.raw_dir = os.path.join(cache_dir, "raw")
        self.processed_dir = os.path.join(cache_dir, "processed")
        self.client = client or HttpClient(timeout=10.0, retries=2)
        self.enabled = enabled
        self._memory: "OrderedDict[str, Image.Image]" = OrderedDict()
        self._lock = threading.RLock()
        self._failures: Dict[str, _Failure] = {}
        self._queue: "queue.Queue[Tuple[str, str, str]]" = queue.Queue(maxsize=256)
        self._queued: set = set()
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()
        for directory in (self.raw_dir, self.processed_dir):
            os.makedirs(directory, exist_ok=True)

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._worker is not None:
            return
        self._worker = threading.Thread(target=self._download_loop, name="logo-fetch", daemon=True)
        self._worker.start()
        log.debug("Logo download worker started")

    def stop(self) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(("", "", ""))  # unblock the worker
        except queue.Full:
            pass
        worker = self._worker
        if worker is not None:
            worker.join(timeout=3.0)
        self._worker = None

    # -- public API ------------------------------------------------------

    def get(
        self,
        league: str,
        team_id: str,
        url: Optional[str],
        size: Tuple[int, int],
    ) -> Optional[Image.Image]:
        """Return an RGBA logo fitted into ``size``, or ``None`` if unavailable.

        Never blocks: a missing logo is queued for download and ``None`` is
        returned so the caller can fall back to the team abbreviation.
        """
        if not self.enabled or not team_id:
            return None
        width, height = max(1, size[0]), max(1, size[1])
        memory_key = f"{league}:{team_id}:{width}x{height}"
        with self._lock:
            cached = self._memory.get(memory_key)
            if cached is not None:
                self._memory.move_to_end(memory_key)
                return cached

        processed_path = self._processed_path(league, team_id, width, height)
        image = self._load(processed_path)
        if image is None:
            raw = self._load(self._raw_path(league, team_id))
            if raw is None:
                self._enqueue(league, team_id, url)
                return None
            image = self._process(raw, width, height)
            self._save(image, processed_path)
        self._remember(memory_key, image)
        return image

    def prefetch(self, league: str, team_id: str, url: Optional[str]) -> None:
        """Queue a download without needing a rendered size yet."""
        if self.enabled and not os.path.exists(self._raw_path(league, team_id)):
            self._enqueue(league, team_id, url)

    def stats(self) -> Dict[str, int]:
        with self._lock:
            failed = sum(1 for failure in self._failures.values()
                         if failure.count >= MAX_FAILURES)
            return {
                "cached_raw": self._count_files(self.raw_dir),
                "cached_processed": self._count_files(self.processed_dir),
                "memory": len(self._memory),
                "failed": failed,
                "queued": self._queue.qsize(),
            }

    @staticmethod
    def _count_files(directory: str) -> int:
        total = 0
        for _root, _dirs, files in os.walk(directory):
            total += len(files)
        return total

    # -- processing ------------------------------------------------------

    @staticmethod
    def _process(image: Image.Image, width: int, height: int) -> Image.Image:
        """Fit ``image`` inside ``width x height``, keeping aspect + alpha."""
        source = image.convert("RGBA")
        source.thumbnail((width, height), Image.LANCZOS)
        # Harden the alpha: half-transparent pixels read as dim smudges on
        # a panel, so make each pixel either lit or off.
        alpha = source.getchannel("A").point(lambda value: 255 if value >= 110 else 0)
        source.putalpha(alpha)
        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        canvas.paste(
            source,
            ((width - source.width) // 2, (height - source.height) // 2),
            source,
        )
        return canvas

    # -- paths and IO ----------------------------------------------------

    def _raw_path(self, league: str, team_id: str) -> str:
        return os.path.join(self.raw_dir, league, f"{team_id}.png")

    def _processed_path(self, league: str, team_id: str, width: int, height: int) -> str:
        return os.path.join(self.processed_dir, league, f"{team_id}-{width}x{height}.png")

    @staticmethod
    def _load(path: str) -> Optional[Image.Image]:
        if not os.path.exists(path):
            return None
        try:
            with Image.open(path) as handle:
                return handle.convert("RGBA")
        except Exception as exc:  # truncated download, unsupported format, ...
            log.warning("Discarding unreadable logo %s: %s", path, exc)
            try:
                os.unlink(path)
            except OSError:
                pass
            return None

    @staticmethod
    def _save(image: Image.Image, path: str) -> None:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            temp = path + ".tmp"
            image.save(temp, format="PNG")
            os.replace(temp, path)
        except OSError as exc:
            log.warning("Could not cache logo %s: %s", path, exc)

    def _remember(self, key: str, image: Image.Image) -> None:
        with self._lock:
            self._memory[key] = image
            self._memory.move_to_end(key)
            while len(self._memory) > MEMORY_CACHE_SIZE:
                self._memory.popitem(last=False)

    # -- background downloads --------------------------------------------

    def _enqueue(self, league: str, team_id: str, url: Optional[str]) -> None:
        if not url:
            return
        key = f"{league}:{team_id}"
        now = time.time()
        with self._lock:
            if key in self._queued:
                return
            failure = self._failures.get(key)
            if failure and not failure.should_retry(now):
                return
            self._queued.add(key)
        try:
            self._queue.put_nowait((league, team_id, url))
        except queue.Full:
            with self._lock:
                self._queued.discard(key)
            log.debug("Logo download queue full; skipping %s", key)

    def _download_loop(self) -> None:
        while not self._stop.is_set():
            try:
                league, team_id, url = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if not team_id:
                continue
            key = f"{league}:{team_id}"
            try:
                self._download(league, team_id, url)
                with self._lock:
                    self._failures.pop(key, None)
            except Exception as exc:
                with self._lock:
                    failure = self._failures.setdefault(key, _Failure())
                    failure.count += 1
                    failure.last_attempt = time.time()
                    count = failure.count
                log.warning("Logo download failed for %s (%d attempts): %s", key, count, exc)
            finally:
                with self._lock:
                    self._queued.discard(key)

    def _download(self, league: str, team_id: str, url: str) -> None:
        try:
            payload = self.client.get_bytes(url)
        except FetchError as exc:
            raise RuntimeError(str(exc)) from exc
        with Image.open(io.BytesIO(payload)) as handle:
            image = handle.convert("RGBA")
        self._save(image, self._raw_path(league, team_id))
        log.info("Cached logo for %s:%s (%dx%d)", league, team_id, image.width, image.height)
