"""Display backends, font resolution and the render loop."""

from __future__ import annotations

import importlib.util
import time

import pytest
from PIL import Image

from scoreboard.config import ConfigStore, DisplayConfig
from scoreboard.display.base import Display, NullDisplay
from scoreboard.display.fonts import ROLES, FontRegistry
from scoreboard.display.preview import PreviewDisplay, scale_nearest
from scoreboard.models import LeagueSnapshot
from scoreboard.runner import DisplayRunner
from scoreboard.state import AppState


def write_bdf(path, width=5, height=7):
    """A minimal but valid BDF, standing in for rpi-rgb-led-matrix's fonts."""
    lines = [
        "STARTFONT 2.1",
        "FONT -test-fixed-medium-r-normal--7-70-75-75-c-50-iso8859-1",
        "SIZE 7 75 75",
        f"FONTBOUNDINGBOX {width} {height} 0 -1",
        "STARTPROPERTIES 2", "FONT_ASCENT 6", "FONT_DESCENT 1", "ENDPROPERTIES",
    ]
    codes = list(range(32, 127))
    lines.append(f"CHARS {len(codes)}")
    for code in codes:
        rows = [f"{(0b11111 if (code + row) % 3 == 0 else 0b10001) << 3:02X}"
                for row in range(height)]
        lines += [f"STARTCHAR c{code}", f"ENCODING {code}", "SWIDTH 500 0",
                  f"DWIDTH {width} 0", f"BBX {width} {height} 0 -1", "BITMAP"]
        lines += rows + ["ENDCHAR"]
    lines.append("ENDFONT")
    with open(path, "w", encoding="ascii") as handle:
        handle.write("\n".join(lines) + "\n")


# -- fonts -----------------------------------------------------------------

def test_bdf_fonts_are_preferred_when_present(tmp_path):
    font_dir = tmp_path / "fonts"
    font_dir.mkdir()
    write_bdf(str(font_dir / "5x7.bdf"))
    registry = FontRegistry(cache_dir=str(tmp_path / "cache"),
                            search_dirs=[str(font_dir)])
    font = registry.get("small")
    assert font.source == "bdf:5x7.bdf"
    assert font.height == 7
    assert font.text_width("MIN") == 15


def test_converted_bdf_is_cached_on_disk(tmp_path):
    font_dir = tmp_path / "fonts"
    font_dir.mkdir()
    write_bdf(str(font_dir / "5x7.bdf"))
    cache = tmp_path / "cache"
    FontRegistry(cache_dir=str(cache), search_dirs=[str(font_dir)]).get("small")
    assert (cache / "5x7.pil").exists()
    # A second registry reuses the converted file rather than re-parsing.
    assert FontRegistry(cache_dir=str(cache),
                        search_dirs=[str(font_dir)]).get("small").height == 7


def test_registry_falls_back_when_no_bdf_exists(tmp_path):
    registry = FontRegistry(cache_dir=str(tmp_path), search_dirs=[str(tmp_path)])
    font = registry.get("small")
    assert font.source != ""
    assert not font.source.startswith("bdf:")
    assert font.height > 0 and font.text_width("MIN") > 0


def test_every_role_resolves(fonts):
    described = fonts.describe()
    assert set(described) == set(ROLES)
    assert all(source for source in described.values())


def test_unknown_role_falls_back_to_small(fonts):
    assert fonts.get("nonexistent").height == fonts.get("small").height


def test_fonts_measure_and_truncate(fonts):
    font = fonts.get("small")
    assert font.text_width("") == 0
    assert font.fit("", 10) == ""
    assert font.text_width(font.fit("ABCDEFGHIJKL", 10)) <= 10
    assert font.fit("AB", 1000) == "AB"


# -- backends --------------------------------------------------------------

def test_null_display_counts_frames():
    display = NullDisplay(192, 32)
    display.show(Image.new("RGB", (192, 32)))
    assert display.frame_count == 1 and display.last_image is not None


def test_display_resizes_a_mismatched_frame():
    display = NullDisplay(192, 32)
    display.show(Image.new("RGB", (64, 32)))
    assert display.last_image.size == (192, 32)


def test_preview_display_keeps_the_last_frame():
    config = DisplayConfig()
    display = PreviewDisplay(config)
    display.start()
    assert display.image is None
    frame = Image.new("RGB", (config.width, config.height), (10, 20, 30))
    display.show(frame)
    assert display.image.size == (config.width, config.height)
    assert display.image.getpixel((0, 0)) == (10, 20, 30)
    display.clear()
    assert display.image.getpixel((0, 0)) == (0, 0, 0)


