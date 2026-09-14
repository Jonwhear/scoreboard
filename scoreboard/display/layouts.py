"""Layouts for a 192x32 canvas (three chained 64x32 panels).

Everything here works on a logical canvas and returns a Pillow ``RGB``
image.  It knows nothing about rgbmatrix, ESPN, or HTTP -- which is what
lets the browser preview and the panels render identically.

Design constraints that drove these layouts:

* 32 rows is about four lines of text, total.  Every screen picks three.
* Colour is never the only signal: live/final/upcoming are spelled out in
  words, and the winner of a final gets a marker bar, not just a brighter
  colour.
* Nothing is positioned by eyeballed magic numbers alone -- text is
  measured and centred, and score fonts shrink when a score gets wide.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence, Tuple

from PIL import Image, ImageDraw

from ..models import Game, GameState, GameTeam
from ..rotation import Layout, ScreenItem
from .fonts import Font, FontRegistry

log = logging.getLogger(__name__)

# -- palette ---------------------------------------------------------------

BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
BRIGHT = (255, 255, 255)
DIM = (110, 110, 110)
FAINT = (48, 48, 48)
LIVE = (0, 220, 90)
UPCOMING = (90, 160, 255)
FINAL = (185, 185, 185)
ACCENT = (255, 176, 0)
STALE = (255, 140, 0)
POSSESSION = (255, 210, 40)

PANEL_WIDTH = 64


@dataclass
class RenderContext:
    """Everything a layout needs that is not the screen itself."""

    fonts: FontRegistry
    logos: Optional[object] = None          # LogoCache, or None to disable
    show_logos: bool = True
    stale: bool = False
    now: Optional[datetime] = None
    width: int = 192
    height: int = 32

    @property
    def clock_now(self) -> datetime:
        return self.now or datetime.now()


# -- small drawing helpers -------------------------------------------------

def new_canvas(width: int, height: int) -> Image.Image:
    return Image.new("RGB", (width, height), BLACK)


def _hline(draw: ImageDraw.ImageDraw, x0: int, x1: int, y: int, color) -> None:
    draw.line([(x0, y), (x1, y)], fill=color)


def _vline(draw: ImageDraw.ImageDraw, x: int, y0: int, y1: int, color) -> None:
    draw.line([(x, y0), (x, y1)], fill=color)


def readable(color: Optional[str], fallback=WHITE) -> Tuple[int, int, int]:
    """Convert ``#rrggbb`` to RGB, brightening colours too dark for LEDs."""
    if not color:
        return fallback
    try:
        value = color.lstrip("#")
        rgb = tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, IndexError):
        return fallback
    luma = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
    if luma < 60:  # near-black team colours vanish on a panel
        scale = 60.0 / max(luma, 1.0)
        rgb = tuple(min(255, int(channel * scale) + 30) for channel in rgb)
    return rgb  # type: ignore[return-value]


def fit_font(fonts: FontRegistry, roles: Sequence[str], text: str, max_width: int) -> Font:
    """Largest font from ``roles`` (largest first) whose text fits."""
    chosen = fonts.get(roles[-1])
    for role in roles:
        font = fonts.get(role)
        if font.text_width(text) <= max_width:
            return font
        chosen = font
    return chosen


def state_color(game: Game) -> Tuple[int, int, int]:
    if game.is_live:
        return LIVE
    if game.is_final:
        return FINAL
    return UPCOMING


def state_word(game: Game) -> str:
    if game.is_live:
        return "LIVE"
    if game.is_final:
        return "FINAL"
    return "NEXT"


