"""Remote updates: the version/update API, the root helper and the updater.

The shell scripts are exercised for real against throwaway git
repositories, with ``systemctl`` and ``runuser`` replaced by recorders, so
nothing here can update or reboot the machine running the tests.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys

import pytest

from scoreboard.config import ConfigStore
from scoreboard.state import AppState
from scoreboard.system import ACTIONS, CONTROL_UNIT, SystemControl
from scoreboard.web import Services, create_app

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

pytestmark = pytest.mark.skipif(shutil.which("git") is None or sys.platform == "win32",
                                reason="needs git and a POSIX shell")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def commit(cwd, name, text="x"):
    with open(os.path.join(cwd, name), "w") as handle:
        handle.write(text)
    git(cwd, "add", name)
    git(cwd, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", f"add {name}")


@pytest.fixture
def remote_and_clone(tmp_path):
    """A bare 'GitHub', a clone standing in for the Pi, and a second clone
    standing in for wherever new commits are made."""
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    upstream = tmp_path / "upstream"
    git(tmp_path, "clone", "-q", str(remote), str(upstream))
    git(upstream, "checkout", "-q", "-b", "main")
    commit(upstream, "README", "v1")
    git(upstream, "push", "-q", "origin", "main")
    pi = tmp_path / "pi"
    git(tmp_path, "clone", "-q", "-b", "main", str(remote), str(pi))
    return remote, upstream, pi


# -- SystemControl ---------------------------------------------------------

def test_version_reports_the_running_commit(remote_and_clone, tmp_path):
    _, _, pi = remote_and_clone
    info = SystemControl(str(pi), str(tmp_path / "control")).version()
    assert info["branch"] == "main"
    assert info["commit"] == git(pi, "rev-parse", "--short", "HEAD")
    assert info["subject"] == "add README" and info["date"]


def test_check_lists_what_is_waiting_upstream(remote_and_clone, tmp_path):
    _, upstream, pi = remote_and_clone
    system = SystemControl(str(pi), str(tmp_path / "control"))
    assert system.check() == {"ok": True, "branch": "main", "behind": 0, "commits": []}
    commit(upstream, "feature", "new")
    git(upstream, "push", "-q", "origin", "main")
    result = system.check()
    assert result["behind"] == 1 and result["commits"][0]["subject"] == "add feature"
    assert git(pi, "rev-parse", "HEAD") != git(upstream, "rev-parse", "HEAD"), \
        "checking must not install anything"


def test_check_reports_why_it_failed(remote_and_clone, tmp_path):
    remote, _, pi = remote_and_clone
    shutil.rmtree(remote)
    result = SystemControl(str(pi), str(tmp_path / "control")).check()
    assert result["ok"] is False and "git fetch failed" in result["error"]


def test_a_request_is_one_word_written_atomically(tmp_path):
    system = SystemControl(str(tmp_path), str(tmp_path / "control"))
    system.request("update-reboot")
    assert (tmp_path / "control" / "request").read_text() == "update-reboot\n"
    assert os.listdir(tmp_path / "control") == ["request"], "no temp files left behind"
    assert system.pending() == "update-reboot"


def test_only_known_actions_can_be_requested(tmp_path):
    system = SystemControl(str(tmp_path), str(tmp_path / "control"))
    with pytest.raises(ValueError):
        system.request("rm -rf /")
    assert system.pending() is None


def test_installed_units_are_detected(tmp_path):
    units = tmp_path / "units"
    system = SystemControl(str(tmp_path), str(tmp_path / "control"), unit_dir=str(units))
    assert not system.controls_installed() and not system.boot_update_enabled()
    (units / "multi-user.target.wants").mkdir(parents=True)
    (units / CONTROL_UNIT).write_text("")
    (units / "multi-user.target.wants" / "sports-scoreboard-update.service").write_text("")
    assert system.controls_installed() and system.boot_update_enabled()


def test_history_is_the_tail_of_the_log(tmp_path):
    control = tmp_path / "control"
    control.mkdir()
    (control / "history.log").write_text("".join(f"line {n}\n" for n in range(50)))
    lines = SystemControl(str(tmp_path), str(control)).history(lines=3)
    assert lines == ["line 47", "line 48", "line 49"]


# -- API -------------------------------------------------------------------

@pytest.fixture
def web(tmp_path):
    store = ConfigStore(str(tmp_path / "config.json"))
    store.load()
    units = tmp_path / "units"
    units.mkdir()
    system = SystemControl(REPO, str(tmp_path / "control"), unit_dir=str(units))
    app = create_app(Services(config_store=store, state=AppState(), system=system))
    return app.test_client(), system, units


def test_the_api_describes_the_installed_software(web):
    client, _, _ = web
    info = client.get("/api/system").get_json()
    assert info["available"] is True and info["controls_installed"] is False
    assert info["version"]["commit"]


def test_buttons_explain_themselves_until_the_helper_is_installed(web):
    client, system, _ = web
    response = client.post("/api/system/update-reboot")
    assert response.status_code == 409
    assert "install.sh --auto-update" in response.get_json()["error"]
    assert system.pending() is None


@pytest.mark.parametrize("action", ACTIONS)
def test_each_action_writes_its_request(web, action):
    client, system, units = web
    (units / CONTROL_UNIT).write_text("")
    assert client.post(f"/api/system/{action}").status_code == 200
    assert system.pending() == action


def test_unknown_actions_are_refused(web):
    client, system, units = web
    (units / CONTROL_UNIT).write_text("")
    assert client.post("/api/system/shutdown").status_code == 404
    assert system.pending() is None


def test_without_a_system_object_the_card_hides(tmp_path):
    store = ConfigStore(str(tmp_path / "config.json"))
    store.load()
    client = create_app(Services(config_store=store, state=AppState())).test_client()
    assert client.get("/api/system").get_json() == {"available": False}
    assert client.post("/api/system/reboot").status_code == 503


# -- scripts/control.sh -----------------------------------------------------

def write_script(path, body):
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def control(tmp_path):
    """control.sh in a throwaway project, with systemctl/runuser recorded."""
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    (project / "systemd").mkdir()
    (project / ".git").mkdir()
    (project / "var" / "control").mkdir(parents=True)
    shutil.copy(os.path.join(REPO, "scripts", "control.sh"), project / "scripts")
    write_script(project / "scripts" / "auto-update.sh",
                 'echo "fake update force=$SCOREBOARD_UPDATE_FORCE"\n')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    write_script(bin_dir / "systemctl", f'echo "systemctl $*" >> {calls}\n')
    write_script(bin_dir / "runuser",
                 f'echo "runuser $1 $2" >> {calls}\nshift 3\nexec "$@"\n')
    units = tmp_path / "units"
    units.mkdir()
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
               SCOREBOARD_USER=subprocess.run(["id", "-un"], capture_output=True,
                                              text=True).stdout.strip(),
               SCOREBOARD_UNIT_DIR=str(units))

    def run(verb=None):
        if verb is not None:
            (project / "var" / "control" / "request").write_text(verb + "\n")
        subprocess.run([str(project / "scripts" / "control.sh")], env=env, check=True,
                       capture_output=True, timeout=30)
        history = project / "var" / "control" / "history.log"
        return (calls.read_text() if calls.exists() else "",
                history.read_text() if history.exists() else "")

    return run, project, units


def test_update_reboot_updates_then_reboots(control):
    run, project, _ = control
    calls, history = run("update-reboot")
    assert "runuser -u" in calls
    assert calls.strip().endswith("systemctl reboot")
    assert "fake update force=1" in history and "Rebooting." in history
    assert not (project / "var" / "control" / "request").exists()


def test_update_restarts_the_scoreboard_without_rebooting(control):
    run, _, _ = control
    calls, _ = run("update")
    assert "systemctl restart sports-scoreboard" in calls
    assert "reboot" not in calls


def test_reboot_does_not_update(control):
    run, _, _ = control
    calls, _ = run("reboot")
    assert calls.strip() == "systemctl reboot"


def test_anything_else_is_ignored(control):
    run, project, _ = control
    calls, history = run("rm -rf /; reboot")
    assert calls == ""
    assert "Ignored an unrecognised request" in history
    assert not (project / "var" / "control" / "request").exists()


def test_no_request_means_nothing_happens(control):
    run, _, _ = control
    assert run() == ("", "")


def test_installed_units_are_refreshed_after_an_update(control):
    run, project, units = control
    (project / "systemd" / "sports-scoreboard.service").write_text(
        "ExecStart=__INSTALL_DIR__/x\nUser=__RUN_USER__\n")
    (project / "systemd" / "sports-scoreboard-new.service").write_text("new\n")
    (units / "sports-scoreboard.service").write_text("old\n")
    calls, history = run("update")
    assert (units / "sports-scoreboard.service").read_text().startswith(
        f"ExecStart={project}/x")
    assert not (units / "sports-scoreboard-new.service").exists(), \
        "a unit that was never installed is not installed by an update"
    assert "systemctl daemon-reload" in calls and "Updated sports-scoreboard.service" in history


# -- scripts/auto-update.sh -------------------------------------------------

@pytest.fixture
def updater(remote_and_clone):
    """The real auto-update.sh inside the Pi stand-in, with a stub venv."""
    remote, upstream, pi = remote_and_clone
    (pi / "scripts").mkdir()
    shutil.copy(os.path.join(REPO, "scripts", "auto-update.sh"), pi / "scripts")
    git(pi, "add", "scripts")
    git(pi, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "scripts")
    git(pi, "push", "-q", "origin", "main")
    git(upstream, "pull", "-q", "origin", "main")
    (pi / ".git" / "info" / "exclude").write_text(".venv/\nvar/\n")
    (pi / ".venv" / "bin").mkdir(parents=True)
    write_script(pi / ".venv" / "bin" / "python", "exit 0\n")

    def run(**env):
        result = subprocess.run(
            [str(pi / "scripts" / "auto-update.sh")], capture_output=True, text=True,
            timeout=60, env=dict(os.environ, SCOREBOARD_NETWORK_WAIT="0", **env))
        assert result.returncode == 0, "the updater must never fail the boot"
        return result.stdout

    def publish(name):
        commit(upstream, name)
        git(upstream, "push", "-q", "origin", "main")
        return git(upstream, "rev-parse", "HEAD")

    return run, publish, pi, remote


def test_a_new_commit_is_installed(updater):
    run, publish, pi, _ = updater
    target = publish("feature")
    output = run()
    assert "looks good" in output
    assert git(pi, "rev-parse", "HEAD") == target


def test_being_offline_says_what_git_said(updater):
    run, _, pi, remote = updater
    shutil.rmtree(remote)
    output = run()
    assert "Could not fetch" in output and "git said:" in output


def test_local_edits_block_a_boot_update_but_not_a_forced_one(updater):
    run, publish, pi, _ = updater
    target = publish("feature")
    (pi / "README").write_text("edited on the Pi")
    assert "leaving them alone" in run()
    assert git(pi, "rev-parse", "HEAD") != target

    output = run(SCOREBOARD_UPDATE_FORCE="1")
    assert "git stash" in output
    assert git(pi, "rev-parse", "HEAD") == target
    assert "edited on the Pi" in git(pi, "stash", "show", "-p"), "the edit is recoverable"


def test_a_permission_change_is_not_an_edit(updater):
    run, publish, pi, _ = updater
    target = publish("feature")
    readme = pi / "README"
    readme.chmod(readme.stat().st_mode | stat.S_IEXEC)
    run()
    assert git(pi, "rev-parse", "HEAD") == target
