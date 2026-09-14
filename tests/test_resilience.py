"""Offline, stale and broken-input behaviour.

The scoreboard's job when the internet disappears is to keep showing the
last thing it knew, mark it as stale, and keep the clock running.
"""

from __future__ import annotations

import io
import json
import os
import threading
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from scoreboard.config import Config, ConfigStore
from scoreboard.httpclient import FetchError, HttpClient
from scoreboard.logos import LogoCache
from scoreboard.models import LeagueSnapshot
from scoreboard.providers.espn import EspnProvider
from scoreboard.scheduler import DataScheduler
from scoreboard.state import AppState


# -- HTTP client -----------------------------------------------------------

class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FlakySession:
    """Fails ``failures`` times, then succeeds."""

    def __init__(self, payload, failures=0, exception=None):
        import requests
        self.payload = payload
        self.remaining = failures
        self.exception = exception or requests.ConnectionError("network is down")
        self.calls = 0
        self.headers = {}

    def get(self, *args, **kwargs):
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise self.exception
        return FakeResponse(self.payload)

    def close(self):
        pass


def make_client(tmp_path, session):
    client = HttpClient(cache_dir=str(tmp_path / "http"), timeout=0.1, retries=2,
                        backoff_base=0.001)
    client._session = session
    return client


def test_transient_failure_is_retried(tmp_path):
    session = FlakySession({"ok": True}, failures=2)
    client = make_client(tmp_path, session)
    result = client.get_json("https://example.test/x", cache_key="k")
    assert result.data == {"ok": True} and session.calls == 3
    assert result.from_cache is False


def test_total_failure_falls_back_to_the_cached_copy(tmp_path):
    good = make_client(tmp_path, FlakySession({"value": 1}))
    good.get_json("https://example.test/x", cache_key="k")

    offline = make_client(tmp_path, FlakySession({}, failures=99))
    result = offline.get_json("https://example.test/x", cache_key="k")
    assert result.data == {"value": 1}
    assert result.from_cache is True and result.stale is True


def test_failure_with_no_cache_raises(tmp_path):
    client = make_client(tmp_path, FlakySession({}, failures=99))
    with pytest.raises(FetchError):
        client.get_json("https://example.test/never-seen", cache_key="cold")


def test_client_http_404_is_not_retried(tmp_path):
    class NotFound(FlakySession):
        def get(self, *args, **kwargs):
            self.calls += 1
            return FakeResponse({}, status=404)

    session = NotFound({})
    client = make_client(tmp_path, session)
    with pytest.raises(FetchError):
        client.get_json("https://example.test/gone")
    assert session.calls == 1, "client errors should not be retried"


def test_shutdown_cancels_retries(tmp_path):
    session = FlakySession({}, failures=99)
    client = make_client(tmp_path, session)
    client.cancel_event.set()
    with pytest.raises(FetchError):
        client.get_json("https://example.test/x")
    assert session.calls == 0


def test_unreadable_cache_file_is_ignored(tmp_path):
    client = make_client(tmp_path, FlakySession({"a": 1}))
    client.get_json("https://example.test/x", cache_key="k")
    path = client._cache_path("k")
    with open(path, "w") as handle:
        handle.write("not json")
    offline = make_client(tmp_path, FlakySession({}, failures=99))
    with pytest.raises(FetchError):
        offline.get_json("https://example.test/x", cache_key="k")


# -- provider --------------------------------------------------------------

class DeadClient:
    def __init__(self):
        self.cancel_event = threading.Event()

    def get_json(self, *args, **kwargs):
        raise FetchError("offline")


def test_provider_returns_an_error_snapshot_instead_of_raising():
    provider = EspnProvider(client=DeadClient())
    snapshot = provider.fetch_scoreboard("nfl")
    assert snapshot.games == [] and snapshot.error and snapshot.ok is False


def test_provider_rejects_unknown_leagues():
    snapshot = EspnProvider(client=DeadClient()).fetch_scoreboard("quidditch")
    assert snapshot.error and "not supported" in snapshot.error


def test_repeated_failures_do_not_escalate(caplog):
    provider = EspnProvider(client=DeadClient())
    with caplog.at_level("ERROR"):
        for _ in range(10):
            provider.fetch_scoreboard("nfl")
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1, "an outage should log once, not once per poll"


# -- state -----------------------------------------------------------------

def test_state_keeps_the_last_good_games_through_a_failure(nfl_games):
    state = AppState()
    state.update_snapshot(LeagueSnapshot("nfl", games=nfl_games))
    assert len(state.games()) == 3
    assert state.online is True and state.any_stale is False

    state.update_snapshot(LeagueSnapshot("nfl", error="offline"))
    assert len(state.games()) == 3, "the last good data must survive"
    assert state.any_stale is True


def test_state_marks_cached_responses_as_stale(nfl_games):
    state = AppState()
    state.update_snapshot(
        LeagueSnapshot("nfl", games=nfl_games, stale=True, error="cached response"))
    assert state.any_stale is True
    assert len(state.games()) == 3


def test_state_forgets_disabled_leagues(nfl_games):
    state = AppState()
    state.update_snapshot(LeagueSnapshot("nfl", games=nfl_games))
    state.update_snapshot(LeagueSnapshot("nba", games=[]))
    state.forget_leagues(["nba"])
    assert state.games() == []