def local_time(when: datetime, now: Optional[datetime] = None) -> datetime:
    """Convert a UTC timestamp to the Pi's local time zone."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone()


def format_clock_only(game: Game) -> str:
    """Local start time with no relative prefix, e.g. ``4:25P``."""
    start = local_time(game.start_time)
    hour = start.hour % 12 or 12
    return f"{hour}:{start.minute:02d}{'P' if start.hour >= 12 else 'A'}"


def format_start(game: Game, now: Optional[datetime] = None) -> str:
    """Short local start time, e.g. ``"TODAY 4:25P"`` or ``"SUN 1:00P"``."""
    start = local_time(game.start_time)
    reference = now or datetime.now()
    if reference.tzinfo is not None:
        reference = reference.astimezone()
    hour = start.hour % 12 or 12
    suffix = "P" if start.hour >= 12 else "A"
    clock = f"{hour}:{start.minute:02d}{suffix}"
    delta_days = (start.date() - reference.date()).days
    if delta_days == 0:
        return clock
    if delta_days == 1:
        return f"TMW {clock}"
    return f"{start.strftime('%a').upper()} {clock}"


def period_text(game: Game) -> str:
    """Compact period label, e.g. ``Q3``, ``BOT 7``, ``P2``, ``HALF``."""
    detail = (game.status_short or game.status_detail or "").strip()
    if game.league == "mlb":
        upper = detail.upper()
        if upper.startswith(("TOP", "BOT", "MID", "END")) and game.period:
            return f"{upper[:3]} {game.period}"
        return f"INN {game.period}" if game.period else upper[:8]
    if game.league in ("mls", "epl", "efl_championship"):
        if "half" in detail.lower():
            return "HALF"
        return f"{game.period}H" if game.period else "LIVE"
    if game.league == "nhl":
        if game.period and game.period > 3:
            return "OT" if game.period == 4 else "SO"
        return f"P{game.period}" if game.period else "LIVE"
    if game.league == "ncaam":
        if game.period and game.period > 2:
            return f"OT{game.period - 2}"
        return f"{game.period}H" if game.period else "LIVE"
    if game.period and game.period > 4 and game.league in ("nfl", "ncaaf", "nba"):
        return "OT"
    return f"Q{game.period}" if game.period else "LIVE"


def final_text(game: Game) -> str:
    detail = (game.status_short or game.status_detail or "FINAL").upper()
    if "OT" in detail or "SHOOTOUT" in detail:
        return "FINAL/OT"
    return "FINAL"


def _clock_text(game: Game) -> str:
    clock = (game.clock or "").strip()
    if not clock or clock in ("0:00", "0.0"):
        return ""
    return clock


# -- logo drawing ----------------------------------------------------------

def paste_logo(
    canvas: Image.Image,
    context: RenderContext,
    team: GameTeam,
    box: Tuple[int, int, int, int],
) -> bool:
    """Paste a team logo into ``box`` (x0, y0, x1, y1). False if unavailable."""
    if not context.show_logos or context.logos is None:
        return False
    x0, y0, x1, y1 = box
    width, height = x1 - x0 + 1, y1 - y0 + 1
    try:
        logo = context.logos.get(team.league, team.team_id, team.logo_url, (width, height))
    except Exception:  # a broken cache must never stop a frame
        log.exception("Logo lookup failed for %s", team.key)
        return False
    if logo is None:
        return False
    canvas.paste(logo, (x0, y0), logo)
    return True


def draw_team_mark(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    context: RenderContext,
    team: GameTeam,
    box: Tuple[int, int, int, int],
    with_initials: bool = True,
) -> None:
    """Logo if we have one, otherwise a team-coloured crest.

    ``with_initials`` is False where the abbreviation is already on screen
    (the featured layout puts it in the header), so the fallback stays a
    colour crest instead of printing the same three letters twice.
    """
    if paste_logo(canvas, context, team, box):
        return
    x0, y0, x1, y1 = box
    width = x1 - x0 + 1
    primary = readable(team.color)
    secondary = readable(team.alt_color, BRIGHT)
    if not with_initials:
        draw.rectangle([(x0, y0), (x1, y1)], fill=primary, outline=secondary)
        draw.rectangle([(x0 + 3, y0 + 3), (x1 - 3, y1 - 3)], outline=secondary)
        return
    label = team.label[:4]
    font = fit_font(context.fonts, ("medium", "small", "tiny"), label, width)
    text_y = y0 + max(0, ((y1 - y0 + 1) - font.height) // 2)
    font.draw_centered(draw, (x0 + x1) // 2, text_y, font.fit(label, width), primary)
    # A colour bar under the initials keeps the team identifiable at a glance
    # without relying on the text colour alone.
    _hline(draw, x0, x1, y1, secondary)


# -- layout A: featured game ----------------------------------------------

def render_featured(game: Game, context: RenderContext) -> Image.Image:
    """One game across all three panels: logo, score, status, clock."""
    canvas = new_canvas(context.width, context.height)
    draw = ImageDraw.Draw(canvas)
    fonts = context.fonts

    left = (0, PANEL_WIDTH - 1)
    right = (context.width - PANEL_WIDTH, context.width - 1)
    center = (PANEL_WIDTH, context.width - PANEL_WIDTH - 1)

    _draw_featured_side(canvas, draw, context, game.away, left, align_left=True)
    _draw_featured_side(canvas, draw, context, game.home, right, align_left=False)

    _vline(draw, center[0] - 1, 3, context.height - 4, FAINT)
    _vline(draw, center[1] + 1, 3, context.height - 4, FAINT)

    center_x = (center[0] + center[1]) // 2
    color = state_color(game)

    banner = state_word(game) if game.state is not GameState.PRE else ""
    if game.is_final:
        banner = final_text(game)
    if banner:
        banner_font = fit_font(fonts, ("small", "tiny"), banner, center[1] - center[0] - 2)
        banner_font.draw_centered(draw, center_x, 1, banner, color)
        _hline(draw, center_x - 12, center_x + 12, banner_font.height + 2, color)

    if game.is_live:
        label = period_text(game)
        clock = _clock_text(game)
        label_font = fit_font(fonts, ("medium", "small", "tiny"), label, 60)
        label_font.draw_centered(draw, center_x, 12, label, BRIGHT)
        if clock:
            clock_font = fit_font(fonts, ("medium", "small", "tiny"), clock, 60)
            clock_font.draw_centered(draw, center_x, 22, clock, BRIGHT)
        extra = game.situation.get("down_distance") or _mlb_count(game)
        if extra and not clock:
            extra_font = fonts.get("tiny")
            extra_font.draw_centered(draw, center_x, 23, extra_font.fit(extra, 62), DIM)
    elif game.is_final:
        detail = game.venue or game.broadcast or ""
        tiny = fonts.get("tiny")
        if game.note:
            detail = game.note
        if detail:
            tiny.draw_centered(draw, center_x, 22, tiny.fit(detail, 62), DIM)
        small = fonts.get("small")
        winner = game.home if game.home.winner else (game.away if game.away.winner else None)
        if winner is not None:
            small.draw_centered(draw, center_x, 12, small.fit(f"{winner.label} WIN", 62), FINAL)
    else:
        start = format_clock_only(game)
        start_font = fit_font(fonts, ("large", "medium", "small"), start, 62)
        start_font.draw_centered(draw, center_x, 2, start, UPCOMING)
        date_text = local_time(game.start_time).strftime("%a %b %-d").upper()
        small = fonts.get("small")
        small.draw_centered(draw, center_x, start_font.height + 5, small.fit(date_text, 62), DIM)
        if game.broadcast:
            tiny = fonts.get("tiny")
            tiny.draw_centered(draw, center_x, 25, tiny.fit(game.broadcast, 62), ACCENT)

    _draw_stale_marker(draw, context)
    return canvas


def _mlb_count(game: Game, brief: bool = False) -> str:
    """Ball-strike count, e.g. ``2-1`` or ``2-1 1OUT``."""
    situation = game.situation
    if "balls" not in situation or "strikes" not in situation:
        return ""
    text = f"{situation['balls']}-{situation['strikes']}"
    outs = situation.get("outs")
    if outs is not None and not brief:
        text += f" {outs}OUT"
    return text


def _draw_featured_side(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    context: RenderContext,
    team: GameTeam,
    span: Tuple[int, int],
    align_left: bool,
) -> None:
    """One team's panel: abbreviation strip on top, logo + score below."""
    x0, x1 = span
    fonts = context.fonts
    label_font = fonts.get("small")
    label = label_font.fit(team.label, (x1 - x0) - 10)
    color = readable(team.color)

    rank = f"{team.rank} " if team.rank and team.rank <= 25 else ""
    header = f"{rank}{label}"
    label_font.draw_centered(draw, (x0 + x1) // 2, 0, header, BRIGHT)
    _hline(draw, x0 + 6, x1 - 6, label_font.height + 1, color)

    body_top, body_bottom = label_font.height + 4, context.height - 1
    logo_size = min(20, body_bottom - body_top + 1)
    available = (x1 - x0 + 1) - logo_size - 5
    if team.score is None:
        # Pre-game: a big "-" says nothing, the record says something. It sits
        # at the foot of the column so the centre panel owns the middle band.
        value_text = team.record or ""
        value_font = fit_font(fonts, ("medium", "small", "tiny"), value_text, available)
        value_color = DIM
        value_y = body_bottom - value_font.height
    else:
        value_text = team.score_text
        value_font = fit_font(fonts, ("score", "large", "medium"), value_text, available)
        value_color = BRIGHT
        value_y = body_top + max(0, (body_bottom - body_top + 1 - value_font.height) // 2)

    if align_left:
        logo_box = (x0 + 2, body_top, x0 + 1 + logo_size, body_top + logo_size - 1)
        draw_team_mark(canvas, draw, context, team, logo_box, with_initials=False)
        value_font.draw_right(draw, x1 - 2, value_y, value_text, value_color)
    else:
        logo_box = (x1 - 1 - logo_size, body_top, x1 - 2, body_top + logo_size - 1)
        draw_team_mark(canvas, draw, context, team, logo_box, with_initials=False)
        value_font.draw(draw, (x0 + 2, value_y), value_text, value_color)

    if team.has_possession:
        marker_x = x0 + 2 if align_left else x1 - 3
        draw.rectangle([(marker_x, 0), (marker_x + 1, 1)], fill=POSSESSION)


# -- layout B: three game cards -------------------------------------------

def render_cards(games: Sequence[Game], context: RenderContext) -> Image.Image:
    """One game per physical 64x32 panel."""
    canvas = new_canvas(context.width, context.height)
    draw = ImageDraw.Draw(canvas)
    panels = max(1, context.width // PANEL_WIDTH)
    for index in range(panels):
        x0 = index * PANEL_WIDTH
        if index < len(games):
            _draw_card(canvas, draw, context, games[index], x0)
        if index:
            _vline(draw, x0 - 1, 2, context.height - 3, FAINT)
    _draw_stale_marker(draw, context)
    return canvas


def _draw_card(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    context: RenderContext,
    game: Game,
    x0: int,
) -> None:
    fonts = context.fonts
    small = fonts.get("small")
    tiny = fonts.get("tiny")
    inner_left = x0 + 2
    inner_right = x0 + PANEL_WIDTH - 3

    rows = ((game.away, 1), (game.home, 11))
    for team, y in rows:
        logo_drawn = False
        if context.show_logos:
            logo_drawn = paste_logo(canvas, context, team, (inner_left, y, inner_left + 8, y + 8))
        text_x = inner_left + (10 if logo_drawn else 0)
        if not logo_drawn:
            _vline(draw, inner_left, y, y + 8, readable(team.color))
            text_x = inner_left + 3
        score_text = team.score_text if game.state is not GameState.PRE else ""
        score_width = small.text_width(score_text) if score_text else 0
        label_width = inner_right - text_x - score_width - 3
        label_color = BRIGHT if not game.is_final or team.winner else DIM
        small.draw(draw, (text_x, y), small.fit(team.label, max(4, label_width)), label_color)
        if score_text:
            small.draw_right(draw, inner_right, y, score_text, BRIGHT)
        if game.is_final and team.winner:
            # Non-colour winner cue: a solid bar on the outer edge of the row.
            draw.rectangle([(x0, y), (x0 + 1, y + 7)], fill=ACCENT)
        if team.has_possession:
            draw.rectangle([(x0, y + 1), (x0 + 1, y + 6)], fill=POSSESSION)

    status, color = _card_status(game, context)
    _hline(draw, inner_left, inner_right, 20, FAINT)
    status_font = fit_font(fonts, ("small", "tiny"), status, PANEL_WIDTH - 6)
    status_font.draw(draw, (inner_left, 23), status_font.fit(status, PANEL_WIDTH - 6), color)
    if game.broadcast and game.is_upcoming:
        tiny.draw_right(draw, inner_right, 24, tiny.fit(game.broadcast, 22), ACCENT)


def _card_status(game: Game, context: RenderContext) -> Tuple[str, Tuple[int, int, int]]:
    if game.is_live:
        clock = _clock_text(game)
        text = f"{period_text(game)} {clock}".strip()
        extra = game.situation.get("down_distance") or _mlb_count(game, brief=True)
        if not clock and extra:
            text = f"{period_text(game)}  {extra}"
        return text, LIVE
    if game.is_final:
        return final_text(game), FINAL
    return format_start(game, context.now), UPCOMING


# -- layout C: upcoming ----------------------------------------------------

def render_upcoming(games: Sequence[Game], context: RenderContext) -> Image.Image:
    """Upcoming games: matchup, local start time, broadcast, records."""
    canvas = new_canvas(context.width, context.height)
    draw = ImageDraw.Draw(canvas)
    fonts = context.fonts
    tiny = fonts.get("tiny")

    if len(games) == 1:
        return _render_upcoming_single(canvas, draw, context, games[0])

    panels = max(1, context.width // PANEL_WIDTH)
    small = fonts.get("small")
    for index in range(panels):
        x0 = index * PANEL_WIDTH
        if index:
            _vline(draw, x0 - 1, 2, context.height - 3, FAINT)
        if index >= len(games):
            continue
        game = games[index]
        inner_left, inner_right = x0 + 2, x0 + PANEL_WIDTH - 3
        for row, team in ((2, game.away), (11, game.home)):
            logo_drawn = (
                paste_logo(canvas, context, team, (inner_left, row, inner_left + 7, row + 7))
                if context.show_logos else False
            )
            text_x = inner_left + (9 if logo_drawn else 3)
            if not logo_drawn:
                _vline(draw, inner_left, row, row + 7, readable(team.color))
            record = team.record or ""
            record_width = tiny.text_width(record) if record else 0
            label_width = inner_right - text_x - record_width - 2
            small.draw(draw, (text_x, row), small.fit(team.label, max(4, label_width)), BRIGHT)
            if record and record_width <= inner_right - text_x - small.text_width(team.label) - 3:
                tiny.draw_right(draw, inner_right, row + 1, record, DIM)
        _hline(draw, inner_left, inner_right, 21, FAINT)
        start = format_start(game, context.now)
        tiny.draw(draw, (inner_left, 24), tiny.fit(start, PANEL_WIDTH - 6), UPCOMING)
        if game.broadcast:
            broadcast_width = inner_right - inner_left - tiny.text_width(start) - 3
            if tiny.text_width(game.broadcast) <= broadcast_width:
                tiny.draw_right(draw, inner_right, 24, game.broadcast, ACCENT)
    _draw_stale_marker(draw, context)
    return canvas


def _render_upcoming_single(
    canvas: Image.Image, draw: ImageDraw.ImageDraw, context: RenderContext, game: Game
) -> Image.Image:
    fonts = context.fonts
    tiny, small = fonts.get("tiny"), fonts.get("small")
    center_x = context.width // 2

    tiny.draw_centered(draw, center_x, 0, "NEXT UP", UPCOMING)
    _draw_featured_side(canvas, draw, context, game.away, (0, PANEL_WIDTH - 1), align_left=True)
    _draw_featured_side(canvas, draw, context, game.home,
                        (context.width - PANEL_WIDTH, context.width - 1), align_left=False)

    start = format_clock_only(game)
    date_text = local_time(game.start_time).strftime("%a %b %-d").upper()
    start_font = fit_font(fonts, ("large", "medium", "small"), start, 60)
    start_font.draw_centered(draw, center_x, 7, start, BRIGHT)
    small.draw_centered(draw, center_x, start_font.height + 10, small.fit(date_text, 62), DIM)
    _draw_stale_marker(draw, context)
    return canvas


# -- layout D: finals ------------------------------------------------------

def render_finals(games: Sequence[Game], context: RenderContext) -> Image.Image:
    """Recently completed games, with the winner marked."""
    canvas = new_canvas(context.width, context.height)
    draw = ImageDraw.Draw(canvas)
    fonts = context.fonts
    tiny, small = fonts.get("tiny"), fonts.get("small")

    if len(games) == 1:
        game = games[0]
        _draw_featured_side(canvas, draw, context, game.away, (0, PANEL_WIDTH - 1), align_left=True)
        _draw_featured_side(canvas, draw, context, game.home,
                            (context.width - PANEL_WIDTH, context.width - 1), align_left=False)
        center_x = context.width // 2
        label = final_text(game)
        label_font = fit_font(fonts, ("medium", "small"), label, 62)
        label_font.draw_centered(draw, center_x, 3, label, FINAL)
        _hline(draw, center_x - 16, center_x + 16, 3 + label_font.height + 2, FINAL)
        winner = game.home if game.home.winner else (game.away if game.away.winner else None)
        if winner is not None:
            small.draw_centered(draw, center_x, 16, small.fit(f"{winner.label} WIN", 62), BRIGHT)
        detail = local_time(game.start_time).strftime("%a %b %-d").upper()
        tiny.draw_centered(draw, center_x, 26, tiny.fit(detail, 62), DIM)
        _draw_stale_marker(draw, context)
        return canvas

    panels = max(1, context.width // PANEL_WIDTH)
    for index in range(panels):
        x0 = index * PANEL_WIDTH
        if index:
            _vline(draw, x0 - 1, 2, context.height - 3, FAINT)
        if index >= len(games):
            continue
        game = games[index]
        inner_left, inner_right = x0 + 2, x0 + PANEL_WIDTH - 3
        header = "F/OT" if final_text(game) != "FINAL" else "FINAL"
        tiny.draw(draw, (inner_left, 0), header, FINAL)
        date_text = local_time(game.start_time).strftime("%a").upper()
        tiny.draw_right(draw, inner_right, 0, date_text, DIM)
        _hline(draw, inner_left, inner_right, 7, FAINT)
        for row, team in ((10, game.away), (20, game.home)):
            if team.winner:
                draw.rectangle([(x0, row), (x0 + 1, row + 7)], fill=ACCENT)
            logo_drawn = paste_logo(canvas, context, team, (inner_left, row, inner_left + 7,
                                                            row + 7)) if context.show_logos else False
            text_x = inner_left + (9 if logo_drawn else 0)
            color = BRIGHT if team.winner else DIM
            small.draw(draw, (text_x, row), small.fit(team.label, inner_right - text_x - 18), color)
            small.draw_right(draw, inner_right, row, team.score_text, color)
    _draw_stale_marker(draw, context)
    return canvas


# -- layout E: idle --------------------------------------------------------

def render_idle(
    context: RenderContext,
    next_game: Optional[Game] = None,
    headline: Optional[str] = None,
    online: bool = True,
) -> Image.Image:
    """Clock, date, and the next favourite-team game when there is one."""
    canvas = new_canvas(context.width, context.height)
    draw = ImageDraw.Draw(canvas)
    fonts = context.fonts
    tiny, small = fonts.get("tiny"), fonts.get("small")
    now = context.clock_now

    split = 86
    clock_text = now.strftime("%-I:%M")
    clock_font = fit_font(fonts, ("score", "large", "medium"), clock_text, split - 8)
    clock_font.draw_centered(draw, split // 2, 4, clock_text, BRIGHT)
    suffix = now.strftime("%p")
    tiny.draw(draw, (split - 14, 6), suffix, DIM)
    date_text = now.strftime("%a %b %-d").upper()
    small.draw_centered(draw, split // 2, 22, small.fit(date_text, split - 4), DIM)

    _vline(draw, split + 2, 3, context.height - 4, FAINT)
    right_left, right_right = split + 8, context.width - 3

    if next_game is not None:
        tiny.draw(draw, (right_left, 1), "NEXT UP", UPCOMING)
        matchup = f"{next_game.away.label} @ {next_game.home.label}"
        matchup_font = fit_font(fonts, ("medium", "small", "tiny"), matchup,
                                right_right - right_left)
        matchup_font.draw(draw, (right_left, 10),
                          matchup_font.fit(matchup, right_right - right_left), BRIGHT)
        start = local_time(next_game.start_time)
        when = f"{start.strftime('%a %b %-d').upper()} {format_clock_only(next_game)}"
        when_font = fit_font(fonts, ("small", "tiny"), when, right_right - right_left)
        when_font.draw(draw, (right_left, 22), when_font.fit(when, right_right - right_left), DIM)
    else:
        title = headline or "SCOREBOARD"
        title_font = fit_font(fonts, ("medium", "small"), title, right_right - right_left)
        title_font.draw(draw, (right_left, 6), title_font.fit(title, right_right - right_left),
                        BRIGHT)
        status = "NO GAMES TODAY" if online else "OFFLINE - CACHED"
        small.draw(draw, (right_left, 20), small.fit(status, right_right - right_left),
                   DIM if online else STALE)

    _draw_stale_marker(draw, context)
    return canvas


def render_message(context: RenderContext, title: str, detail: str = "") -> Image.Image:
    """A plain two-line message (startup, fatal errors, test screens)."""
    canvas = new_canvas(context.width, context.height)
    draw = ImageDraw.Draw(canvas)
    fonts = context.fonts
    center_x = context.width // 2
    title_font = fit_font(fonts, ("score", "large", "medium"), title, context.width - 8)
    title_font.draw_centered(draw, center_x, 4, title_font.fit(title, context.width - 8), BRIGHT)
    if detail:
        small = fonts.get("small")
        small.draw_centered(draw, center_x, 22, small.fit(detail, context.width - 8), DIM)
    _draw_stale_marker(draw, context)
    return canvas


def render_test_pattern(context: RenderContext) -> Image.Image:
    """Panel alignment aid: per-panel borders, numbers and a colour ramp."""
    canvas = new_canvas(context.width, context.height)
    draw = ImageDraw.Draw(canvas)
    fonts = context.fonts
    panels = max(1, context.width // PANEL_WIDTH)
    colors = [(255, 0, 0), (0, 255, 0), (0, 90, 255)]
    for index in range(panels):
        x0 = index * PANEL_WIDTH
        x1 = x0 + PANEL_WIDTH - 1
        color = colors[index % len(colors)]
        draw.rectangle([(x0, 0), (x1, context.height - 1)], outline=color)
        label = str(index + 1)
        font = fonts.get("score")
        font.draw_centered(draw, (x0 + x1) // 2, 6, label, WHITE)
        for step in range(8):
            level = 32 * step + 31
            draw.rectangle(
                [(x0 + 4 + step * 7, 24), (x0 + 9 + step * 7, 28)],
                fill=(level, level, level),
            )
    return canvas


def _draw_stale_marker(draw: ImageDraw.ImageDraw, context: RenderContext) -> None:
    """A small amber corner tick meaning 'this data is not fresh'."""
    if not context.stale:
        return
    x = context.width - 1
    draw.point([(x, 0), (x - 1, 0), (x, 1)], fill=STALE)


# -- dispatch --------------------------------------------------------------

def render_screen(
    screen: ScreenItem,
    context: RenderContext,
    next_game: Optional[Game] = None,
    online: bool = True,
) -> Image.Image:
    """Render any screen item, falling back to a message frame on error."""
    try:
        if screen.layout is Layout.FEATURED and screen.games:
            return render_featured(screen.games[0], context)
        if screen.layout is Layout.CARDS and screen.games:
            return render_cards(screen.games, context)
        if screen.layout is Layout.UPCOMING and screen.games:
            return render_upcoming(screen.games, context)
        if screen.layout is Layout.FINAL and screen.games:
            return render_finals(screen.games, context)
        return render_idle(context, next_game=next_game, online=online)
    except Exception:
        log.exception("Layout %s failed; showing fallback frame", screen.layout)
        return render_message(context, "SCOREBOARD", "layout error - see logs")
