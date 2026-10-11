"""Configuration validation, atomic writes and corruption recovery."""

from __future__ import annotations

import json
import os
from datetime import datetime

import pytest

from scoreboard.config import (CONFIG_VERSION, Config, ConfigStore, SleepConfig,
                               deep_merge, default_config)


def test_defaults_describe_the_real_hardware():
    config = default_config()
    assert (config.display.rows, config.display.cols) == (32, 64)
    assert config.display.chain_length == 2
    assert config.display.gpio_mapping == "adafruit-hat"
    assert config.display.slowdown_gpio == 4
    assert (config.display.width, config.display.height) == (128, 32)


@pytest.mark.parametrize("chain,expected", [(1, 64), (2, 128), (3, 192), (4, 256)])
def test_canvas_width_follows_chain_length(chain, expected):
    """Panels are chained, so the canvas grows with the chain."""
    config = Config.from_dict({"display": {"chain_length": chain}})
    assert config.display.width == expected
    assert config.display.height == 32


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
    assert config.display.chain_length == 2
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


def test_diagnostic_display_settings_default_off():
    """show_refresh_rate and force_safe_blit are for debugging only.

    tune-matrix.py turns them on for its own run; a --save must not
    persist them as a side effect of that.
    """
    config = default_config()
    assert config.display.show_refresh_rate is False
    assert config.display.force_safe_blit is False
    assert Config.from_dict(config.to_dict()).display.show_refresh_rate is False


def test_reset_timing_targets_are_the_shipped_defaults():
    """--reset-timing must restore exactly the free-running configuration.

    Capping the refresh rate was a tuning step that made a working display
    flicker: pinning 170Hz down to 100Hz throws away refresh headroom and
    lands on the edge of visible flicker. Unlimited is the default for a
    reason, and the escape hatch has to get back to it.
    """
    display = default_config().display
    assert display.limit_refresh_rate_hz == 0, "unlimited, not pinned"
    assert display.disable_hardware_pulsing is True
    assert display.pwm_bits == 11
    assert display.pwm_lsb_nanoseconds == 130
    assert display.slowdown_gpio == 4


def test_reset_timing_leaves_geometry_alone(tmp_path):
    """Panel geometry describes the hardware, not the tuning."""
    store = ConfigStore(str(tmp_path / "config.json"))
    store.load()
    store.update({"display": {"chain_length": 2, "gpio_mapping": "adafruit-hat-pwm",
                              "limit_refresh_rate_hz": 100, "pwm_bits": 8}})
    store.update({"display": {"limit_refresh_rate_hz": 0, "pwm_bits": 11}})
    display = store.config.display
    assert display.chain_length == 2, "geometry survives a timing reset"
    assert display.gpio_mapping == "adafruit-hat-pwm"
    assert display.limit_refresh_rate_hz == 0
    assert display.pwm_bits == 11


# -- upgrade safety --------------------------------------------------------
#
# The promise: upgrading the scoreboard never loses what you configured.
# These tests exist because favourites are tedious to re-enter on a phone.

def test_a_pre_versioning_config_keeps_everything(tmp_path):
    """A config.json written before schema versions existed must survive."""
    legacy = {
        "display": {"rows": 32, "cols": 64, "chain_length": 2, "brightness": 72},
        "sports": {
            "enabled_leagues": ["nfl", "nhl"],
            "favorite_teams": [
                {"league": "nfl", "team_id": "16", "abbreviation": "MIN",
                 "display_name": "Minnesota Vikings"},
                {"league": "nhl", "team_id": "30", "abbreviation": "MIN",
                 "display_name": "Minnesota Wild"},
            ],
        },
        "rotation": {"screen_seconds": 12, "favorites_only": True},
        "sleep": {"enabled": True, "start": "23:30", "end": "06:30"},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(legacy))

    config = ConfigStore(str(path)).load()

    assert [fav.key for fav in config.sports.favorite_teams] == ["nfl:16", "nhl:30"]
    assert config.sports.favorite_teams[1].display_name == "Minnesota Wild"
    assert config.sports.enabled_leagues == ["nfl", "nhl"]
    assert config.display.brightness == 72
    assert config.display.chain_length == 2
    assert config.rotation.screen_seconds == 12
    assert config.rotation.favorites_only is True
    assert (config.sleep.start, config.sleep.end) == ("23:30", "06:30")
    assert config.version == CONFIG_VERSION


