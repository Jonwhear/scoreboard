# Configuration

`config.json` is written and rewritten by the application and by the web UI.
It is **not** in version control — `config.example.json` is the template.

* The app creates `config.json` from built-in defaults on first start.
* Every save is atomic (temp file + `os.replace`) and the previous version is
  kept as `config.json.bak`.
* If `config.json` is unreadable at startup it is moved aside as
  `config.json.corrupt-<timestamp>` (never deleted), the `.bak` is tried next,
  and built-in defaults are used as a last resort. The error is logged.

## Fields

| Section | Key | Meaning |
| --- | --- | --- |
| `display` | `rows`, `cols` | Geometry of **one** panel (64×32 here). |
| | `chain_length` | Panels wired in series. 2 → a 128×32 canvas, 3 → 192×32. Layouts and rotation both follow it. |
| | `parallel` | Parallel chains. 1 for the Adafruit bonnet. |
| | `gpio_mapping` | `adafruit-hat` for the bonnet (`adafruit-hat-pwm` if you soldered the E/4 jumper). |
| | `slowdown_gpio` | Higher = slower GPIO writes. 4 is right for a Pi 4. |
| | `brightness` | 1–100. Also a slider in the web UI. |
| | `pwm_bits`, `pwm_lsb_nanoseconds` | Colour depth / refresh trade-off. Lower `pwm_bits` if the panels flicker. |
| | `limit_refresh_rate_hz` | 0 = unlimited. Set ~100 for a steadier current draw. |
| | `disable_hardware_pulsing` | Keep `true` unless you have blacklisted the onboard sound module. |
| | `show_logos` | Draw team logos, or fall back to abbreviations. |
| | `run_as_user` | Account to drop to after the matrix is initialized. Empty = auto-detect. |
| `sports` | `enabled_leagues` | Canonical ids: `nfl`, `ncaaf`, `mlb`, `nhl`, `nba`, `ncaam`, `mls`, `epl`, `efl_championship`. |
| | `favorite_teams` | `{league, team_id, abbreviation, display_name}`. Set these from the web UI. |
| `rotation` | `screen_seconds` | How long each screen is shown. |
| | `favorites_only` | Show nothing but favourite-team games. |
| | `show_nonfavorite_live` / `show_upcoming` / `show_recent_finals` | Which categories are eligible. |
| | `layout_mode` | `auto`, `featured` (one game full width) or `cards` (three games). |
| | `final_window_hours` / `upcoming_window_hours` | How far back/forward games stay eligible. |
| | `max_screens` | Cap on rotation length, so a busy Saturday still cycles promptly. |
| `sleep` | `enabled`, `start`, `end` | Blank the panels overnight. The window may cross midnight. |
| `polling` | `favorite_live_seconds` | Poll interval when a favourite is playing. |
| | `live_seconds` | Poll interval when any game is live or about to start. |
| | `idle_seconds` | Poll interval when nothing is on. |
| | `min_interval_seconds` | Hard floor between two requests for the same league. |
| | `http_timeout_seconds`, `http_retries` | HTTP behaviour. |
| `web` | `host`, `port` | Bind address for the UI. `0.0.0.0:8080` serves the LAN. |

Out-of-range values are clamped and unknown keys are ignored; the corrected
value is written back on the next save.
