"""Rendering: fonts, layouts and the display backends.

Backend selection lives in :mod:`scoreboard.app`, because choosing between
the matrix and the preview is entangled with privilege dropping and with the
fallback behaviour when hardware is missing.
"""

from .base import Display, NullDisplay
from .preview import PreviewDisplay

__all__ = ["Display", "NullDisplay", "PreviewDisplay"]