def test_new_settings_appear_with_defaults_without_touching_old_ones(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "sports": {"favorite_teams": [{"league": "epl", "team_id": "359"}]},
    }))
    config = ConfigStore(str(path)).load()
    assert [fav.key for fav in config.sports.favorite_teams] == ["epl:359"]
    # Everything added later is simply present at its default.
    assert config.rotation.layout_mode == "auto"
    assert config.display.show_refresh_rate is False


def test_a_config_from_a_newer_version_still_loads(tmp_path, caplog):
    """Downgrading must not wipe the file: ignore what we do not know."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "version": CONFIG_VERSION + 5,
        "display": {"brightness": 33},
        "sports": {"favorite_teams": [{"league": "nfl", "team_id": "9"}]},
        "something_from_the_future": {"nested": [1, 2, 3]},
    }))
    with caplog.at_level("WARNING"):
        config = ConfigStore(str(path)).load()
    assert config.display.brightness == 33
    assert [fav.key for fav in config.sports.favorite_teams] == ["nfl:9"]
    assert any("newer version" in record.message for record in caplog.records)


def test_unknown_keys_are_ignored_not_fatal(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "display": {"brightness": 44, "a_key_we_removed": True},
        "sports": {"favorite_teams": [{"league": "mlb", "team_id": "16",
                                       "nickname": "gone"}]},
        "an_entire_section_we_removed": {"x": 1},
    }))
    config = ConfigStore(str(path)).load()
    assert config.display.brightness == 44
    assert [fav.key for fav in config.sports.favorite_teams] == ["mlb:16"]


def test_favourites_round_trip_through_a_save(tmp_path):
    """Load an old file, save it back, and still have the favourites."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"sports": {"favorite_teams": [
        {"league": "nfl", "team_id": "16", "abbreviation": "MIN",
         "display_name": "Minnesota Vikings"}]}}))
    store = ConfigStore(str(path))
    store.load()
    store.update({"display": {"brightness": 60}})

    reloaded = ConfigStore(str(path)).load()
    assert [fav.key for fav in reloaded.sports.favorite_teams] == ["nfl:16"]
    assert reloaded.sports.favorite_teams[0].display_name == "Minnesota Vikings"
    assert reloaded.display.brightness == 60
    assert json.loads(path.read_text())["version"] == CONFIG_VERSION


def test_check_only_mode_builds_everything_and_exits(tmp_path, monkeypatch):
    """The auto-updater's safety net: a commit that cannot start is refused."""
    from scoreboard import app as app_module

    monkeypatch.setenv("SCOREBOARD_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr(app_module.paths, "VAR_DIR", str(tmp_path / "var"))
    for attribute in ("LOGO_CACHE_DIR", "TEAM_CACHE_DIR", "STANDINGS_CACHE_DIR",
                      "HTTP_CACHE_DIR", "FONT_CACHE_DIR"):
        monkeypatch.setattr(app_module.paths, attribute,
                            str(tmp_path / "var" / attribute.lower()))
    monkeypatch.setattr(app_module.paths, "CONFIG_DIR", str(tmp_path))

    assert app_module.main(["--check-only", "--no-web",
                            "--config", str(tmp_path / "config.json")]) == 0


def test_check_only_reports_failure_rather_than_raising(tmp_path, monkeypatch):
    from scoreboard import app as app_module

    def explode(*args, **kwargs):
        raise RuntimeError("a layout blew up")

    monkeypatch.setattr(app_module, "DisplayRunner", explode)
    monkeypatch.setattr(app_module.paths, "CONFIG_DIR", str(tmp_path))
    assert app_module.main(["--check-only", "--no-web",
                            "--config", str(tmp_path / "config.json")]) == 1
