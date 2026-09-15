"""Application entry point.

Threading model -- four participants, one shared state object:

* ``main``               parses arguments, initializes hardware, then waits
                         for a shutdown signal
* ``data-scheduler``     polls the sports provider on an adaptive interval
* ``display-loop``       renders frames and pushes them to the display
* ``web``                Flask/waitress, serving the configuration UI

The only thing they share is :class:`~scoreboard.state.AppState` plus the
:class:`~scoreboard.config.ConfigStore`, both of which are internally
locked.  No thread ever waits on another: a hung HTTP request can make the
data stale but can never stall the matrix.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from typing import Optional

from . import __version__, paths
from .config import ConfigStore
from .display.base import Display
from .display.fonts import FontRegistry
from .display.preview import PreviewDisplay
from .httpclient import HttpClient
from .logos import LogoCache
from .privileges import (current_username, drop_privileges, ensure_writable_by,
                         is_root, resolve_target_user)
from .providers import build_provider
from .runner import DisplayRunner
from .scheduler import DataScheduler
from .state import AppState
from .teams import TeamCatalog

log = logging.getLogger("scoreboard")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scoreboard.app",
        description="Sports scoreboard for chained HUB75 LED matrices.",
    )
    parser.add_argument("--preview", action="store_true",
                        help="render to images instead of LED hardware (no GPIO/root needed)")
    parser.add_argument("--config", default=None,
                        help=f"path to config.json (default: {paths.DEFAULT_CONFIG_PATH})")
    parser.add_argument("--host", default=None, help="web UI bind address")
    parser.add_argument("--port", type=int, default=None, help="web UI port")
    parser.add_argument("--no-web", action="store_true", help="do not start the web UI")
    parser.add_argument("--preview-out", default=None,
                        help="also write each frame to this PNG path")
    parser.add_argument("--log-level", default=os.environ.get("SCOREBOARD_LOG_LEVEL", "INFO"),
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--strict-hardware", action="store_true",
                        help="exit if the matrix cannot be initialized instead of "
                             "falling back to preview mode")
    parser.add_argument("--version", action="version", version=f"sports-scoreboard {__version__}")
    return parser


def configure_logging(level: str) -> None:
    """Log to stderr; systemd captures it into the journal."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for existing in list(root.handlers):
        root.removeHandler(existing)
    handler = logging.StreamHandler(sys.stderr)
    if os.environ.get("JOURNAL_STREAM"):
        # systemd already stamps the time and unit name.
        handler.setFormatter(logging.Formatter("%(levelname)-7s %(name)s: %(message)s"))
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                              datefmt="%H:%M:%S")
        )
    root.addHandler(handler)
    # These are noisy and rarely tell us anything we do not already log.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def create_display(config, use_preview: bool, preview_out: Optional[str],
                   state: AppState, strict: bool) -> Display:
    """Initialize the display backend, dropping root once the matrix is up."""
    if use_preview:
        display = PreviewDisplay(config.display, output_path=preview_out)
        display.start()
        state.set_display_backend(display.backend_name)
        return display

    from .display.matrix import MatrixDisplay, MatrixUnavailableError

    display: Display = MatrixDisplay(config.display)
    try:
        display.start()
    except MatrixUnavailableError as exc:
        log.error("%s", exc)
        if strict:
            raise
        log.error("Falling back to preview mode; the web UI will still work.")
        _drop_privileges_if_needed(config)
        fallback = PreviewDisplay(config.display, output_path=preview_out)
        fallback.start()
        state.set_display_backend(fallback.backend_name, error=str(exc))
        return fallback

    _drop_privileges_if_needed(config)
    state.set_display_backend(display.backend_name)
    return display


def _drop_privileges_if_needed(config) -> None:
    if not is_root():
        log.info("Running as %s", current_username())
        return
    target = resolve_target_user(config.display.run_as_user)
    # Do this before dropping, not after: once we are unprivileged we can no
    # longer fix the ownership of anything root created on the way up.
    ensure_writable_by([paths.CONFIG_DIR, paths.VAR_DIR], target)
    drop_privileges(target)


