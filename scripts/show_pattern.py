#!/usr/bin/env python3
"""Put the panel alignment pattern on the real matrix for a few seconds.

Used by ``scripts/test-matrix.sh``; needs root, like anything that drives
the panels.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scoreboard import paths  # noqa: E402
from scoreboard.config import ConfigStore  # noqa: E402
from scoreboard.display.fonts import FontRegistry  # noqa: E402
from scoreboard.display.layouts import RenderContext, render_test_pattern  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    paths.ensure_directories()
    config = ConfigStore(args.config or paths.config_path()).load()

    from scoreboard.display.matrix import MatrixDisplay, MatrixUnavailableError

    display = MatrixDisplay(config.display)
    try:
        display.start()
    except MatrixUnavailableError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    fonts = FontRegistry(cache_dir=paths.FONT_CACHE_DIR)
    image = render_test_pattern(
        RenderContext(fonts=fonts, logos=None, show_logos=False,
                      width=config.display.width, height=config.display.height)
    )
    display.show(image)
    print(f"Pattern displayed on a {config.display.width}x{config.display.height} canvas "
          f"({config.display.chain_length} x {config.display.cols}x{config.display.rows}). "
          f"Ctrl-C to stop early.")
    try:
        time.sleep(args.seconds)
    except KeyboardInterrupt:
        pass
    finally:
        display.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