def test_state_is_json_serialisable(nfl_games):
    state = AppState()
    state.update_snapshot(LeagueSnapshot("nfl", games=nfl_games))
    json.dumps(state.to_dict())


# -- polling intervals -----------------------------------------------------

def test_polling_interval_adapts(nfl_games):
    now = datetime(2025, 9, 14, 18, 0, tzinfo=timezone.utc)
    favourite = Config.from_dict({"sports": {
        "enabled_leagues": ["nfl"],
        "favorite_teams": [{"league": "nfl", "team_id": "16"}]}})
    plain = Config.from_dict({"sports": {"enabled_leagues": ["nfl"]}})

    assert DataScheduler.next_interval(nfl_games, favourite, now) == 15
    assert DataScheduler.next_interval(nfl_games, plain, now) == 25

    finals_only = [game for game in nfl_games if game.is_final]
    assert DataScheduler.next_interval(finals_only, plain, now) == 180


def test_polling_speeds_up_just_before_kickoff(nfl_games):
    upcoming = [game for game in nfl_games if game.is_upcoming]
    config = Config.from_dict({"sports": {"enabled_leagues": ["nfl"]}})
    just_before = upcoming[0].start_time - timedelta(minutes=5)
    assert DataScheduler.next_interval(upcoming, config, just_before) == 25
    long_before = upcoming[0].start_time - timedelta(hours=5)
    assert DataScheduler.next_interval(upcoming, config, long_before) == 180


class ExplodingProvider:
    name = "boom"

    def supported_leagues(self):
        return ["nfl"]

    def fetch_scoreboard(self, league_id):
        raise RuntimeError("provider went rogue")

    def fetch_teams(self, league_id):
        raise RuntimeError("provider went rogue")


def test_scheduler_survives_a_provider_that_raises(tmp_path):
    store = ConfigStore(str(tmp_path / "config.json"))
    store.load()
    state = AppState()
    scheduler = DataScheduler(ExplodingProvider(), state, store)
    interval = scheduler.poll_once()          # must not raise
    assert interval > 0
    assert any(status.error for status in state.league_statuses())


# -- logo cache ------------------------------------------------------------

def png_bytes(size=(64, 64), color=(255, 0, 0, 255)):
    buffer = io.BytesIO()
    Image.new("RGBA", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeLogoClient:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = 0

    def get_bytes(self, url):
        self.calls += 1
        if self.error:
            raise self.error
        return self.payload


def test_logo_is_processed_to_the_requested_box(tmp_path):
    cache = LogoCache(str(tmp_path), client=FakeLogoClient(png_bytes()))
    cache._download("nfl", "16", "https://example.test/logo.png")
    logo = cache.get("nfl", "16", None, (16, 16))
    assert logo is not None and logo.size == (16, 16) and logo.mode == "RGBA"


def test_aspect_ratio_is_preserved(tmp_path):
    cache = LogoCache(str(tmp_path), client=FakeLogoClient(png_bytes((100, 50))))
    cache._download("nfl", "9", "https://example.test/logo.png")
    logo = cache.get("nfl", "9", None, (20, 20))
    assert logo.size == (20, 20)
    # The source is 2:1, so the top and bottom rows must be transparent padding.
    assert logo.getpixel((10, 0))[3] == 0
    assert logo.getpixel((10, 10))[3] == 255


def test_missing_logo_returns_none_and_never_blocks(tmp_path):
    cache = LogoCache(str(tmp_path), client=FakeLogoClient(error=RuntimeError("404")))
    assert cache.get("nfl", "999", "https://example.test/missing.png", (16, 16)) is None


def test_a_download_is_not_repeated(tmp_path):
    client = FakeLogoClient(png_bytes())
    cache = LogoCache(str(tmp_path), client=client)
    cache._download("nfl", "16", "https://example.test/logo.png")
    for _ in range(5):
        cache.get("nfl", "16", "https://example.test/logo.png", (16, 16))
    assert client.calls == 1


def test_processed_sizes_are_cached_on_disk(tmp_path):
    cache = LogoCache(str(tmp_path), client=FakeLogoClient(png_bytes()))
    cache._download("nfl", "16", "https://example.test/logo.png")
    cache.get("nfl", "16", None, (16, 16))
    assert os.path.exists(cache._processed_path("nfl", "16", 16, 16))

    # A fresh cache object (i.e. after a reboot, offline) still finds it.
    cold = LogoCache(str(tmp_path), client=FakeLogoClient(error=RuntimeError("offline")))
    assert cold.get("nfl", "16", None, (16, 16)) is not None


def test_corrupt_cached_logo_is_discarded(tmp_path):
    cache = LogoCache(str(tmp_path), client=FakeLogoClient(png_bytes()))
    path = cache._raw_path("nfl", "16")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"this is not a PNG")
    assert cache.get("nfl", "16", None, (16, 16)) is None
    assert not os.path.exists(path), "the bad file should be removed"


def test_disabled_cache_returns_nothing(tmp_path):
    cache = LogoCache(str(tmp_path), client=FakeLogoClient(png_bytes()), enabled=False)
    assert cache.get("nfl", "16", "https://example.test/logo.png", (16, 16)) is None


def test_stats_are_reportable(tmp_path):
    cache = LogoCache(str(tmp_path), client=FakeLogoClient(png_bytes()))
    stats = cache.stats()
    assert set(stats) == {"cached_raw", "cached_processed", "memory", "failed", "queued"}
