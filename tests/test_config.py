"""Configuration validation, atomic writes and corruption recovery."""

from __future__ import annotations

import json
import os
from datetime import datetime

import pytest

from scoreboard.config import (Config, ConfigStore, SleepConfig, deep_merge,
                               default_config)


def test_defaults_describe_the_real_hardware():
    config = default_config()
    assert (config.display.rows, config.display.cols) == (32, 64)
    assert config.display.chain_length == 3
    assert config.display.gpio_mapping == "adafruit-hat"
    assert config.display.slowdown_gpio == 4
    assert (config.display.width, config.display.height) == (192, 32)


def test_out_of_range_values_are_clamped_not_rejected():
    config = Config.from_dict({"display": {"brightness": 900, "chain_length": 0},
                               "rotation": {"screen_seconds": -4}})
    assert config.display.brightness == 100
    assert config.display.chain_length == 1
    assert config.rotation.screen_seconds == 3


def test_unknown_enum_values_fall_back():
    config = Config.from_dict({"display": {"gpio_mapping": "nonsense"},
                               "rotation": {"layout_mode": "3d"}})
    assert config.display.gpio_mapping == "adafruit-hat"
    assert config.rotation.layout_mode == "auto"


def test_league_ids_are_normalized_and_filtered():
    config = Config.from_dict(
        {"sports": {"enabled_leagues": ["NFL", "premier-league", "quidditch", "nfl"]}}
    )
    assert config.sports.enabled_leagues == ["nfl", "epl"]


def test_empty_league_list_falls_back_to_defaults():
    config = Config.from_dict({"sports": {"enabled_leagues": []}})
    assert config.sports.enabled_leagues == ["nfl", "mlb", "nhl", "nba"]


def test_favorites_need_a_league_and_id():
    config = Config.from_dict({"sports": {"favorite_teams": [
        {"league": "nfl", "team_id": "16", "abbreviation": "MIN"},
        {"league": "nfl"},                       # no id -> dropped
        {"team_id": "5"},                        # no league -> dropped
        {"league": "nfl", "team_id": "16"},      # duplicate -> dropped
        "garbage",
    ]}})
    assert [fav.key for fav in config.sports.favorite_teams] == ["nfl:16"]


def test_round_trip_is_stable():
    config = Config.from_dict({"sports": {"favorite_teams": [
        {"league": "nhl", "team_id": "3", "abbreviation": "NYR"}]}})
    assert Config.from_dict(config.to_dict()).to_dict() == config.to_dict()


@pytest.mark.parametrize("start,end,when,expected", [
    ("01:00", "07:00", datetime(2025, 1, 1, 3, 0), True),
    ("01:00", "07:00", datetime(2025, 1, 1, 9, 0), False),
    ("23:00", "07:00", datetime(2025, 1, 1, 23, 30), True),   # crosses midnight
    ("23:00", "07:00", datetime(2025, 1, 1, 2, 0), True),
    ("23:00", "07:00", datetime(2025, 1, 1, 12, 0), False),
    ("07:00", "07:00", datetime(2025, 1, 1, 7, 0), False),    # empty window
])
def test_sleep_window(start, end, when, expected):
    assert SleepConfig(enabled=True, start=start, end=end).is_sleeping(when) is expected


def test_sleep_disabled_is_never_sleeping():
    assert SleepConfig(enabled=False, start="00:00", end="23:59").is_sleeping() is False


def test_invalid_times_fall_back():
    config = Config.from_dict({"sleep": {"start": "25:99", "end": "nope"}})
    assert (config.sleep.start, config.sleep.end) == ("01:00", "07:00")


def test_deep_merge_recurses():
    merged = deep_merge({"a": {"b": 1, "c": 2}, "d": 3}, {"a": {"c": 9}})
    assert merged == {"a": {"b": 1, "c": 9}, "d": 3}


# -- persistence -----------------------------------------------------------

def test_save_then_load(tmp_path):
    path = tmp_path / "config.json"
    store = ConfigStore(str(path))
    store.load()
    store.update({"display": {"brightness": 33}})
    assert json.loads(path.read_text())["display"]["brightness"] == 33
    assert ConfigStore(str(path)).load().display.brightness == 33


def test_save_is_atomic_and_leaves_no_temp_files(tmp_path):
    store = ConfigStore(str(tmp_path / "config.json"))
    store.load()
    store.save()
    store.update({"display": {"brightness": 40}})
    leftovers = [name for name in os.listdir(tmp_path) if name.startswith(".config-")]
    assert leftovers == []


def test_previous_version_is_kept_as_backup(tmp_path):
    path = tmp_path / "config.json"
    store = ConfigStore(str(path))
    store.load()
    store.update({"display": {"brightness": 10}})
    store.update({"display": {"brightness": 90}})
    assert json.loads(path.read_text())["display"]["brightness"] == 90
    assert json.loads((tmp_path / "config.json.bak").read_text())["display"]["brightness"] == 10


def test_corrupt_config_is_quarantined_and_backup_is_used(tmp_path):
    path = tmp_path / "config.json"
    store = ConfigStore(str(path))
    store.load()
    store.update({"display": {"brightness": 44}})
    store.update({"display": {"brightness": 55}})     # brightness 44 -> .bak

    path.write_text("{ this is not json")
    recovered = ConfigStore(str(path)).load()

    assert recovered.display.brightness == 44, "should fall back to the backup"
    quarantined = [name for name in os.listdir(tmp_path) if ".corrupt-" in name]
    assert len(quarantined) == 1, "the bad file must be preserved, not deleted"
    assert (tmp_path / quarantined[0]).read_text() == "{ this is not json"


def test_config_with_no_files_uses_defaults(tmp_path):
    store = ConfigStore(str(tmp_path / "missing.json"))
    config = store.load()
    assert config.display.chain_length == 3
    assert store.source == "defaults"


def test_non_object_json_is_treated_as_corrupt(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("[1, 2, 3]")
    config = ConfigStore(str(path)).load()
    assert config.display.brightness == 50


def test_listeners_are_notified_and_failures_are_contained(tmp_path):
    store = ConfigStore(str(tmp_path / "config.json"))
    store.load()
    seen = []
    store.add_listener(lambda config: (_ for _ in ()).throw(RuntimeError("boom")))
    store.add_listener(seen.append)
    store.update({"display": {"brightness": 21}})
    assert len(seen) == 1 and seen[0].display.brightness == 21


def test_saved_file_is_readable_after_privilege_drop(tmp_path):
    """Root writes it; the unprivileged service account must still read it."""
    path = tmp_path / "config.json"
    store = ConfigStore(str(path))
    store.load()
    store.save()
    assert os.stat(path).st_mode & 0o044, "config must be world/group readable"
