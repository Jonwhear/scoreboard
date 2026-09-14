"""The display backend interface.

A backend receives a fully rendered RGB image of exactly ``width x height``
pixels and puts it somewhere.  It does no layout work, which is what lets
the preview and the physical matrix share one renderer.
"""

from __future__ import annotations

import abc
import logging
from typing import Optional

from PIL import Image

log = logging.getLogger(__name__)


class Display(abc.ABC):
    """Something that can show a 192x32 (or however configured) RGB frame."""

    backend_name = "base"

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height

    def start(self) -> None:
        """Acquire hardware resources.  Safe to call once."""

    @abc.abstractmethod
    def show(self, image: Image.Image) -> None:
        """Display one frame.  ``image`` is RGB and exactly canvas-sized."""

    def set_brightness(self, brightness: int) -> None:
        """Set panel brightness as a percentage (1-100)."""

    def clear(self) -> None:
        """Blank the display."""

    def stop(self) -> None:
        """Release hardware resources."""

    def _check_size(self, image: Image.Image) -> Image.Image:
        if image.size != (self.width, self.height):
            log.warning(
                "Frame size %s does not match display %sx%s; resizing",
                image.size, self.width, self.height,
            )
            return image.resize((self.width, self.height))
        return image


class NullDisplay(Display):
    """Discards frames.  Used by tests and by ``--headless``."""

    backend_name = "null"

    def __init__(self, width: int = 192, height: int = 32) -> None:
        super().__init__(width, height)
        self.last_image: Optional[Image.Image] = None
        self.frame_count = 0

    def show(self, image: Image.Image) -> None:
        self.last_image = self._check_size(image)
        self.frame_count += 1
