"""Font loading for a 192x32 canvas.

Layouts ask for a *role* ("small", "score", ...) rather than a file, and the
registry resolves it against whatever is available, in order:

1. BDF bitmap fonts shipped with rpi-rgb-led-matrix -- designed for exactly
   this kind of panel, so they are always preferred;
2. a system TrueType face rendered with anti-aliasing disabled;
3. Pillow's bundled default font, so rendering never hard-fails.

All text drawing goes through :class:`Font`, which gives layouts a single
measure/draw interface regardless of which backend won.
"""

from __future__ import annotations

import glob
import logging
import os
import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from PIL import ImageDraw, ImageFont

log = logging.getLogger(__name__)

#: Extra directories can be added with this environment variable.
BDF_ENV_VAR = "SCOREBOARD_BDF_FONTS"

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BDF_SEARCH_DIRS: Tuple[str, ...] = (
    os.path.join(_PROJECT_ROOT, "scoreboard", "assets", "fonts"),
    os.path.expanduser("~/rpi-rgb-led-matrix/fonts"),
    os.path.join(os.path.dirname(_PROJECT_ROOT), "rpi-rgb-led-matrix", "fonts"),
    "/usr/local/share/rpi-rgb-led-matrix/fonts",
    "/opt/rpi-rgb-led-matrix/fonts",
)

TTF_CANDIDATES: Tuple[str, ...] = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
)


#: Characters ESPN uses that have no ASCII equivalent under NFKD.
_EXTRA_TRANSLITERATIONS = {
    "\u00d8": "O", "\u00f8": "o", "\u0110": "D", "\u0111": "d",
    "\u00c6": "AE", "\u00e6": "ae", "\u0152": "OE", "\u0153": "oe",
    "\u00df": "ss", "\u00d0": "D", "\u00f0": "d", "\u00de": "TH",
    "\u00fe": "th", "\u0141": "L", "\u0142": "l",
    "\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u2026": "...", "\u00a0": " ",
}


def to_ascii(text: str) -> str:
    """Fold text down to ASCII.

    The BDF fonts that ship with rpi-rgb-led-matrix are ASCII-only. Pillow's
    bitmap-font renderer indexes its glyph table by code point, so feeding it
    "Montr\u00e9al Canadiens" or "Atl\u00e9tico" is undefined behaviour --
    which is exactly the kind of name ESPN returns. Transliterating is also
    simply more readable on a 32-pixel-tall panel than a missing glyph.
    """
    if not text:
        return ""
    if text.isascii():
        return text
    folded = "".join(_EXTRA_TRANSLITERATIONS.get(char, char) for char in text)
    decomposed = unicodedata.normalize("NFKD", folded)
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return "".join(char if ord(char) < 128 else "?" for char in stripped)


@dataclass(frozen=True)
class FontRole:
    """What a layout needs, expressed independently of any font file."""

    name: str
    #: Preferred BDF filenames, best first.
    bdf: Sequence[str]
    #: Pixel size to use if we fall back to a scalable face.
    ttf_size: int


ROLES: Dict[str, FontRole] = {
    "tiny": FontRole("tiny", ("4x6.bdf", "5x7.bdf"), 7),
    "small": FontRole("small", ("5x7.bdf", "5x8.bdf", "6x9.bdf"), 9),
    "medium": FontRole("medium", ("6x10.bdf", "6x9.bdf", "6x12.bdf"), 11),
    "large": FontRole("large", ("7x13B.bdf", "7x13.bdf", "6x13B.bdf", "8x13B.bdf"), 14),
    "score": FontRole("score", ("9x18B.bdf", "10x20.bdf", "9x15B.bdf", "9x15.bdf"), 19),
}


