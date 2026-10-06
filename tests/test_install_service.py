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

    result = sandbox.runner.invoke(cli_module.cli, ["init", str(remote), "--new"],
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

    result = sandbox.runner.invoke(cli_module.cli, ["init", str(remote)], input="n\n")

    assert result.exit_code == 0, result.output
    assert "repo is empty" in result.output
    (home / ".vimrc").write_text("v\n")
    sandbox.run("add", "~/.vimrc")
    assert git(remote, "show", "base:files/.vimrc") == "v\n"


def test_second_machine_clones_and_checks_out(sandbox, fresh_machine):
    home_a = fresh_machine("machine-a")
    remote = new_remote(sandbox)
    sandbox.runner.invoke(cli_module.cli, ["init", str(remote), "--new"], input="n\n")
    (home_a / ".config/kitty").mkdir(parents=True)
    (home_a / ".config/kitty/kitty.conf").write_text("font_size 12\n")
    sandbox.run("add", "~/.config/kitty")

    home_b = fresh_machine("machine-b")
    result = sandbox.runner.invoke(cli_module.cli, ["init", str(remote)], input="n\n")
    assert result.exit_code == 0, result.output
    # Regression (0.6.0): the remote's default branch is `main`, which doesn't
    # exist, so nothing got checked out and install wrongly re-initialised it
    assert "repo is empty" not in result.output
    clone_b = home_b / ".local/share/dotsync/repo"
    assert git(clone_b, "rev-parse", "base") == git(remote, "rev-parse", "base")
    sandbox.run("pull")

    assert (home_b / ".config/kitty/kitty.conf").read_text() == "font_size 12\n"


def test_install_refuses_a_repo_that_is_not_a_dotsync_repo(sandbox, fresh_machine):
    fresh_machine("machine-a")
    remote = new_remote(sandbox, "other-project.git")
    work = sandbox.root / "other-project"
    git(sandbox.root, "clone", "-q", str(remote), str(work))
    (work / "README").write_text("not dotfiles\n")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "init")
    git(work, "push", "-q", "origin", "HEAD:main")

    result = sandbox.runner.invoke(cli_module.cli, ["init", str(remote)], input="n\n")

    assert result.exit_code != 0
    assert "doesn't look like a dotsync repo" in result.output
    assert git(remote, "for-each-ref", "--format=%(refname)").split() == ["refs/heads/main"]


def test_install_does_not_enable_autosync_by_default(sandbox, fresh_machine):
    home = fresh_machine("machine-a")
    remote = new_remote(sandbox)

    result = sandbox.runner.invoke(cli_module.cli, ["init", str(remote), "--new"],
                                   input="\n")   # just press Enter at the prompt

    assert result.exit_code == 0, result.output
    assert "[y/N]" in result.output
    assert not (home / ".config/systemd/user/dotsync.service").exists()
    assert sandbox.systemctl_calls() == []


def test_install_can_opt_in_to_sync_at_login(sandbox, fresh_machine):
    home = fresh_machine("machine-a")
    remote = new_remote(sandbox)

    result = sandbox.runner.invoke(cli_module.cli, ["init", str(remote), "--new"],
                                   input="y\n")

    assert result.exit_code == 0, result.output
    assert (home / ".config/systemd/user/dotsync.service").exists()
    assert "--user enable dotsync.service" in sandbox.systemctl_calls()


# ── service ────────────────────────────────────────────────────────────────────

def units(sandbox):
    return sandbox.home / ".config/systemd/user"


def test_service_install_enables_login_sync_only(sandbox):
    sandbox.run("service", "install")

    service = (units(sandbox) / "dotsync.service").read_text()
    assert "-m dotsync sync" in service and "WantedBy=default.target" in service
    assert not (units(sandbox) / "dotsync.timer").exists()
    # Enabled for the next login, not run right away
    assert sandbox.systemctl_calls() == ["--user daemon-reload", "--user enable dotsync.service"]


def test_service_install_and_uninstall_remove_a_timer_from_0_6_0(sandbox):
    units(sandbox).mkdir(parents=True)
    (units(sandbox) / "dotsync.timer").write_text("[Timer]\nOnUnitActiveSec=1h\n")

    out = sandbox.run("service", "install").output

    assert "periodic sync is no longer supported" in out
    assert not (units(sandbox) / "dotsync.timer").exists()
    assert "--user disable --now dotsync.timer" in sandbox.systemctl_calls()


def test_service_uninstall_removes_the_service(sandbox):
    sandbox.run("service", "install")

    sandbox.run("service", "uninstall")

    assert not (units(sandbox) / "dotsync.service").exists()
    assert "--user disable dotsync.service" in sandbox.systemctl_calls()


def test_service_status_asks_systemd(sandbox):
    sandbox.run("service", "status")

    assert sandbox.systemctl_calls() == ["--user status --no-pager dotsync.service"]


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
