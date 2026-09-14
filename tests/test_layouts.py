"""Layout rendering: correct size, no crashes, and graceful logo fallback."""

from __future__ import annotations

import pytest
from PIL import Image

from scoreboard.display import layouts
from scoreboard.display.layouts import RenderContext
from scoreboard.display.preview import scale_nearest, to_png_bytes
from scoreboard.models import GameState
from scoreboard.rotation import Layout, ScreenItem, ScreenKind
from scoreboard.samples import (sample_card_games, sample_final_games,
                                sample_live_game, sample_upcoming_games)

CANVAS = (192, 32)


def assert_canvas(image):
    assert isinstance(image, Image.Image)
    assert image.size == CANVAS, f"expected {CANVAS}, rendered {image.size}"
    assert image.mode == "RGB"


def lit_pixels(image):
    """Count non-black pixels (a cheap "did we actually draw something")."""
    data = image.tobytes()
    return sum(1 for i in range(0, len(data), 3) if data[i:i + 3] != b"\x00\x00\x00")


# -- every layout renders a correct canvas ---------------------------------

def test_featured_live(render_context):
    image = layouts.render_featured(sample_live_game(), render_context)
    assert_canvas(image)
    assert lit_pixels(image) > 150, "the frame should not be nearly blank"


def test_featured_upcoming_and_final(render_context):
    assert_canvas(layouts.render_featured(sample_upcoming_games()[0], render_context))
    assert_canvas(layouts.render_featured(sample_final_games()[0], render_context))


def test_cards(render_context):
    assert_canvas(layouts.render_cards(sample_card_games(), render_context))


def test_cards_with_fewer_than_three_games(render_context):
    for count in (1, 2, 3):
        assert_canvas(layouts.render_cards(sample_card_games()[:count], render_context))


def test_cards_with_more_games_than_panels(render_context):
    games = sample_card_games() + sample_final_games()
    assert_canvas(layouts.render_cards(games, render_context))


def test_upcoming(render_context):
    assert_canvas(layouts.render_upcoming(sample_upcoming_games(), render_context))
    assert_canvas(layouts.render_upcoming(sample_upcoming_games()[:1], render_context))


def test_finals(render_context):
    assert_canvas(layouts.render_finals(sample_final_games(), render_context))
    assert_canvas(layouts.render_finals(sample_final_games()[:1], render_context))


def test_idle(render_context):
    assert_canvas(layouts.render_idle(render_context))
    assert_canvas(layouts.render_idle(render_context, next_game=sample_upcoming_games()[0]))
    assert_canvas(layouts.render_idle(render_context, online=False))


def test_message_and_test_pattern(render_context):
    assert_canvas(layouts.render_message(render_context, "SCOREBOARD", "starting"))
    assert_canvas(layouts.render_test_pattern(render_context))


def test_render_screen_dispatches_every_layout(render_context):
    cases = [
        ScreenItem(ScreenKind.FAVORITE_LIVE, Layout.FEATURED, [sample_live_game()]),
        ScreenItem(ScreenKind.LIVE, Layout.CARDS, sample_card_games()),
        ScreenItem(ScreenKind.UPCOMING, Layout.UPCOMING, sample_upcoming_games()),
        ScreenItem(ScreenKind.FINAL, Layout.FINAL, sample_final_games()),
        ScreenItem(ScreenKind.IDLE, Layout.IDLE, []),
    ]
    for screen in cases:
        assert_canvas(layouts.render_screen(screen, render_context))


def test_render_screen_falls_back_when_a_layout_raises(render_context, monkeypatch):
    def explode(*args, **kwargs):
        raise ValueError("simulated layout bug")

    monkeypatch.setattr(layouts, "render_featured", explode)
    screen = ScreenItem(ScreenKind.FAVORITE_LIVE, Layout.FEATURED, [sample_live_game()])
    assert_canvas(layouts.render_screen(screen, render_context))


def test_a_layout_with_no_games_falls_back_to_idle(render_context):
    screen = ScreenItem(ScreenKind.LIVE, Layout.CARDS, [])
    assert_canvas(layouts.render_screen(screen, render_context))


# -- geometry follows configuration ----------------------------------------

@pytest.mark.parametrize("chain,expected", [(1, 64), (2, 128), (3, 192), (4, 256)])
def test_canvas_width_follows_chain_length(fonts, chain, expected):
    context = RenderContext(fonts=fonts, logos=None, show_logos=False,
                            width=64 * chain, height=32)
    image = layouts.render_cards(sample_card_games(), context)
    assert image.size == (expected, 32)