class Font:
    """A measurable, drawable font, independent of the underlying backend."""

    def __init__(self, pil_font, role: str, source: str, antialias: bool) -> None:
        self._font = pil_font
        self.role = role
        self.source = source
        self.antialias = antialias
        self._height = self._measure_height()

    @property
    def pil_font(self):
        return self._font

    @property
    def height(self) -> int:
        """Cap-to-baseline height in pixels for digits/uppercase text."""
        return self._height

    def _measure_height(self) -> int:
        try:
            bbox = self._font.getbbox("ABCXYZ0123456789")
            return max(1, int(bbox[3] - bbox[1]))
        except Exception:  # pragma: no cover - exotic font backends
            return 8

    def text_width(self, text: str) -> int:
        text = to_ascii(text)
        if not text:
            return 0
        try:
            bbox = self._font.getbbox(text)
            return max(0, int(bbox[2]))
        except Exception:  # pragma: no cover
            return len(text) * 6

    def text_size(self, text: str) -> Tuple[int, int]:
        return self.text_width(text), self.height

    def draw(self, draw: ImageDraw.ImageDraw, xy: Tuple[int, int], text: str, fill) -> None:
        """Draw ``text`` with its *top-left* at ``xy``.

        Pillow anchors bitmap text at the glyph box origin, which differs
        between BDF and TrueType faces; normalizing here keeps layouts free
        of per-font fudge factors.
        """
        text = to_ascii(text)
        if not text:
            return
        previous_mode = draw.fontmode
        if not self.antialias:
            draw.fontmode = "1"  # crisp on/off pixels; no grey fringing
        try:
            try:
                bbox = self._font.getbbox(text)
                offset_y = int(bbox[1])
            except Exception:  # pragma: no cover
                offset_y = 0
            draw.text((xy[0], xy[1] - offset_y), text, font=self._font, fill=fill)
        finally:
            draw.fontmode = previous_mode

    def draw_centered(
        self, draw: ImageDraw.ImageDraw, center_x: int, top_y: int, text: str, fill
    ) -> None:
        self.draw(draw, (center_x - self.text_width(text) // 2, top_y), text, fill)

    def draw_right(
        self, draw: ImageDraw.ImageDraw, right_x: int, top_y: int, text: str, fill
    ) -> None:
        self.draw(draw, (right_x - self.text_width(text), top_y), text, fill)

    def fit(self, text: str, max_width: int) -> str:
        """Truncate ``text`` so it fits within ``max_width`` pixels."""
        text = to_ascii(text)
        if self.text_width(text) <= max_width:
            return text
        for end in range(len(text) - 1, 0, -1):
            candidate = text[:end]
            if self.text_width(candidate) <= max_width:
                return candidate
        return ""


class FontRegistry:
    """Resolves font roles once and caches the result."""

    def __init__(self, cache_dir: Optional[str] = None,
                 search_dirs: Optional[Sequence[str]] = None) -> None:
        self.cache_dir = cache_dir
        self.search_dirs = list(search_dirs or self._default_dirs())
        self._cache: Dict[str, Font] = {}
        self._bdf_index: Optional[Dict[str, str]] = None
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)

    @staticmethod
    def _default_dirs() -> List[str]:
        dirs = list(BDF_SEARCH_DIRS)
        extra = os.environ.get(BDF_ENV_VAR)
        if extra:
            dirs = [part for part in extra.split(os.pathsep) if part] + dirs
        return dirs

    # -- lookup ----------------------------------------------------------

    def get(self, role_name: str) -> Font:
        """Return the font for a role, loading it on first use."""
        if role_name in self._cache:
            return self._cache[role_name]
        role = ROLES.get(role_name) or ROLES["small"]
        font = self._load_bdf(role) or self._load_ttf(role) or self._load_default(role)
        log.info("Font role %-7s -> %s", role.name, font.source)
        self._cache[role_name] = font
        return font

    def describe(self) -> Dict[str, str]:
        """Which concrete font backs each role (for the status page)."""
        return {name: self.get(name).source for name in ROLES}

    # -- backends --------------------------------------------------------

    def _index_bdf(self) -> Dict[str, str]:
        if self._bdf_index is not None:
            return self._bdf_index
        index: Dict[str, str] = {}
        for directory in self.search_dirs:
            if not os.path.isdir(directory):
                continue
            for path in sorted(glob.glob(os.path.join(directory, "*.bdf"))):
                index.setdefault(os.path.basename(path), path)
        if index:
            log.debug("Found %d BDF fonts in %s", len(index), self.search_dirs)
        self._bdf_index = index
        return index

    def _load_bdf(self, role: FontRole) -> Optional[Font]:
        index = self._index_bdf()
        for filename in role.bdf:
            path = index.get(filename)
            if not path:
                continue
            try:
                pil_font = self._bdf_to_pil(path)
            except Exception as exc:
                log.warning("Could not load BDF font %s: %s", path, exc)
                continue
            return Font(pil_font, role.name, f"bdf:{os.path.basename(path)}", antialias=False)
        return None

    def _bdf_to_pil(self, path: str):
        """Convert a BDF to Pillow's bitmap font format (cached on disk)."""
        from PIL import BdfFontFile

        target_dir = self.cache_dir or os.path.join(os.path.dirname(path), ".pilfonts")
        os.makedirs(target_dir, exist_ok=True)
        stem = os.path.splitext(os.path.basename(path))[0]
        target = os.path.join(target_dir, stem)
        if not os.path.exists(target + ".pil"):
            with open(path, "rb") as handle:
                BdfFontFile.BdfFontFile(handle).save(target)
        return ImageFont.load(target + ".pil")

    def _load_ttf(self, role: FontRole) -> Optional[Font]:
        for path in TTF_CANDIDATES:
            if not os.path.exists(path):
                continue
            try:
                pil_font = ImageFont.truetype(path, role.ttf_size)
            except Exception as exc:
                log.warning("Could not load TrueType font %s: %s", path, exc)
                continue
            return Font(pil_font, role.name, f"ttf:{os.path.basename(path)}@{role.ttf_size}",
                        antialias=False)
        return None

    def _load_default(self, role: FontRole) -> Font:
        log.warning(
            "No BDF or TrueType font found for role %r; falling back to Pillow's built-in "
            "font. Install rpi-rgb-led-matrix's fonts/ directory for better results.",
            role.name,
        )
        try:
            pil_font = ImageFont.load_default(size=role.ttf_size)
        except TypeError:  # Pillow < 10.1 has no size argument
            pil_font = ImageFont.load_default()
        return Font(pil_font, role.name, "pillow-default", antialias=False)
