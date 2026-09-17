# Sports Scoreboard

A local, self-contained sports scoreboard for a Raspberry Pi driving three
chained 64×32 HUB75 LED matrices (a 128×32 canvas) through an Adafruit RGB
Matrix Bonnet. The chain length is configuration, not code — one, two, three
or more panels all work, and every layout adapts to the canvas it is given.

It fetches live scores, prioritises your favourite teams, draws them on the
panels, and is configured from a phone or laptop on your own Wi-Fi. No cloud
account, no API key, nothing exposed to the internet.

Two panels — the default:

```
┌───────────────────────────────────────┐
│              Q3   4:23                │
├───────────────────┬───────────────────┤
│       MIN         │        GB         │
│ [logo]       24   │   17      [logo]  │
└───────────────────┴───────────────────┘
       panel 1             panel 2
```

Three panels, if your 5 V supply can feed them (see the power warning) — the
status gets a column of its own:

```
┌─────────────────┬─────────────────┬─────────────────┐
│  MIN            │      LIVE       │            GB   │
│ [logo]      24  │       Q3        │  17     [logo]  │
│                 │      4:23       │                 │
└─────────────────┴─────────────────┴─────────────────┘
        panel 1           panel 2           panel 3
```

---

## Contents

- [What it does](#what-it-does)
- [Hardware assumptions](#hardware-assumptions)
- [⚡ Power warning — read this first](#-power-warning--read-this-first)
- [Installation](#installation)
- [Running it](#running-it)
- [The web UI](#the-web-ui)
- [Configuration](#configuration)
- [systemd service](#systemd-service)
- [Logs](#logs)
- [Tests](#tests)
- [Architecture](#architecture)
- [Root, GPIO and privilege dropping](#root-gpio-and-privilege-dropping)
- [Matrix arguments](#matrix-arguments)
- [Troubleshooting](#troubleshooting)
- [The ESPN caveat](#the-espn-caveat)
- [Project structure](#project-structure)
- [Possible future work](#possible-future-work)

---

## What it does

* Polls ESPN's public JSON endpoints for NFL, NCAA football, MLB, NHL, NBA,
  NCAA men's basketball, MLS, the Premier League and the EFL Championship.
* Normalises everything into one internal `Game` model — no provider-specific
  JSON leaks past `scoreboard/providers/espn.py`.
* Prioritises favourite teams, then other live games, then upcoming, then
  recent finals, then an idle clock; rotates when several are eligible.
* Downloads and caches team logos once, processes them for the panel size,
  and falls back to team abbreviations when a logo is missing.
* Adapts its polling rate: ~15 s when a favourite is playing, ~25 s when any
  game is live, ~3 min when nothing is on.
* Keeps working offline: the last good data is shown with a small amber
  "stale" marker in the corner, and the clock keeps running.
* Serves a configuration UI with a **pixel-accurate preview of the exact
  framebuffer** the LEDs are receiving.
* Starts at boot under systemd and restarts itself after a crash.

---

## Hardware assumptions

| Item | Assumption |
| --- | --- |
| Computer | Raspberry Pi 4 |
| OS | Raspberry Pi OS Buster (Python 3.7) or newer, 32- or 64-bit |
| HAT | Adafruit RGB Matrix Bonnet (product 3211) |
| Panels | 2 × HUB75 64×32, chained horizontally (3 supported) |
| Logical canvas | 128 × 32 (64 × panels) |
| GPIO mapping | `adafruit-hat` |
| GPIO slowdown | `4` |
| Driver | [hzeller/rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix), already installed |

### Wiring

The panels are chained **output → input**: the bonnet's HUB75 socket goes to
panel 1's *input*, and panel 1's *output* to panel 2's *input* (and so on for
a longer chain). The library then treats the chain as one wide canvas.

This project configures the library as `cols=64, chain_length=2`. It is **not**
configured as a single 128-wide panel, because that is not what the hardware
is and the library addresses each panel separately.

To change the number of panels, set `display.chain_length` — every layout,
the rotation grouping and the web preview follow it:

```bash
sudo .venv/bin/python scripts/tune-matrix.py --chain 3 --save
sudo systemctl restart sports-scoreboard
```

---

## ⚡ Power warning — read this first

**Do not power the LED panels from the Raspberry Pi.**

A 64×32 HUB75 panel can draw around **2–4 A at 5 V** with a bright, full-white
image. Two of them is a realistic worst case of **4–8 A**, three is **6–12 A**.
The Pi's own supply cannot do this, and the Pi's 5 V rail is not designed to
pass it.

This is the usual reason a chain that works at two panels misbehaves at three:
the supply runs out before the software does.

* Use a dedicated, quality **5 V supply rated well above your worst case** —
  a 5 V / 10 A supply for two panels, or 15 A or larger for three, is sensible
  headroom.
* Feed power **to each panel's own screw terminals**, not by daisy-chaining
  thin power leads from panel to panel. Use proper distribution and adequate
  wire gauge; thin or long runs cause voltage drop.
* Keep the Pi on its own supply, and make sure the panel supply and the Pi
  **share a ground**.
* Lowering `brightness` in the web UI genuinely lowers current draw, and is
  the easiest way to reduce strain while you sort the supply out.

Symptoms of inadequate power are covered under
[Troubleshooting](#troubleshooting). **Power and wiring problems cannot be
fixed in software, and this project does not try to hide them.**

---

## Installation

### Deploying to the Pi, start to finish

Run these **as your normal user on the Pi** (the `pi` account, or whatever you
log in as). Do not `sudo git clone` — the installer works out which account to
drop privileges to from who owns the files, and a root-owned clone means the
service never gives up root.

```bash
# 1. Clone. Note the branch and the target directory name.
cd ~
git clone -b claude/pi-sports-scoreboard-mmuqbi \
    https://github.com/Jonwhear/scoreboard.git sports-scoreboard
cd ~/sports-scoreboard

# 2. Inspect the system, build the venv, install dependencies.
#    Read the report it prints -- it tells you whether the rgbmatrix
#    bindings and the BDF fonts were found.
./scripts/install.sh

# 3. Prove it runs before involving hardware or systemd.
.venv/bin/python -m pytest          # ~195 tests, offline, ~3 seconds
./scripts/run-dev.sh                # then open the web UI, Ctrl-C to stop

# 4. Prove the panels are wired and configured correctly.
./scripts/test-matrix.sh            # numbered borders, one per panel

# 5. Install the service so it starts at boot.
#    This prints exactly what it will change and asks first.
sudo ./scripts/install.sh --service

# 6. Confirm.
systemctl status sports-scoreboard
journalctl -u sports-scoreboard -f
```

Step 5 is the only step that changes anything outside `~/sports-scoreboard`,
and it is the step that makes it start on boot: it writes
`/etc/systemd/system/sports-scoreboard.service`, runs `systemctl
daemon-reload`, then `systemctl enable --now sports-scoreboard` (`enable` =
at boot, `--now` = also start it immediately).

**Sanity checks in the startup log.** `journalctl -u sports-scoreboard -n 40`
should show, in order:

```
Initializing matrix: 64x32 per panel, chain=2, ... -> 128x32 canvas
Matrix initialized (128x32)
Dropped privileges to pi (uid=1000 gid=1000)
Leagues enabled: nfl, mlb, nhl, nba | favourites: 0 | canvas 128x32 | backend matrix
Web UI on http://0.0.0.0:8080
```

If you see `backend preview` instead of `backend matrix`, the matrix could not
be initialized — the ERROR line just above it says why, and the web UI still
works so you can debug from your phone. If you see `Still running as root`,
the installation is owned by root; fix it with
`sudo chown -R $USER ~/sports-scoreboard` and restart the service.

### Updating later

```bash
cd ~/sports-scoreboard
git pull
.venv/bin/python -m pip install -q -r requirements.txt   # only if deps changed
sudo systemctl restart sports-scoreboard
```

Your `config/config.json`, cached logos and team lists are gitignored, so a
`git pull` never disturbs your settings.

### What the installer does

The installer is idempotent and **inspects before it acts**. It reports:

* Python version and whether `venv` is available
* operating system and Raspberry Pi model
* the `rpi-rgb-led-matrix` checkout, its `demo` binary and its BDF fonts
* whether the `rgbmatrix` Python bindings are importable
* whether the configured web port is already in use
* the account the service should run as

It then creates `.venv/`, installs the dependencies, links the existing
system-wide `rgbmatrix` bindings into the venv (it never reinstalls or
rebuilds them), and creates `config/config.json` if it does not exist.

**It does not touch your rpi-rgb-led-matrix installation.**

To also install the systemd unit — the only system-level change — run:

```bash
sudo ./scripts/install.sh --service
```

which prints exactly what it will change and asks for confirmation first.

### Older Raspberry Pi OS (Buster, Python 3.7)

Supported. The dependency ranges are deliberately wide, so pip installs the
newest release of each library that still supports the Python it finds:

| | Buster / Python 3.7 | Bookworm / Python 3.11 |
| --- | --- | --- |
| Flask | 2.2.5 | 3.x |
| Pillow | 9.5.0 | 11.x / 12.x |
| waitress | 2.1.2 | 3.x |
| pytest | 7.4 | 8.x / 9.x |

The test suite passes and the layouts render pixel-for-pixel identically on
both sets, so there is no reason to reinstall the OS just to run this — and
upgrading would mean rebuilding your working rpi-rgb-led-matrix.

Worth doing eventually, though: Buster stopped getting security updates in
2024, and a 64-bit Bookworm image is faster and gets prebuilt wheels for
everything. Just budget time to rebuild the matrix library afterwards.

### If `pip install` is slow or fails on Pillow

Raspberry Pi OS configures [piwheels](https://www.piwheels.org) out of the
box, which serves prebuilt ARM wheels — so Pillow normally installs in
seconds even on 32-bit. If piwheels has no wheel for your combination, pip
compiles from source instead, which takes several minutes and fails without
the build dependencies:

```bash
sudo apt install -y python3-dev libjpeg-dev zlib1g-dev libfreetype6-dev
./scripts/install.sh          # then re-run
```

Check which architecture you have with `uname -m`: `aarch64` is 64-bit,
`armv7l` is 32-bit.

### If the `rgbmatrix` bindings are missing

```bash
cd ~/rpi-rgb-led-matrix/bindings/python
sudo make install-python PYTHON=$(which python3)
```

Then re-run `./scripts/install.sh`.

---

## Running it

### Preview mode — no hardware, no root

```bash
./scripts/run-dev.sh
# or
.venv/bin/python -m scoreboard.app --preview
```

Renders to an in-memory framebuffer instead of LEDs. The web UI, the preview
image, the rotation engine and the data layer all behave exactly as they do on
the panels. This is the right way to develop layouts, and it runs on any
computer — a Mac or a laptop included.

### On the panels

```bash
sudo .venv/bin/python -m scoreboard.app
```

Root is needed only to initialise the matrix; the process drops privileges
immediately afterwards (see [below](#root-gpio-and-privilege-dropping)).

### Useful flags

| Flag | Effect |
| --- | --- |
| `--preview` | No GPIO, no root; render to images |
| `--config PATH` | Use a different `config.json` |
| `--host` / `--port` | Override the web UI bind address |
| `--no-web` | Run the display without the web UI |
| `--preview-out FILE.png` | Also write every frame to a PNG |
| `--log-level DEBUG` | More detail in the journal |
| `--strict-hardware` | Exit on matrix failure instead of falling back to preview |

### Render the layouts to PNGs

```bash
.venv/bin/python scripts/render_samples.py preview-samples --scale 6
```

Writes one PNG per layout (featured live / final / upcoming, three-game cards,
upcoming, finals, idle clock, panel test pattern) so you can check a layout
change without a Pi.

---

## The web UI

```
http://<pi-hostname>.local:8080
http://<pi-ip>:8080
```

Find the IP with `hostname -I`. The UI is responsive and built for a phone
first; it works the same on a desktop browser or tablet.

It shows:

* **Status** — running state, sports-data health, last successful update,
  which screen is on the panels right now, matrix configuration, uptime,
  which font file each text size resolved to, logo cache statistics
* **Preview** — the live framebuffer, upscaled with nearest-neighbour
  so individual LED pixels stay visible, refreshed once a second
* **Favourites** — searchable team picker per league, with logos; stored by
  stable league + team id, never by name
* **Leagues** — which leagues are polled
* **Display** — brightness, seconds per screen, layout mode, logo toggle,
  favourites-only, which categories appear, sleep schedule
* **Buttons** — force next screen, refresh data now, and test screens
  (clock, sample game, sample cards, panel test pattern)

**Changes take effect immediately.** Nothing here requires restarting the
service. Saves are atomic, so a power cut cannot easily corrupt the config.

### `scoreboard.local` instead of `<hostname>.local`

Raspberry Pi OS already runs Avahi, so `<hostname>.local` works out of the
box. If you want the name `scoreboard.local` specifically, the clean and
conventional way is an Avahi alias or simply renaming the host:

```bash
# Option A — rename the Pi (changes the hostname system-wide)
sudo raspi-config nohup do_hostname scoreboard   # then reboot

# Option B — publish an extra mDNS name, leaving the hostname alone
sudo apt install avahi-utils
# add a systemd unit that runs:
#   avahi-publish -a -R scoreboard.local $(hostname -I | awk '{print $1}')
```

**This project does not do either automatically** — both change how your Pi
appears on the network, so they are your call. Getting rid of the `:8080`
would additionally mean binding port 80, which needs either root (which we
deliberately drop) or a `CAP_NET_BIND_SERVICE` grant or a reverse proxy.

**Do not port-forward this to the internet.** It has no authentication by
design; it is a LAN appliance.

---

## Configuration

`config/config.json` — see [`config/README.md`](config/README.md) for the full
field reference. It is created from `config/config.example.json` (or from
built-in defaults) on first run, and is not in version control.

Safety properties:

* writes are atomic (temp file → `fsync` → `os.replace`)
* the previous version is kept as `config.json.bak`
* a corrupt file is moved aside as `config.json.corrupt-<timestamp>` — it is
  never deleted — the backup is tried next, then built-in defaults
* out-of-range values are clamped, unknown leagues dropped, unknown keys
  ignored; a bad value never prevents startup

---

## systemd service

```bash
sudo ./scripts/install.sh --service      # install + enable + start
```

Then, via the wrapper (or plain `systemctl`):

```bash
./scripts/service.sh start
./scripts/service.sh stop
./scripts/service.sh restart
./scripts/service.sh status
./scripts/service.sh enable      # start at boot
./scripts/service.sh disable
```

```bash
sudo systemctl start sports-scoreboard
sudo systemctl stop sports-scoreboard
sudo systemctl restart sports-scoreboard
systemctl status sports-scoreboard
sudo systemctl enable sports-scoreboard
```

The unit starts after `network-online.target`, restarts on failure after a
5 second delay (giving up after 5 failures in 5 minutes so a broken install
does not spin forever), stops cleanly on `SIGTERM`, runs from the project
directory using the project's virtualenv, and logs to the journal.

There is also `systemd/sports-scoreboard-preview.service`, a variant that runs
entirely unprivileged with `--preview` — handy when the panels are unplugged.

---

## Logs

```bash
journalctl -u sports-scoreboard -f          # follow
journalctl -u sports-scoreboard -n 200      # last 200 lines
journalctl -u sports-scoreboard -p warning  # warnings and errors only
./scripts/service.sh logs                   # same, shorter
```

What you should expect to see:

* **INFO** — startup, enabled leagues, each data refresh and its game count,
  every screen change, configuration updates, logos cached
* **WARNING** — stale/cached data in use, a logo that would not download,
  a malformed event that was skipped, a team catalog that could not refresh
* **ERROR** — matrix initialisation failure, a provider that is fully down,
  corrupt configuration

Nothing is logged per frame. A sustained outage logs **once**, not once per
poll, so the journal stays readable.

---

## Tests

```bash
.venv/bin/python -m pytest              # ~180 tests, about 2 seconds
.venv/bin/python -m pytest -v
```

The suite is fully offline — it uses checked-in ESPN payload fixtures in
`tests/fixtures/`, so it neither needs the internet nor cares whether anything
is in season. It covers ESPN normalisation (including malformed events),
favourite matching, priority ordering and rotation, config validation and
corruption recovery, layout rendering at several chain lengths, logo
processing and fallback, stale/offline behaviour, the web API, font
resolution (including the BDF conversion path), and the render loop.

---

## Architecture

Four participants, one shared state object, no thread ever waiting on another:

```
  ┌──────────────┐   polls ESPN      ┌──────────────┐
  │ DataScheduler│ ────────────────► │              │
  │   (thread)   │  Game objects     │   AppState   │
  └──────────────┘                   │  (locked)    │
                                     │              │
  ┌──────────────┐   reads state     │              │
  │ DisplayRunner│ ◄──────────────── │              │
  │   (thread)   │                   └──────────────┘
  │              │                          ▲
  │  rotation →  │  the frame               │ reads
  │  layouts  →  │ ───────► Display ──► LEDs│
  │              │ ───────► preview PNG     │
  └──────────────┘                   ┌──────────────┐
                                     │  Flask/web   │
  ┌──────────────┐   validated write │  (threads)   │
  │ ConfigStore  │ ◄──────────────── │              │
  │  (atomic)    │                   └──────────────┘
  └──────────────┘
```

* A hung ESPN request can only make the data stale — it cannot stall the
  matrix.
* A slow web request cannot affect the refresh loop.
* A logo that is not cached yet never blocks a frame: the renderer draws the
  abbreviation and a background worker fetches the artwork.

Layer boundaries that matter:

| Layer | Knows about | Never knows about |
| --- | --- | --- |
| `providers/espn.py` | ESPN's JSON and URLs | rendering, config, threads |
| `models.py` | normalised games | any provider |
| `rotation.py` | games, config | Pillow, rgbmatrix |
| `display/layouts.py` | a 64·N × 32 canvas | rgbmatrix, HTTP, ESPN |
| `display/matrix.py` | rgbmatrix | layouts, games |
| `web/` | config + state | how anything is drawn |

The `Display` abstraction is the key one. `MatrixDisplay` and `PreviewDisplay`
receive the *identical* rendered image, which is why the browser preview is
trustworthy and why the whole application runs on a laptop.

---

## Root, GPIO and privilege dropping

`rpi-rgb-led-matrix` needs root: it maps `/dev/mem` and asks for a real-time
thread. Nothing else in this project does.

**The decision:** the service starts as root, initialises the matrix, and then
**permanently drops to an unprivileged account** before starting the web
server, the HTTP client, or any file writes. From that point the process
cannot regain root.

The drop is done by `scoreboard/privileges.py` rather than by the library's
own `drop_privileges` option, because the library drops to the `daemon`
account — which cannot write `config/config.json` or the logo cache. We drop
to the account that owns the installation instead, so file ownership stays
sensible. The target is `display.run_as_user` in the config, falling back to
`$SUDO_USER` and then to the owner of the source tree.

Splitting the display and the web UI into two processes would be marginally
tighter, but it would mean adding IPC and a second unit to a private home
appliance whose web UI is already unprivileged and LAN-only. That is not a
trade worth making here.

In `--preview` mode none of this applies: no root is needed at all.

---

## Matrix arguments

The settings in `config.json` map onto the library's options like this:

| config.json | `demo` flag | Value here |
| --- | --- | --- |
| `rows` | `--led-rows` | 32 |
| `cols` | `--led-cols` | 64 |
| `chain_length` | `--led-chain` | 2 |
| `parallel` | `--led-parallel` | 1 |
| `gpio_mapping` | `--led-gpio-mapping` | `adafruit-hat` |
| `slowdown_gpio` | `--led-slowdown-gpio` | 4 |
| `brightness` | `--led-brightness` | 50 |
| `pwm_bits` | `--led-pwm-bits` | 11 |
| `pwm_lsb_nanoseconds` | `--led-pwm-lsb-nanoseconds` | 130 |
| `limit_refresh_rate_hz` | `--led-limit-refresh` | 0 (unlimited) |
| `disable_hardware_pulsing` | `--led-no-hardware-pulse` | true |

The equivalent known-good `demo` invocation for this chain:

```bash
cd ~/rpi-rgb-led-matrix/examples-api-use
sudo ./demo -D 0 \
  --led-rows=32 --led-cols=64 --led-chain=2 \
  --led-gpio-mapping=adafruit-hat --led-slowdown-gpio=4
```

`./scripts/test-matrix.sh demo` runs exactly that, using whatever values are
in your `config.json`.

---

## Troubleshooting

Start here:

```bash
./scripts/test-matrix.sh          # this project's panel test pattern
./scripts/test-matrix.sh demo     # hzeller's demo, same settings
./scripts/test-matrix.sh samples  # render layouts to PNGs, no hardware

# Chasing flicker: drive the panels with settings passed on the command
# line and print the refresh rate actually being achieved.
sudo .venv/bin/python scripts/tune-matrix.py --help
```

The test pattern draws a numbered, coloured border around each 64×32 panel
plus a grey ramp. **Use it to tell software problems from hardware problems.**

### Flicker, and why a third panel starts it

This deserves its own section because it is the most common surprise: **two
panels look fine, adding a third makes everything shimmer.**

The library clocks the whole chain out serially, so each panel you add is
another panel's worth of data per frame. The achieved refresh rate falls
accordingly, and below roughly 100 Hz your eye starts to see it — especially
in peripheral vision, or through a phone camera.

**First, decide whether it is timing or power.** These need completely
different fixes, and guessing wastes the evening. Start with:

```bash
./scripts/diagnose.sh
```

which checks whether the Pi itself is being under-volted or throttled, what
is competing for CPU, and which timing settings are actually in effect. Then
measure the two extremes of current draw:

```bash
sudo .venv/bin/python scripts/tune-matrix.py --pattern pattern   # low current
sudo .venv/bin/python scripts/tune-matrix.py --pattern white     # max current
```

The library prints the refresh rate it is achieving, as
`157.3Hz (lowest: 112.6Hz)`.

**Read the spread, not just the average.** This is the part that is easy to
get wrong. A rock-steady 120 Hz looks fine; a rate swinging between 112 and
157 Hz flickers badly, because the modulation period keeps changing. So:

| What you see | Cause | Go to |
| --- | --- | --- |
| Average below ~100 Hz | Timing — not enough refresh | steps 2 and 5 below |
| **Wide gap between the current rate and the `lowest` figure** (say 112 vs 157) | Timing — *jitter*, the frame period keeps moving | step 1 first, then 3 and 4 |
| Rate is high **and steady**, and it still flickers | **Power** | the hardware table further down |
| `white` far worse than `pattern`, or brightness 25 largely fixes it | **Power** | the hardware table further down |
| `./scripts/diagnose.sh` reports any under-voltage | **Power — the Pi's own supply** | give the Pi its own adequate supply |

Note the last one: an under-volted *Pi* throttles its CPU, which destabilises
the refresh thread. That shows up as jitter, so power and timing symptoms can
masquerade as each other. `vcgencmd get_throttled` returning `0x0` rules the
Pi's own supply out — but says nothing about the separate supply feeding the
panels, which still needs measuring at the far panel's terminals.

**Timing fixes, in descending order of how much they help.** Try them one at
a time with `tune-matrix.py`, then persist the winner with `--save`:

1. **Pin the refresh rate.** If the complaint is *jitter* (a wide gap between
   the current rate and the `lowest` figure) this is the first thing to try:
   it costs nothing, needs no reboot, and changes nothing about your system.
   The library then makes every frame take the same time instead of running
   as fast as it happens to manage, so the modulation period stops moving —
   which is the thing your eye is actually picking up.

   Pick a value just below the `lowest` figure you measured:
   ```bash
   # measured 157Hz swinging down to 112Hz -> pin it under the floor
   sudo .venv/bin/python scripts/tune-matrix.py --limit-refresh 100 --pattern white
   ```
   Then creep it up (105, 110) to find the highest rate that stays rock
   steady, and save it. A constant 100 Hz looks far better than anything
   oscillating between 112 and 157 Hz.

2. **Fewer PWM bits.** Raises the average — each bit you drop roughly doubles
   the refresh rate. Worth doing if the average is low, or to buy headroom so
   you can pin the rate higher in step 1. It does not by itself help jitter.
   Scoreboard content is flat colour, so the lost colour depth is essentially
   invisible:
   ```bash
   sudo .venv/bin/python scripts/tune-matrix.py --pwm-bits 8
   sudo .venv/bin/python scripts/tune-matrix.py --pwm-bits 7   # if still not enough
   ```

3. **Hardware pulsing.** The deepest fix for *jitter*: it moves the bit timing
   onto a hardware timer instead of a software loop the scheduler can
   interrupt. It shares hardware with the onboard sound, so that has to go
   first. **This changes your system:** it disables the Pi's analogue/HDMI
   audio.
   ```bash
   echo "blacklist snd_bcm2835" | sudo tee /etc/modprobe.d/blacklist-rgb-matrix.conf
   sudo update-initramfs -u
   sudo reboot
   # then:
   sudo .venv/bin/python scripts/tune-matrix.py --hardware-pulsing --pwm-bits 8
   ```

4. **Reserve a CPU core for the refresh thread.** The other structural fix for
   jitter, and the library suggests it itself at startup. **This changes your
   boot configuration:** append `isolcpus=3` to the single line in
   `/boot/cmdline.txt` (do not add a new line), then reboot.

   Related and free: a desktop session with a browser open steals a lot of CPU
   on a Pi 4. Close Chromium, or test over SSH with the desktop stopped
   (`sudo systemctl isolate multi-user.target`), before blaming the supply.

5. **GPIO slowdown.** Longer chains sometimes need a different value. It is
   cheap to try 3, 4 and 5 — lower is faster but less tolerant of long
   ribbons:
   ```bash
   sudo .venv/bin/python scripts/tune-matrix.py --slowdown 5 --pwm-bits 8
   ```

6. **Lower the brightness.** Beyond reducing current draw, it narrows the
   range the PWM has to cover, so a marginal supply sags less.

When a combination looks right:

```bash
sudo .venv/bin/python scripts/tune-matrix.py --pwm-bits 8 --slowdown 4 --save
sudo systemctl restart sports-scoreboard
```

**When the rate is flat and it still flickers, stop tuning.** A reading like
`100.0Hz (lowest: 100.0Hz)` means the timing is perfect and the remaining
fault is electrical. Keep the settings that got you there and go to the
hardware section — but first, narrow it down without a meter by walking the
chain back up:

```bash
sudo .venv/bin/python scripts/tune-matrix.py --chain 1 --pattern white
sudo .venv/bin/python scripts/tune-matrix.py --chain 2 --pattern white
sudo .venv/bin/python scripts/tune-matrix.py --chain 3 --pattern white
```

Full white is the maximum-current image, so each panel you add is another
~2-4 A. The point at which a rock-steady display starts to misbehave is your
supply's ceiling. Two corroborating checks:

```bash
# Same chain, a quarter of the current: if this is stable and white is not,
# the panels are drawing more than the supply can deliver.
sudo .venv/bin/python scripts/tune-matrix.py --chain 3 --brightness 20 --pattern white

# The low-current image at full brightness, for comparison.
sudo .venv/bin/python scripts/tune-matrix.py --chain 3 --pattern pattern
```

A word on photographing the panels: **don't trust the picture.** These
displays are multiplexed, lighting a couple of rows at a time, so a camera
often captures only the rows lit during its exposure and shows a few bright
bands on a dark panel. That is a photographic artifact, not a fault. Judge
brightness and steadiness with your eyes, and use the camera only to check
for flicker (a rolling dark band across the image in video).

### Software symptoms — configuration is wrong

These are wrong from the very first frame and are always wrong the same way.

| Symptom | Cause | Fix |
| --- | --- | --- |
| Only the first panel lights; the rest are dark | `chain_length` too low | Set it to your actual panel count |
| Image repeats on every panel | Configured as one wide panel | Use `cols: 64` plus `chain_length: N`, **not** `cols: 128` |
| Panel numbers appear out of order | Chain wired in a different order | Re-cable output→input, or reorder physically |
| Image split/interleaved vertically, garbled halves | Wrong `rows`, or a 1/8-scan panel | Check the panel's scan rate; `rows` must match |
| Nothing lights at all, no errors | Wrong `gpio_mapping` | `adafruit-hat`, or `adafruit-hat-pwm` **only** if you soldered the E/4 jumper |
| Steady flicker or shimmer everywhere | GPIO timing too fast, or hardware pulsing | Raise `slowdown_gpio` (4 → 5), keep `disable_hardware_pulsing: true` |
| Dim, washed-out colours | Low `pwm_bits` or low brightness | Raise `pwm_bits`, raise `brightness` |
| `MatrixUnavailableError` on start | Not root, or bindings missing | `sudo`, and build the Python bindings |

If `./scripts/test-matrix.sh demo` is *also* wrong in the same way, the problem
is not this application.

### Hardware symptoms — power, wiring, heat

These typically **look fine at first and degrade after seconds or minutes**,
or change when you touch a cable. Your reported "corrupted pixels after
running for a while" sits squarely in this category.

| Symptom | Likely cause |
| --- | --- |
| Random bright pixels appearing over time, worse when the image is bright | **Inadequate 5 V supply** — current sags as the panels warm up and draw more |
| Later panels in the chain dimmer or redder than the first | **Voltage drop** — power daisy-chained through thin wire; feed each panel its own pair |
| Whole panel flickers or blanks intermittently | Loose power screw terminal, or an under-crimped spade |
| One panel corrupts, the rest are fine | Loose/damaged HUB75 ribbon on that link; reseat or replace it |
| Corruption starts near a specific panel and spreads down-chain | Bad output connector on the panel before it |
| Gets worse as the session goes on, better after a cool-down | Overheating panel, or a supply drifting out of regulation under sustained load |
| Pi reboots, or you see under-voltage warnings | The panels are loading the Pi's rail — separate the supplies |

Checks, in order of how often they are the answer:

1. **Measure 5 V at the far panel's terminals while displaying full white.**
   Below about 4.7 V means the supply or wiring is inadequate. This is the
   single most informative measurement.
2. Turn `brightness` down to 25 in the web UI. If the corruption stops, it is
   power, not software.
3. Reseat every HUB75 ribbon and every power terminal.
4. Test each panel on its own with `chain_length: 1` to isolate a bad one.
5. Swap the ribbon between panel 1→2 and 2→3 and see whether the fault follows
   the cable.

**None of this is a software problem and it should not be papered over in
software.** The application logs matrix errors and keeps drawing; it will not
mask a failing supply.

### Application symptoms

| Symptom | Cause |
| --- | --- |
| Small amber mark in the top-right corner | Data is stale — ESPN is unreachable, cached data is on screen. Intentional. |
| Idle clock when games should be on | Check enabled leagues and the sleep schedule; check `journalctl` for fetch errors |
| Abbreviations instead of logos | Logos not downloaded yet, or `show_logos` is off, or the logo URL 404s. Harmless. |
| Web UI unreachable | Check `systemctl status`, the port, and that you are on the same network |
| Team picker stays empty; no logos ever appear | The service user cannot write `var/`. The log says so explicitly: `sudo chown -R $USER ~/sports-scoreboard`, then restart |
| Log says `Still running as root` | The project is root-owned (usually a `sudo git clone`). `sudo chown -R $USER ~/sports-scoreboard` |
| Blocky/soft text | No BDF fonts found — the status panel shows which font each role resolved to. Point `SCOREBOARD_BDF_FONTS` at `~/rpi-rgb-led-matrix/fonts` |

---

## The ESPN caveat

**ESPN's endpoints are not an official, supported, public developer API.**
They are the undocumented JSON endpoints ESPN's own site and apps use. They
need no key and no account, which is why version one uses them — but ESPN can
change or withdraw them at any time, with no notice and no deprecation period.

This is exactly why the provider abstraction exists:

* `scoreboard/providers/base.py` defines the interface.
* `scoreboard/providers/espn.py` is the **only** module that knows ESPN's URLs
  or JSON shape.
* Everything downstream consumes `scoreboard/models.py` types.

To move to another source, write one new class implementing `SportsProvider`,
register it in `providers/__init__.py`, and change nothing else.

Parsing is deliberately defensive: a malformed event is logged and skipped, a
missing field becomes `None`, and an unrecognised payload yields zero games
rather than an exception. A schema change should degrade the display, not
crash it.

Please be a good citizen: the polling intervals here are deliberately modest,
and `min_interval_seconds` enforces a floor. Do not lower them much.

---

## Project structure

```
sports-scoreboard/
├── README.md
├── requirements.txt / pyproject.toml
├── config/
│   ├── config.example.json      template (config.json is generated, gitignored)
│   └── README.md                field-by-field reference
├── scoreboard/
│   ├── app.py                   entry point, wiring, signals, shutdown
│   ├── config.py                typed config + atomic persistence
│   ├── models.py                Game / GameTeam / TeamInfo — provider-independent
│   ├── leagues.py               canonical league ids (never ESPN URLs)
│   ├── state.py                 the one shared, locked state object
│   ├── scheduler.py             adaptive polling thread
│   ├── runner.py                the render loop
│   ├── rotation.py              priority + rotation engine
│   ├── logos.py                 download, process and cache logos
│   ├── teams.py                 cached team catalogs for the picker
│   ├── httpclient.py            timeouts, retries, last-known-good cache
│   ├── privileges.py            drop root after the matrix is up
│   ├── paths.py                 where files live
│   ├── samples.py               synthetic games for tests and the UI
│   ├── providers/
│   │   ├── base.py              the SportsProvider interface
│   │   └── espn.py              the only ESPN-aware module
│   ├── display/
│   │   ├── base.py              Display interface
│   │   ├── matrix.py            rgbmatrix backend
│   │   ├── preview.py           image backend (browser preview, dev)
│   │   ├── layouts.py           layouts A–E, any chain length
│   │   └── fonts.py             BDF → TTF → built-in font resolution
│   ├── assets/fonts/            drop extra .bdf files here
│   └── web/                     Flask app, JSON API, HTML/CSS/JS
├── scripts/
│   ├── install.sh               inspect, set up, optionally install the service
│   ├── run-dev.sh               preview mode
│   ├── test-matrix.sh           panel test pattern / demo / sample PNGs
│   ├── service.sh               start/stop/restart/status/logs
│   ├── render_samples.py        render every layout to PNG
│   └── show_pattern.py          the panel test pattern, on real hardware
├── systemd/
│   ├── sports-scoreboard.service
│   └── sports-scoreboard-preview.service
├── tests/                       ~180 offline tests + ESPN fixtures
└── var/                         runtime caches (gitignored)
```

`var/` holds everything the app writes at runtime — logos, team catalogs,
cached HTTP responses, converted fonts — deliberately kept out of the package
directory so the source tree stays clean and can be read-only.

---

## Possible future work

Not needed for version one, but genuinely worthwhile:

* **A second provider.** The abstraction is there; a paid or alternative feed
  would remove the ESPN risk entirely.
* **Per-sport layouts.** Baseball wants a base-runner diamond and a count;
  football wants down and distance and a red-zone cue. The hooks
  (`Game.situation`) are already populated.
* **Scrolling text** for long team names, headlines or a ticker row.
* **Score-change animation** — a brief flash or wipe when a favourite scores.
  Very effective on a panel, and cheap to add in `layouts.py`.
* **Ambient brightness** via a cheap I²C light sensor, instead of a fixed
  schedule.
* **A "game starting soon" alert screen** in the last few minutes before a
  favourite's kickoff.
* **Standings and records screens** — ESPN exposes both.
* **Authentication on the web UI** if it ever leaves a trusted LAN. Today it
  is deliberately open and deliberately LAN-only.