def test_preview_display_writes_a_file(tmp_path):
    config = DisplayConfig()
    target = tmp_path / "frame.png"
    display = PreviewDisplay(config, output_path=str(target))
    display.show(Image.new("RGB", (config.width, config.height), (255, 0, 0)))
    assert target.exists()
    assert Image.open(target).size == (config.width, config.height)
    assert not (tmp_path / "frame.png.tmp").exists(), "writes must be atomic"


@pytest.mark.parametrize("chain,width", [(1, 64), (2, 128), (3, 192)])
def test_preview_display_geometry_follows_config(chain, width):
    display = PreviewDisplay(DisplayConfig(chain_length=chain))
    assert (display.width, display.height) == (width, 32)


def test_matrix_backend_reports_a_useful_error_without_hardware():
    from scoreboard.display.matrix import MatrixDisplay, MatrixUnavailableError

    if importlib.util.find_spec("rgbmatrix") is not None:
        pytest.skip("rgbmatrix is installed; this test covers the absence case")

    with pytest.raises(MatrixUnavailableError) as excinfo:
        MatrixDisplay(DisplayConfig()).start()
    assert "rgbmatrix" in str(excinfo.value)


@pytest.mark.parametrize("chain,width", [(1, 64), (2, 128), (3, 192)])
def test_matrix_display_declares_the_right_canvas(chain, width):
    """cols is one panel; the chain is what makes the canvas wide."""
    from scoreboard.display.matrix import MatrixDisplay

    display = MatrixDisplay(DisplayConfig(rows=32, cols=64, chain_length=chain))
    assert (display.width, display.height) == (width, 32)


def test_scale_nearest_is_bounded():
    image = Image.new("RGB", (192, 32))
    assert scale_nearest(image, 1).size == (192, 32)
    assert scale_nearest(image, 100).size == (192 * 20, 32 * 20)


# -- the render loop -------------------------------------------------------

class RecordingDisplay(Display):
    backend_name = "recording"

    def __init__(self, width=128, height=32):
        super().__init__(width, height)
        self.frames = []
        self.brightness = None
        self.cleared = 0

    def show(self, image):
        self.frames.append(image.copy())

    def set_brightness(self, brightness):
        self.brightness = brightness

    def clear(self):
        self.cleared += 1


def make_runner(tmp_path, patch=None):
    store = ConfigStore(str(tmp_path / "config.json"))
    store.load()
    if patch:
        store.update(patch)
    state = AppState()
    display = RecordingDisplay()
    runner = DisplayRunner(display, state, store,
                           FontRegistry(cache_dir=str(tmp_path / "fonts")), logos=None)
    return runner, display, state, store


def test_runner_renders_frames_and_publishes_a_preview(tmp_path, nfl_games):
    runner, display, state, _ = make_runner(tmp_path)
    state.update_snapshot(LeagueSnapshot("nfl", games=nfl_games))
    runner._tick(0.0)
    assert display.frames and display.frames[-1].size == (display.width, display.height)
    assert state.frame_png is not None and state.frame_png.startswith(b"\x89PNG")
    assert state.screen.layout in ("featured", "cards", "upcoming", "final", "idle")


def test_runner_renders_idle_with_no_data(tmp_path):
    runner, display, state, _ = make_runner(tmp_path)
    runner._tick(0.0)
    assert state.screen.layout == "idle"
    assert display.frames


def test_runner_applies_brightness_once_per_change(tmp_path):
    runner, display, _, store = make_runner(tmp_path, {"display": {"brightness": 40}})
    runner._tick(0.0)
    assert display.brightness == 40
    display.brightness = None
    runner._tick(0.2)
    assert display.brightness is None, "unchanged brightness should not be re-sent"
    store.update({"display": {"brightness": 90}})
    runner._tick(0.4)
    assert display.brightness == 90


def test_runner_blanks_the_display_while_asleep(tmp_path):
    runner, display, state, _ = make_runner(
        tmp_path, {"sleep": {"enabled": True, "start": "00:00", "end": "23:59"}})
    runner._tick(0.0)
    assert state.screen.kind == "sleep"
    assert display.frames[-1].getextrema() == ((0, 0), (0, 0), (0, 0))


def test_runner_force_next_advances(tmp_path, nfl_games):
    runner, _, state, store = make_runner(
        tmp_path, {"rotation": {"layout_mode": "featured", "screen_seconds": 999}})
    state.update_snapshot(LeagueSnapshot("nfl", games=nfl_games))
    runner._tick(0.0)
    first = state.screen.title
    runner.force_next()
    runner._tick(0.2)
    assert state.screen.title != first


def test_runner_test_screens(tmp_path):
    runner, _, state, _ = make_runner(tmp_path)
    assert runner.show_test_screen("test_pattern", seconds=60) is True
    runner._tick(0.0)
    assert state.screen.kind == "test" and state.screen.layout == "test_pattern"

    assert runner.show_test_screen("not-a-screen") is False

    runner.clear_override()
    runner._tick(0.2)
    assert state.screen.kind != "test"