def run(args: argparse.Namespace) -> int:
    configure_logging(args.log_level)
    paths.ensure_directories()

    log.info("sports-scoreboard %s starting (pid %d)", __version__, os.getpid())

    store = ConfigStore(args.config or paths.config_path())
    config = store.load()
    if not os.path.exists(store.path):
        store.save(config)          # materialise a documented default file
        log.info("Wrote default configuration to %s", store.path)

    state = AppState()
    stop_event = threading.Event()

    display = create_display(config, args.preview, args.preview_out, state,
                             args.strict_hardware)

    fonts = FontRegistry(cache_dir=paths.FONT_CACHE_DIR)
    fonts.get("small")  # resolve eagerly so font problems surface at startup

    logos = LogoCache(paths.LOGO_CACHE_DIR, enabled=config.display.show_logos)
    logos.start()

    http = HttpClient(
        cache_dir=paths.HTTP_CACHE_DIR,
        timeout=config.polling.http_timeout_seconds,
        retries=config.polling.http_retries,
        cancel_event=stop_event,
    )
    provider = build_provider("espn", client=http)
    catalog = TeamCatalog(provider, paths.TEAM_CACHE_DIR)

    scheduler = DataScheduler(provider, state, store, logos=logos, stop_event=stop_event)
    runner = DisplayRunner(display, state, store, fonts, logos=logos, stop_event=stop_event)

    def on_config_change(new_config) -> None:
        logos.enabled = new_config.display.show_logos
        scheduler.request_refresh()

    store.add_listener(on_config_change)

    log.info(
        "Leagues enabled: %s | favourites: %d | canvas %dx%d | backend %s",
        ", ".join(config.sports.enabled_leagues) or "none",
        len(config.sports.favorite_teams),
        config.display.width, config.display.height, display.backend_name,
    )

    runner.start()
    scheduler.start()
    for league in config.sports.enabled_leagues:
        catalog.refresh_async(league)

    web_thread = None
    if not args.no_web:
        web_thread = start_web(config, args, store, state, runner, scheduler, catalog,
                               logos, fonts)

    install_signal_handlers(stop_event)
    try:
        while not stop_event.wait(timeout=1.0):
            pass
    except KeyboardInterrupt:  # pragma: no cover - interactive use
        log.info("Interrupted")

    log.info("Shutting down…")
    http.cancel_event.set()   # abort retry backoffs immediately
    scheduler.stop()
    runner.stop()
    runner.join(timeout=5.0)
    scheduler.join(timeout=5.0)
    logos.stop()
    try:
        display.stop()
    except Exception:
        log.exception("Error while stopping the display")
    http.close()
    if web_thread is not None and web_thread.is_alive():
        log.debug("Web server thread will exit with the process")
    log.info("Goodbye")
    return 0


def start_web(config, args, store, state, runner, scheduler, catalog, logos,
              fonts) -> threading.Thread:
    """Start the web UI in a background thread."""
    from .web import Services, create_app

    services = Services(
        config_store=store, state=state, runner=runner, scheduler=scheduler,
        teams=catalog, logos=logos, fonts=fonts, version=__version__,
    )
    app = create_app(services)
    host = args.host or config.web.host
    port = args.port or config.web.port

    def serve() -> None:
        try:
            from waitress import serve as waitress_serve

            log.info("Web UI on http://%s:%d (waitress)", host, port)
            waitress_serve(app, host=host, port=port, threads=6, _quiet=True)
        except ImportError:
            log.info("Web UI on http://%s:%d (flask built-in server)", host, port)
            app.run(host=host, port=port, threaded=True, debug=False, use_reloader=False)
        except OSError as exc:
            log.error("Web UI could not bind %s:%d: %s", host, port, exc)
        except Exception:
            log.exception("Web server stopped unexpectedly")

    thread = threading.Thread(target=serve, name="web", daemon=True)
    thread.start()
    return thread


def install_signal_handlers(stop_event: threading.Event) -> None:
    """Stop cleanly on SIGTERM (systemd) and SIGINT (Ctrl-C)."""

    def handler(signum, _frame):
        log.info("Received %s; stopping", signal.Signals(signum).name)
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, handler)
        except ValueError:  # pragma: no cover - non-main thread
            log.debug("Could not install handler for %s", sig)


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except Exception:
        logging.getLogger("scoreboard").exception("Fatal error during startup")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
