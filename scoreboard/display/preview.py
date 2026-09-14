"""Preview backend: the same framebuffer, as an image instead of LEDs.

This is what makes the project developable on a laptop and debuggable from
a browser -- it is the identical frame the matrix would receive.
"""

from __future__ import annotations

import io
import logging
import os
import threading
from typing import Optional

from PIL import Image

from .base import Display

log = logging.getLogger(__name__)


class PreviewDisplay(Display):
    """Keeps the most recent frame in memory, optionally writing it to disk."""

    backend_name = "preview"

    def __init__(self, config, output_path: Optional[str] = None) -> None:
        super().__init__(config.width, config.height)
        self.output_path = output_path
        self._lock = threading.Lock()
        self._image: Optional[Image.Image] = None
        self.frame_count = 0

    def start(self) -> None:
        log.info(
            "Preview display active (%dx%d)%s",
            self.width, self.height,
            f", writing {self.output_path}" if self.output_path else "",
        )

    def show(self, image: Image.Image) -> None:
        image = self._check_size(image)
        with self._lock:
            self._image = image.copy()
            self.frame_count += 1
        if self.output_path:
            self._write(image)

    def set_brightness(self, brightness: int) -> None:
        # Brightness is a hardware property; the preview always shows the
        # frame at full value so the layout stays readable on screen.
        log.debug("Preview ignoring brightness change to %s", brightness)

    def clear(self) -> None:
        with self._lock:
            self._image = Image.new("RGB", (self.width, self.height), (0, 0, 0))

    @property
    def image(self) -> Optional[Image.Image]:
        with self._lock:
            return self._image.copy() if self._image is not None else None

    def _write(self, image: Image.Image) -> None:
        try:
            directory = os.path.dirname(os.path.abspath(self.output_path))
            os.makedirs(directory, exist_ok=True)
            temp = f"{self.output_path}.tmp"
            image.save(temp, format="PNG")
            os.replace(temp, self.output_path)
        except OSError as exc:
            log.warning("Could not write preview image %s: %s", self.output_path, exc)


def scale_nearest(image: Image.Image, scale: int) -> Image.Image:
    """Blow a frame up with hard pixel edges so individual LEDs stay visible."""
    scale = max(1, min(20, int(scale)))
    if scale == 1:
        return image
    return image.resize((image.width * scale, image.height * scale), Image.NEAREST)


def to_png_bytes(image: Image.Image, scale: int = 1) -> bytes:
    """Encode a frame as PNG, optionally nearest-neighbour upscaled."""
    buffer = io.BytesIO()
    scale_nearest(image, scale).save(buffer, format="PNG")
    return buffer.getvalue()