def test_test_screen_expires(tmp_path):
    runner, _, state, _ = make_runner(tmp_path)
    runner.show_test_screen("clock", seconds=1)
    runner._tick(time.monotonic())
    assert state.screen.kind == "test"
    runner._tick(time.monotonic() + 5)
    assert state.screen.kind != "test"


def test_runner_survives_a_display_that_throws(tmp_path):
    class AngryDisplay(RecordingDisplay):
        def show(self, image):
            raise RuntimeError("panel fell off")

    store = ConfigStore(str(tmp_path / "config.json"))
    store.load()
    runner = DisplayRunner(AngryDisplay(), AppState(), store,
                           FontRegistry(cache_dir=str(tmp_path / "fonts")))
    runner._tick(0.0)      # must not raise


def test_runner_thread_starts_and_stops_cleanly(tmp_path):
    runner, display, _, _ = make_runner(tmp_path)
    runner.start()
    try:
        deadline = time.time() + 3
        while not display.frames and time.time() < deadline:
            time.sleep(0.05)
        assert display.frames, "the loop should have drawn something"
    finally:
        runner.stop()
        runner.join(timeout=5)
    assert not runner.is_alive()
    assert display.cleared >= 1


# -- text folding ----------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("MIN", "MIN"),
    ("Montréal Canadiens", "Montreal Canadiens"),
    ("Atlético Madrid", "Atletico Madrid"),
    ("Bayern München", "Bayern Munchen"),
    ("Beşiktaş", "Besiktas"),
    ("Køge", "Koge"),
    ("Saint-Étienne", "Saint-Etienne"),
    ("", ""),
])
def test_non_ascii_names_fold_to_ascii(raw, expected):
    """BDF bitmap fonts are ASCII-only; ESPN is not."""
    from scoreboard.display.fonts import to_ascii

    assert to_ascii(raw) == expected


def test_unfoldable_characters_become_question_marks():
    from scoreboard.display.fonts import to_ascii

    folded = to_ascii("東京 FC")
    assert folded.isascii() and "FC" in folded


def test_fonts_measure_and_draw_non_ascii_text(tmp_path):
    """A name with an accent must not reach Pillow's glyph table unfolded."""
    from PIL import Image, ImageDraw

    font_dir = tmp_path / "fonts"
    font_dir.mkdir()
    write_bdf(str(font_dir / "5x7.bdf"))
    font = FontRegistry(cache_dir=str(tmp_path / "c"),
                        search_dirs=[str(font_dir)]).get("small")

    draw = ImageDraw.Draw(Image.new("RGB", (192, 32)))
    for name in ("Montréal", "Atlético", "München"):
        assert font.text_width(name) > 0
        assert font.fit(name, 40).isascii()
        font.draw(draw, (0, 0), name, (255, 255, 255))   # must not raise


def test_layouts_handle_accented_team_names(render_context):
    from scoreboard.display import layouts
    from scoreboard.samples import sample_live_game

    game = sample_live_game()
    game.home.display_name = "Montréal Canadiens"
    game.home.abbreviation = "MTL"
    game.away.display_name = "Atlético Madrid"
    image = layouts.render_featured(game, render_context)
    assert image.size == (render_context.width, render_context.height)


# -- the rgbmatrix binding's fast-blit incompatibility ---------------------

class _FakeCanvas:
    """Mimics the bindings' Canvas, including the 32-bit pointer failure.

    Older rpi-rgb-led-matrix bindings hand Pillow's internal buffer pointer
    to C. On armv7l any buffer above 2GB arrives as a negative Python int
    and the conversion raises OverflowError -- which is what a Raspberry Pi
    running 32-bit Raspberry Pi OS actually does, mid-run.
    """

    def __init__(self, fast_works=False, supports_unsafe_kwarg=True):
        self.fast_works = fast_works
        self.supports_unsafe_kwarg = supports_unsafe_kwarg
        self.fast_calls = 0
        self.safe_calls = 0
        self.pixels = {}

    def SetImage(self, image, offset_x=0, offset_y=0, unsafe=True):
        if unsafe:
            self.fast_calls += 1
            if not self.fast_works:
                raise OverflowError("can't convert negative value to size_t")
            return
        if not self.supports_unsafe_kwarg:
            raise TypeError("SetImage() takes at most 3 positional arguments")
        self.safe_calls += 1

    def SetPixel(self, x, y, r, g, b):
        self.pixels[(x, y)] = (r, g, b)


class _FakeMatrix:
    def __init__(self, canvas):
        self.canvas = canvas
        self.swaps = 0

    def SwapOnVSync(self, canvas):
        self.swaps += 1
        return canvas

    def Clear(self):
        pass


