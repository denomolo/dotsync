"""install, the systemd service/timer, and error reporting."""

import subprocess

import pytest

import dotsync.__main__ as cli_module
import dotsync.config as config_module
import dotsync.systemd
from conftest import git


@pytest.fixture
def fresh_machine(sandbox, monkeypatch):
    """A new, empty home with no dotsync config yet. Returns a function that
    switches to another such machine."""
    def switch(name: str):
        home = sandbox.root / name
        home.mkdir()
        config = home / ".config" / "dotsync" / "config.yaml"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.chdir(home)
        monkeypatch.setattr(config_module, "CONFIG_PATH", config)
        monkeypatch.setattr(cli_module, "CONFIG_PATH", config)
        return home
    return switch


def new_remote(sandbox, name="dots.git"):
    remote = sandbox.root / name
    git(sandbox.root, "init", "-q", "--bare", "-b", "main", str(remote))
    return remote


def test_install_init_then_track_and_push(sandbox, fresh_machine):
    home = fresh_machine("machine-a")
    remote = new_remote(sandbox)

    result = sandbox.runner.invoke(cli_module.cli, ["install", "--repo-url", str(remote), "--init"],
                                   input="n\n")

    assert result.exit_code == 0, result.output
    assert (home / ".config/dotsync/config.yaml").exists()
    (home / ".vimrc").write_text("set nu\n")
    sandbox.run("add", "~/.vimrc")
    assert git(remote, "show", "base:files/.vimrc") == "set nu\n"


def test_install_with_an_empty_remote_sets_it_up(sandbox, fresh_machine):
    """Regression: cloning an empty repo reported success but left no base
    branch or manifest, so every later command crashed."""
    home = fresh_machine("machine-a")
    remote = new_remote(sandbox)

    result = sandbox.runner.invoke(cli_module.cli, ["install", "--repo-url", str(remote)], input="n\n")

    assert result.exit_code == 0, result.output
    assert "repo is empty" in result.output
    (home / ".vimrc").write_text("v\n")
    sandbox.run("add", "~/.vimrc")
    assert git(remote, "show", "base:files/.vimrc") == "v\n"


def test_second_machine_clones_and_checks_out(sandbox, fresh_machine):
    home_a = fresh_machine("machine-a")
    remote = new_remote(sandbox)
    sandbox.runner.invoke(cli_module.cli, ["install", "--repo-url", str(remote), "--init"], input="n\n")
    (home_a / ".config/kitty").mkdir(parents=True)
    (home_a / ".config/kitty/kitty.conf").write_text("font_size 12\n")
    sandbox.run("add", "~/.config/kitty")

    home_b = fresh_machine("machine-b")
    result = sandbox.runner.invoke(cli_module.cli, ["install", "--repo-url", str(remote)], input="n\n")
    assert result.exit_code == 0, result.output
    sandbox.run("pull")

    assert (home_b / ".config/kitty/kitty.conf").read_text() == "font_size 12\n"


def test_install_can_set_up_the_service(sandbox, fresh_machine):
    home = fresh_machine("machine-a")
    remote = new_remote(sandbox)

    result = sandbox.runner.invoke(cli_module.cli, ["install", "--repo-url", str(remote), "--init"],
                                   input="y\n")

    assert result.exit_code == 0, result.output
    units = home / ".config/systemd/user"
    assert (units / "dotsync.service").exists() and (units / "dotsync.timer").exists()
    assert "--user enable --now dotsync.timer" in sandbox.systemctl_calls()


# ── service ────────────────────────────────────────────────────────────────────

def units(sandbox):
    return sandbox.home / ".config/systemd/user"


def test_service_install_writes_service_and_hourly_timer(sandbox):
    sandbox.run("service", "install")

    service = (units(sandbox) / "dotsync.service").read_text()
    timer = (units(sandbox) / "dotsync.timer").read_text()
    assert "-m dotsync sync" in service and "WantedBy=default.target" in service
    assert "OnUnitActiveSec=1h" in timer
    assert sandbox.systemctl_calls() == [
        "--user daemon-reload",
        "--user enable --now dotsync.service",
        "--user enable --now dotsync.timer",
    ]


def test_service_install_custom_interval_and_no_timer(sandbox):
    sandbox.run("service", "install", "--interval", "30min")
    assert "OnUnitActiveSec=30min" in (units(sandbox) / "dotsync.timer").read_text()

    sandbox.run("service", "install", "--no-timer")

    assert not (units(sandbox) / "dotsync.timer").exists()
    assert "--user disable --now dotsync.timer" in sandbox.systemctl_calls()


def test_service_install_rejects_a_bad_interval(sandbox):
    result = sandbox.run("service", "install", "--interval", "soon", ok=False)

    assert "Invalid interval" in result.output
    assert not units(sandbox).exists()


def test_service_uninstall_removes_both_units(sandbox):
    sandbox.run("service", "install")

    sandbox.run("service", "uninstall")

    assert not (units(sandbox) / "dotsync.service").exists()
    assert not (units(sandbox) / "dotsync.timer").exists()
    calls = sandbox.systemctl_calls()
    assert "--user disable --now dotsync.timer" in calls
    assert "--user disable --now dotsync.service" in calls


def test_service_status_asks_systemd_about_both_units(sandbox):
    sandbox.run("service", "status")

    assert sandbox.systemctl_calls() == ["--user status --no-pager dotsync.service dotsync.timer"]


def test_service_without_systemd_fails_cleanly(sandbox, monkeypatch):
    monkeypatch.setattr(dotsync.systemd.shutil, "which", lambda name: None)

    result = sandbox.run("service", "install", ok=False)

    assert "systemd isn't available" in result.output


# ── errors and small commands ─────────────────────────────────────────────────

def test_broken_repo_gives_a_message_not_a_traceback(sandbox):
    (sandbox.repo / "manifest.yaml").unlink()

    for args in (("sync",), ("status",), ("push",), ("add", "~/.x")):
        result = sandbox.run(*args, ok=False)
        assert "manifest.yaml not found" in result.output
        assert "Traceback" not in result.output


def test_allow_private_warns_that_permissions_are_not_kept(sandbox):
    sandbox.file(".netrc", "secret", mode=0o600)
    sandbox.file(".vimrc", "v")

    result = sandbox.run("add", "~/.netrc", "~/.vimrc", "--allow-private")

    assert "~/.netrc is private here" in result.output
    assert "~/.vimrc is private" not in result.output


def test_var_and_env_list(sandbox):
    sandbox.run("var", "set", "email=a@b", "--yaml", "n=3")
    sandbox.run("env", "set", "MAIL_TO={{ email }}")

    assert "email = a@b" in " ".join(sandbox.run("var", "list").output.split())
    assert "n = 3" in " ".join(sandbox.run("var", "list").output.split())
    env_out = sandbox.run("env", "list").output
    assert "MAIL_TO" in env_out and "{{ email }}" in env_out and "→ a@b" in env_out