# -- logos -----------------------------------------------------------------

class BrokenLogoCache:
    """Every lookup explodes, the way a corrupted cache might."""

    def get(self, *args, **kwargs):
        raise RuntimeError("disk on fire")


class MissingLogoCache:
    """Nothing is cached yet -- the normal cold-start case."""

    def get(self, *args, **kwargs):
        return None


class SquareLogoCache:
    def get(self, league, team_id, url, size):
        return Image.new("RGBA", size, (0, 120, 255, 255))


def test_missing_logo_falls_back_to_the_abbreviation(fonts):
    context = RenderContext(fonts=fonts, logos=MissingLogoCache(), show_logos=True)
    image = layouts.render_cards(sample_card_games(), context)
    assert_canvas(image)
    assert lit_pixels(image) > 100


def test_a_broken_logo_cache_does_not_break_the_frame(fonts):
    context = RenderContext(fonts=fonts, logos=BrokenLogoCache(), show_logos=True)
    for renderer, games in (
        (layouts.render_featured, sample_live_game()),
        (layouts.render_cards, sample_card_games()),
        (layouts.render_finals, sample_final_games()),
        (layouts.render_upcoming, sample_upcoming_games()),
    ):
        assert_canvas(renderer(games, context))


def test_logos_are_drawn_when_available(fonts):
    with_logos = layouts.render_cards(
        sample_card_games(),
        RenderContext(fonts=fonts, logos=SquareLogoCache(), show_logos=True))
    without = layouts.render_cards(
        sample_card_games(),
        RenderContext(fonts=fonts, logos=SquareLogoCache(), show_logos=False))
    assert_canvas(with_logos)
    assert lit_pixels(with_logos) > lit_pixels(without)


# -- stale marker ----------------------------------------------------------

def test_stale_data_is_marked_without_relying_on_colour_alone(fonts):
    fresh = layouts.render_idle(RenderContext(fonts=fonts, logos=None, show_logos=False,
                                              stale=False))
    stale = layouts.render_idle(RenderContext(fonts=fonts, logos=None, show_logos=False,
                                              stale=True))
    assert stale.getpixel((191, 0)) != fresh.getpixel((191, 0))
    assert lit_pixels(stale) > lit_pixels(fresh)


def test_offline_idle_says_so_in_words(fonts):
    online = layouts.render_idle(RenderContext(fonts=fonts, logos=None, show_logos=False))
    offline = layouts.render_idle(RenderContext(fonts=fonts, logos=None, show_logos=False),
                                  online=False)
    assert online.tobytes() != offline.tobytes()


# -- helpers ---------------------------------------------------------------

def test_period_text_is_sport_aware():
    live = sample_live_game()
    assert layouts.period_text(live) == "Q3"
    mlb = sample_card_games()[1]
    assert layouts.period_text(mlb) == "BOT 7"
    nhl = sample_card_games()[2]
    nhl.state = GameState.IN
    nhl.period = 2
    assert layouts.period_text(nhl) == "P2"


def test_readable_lifts_near_black_team_colours():
    assert layouts.readable("#000000") != (0, 0, 0)
    assert layouts.readable("#ff0000") == (255, 0, 0)
    assert layouts.readable(None) == layouts.WHITE
    assert layouts.readable("not a colour") == layouts.WHITE


def test_fit_font_shrinks_to_fit(fonts):
    wide = fonts.get("score")
    narrow = layouts.fit_font(fonts, ("score", "large", "medium", "small", "tiny"),
                              "888", 12)
    assert narrow.text_width("888") <= wide.text_width("888")


def test_font_fit_truncates_long_text(fonts):
    font = fonts.get("small")
    assert font.text_width(font.fit("A very long broadcast name", 30)) <= 30


# -- preview helpers -------------------------------------------------------

def test_nearest_neighbour_scaling_keeps_hard_pixel_edges(render_context):
    image = layouts.render_test_pattern(render_context)
    scaled = scale_nearest(image, 4)
    assert scaled.size == (768, 128)
    # Every pixel of a 4x block must be identical under nearest-neighbour.
    for dx in range(4):
        for dy in range(4):
            assert scaled.getpixel((dx, dy)) == scaled.getpixel((0, 0))


def test_png_encoding(render_context):
    png = to_png_bytes(layouts.render_idle(render_context), scale=2)
    assert png.startswith(b"\x89PNG")
    assert len(png) > 100
