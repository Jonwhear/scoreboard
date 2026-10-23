# Configuration

`config.json` is written and rewritten by the application and by the web UI.
It is **not** in version control — `config.example.json` is the template.

* The app creates `config.json` from built-in defaults on first start.
* It carries a schema `version`. An older file is **migrated** on load (missing
  keys take their defaults) and rewritten; a file from a newer version than the
  code is read as best it can be rather than discarded. Upgrading — or rolling
  back — never costs you your favourites, which are stored as league id + team
  id pairs rather than by name.
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
| | `show_logos` | Draw team logos, or fall back to abbreviations. Very dark logos are brightened automatically so they read on an unlit panel. |
| | `show_refresh_rate` | Overlay the achieved refresh rate. Diagnostics only. |
| | `force_safe_blit` | Skip the library's fast `SetImage` path. Only needed on a 32-bit build that raises `OverflowError`; the app detects that case and falls back by itself. |
| | `run_as_user` | Account to drop to after the matrix is initialized. Empty = auto-detect. |
| `sports` | `enabled_leagues` | Canonical ids: `nfl`, `ncaaf`, `mlb`, `nhl`, `nba`, `ncaam`, `mls`, `epl`, `efl_championship`. |
| | `favorite_teams` | `{league, team_id, abbreviation, display_name}`. Set these from the web UI. |
| `rotation` | `screen_seconds` | How long each screen is shown. |
| | `favorites_only` | Show nothing but favourite-team games. |
| | `show_nonfavorite_live` / `show_upcoming` / `show_recent_finals` | Which categories are eligible. |
| | `layout_mode` | `auto`, `featured` (one game full width) or `cards` (three games). |
| | `final_window_hours` / `upcoming_window_hours` | How far back/forward games stay eligible. |
| | `max_screens` | Cap on rotation length, so a busy Saturday still cycles promptly. |
| | `locked_game` | A game key (`nfl:401671789`). While set, that one game is the only screen — regardless of `enabled_leagues` or `favorites_only`. Empty = normal rotation. Set it from **Follow one game** in the web UI. |
| | `lock_release_minutes` | Drop the lock this many minutes after the locked game finishes. 0–1440, default 30. `0` = keep it until you clear it. |
| | `show_standings` | Include league tables in the rotation. |
| | `standings_leagues` | Which leagues' tables to show. Empty = the leagues in `enabled_leagues`. |
| | `standings_seconds` | How long each table is held, 5–120 (default 20). A long table scrolls at one steady speed across exactly this long; a short one (an NFL division) is shown whole. |
| | `show_schedule` | A screen per favourite team listing its next games. Default on. |
| | `schedule_games` | How many games that screen lists, 1–10 (default 4). More than four scroll. |
| `sleep` | `enabled`, `start`, `end` | Blank the panels overnight. The window may cross midnight. |
| `polling` | `favorite_live_seconds` | Poll interval when a favourite is playing. |
| | `live_seconds` | Poll interval when any game is live or about to start. |
| | `idle_seconds` | Poll interval when nothing is on. |
| | `min_interval_seconds` | Hard floor between two requests for the same league. |
| | `http_timeout_seconds`, `http_retries` | HTTP behaviour. |
| `web` | `host`, `port` | Bind address for the UI. `0.0.0.0:8080` serves the LAN. |

Out-of-range values are clamped and unknown keys are ignored; the corrected
value is written back on the next save.
