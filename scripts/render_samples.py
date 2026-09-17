#!/usr/bin/env python3
"""Render one PNG per layout so they can be eyeballed without hardware.

    python scripts/render_samples.py [output-dir] [--scale N]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scoreboard import paths, samples  # noqa: E402
from scoreboard.display import layouts  # noqa: E402
from scoreboard.display.fonts import FontRegistry  # noqa: E402
from scoreboard.display.preview import scale_nearest  # noqa: E402
from scoreboard.logos import LogoCache  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", default="preview-samples")
    parser.add_argument("--scale", type=int, default=4)
    parser.add_argument("--no-logos", action="store_true")
    parser.add_argument("--chain", type=int, default=3,
                        help="panels in the chain (canvas is 64*chain wide)")
    parser.add_argument("--cache-dir", default=paths.VAR_DIR,
                        help="where converted fonts and cached logos live")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    os.makedirs(args.output, exist_ok=True)

    fonts = FontRegistry(cache_dir=os.path.join(args.cache_dir, "fonts"))
    logos = None if args.no_logos else LogoCache(os.path.join(args.cache_dir, "logos"))
    if logos:
        logos.start()
    context = layouts.RenderContext(fonts=fonts, logos=logos, show_logos=not args.no_logos,
                                    width=64 * args.chain, height=32)

    frames = {
        "a-featured-live": lambda: layouts.render_featured(samples.sample_live_game(), context),
        "a-featured-final": lambda: layouts.render_featured(samples.sample_final_games()[0], context),
        "a-featured-upcoming": lambda: layouts.render_featured(samples.sample_upcoming_games()[0], context),
        "b-cards": lambda: layouts.render_cards(samples.sample_card_games(), context),
        "c-upcoming": lambda: layouts.render_upcoming(samples.sample_upcoming_games(), context),
        "c-upcoming-single": lambda: layouts.render_upcoming(samples.sample_upcoming_games()[:1], context),
        "d-finals": lambda: layouts.render_finals(samples.sample_final_games(), context),
        "d-final-single": lambda: layouts.render_finals(samples.sample_final_games()[:1], context),
        "e-idle": lambda: layouts.render_idle(context),
        "e-idle-next": lambda: layouts.render_idle(context, next_game=samples.sample_upcoming_games()[0]),
        "e-idle-offline": lambda: layouts.render_idle(context, online=False),
        "f-test-pattern": lambda: layouts.render_test_pattern(context),
    }

    for name, build in frames.items():
        image = build()
        expected = (64 * args.chain, 32)
        assert image.size == expected, f"{name} rendered {image.size}, expected {expected}"
        path = os.path.join(args.output, f"{name}.png")
        scale_nearest(image, args.scale).save(path)
        print(f"{path}  ({image.size[0]}x{image.size[1]} logical)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
