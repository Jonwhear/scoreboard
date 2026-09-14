"""Team catalog caching/search and the privilege-drop helpers."""

from __future__ import annotations

import json
import time

import pytest

from scoreboard.httpclient import FetchError
from scoreboard.models import TeamInfo
from scoreboard.teams import TeamCatalog


class StubProvider:
    name = "stub"

    def __init__(self, teams=None, error=None):
        self.teams = teams or []
        self.error = error
        self.calls = 0

    def supported_leagues(self):
        return ["nfl"]

    def fetch_scoreboard(self, league_id):
        raise NotImplementedError

    def fetch_teams(self, league_id):
        self.calls += 1
        if self.error:
            raise self.error
        return [team for team in self.teams if team.league == league_id]


TEAMS = [
    TeamInfo("nfl", "16", "MIN", "Minnesota Vikings", "Vikings", "Minnesota"),
    TeamInfo("nfl", "9", "GB", "Green Bay Packers", "Packers", "Green Bay"),
    TeamInfo("nfl", "12", "KC", "Kansas City Chiefs", "Chiefs", "Kansas City"),
]


def test_refresh_populates_and_persists(tmp_path):
    catalog = TeamCatalog(StubProvider(TEAMS), str(tmp_path))
    assert len(catalog.refresh("nfl")) == 3
    assert (tmp_path / "nfl.json").exists()
    with open(tmp_path / "nfl.json") as handle:
        assert len(json.load(handle)["teams"]) == 3


def test_catalog_is_reloaded_from_disk_without_network(tmp_path):
    TeamCatalog(StubProvider(TEAMS), str(tmp_path)).refresh("nfl")

    offline = TeamCatalog(StubProvider(error=FetchError("offline")), str(tmp_path))
    teams = offline.get("nfl", refresh_if_stale=False)
    assert [team.abbreviation for team in teams] == ["GB", "KC", "MIN"]


def test_search_ranks_exact_abbreviations_first(tmp_path):
    catalog = TeamCatalog(StubProvider(TEAMS), str(tmp_path))
    catalog.refresh("nfl")
    assert [team.abbreviation for team in catalog.search("nfl", "kc")] == ["KC"]
    assert [team.abbreviation for team in catalog.search("nfl", "green")] == ["GB"]
    assert [team.abbreviation for team in catalog.search("nfl", "vikings")] == ["MIN"]
    assert catalog.search("nfl", "zzz") == []


def test_search_is_case_insensitive_and_limited(tmp_path):
    catalog = TeamCatalog(StubProvider(TEAMS), str(tmp_path))
    catalog.refresh("nfl")
    assert catalog.search("nfl", "MINNESOTA")[0].team_id == "16"
    assert len(catalog.search("nfl", "", limit=2)) == 2


def test_lookup_by_stable_id(tmp_path):
    catalog = TeamCatalog(StubProvider(TEAMS), str(tmp_path))
    catalog.refresh("nfl")
    assert catalog.lookup("nfl", "9").display_name == "Green Bay Packers"
    assert catalog.lookup("nfl", "404") is None
    assert catalog.lookup("nba", "9") is None, "ids are scoped per league"


def test_failed_refresh_raises_and_cools_down(tmp_path):
    provider = StubProvider(error=FetchError("offline"))
    catalog = TeamCatalog(provider, str(tmp_path))
    with pytest.raises(FetchError):
        catalog.refresh("nfl")
    # The cooldown stops the UI from triggering a refresh storm while offline.
    catalog.refresh_async("nfl")
    time.sleep(0.2)
    assert provider.calls == 1


def test_empty_catalog_does_not_replace_a_good_one(tmp_path):
    catalog = TeamCatalog(StubProvider(TEAMS), str(tmp_path))
    catalog.refresh("nfl")
    catalog.provider = StubProvider([])
    assert len(catalog.refresh("nfl")) == 3


def test_unreadable_cache_is_ignored(tmp_path):
    with open(tmp_path / "nfl.json", "w") as handle:
        handle.write("{{{ not json")
    catalog = TeamCatalog(StubProvider(TEAMS), str(tmp_path))
    assert catalog.get("nfl", refresh_if_stale=False) == []


def test_stale_catalog_triggers_a_background_refresh(tmp_path):
    provider = StubProvider(TEAMS)
    catalog = TeamCatalog(provider, str(tmp_path), max_age_seconds=0)
    catalog.get("nfl")
    for _ in range(30):
        if provider.calls:
            break
        time.sleep(0.05)
    assert provider.calls >= 1


# -- privileges ------------------------------------------------------------

def test_resolve_target_user_ignores_root_and_unknown_accounts(monkeypatch):
    from scoreboard import privileges

    monkeypatch.setenv("SUDO_USER", "")
    assert privileges.resolve_target_user("root") != "root"
    assert privileges.resolve_target_user("definitely-not-a-user-12345") != \
        "definitely-not-a-user-12345"


def test_resolve_target_user_prefers_the_configured_account(monkeypatch):
    from scoreboard import privileges

    # 'nobody' exists on essentially every Linux system.
    try:
        import pwd
        pwd.getpwnam("nobody")
    except KeyError:  # pragma: no cover
        pytest.skip("no 'nobody' account on this system")
    assert privileges.resolve_target_user("nobody") == "nobody"


def test_resolve_target_user_uses_sudo_user(monkeypatch):
    from scoreboard import privileges

    try:
        import pwd
        pwd.getpwnam("nobody")
    except KeyError:  # pragma: no cover
        pytest.skip("no 'nobody' account on this system")
    monkeypatch.setenv("SUDO_USER", "nobody")
    assert privileges.resolve_target_user("") == "nobody"


def test_drop_privileges_is_a_no_op_when_not_root(monkeypatch):
    from scoreboard import privileges

    monkeypatch.setattr(privileges, "is_root", lambda: False)
    assert privileges.drop_privileges("nobody") is False


def test_drop_privileges_refuses_without_a_target(monkeypatch, caplog):
    from scoreboard import privileges

    monkeypatch.setattr(privileges, "is_root", lambda: True)
    with caplog.at_level("WARNING"):
        assert privileges.drop_privileges(None) is False
    assert any("run_as_user" in record.message for record in caplog.records)


def test_drop_privileges_reports_an_unknown_user(monkeypatch, caplog):
    from scoreboard import privileges

    monkeypatch.setattr(privileges, "is_root", lambda: True)
    with caplog.at_level("ERROR"):
        assert privileges.drop_privileges("definitely-not-a-user-12345") is False
    assert any("does not exist" in record.message for record in caplog.records)