def _matrix_display_with(canvas, chain=2):
    from scoreboard.display.matrix import MatrixDisplay

    display = MatrixDisplay(DisplayConfig(chain_length=chain))
    display._matrix = _FakeMatrix(canvas)
    display._canvas = canvas
    return display


def _frame(display, value=10):
    return Image.new("RGB", (display.width, display.height), (value, value + 1, value + 2))


def test_fast_blit_is_used_when_it_works():
    canvas = _FakeCanvas(fast_works=True)
    display = _matrix_display_with(canvas)
    display.show(_frame(display))
    assert canvas.fast_calls == 1 and canvas.safe_calls == 0
    assert display._matrix.swaps == 1


def test_overflow_error_falls_back_and_keeps_drawing():
    """The exact failure seen on 32-bit Raspberry Pi OS."""
    canvas = _FakeCanvas(fast_works=False)
    display = _matrix_display_with(canvas)

    display.show(_frame(display, 10))       # fast path raises, falls back
    assert canvas.fast_calls == 1
    assert canvas.safe_calls == 1, "must fall back rather than drop the frame"
    assert display._matrix.swaps == 1

    display.show(_frame(display, 20))       # and must not retry the broken path
    assert canvas.fast_calls == 1
    assert canvas.safe_calls == 2
    assert display._matrix.swaps == 2


def test_fallback_is_logged_once_not_every_frame(caplog):
    canvas = _FakeCanvas(fast_works=False)
    display = _matrix_display_with(canvas)
    with caplog.at_level("WARNING"):
        for value in range(5):
            display.show(_frame(display, value * 10))
    warnings = [r for r in caplog.records if "fast image path" in r.message]
    assert len(warnings) == 1, "a per-frame warning would flood the journal"


def test_ancient_bindings_fall_back_to_per_pixel_writes():
    canvas = _FakeCanvas(fast_works=False, supports_unsafe_kwarg=False)
    display = _matrix_display_with(canvas)
    display.show(_frame(display, 40))
    assert canvas.pixels, "should have written pixels directly"
    assert len(canvas.pixels) == display.width * display.height
    assert canvas.pixels[(0, 0)] == (40, 41, 42)
    assert canvas.pixels[(display.width - 1, display.height - 1)] == (40, 41, 42)


def test_identical_frames_are_not_re_sent():
    canvas = _FakeCanvas(fast_works=True)
    display = _matrix_display_with(canvas)
    frame = _frame(display, 7)
    for _ in range(4):
        display.show(frame)
    assert canvas.fast_calls == 1, "the panels already hold that frame"
    display.show(_frame(display, 8))
    assert canvas.fast_calls == 2


def test_clear_forces_the_next_frame_to_be_sent():
    canvas = _FakeCanvas(fast_works=True)
    display = _matrix_display_with(canvas)
    frame = _frame(display, 7)
    display.show(frame)
    display.clear()
    display.show(frame)
    assert canvas.fast_calls == 2, "after a clear the panels no longer hold it"


def test_mismatched_frame_is_resized_before_blitting():
    canvas = _FakeCanvas(fast_works=True)
    display = _matrix_display_with(canvas, chain=2)
    display.show(Image.new("RGB", (192, 32), (5, 5, 5)))
    assert canvas.fast_calls == 1


def test_non_rgb_frames_are_converted():
    canvas = _FakeCanvas(fast_works=False, supports_unsafe_kwarg=False)
    display = _matrix_display_with(canvas)
    display.show(Image.new("RGBA", (display.width, display.height), (9, 8, 7, 255)))
    assert canvas.pixels[(0, 0)] == (9, 8, 7)


def test_force_safe_blit_skips_the_fast_path_entirely():
    """Lets the per-pixel path be exercised on hardware that would not
    otherwise trigger the fallback, to tell a drawing fault from a
    power one."""
    from scoreboard.display.matrix import MatrixDisplay

    canvas = _FakeCanvas(fast_works=True)
    display = MatrixDisplay(DisplayConfig(chain_length=2, force_safe_blit=True))
    display._matrix = _FakeMatrix(canvas)
    display._canvas = canvas

    display.show(_frame(display, 12))
    assert canvas.fast_calls == 0, "the fast path must not be attempted"
    assert canvas.safe_calls == 1


def test_force_safe_blit_is_off_by_default():
    from scoreboard.display.matrix import MatrixDisplay

    assert MatrixDisplay(DisplayConfig())._fast_blit is True


def test_blit_cost_is_reported_once(caplog):
    canvas = _FakeCanvas(fast_works=True)
    display = _matrix_display_with(canvas)
    with caplog.at_level("INFO"):
        for value in range(4):
            display.show(_frame(display, value * 10))
    reports = [r for r in caplog.records if "First frame pushed" in r.message]
    assert len(reports) == 1
