#!/usr/bin/env python3
"""Find matrix settings that do not flicker, without editing config.json.

Chaining more panels lowers the achieved refresh rate: the library clocks
out three times as much data per frame for three panels as for one. Below
roughly 100 Hz the panels visibly shimmer. This script drives the real
matrix with settings you pass on the command line and asks the library to
print the refresh rate it is actually achieving, so you can tune by
measurement instead of by guesswork.

    sudo .venv/bin/python scripts/tune-matrix.py                 # current config
    sudo .venv/bin/python scripts/tune-matrix.py --pwm-bits 8    # try a change
    sudo .venv/bin/python scripts/tune-matrix.py --pwm-bits 8 --save

Useful knobs, roughly in order of how much they help:

  --pwm-bits N          fewer bits = much higher refresh, fewer colour steps.
                        11 is the default; 8 is usually indistinguishable on
                        scoreboard content and refreshes ~8x faster.
  --hardware-pulsing    far steadier timing, but it conflicts with the
                        onboard sound module -- blacklist that first
                        (see README, "Flicker").
  --slowdown N          GPIO write speed. Higher is safer on long chains,
                        lower is faster. Try 3, 4 and 5.
  --pwm-lsb-ns N        lower raises refresh rate at some colour cost.
  --brightness N        also the quickest way to tell flicker caused by an
                        inadequate 5V supply from flicker caused by timing.

Patterns: `pattern` (default, low current), `white` (worst case for the
power supply), `grey`, `bars`.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw  # noqa: E402

from scoreboard import paths  # noqa: E402
from scoreboard.config import ConfigStore, DisplayConfig  # noqa: E402
from scoreboard.display.fonts import FontRegistry  # noqa: E402
from scoreboard.display.layouts import RenderContext, render_test_pattern  # noqa: E402


def build_image(name: str, width: int, height: int, context) -> Image.Image:
    """Test images, from lowest to highest current draw."""
    if name == "white":
        return Image.new("RGB", (width, height), (255, 255, 255))
    if name == "grey":
        return Image.new("RGB", (width, height), (128, 128, 128))
    if name == "bars":
        image = Image.new("RGB", (width, height))
        draw = ImageDraw.Draw(image)
        colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0),
                  (0, 255, 255), (255, 0, 255), (255, 255, 255), (60, 60, 60)]
        band = max(1, width // len(colors))
        for index, color in enumerate(colors):
            draw.rectangle([(index * band, 0), ((index + 1) * band - 1, height - 1)],
                           fill=color)
        return image
    return render_test_pattern(context)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pattern", default="pattern",
                        choices=["pattern", "white", "grey", "bars"])
    parser.add_argument("--seconds", type=float, default=0,
                        help="0 (default) means run until Ctrl-C")
    parser.add_argument("--pwm-bits", type=int)
    parser.add_argument("--pwm-lsb-ns", type=int)
    parser.add_argument("--slowdown", type=int)
    parser.add_argument("--brightness", type=int)
    parser.add_argument("--limit-refresh", type=int, metavar="HZ")
    parser.add_argument("--chain", type=int, help="panels in the chain")
    pulse = parser.add_mutually_exclusive_group()
    pulse.add_argument("--hardware-pulsing", dest="hardware_pulsing",
                       action="store_true", default=None)
    pulse.add_argument("--no-hardware-pulsing", dest="hardware_pulsing",
                       action="store_false")
    parser.add_argument("--quiet", action="store_true",
                        help="do not print the refresh rate")
    parser.add_argument("--force-safe-blit", action="store_true",
                        help="skip the bindings' fast image path and use the "
                             "per-pixel one, to test it in isolation")
    parser.add_argument("--reset-timing", action="store_true",
                        help="put every timing knob back to its default -- the "
                             "state before any tuning -- keeping panel geometry. "
                             "Use this when tuning has made things worse.")
    parser.add_argument("--save", action="store_true",
                        help="write these settings into config.json and exit")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    paths.ensure_directories()
    store = ConfigStore(args.config or paths.config_path())
    config = store.load()
    display_config = config.display

    overrides = {}
    if args.reset_timing:
        # Everything a tuning session can change, back to the shipped
        # defaults. Panel geometry, gpio mapping and run_as_user are left
        # alone: those describe the hardware, not the tuning.
        defaults = DisplayConfig()
        for attribute in ("pwm_bits", "pwm_lsb_nanoseconds", "slowdown_gpio",
                          "limit_refresh_rate_hz", "disable_hardware_pulsing",
                          "show_refresh_rate", "force_safe_blit"):
            overrides[attribute] = getattr(defaults, attribute)

    for attribute, value in (
        ("pwm_bits", args.pwm_bits),
        ("pwm_lsb_nanoseconds", args.pwm_lsb_ns),
        ("slowdown_gpio", args.slowdown),
        ("brightness", args.brightness),
        ("limit_refresh_rate_hz", args.limit_refresh),
        ("chain_length", args.chain),
    ):
        if value is not None:
            overrides[attribute] = value
    if args.hardware_pulsing is not None:
        overrides["disable_hardware_pulsing"] = not args.hardware_pulsing

    if args.save:
        if not overrides:
            print("Nothing to save: pass some settings alongside --save.")
            return 2
        store.update({"display": overrides})
        print(f"Saved to {store.path}:")
        for attribute, value in sorted(overrides.items()):
            print(f"  {attribute} = {value}")
        print("Restart the scoreboard for these to take effect:")
        print("  sudo systemctl restart sports-scoreboard")
        return 0

    # Apply the overrides only for this run, after the save branch, so
    # --save writes exactly what was asked for and nothing else.
    for attribute, value in overrides.items():
        setattr(display_config, attribute, value)
    display_config.show_refresh_rate = not args.quiet
    if args.force_safe_blit:
        display_config.force_safe_blit = True

    from scoreboard.display.matrix import MatrixDisplay, MatrixUnavailableError

    display = MatrixDisplay(display_config)
    try:
        display.start()
    except MatrixUnavailableError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    fonts = FontRegistry(cache_dir=paths.FONT_CACHE_DIR)
    context = RenderContext(fonts=fonts, logos=None, show_logos=False,
                            width=display_config.width, height=display_config.height)
    image = build_image(args.pattern, display_config.width, display_config.height, context)

    print()
    print(f"  pattern      : {args.pattern}")
    print(f"  chain        : {display_config.chain_length} x "
          f"{display_config.cols}x{display_config.rows}  "
          f"({display_config.width}x{display_config.height} canvas)")
    print(f"  pwm_bits     : {display_config.pwm_bits}")
    print(f"  pwm_lsb_ns   : {display_config.pwm_lsb_nanoseconds}")
    print(f"  slowdown     : {display_config.slowdown_gpio}")
    print(f"  brightness   : {display_config.brightness}")
    print(f"  hw pulsing   : {'on' if not display_config.disable_hardware_pulsing else 'off'}")
    print(f"  limit refresh: {display_config.limit_refresh_rate_hz or 'unlimited'}")
    print(f"  blit path    : {'per-pixel (forced)' if args.force_safe_blit else 'auto'}")
    print()
    if not args.quiet:
        print("  The library prints the achieved refresh rate below. Aim for 100 Hz+;")
        print("  under about 100 Hz you will see flicker, especially out of the")
        print("  corner of your eye or through a phone camera.")
        print()
    print("  Ctrl-C to stop.")
    print()

    display.show(image)
    try:
        if args.seconds:
            time.sleep(args.seconds)
        else:
            while True:
                time.sleep(1.0)
    except KeyboardInterrupt:
        print()
    finally:
        display.stop()

    if overrides:
        flags = " ".join(
            f"--{key.replace('_', '-')} {value}" for key, value in sorted(overrides.items()))
        print("If that looked good, save it with:")
        print(f"  sudo {sys.argv[0]} {flags} --save")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
