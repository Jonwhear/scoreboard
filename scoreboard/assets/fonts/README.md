# Bundled fonts

Drop `.bdf` bitmap fonts here and they are picked up automatically, ahead of
every other source.

The application prefers, in order:

1. `.bdf` files in this directory
2. `.bdf` files from `~/rpi-rgb-led-matrix/fonts` (and a few other standard
   locations)
3. a system TrueType face, rendered with anti-aliasing disabled
4. Pillow's built-in font

The BDF fonts shipped with rpi-rgb-led-matrix were designed for exactly this
kind of panel and look markedly better than a scaled-down TrueType face, so on
the Pi option 2 is normally what you get — nothing needs to be copied here.

Useful sizes, by the role the layouts ask for:

| Role | Preferred files |
| --- | --- |
| `tiny` | `4x6.bdf`, `5x7.bdf` |
| `small` | `5x7.bdf`, `5x8.bdf`, `6x9.bdf` |
| `medium` | `6x10.bdf`, `6x9.bdf`, `6x12.bdf` |
| `large` | `7x13B.bdf`, `7x13.bdf`, `6x13B.bdf` |
| `score` | `9x18B.bdf`, `10x20.bdf`, `9x15B.bdf` |

Converted fonts are cached under `var/fonts/`. The web UI's status panel shows
which concrete file each role resolved to.

You can also point the search elsewhere:

```bash
SCOREBOARD_BDF_FONTS=/path/to/fonts python -m scoreboard.app --preview
```
