"""Shared test fixtures.

Everything here is offline: fixtures are checked-in ESPN payloads, so the
suite never depends on ESPN being reachable or on any game being in season.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def load_fixture(name: str):
    with open(os.path.join(FIXTURE_DIR, name), "r", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture
def nfl_payload():
    return load_fixture("espn_nfl_scoreboard.json")


@pytest.fixture
def mlb_payload():
    return load_fixture("espn_mlb_scoreboard.json")


@pytest.fixture
def teams_payload():
    return load_fixture("espn_nfl_teams.json")


@pytest.fixture
def provider():
    """An ESPN provider with no HTTP client -- parsing methods only."""
    from scoreboard.providers.espn import EspnProvider

    return EspnProvider.__new__(EspnProvider)


@pytest.fixture
def nfl_games(provider, nfl_payload):
    return provider.parse_scoreboard("nfl", nfl_payload)


@pytest.fixture
def fixed_now():
    """A moment during the live fixture game."""
    return datetime(2025, 9, 14, 18, 0, tzinfo=timezone.utc)


@pytest.fixture
def fonts(tmp_path):
    from scoreboard.display.fonts import FontRegistry

    return FontRegistry(cache_dir=str(tmp_path / "fonts"))


@pytest.fixture
def render_context(fonts):
    from scoreboard.display.layouts import RenderContext

    return RenderContext(fonts=fonts, logos=None, show_logos=False)
