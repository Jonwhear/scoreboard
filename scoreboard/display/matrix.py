"""The rpi-rgb-led-matrix backend.

Three 64x32 panels chained horizontally are configured as
``cols=64, chain_length=3`` -- *not* as one 192-wide panel.  The library
addresses each panel individually and builds the 192x32 canvas itself.

This module is imported only when hardware output is requested, so the rest
of the application runs unchanged on a machine with no GPIO.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from PIL import Image

from ..config import DisplayConfig
from .base import Display

log = logging.getLogger(__name__)


class MatrixUnavailableError(RuntimeError):
    """The rgbmatrix bindings or the hardware could not be initialized."""


class MatrixDisplay(Display):
    """Drives HUB75 panels through hzeller's rpi-rgb-led-matrix."""

    backend_name = "matrix"

    def __init__(self, config: DisplayConfig) -> None:
        super().__init__(config.width, config.height)
        self.config = config
        self._matrix: Optional[Any] = None
        self._canvas: Optional[Any] = None
        self._brightness = config.brightness

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        options = self._build_options()
        matrix_class = self._import_matrix()
        log.info(
            "Initializing matrix: %dx%d per panel, chain=%d, parallel=%d, mapping=%s, "
            "slowdown=%d, brightness=%d%% -> %dx%d canvas",
            self.config.cols, self.config.rows, self.config.chain_length,
            self.config.parallel, self.config.gpio_mapping, self.config.slowdown_gpio,
            self.config.brightness, self.width, self.height,
        )
        try:
            self._matrix = matrix_class(options=options)
        except Exception as exc:
            raise MatrixUnavailableError(
                f"Could not initialize the LED matrix: {exc}. "
                "This usually means the process is not running as root, or the "
                "GPIO mapping is wrong for this bonnet."
            ) from exc
        self._canvas = self._matrix.CreateFrameCanvas()
        log.info("Matrix initialized (%dx%d)", self._matrix.width, self._matrix.height)
        if (self._matrix.width, self._matrix.height) != (self.width, self.height):
            log.error(
                "Matrix reports %dx%d but the renderer draws %dx%d -- check rows/cols/chain",
                self._matrix.width, self._matrix.height, self.width, self.height,
            )

    @staticmethod
    def _import_matrix():
        try:
            from rgbmatrix import RGBMatrix  # type: ignore
        except ImportError as exc:
            raise MatrixUnavailableError(
                "The 'rgbmatrix' Python bindings are not importable. Build and install "
                "them from rpi-rgb-led-matrix/bindings/python, or run with --preview."
            ) from exc
        return RGBMatrix

    def _build_options(self):
        try:
            from rgbmatrix import RGBMatrixOptions  # type: ignore
        except ImportError as exc:
            raise MatrixUnavailableError(
                "The 'rgbmatrix' Python bindings are not importable."
            ) from exc

        config = self.config
        options = RGBMatrixOptions()
        options.rows = config.rows
        options.cols = config.cols
        options.chain_length = config.chain_length
        options.parallel = config.parallel
        options.hardware_mapping = config.gpio_mapping
        options.gpio_slowdown = config.slowdown_gpio
        options.brightness = config.brightness
        options.pwm_bits = config.pwm_bits
        options.pwm_lsb_nanoseconds = config.pwm_lsb_nanoseconds
        options.limit_refresh_rate_hz = config.limit_refresh_rate_hz
        # The Adafruit bonnet shares the PWM pin with onboard sound; leaving
        # hardware pulsing enabled is the usual cause of flicker unless the
        # sound module has been blacklisted.
        options.disable_hardware_pulsing = config.disable_hardware_pulsing
        options.show_refresh_rate = config.show_refresh_rate
        # We drop privileges ourselves, to the account that owns the install,
        # rather than letting the library drop to 'daemon'.
        if hasattr(options, "drop_privileges"):
            options.drop_privileges = False
        return options

    # -- frames ----------------------------------------------------------

    def show(self, image: Image.Image) -> None:
        if self._matrix is None or self._canvas is None:
            raise RuntimeError("MatrixDisplay.show() called before start()")
        image = self._check_size(image)
        if image.mode != "RGB":
            image = image.convert("RGB")
        self._canvas.SetImage(image)
        self._canvas = self._matrix.SwapOnVSync(self._canvas)

    def set_brightness(self, brightness: int) -> None:
        brightness = max(1, min(100, int(brightness)))
        if brightness == self._brightness or self._matrix is None:
            self._brightness = brightness
            return
        self._brightness = brightness
        try:
            self._matrix.brightness = brightness
            log.info("Matrix brightness set to %d%%", brightness)
        except Exception:
            log.exception("Could not change matrix brightness")

    def clear(self) -> None:
        if self._matrix is not None:
            try:
                self._matrix.Clear()
            except Exception:
                log.exception("Could not clear the matrix")

    def stop(self) -> None:
        self.clear()
        self._canvas = None
        self._matrix = None
        log.info("Matrix released")
