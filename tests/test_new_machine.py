"""A machine joining an existing repo: install and sync never change files by
themselves; pull, push and checkout do, explicitly."""

import pytest

import dotsync.__main__ as cli_module
from conftest import git
from test_install_service import fresh_machine, new_remote  # noqa: F401  (fixture)


@pytest.fixture
def joined(sandbox, fresh_machine):
    """Machine A tracks .bashrc, .vimrc and a directory; machine B has just run
    `install` against the same repo. Returns machine B's home."""
    home_a = fresh_machine("machine-a")
    remote = new_remote(sandbox)
    sandbox.runner.invoke(cli_module.cli, ["install", "--repo-url", str(remote), "--init"], input="n\n")
    (home_a / ".bashrc").write_text("my bashrc\n")
    (home_a / ".vimrc").write_text("set nu\n")
    (home_a / ".config/kitty").mkdir(parents=True)
    (home_a / ".config/kitty/kitty.conf").write_text("font_size 12\n")
    sandbox.run("add", "~/.bashrc", "~/.vimrc", "~/.config/kitty")

    home_b = fresh_machine("machine-b")
    (home_b / ".bashrc").write_text("distro default\n")       # differs from the repo
    (home_b / ".vimrc").write_text("set nu\n")                # already identical
    result = sandbox.runner.invoke(cli_module.cli, ["install", "--repo-url", str(remote)], input="n\n")
    assert result.exit_code == 0, result.output
    sandbox.remote = remote
    return home_b


def home_files(home) -> dict:
    return {str(p.relative_to(home)): p.read_text() for p in home.rglob("*")
            if p.is_file() and ".local/share/dotsync" not in str(p) and ".config/dotsync" not in str(p)}


def test_install_changes_nothing_on_disk(sandbox, fresh_machine):
    home = fresh_machine("machine-x")
    (home / ".bashrc").write_text("mine\n")
    before = home_files(home)

    result = sandbox.runner.invoke(cli_module.cli, ["install", "--repo-url", str(new_remote(sandbox, "x.git")),
                                                    "--init"], input="n\n")

    assert result.exit_code == 0, result.output
    assert home_files(home) == before
    assert "Nothing on disk was changed" in result.output


def test_sync_leaves_never_synced_files_alone(sandbox, joined):
    before_remote = git(sandbox.remote, "rev-parse", "base")

    result = sandbox.run("sync")

    assert (joined / ".bashrc").read_text() == "distro default\n"       # not overwritten
    assert not (joined / ".config/kitty").exists()                       # not created
    assert git(sandbox.remote, "rev-parse", "base") == before_remote     # nothing pushed
    assert result.output.count("[skipped: not synced on this machine yet]") == 2
    assert "`dotsync pull <path>`" in result.output


def test_identical_files_start_syncing_without_any_change(sandbox, joined):
    sandbox.run("sync")
    (joined / ".vimrc").write_text("set nu\nset rnu\n")

    sandbox.run("sync")

    assert git(sandbox.remote, "show", "base:files/.vimrc") == "set nu\nset rnu\n"


def test_status_shows_what_still_needs_pull_or_push(sandbox, joined):
    out = sandbox.run("status").output

    assert "~/.bashrc" in out and "~/.config/kitty" in out
    assert out.count("[skipped: not synced on this machine yet]") == 2


def test_pull_or_push_sets_a_file_up_then_sync_takes_over(sandbox, joined):
    sandbox.run("pull", "~/.config/kitty")
    sandbox.run("push", "~/.bashrc")     # keep this machine's version

    assert (joined / ".config/kitty/kitty.conf").read_text() == "font_size 12\n"
    assert git(sandbox.remote, "show", "base:files/.bashrc") == "distro default\n"
    result = sandbox.run("sync")
    assert "not synced on this machine yet" not in result.output
    assert "already in sync" in result.output


def test_files_added_on_another_machine_wait_for_pull(sandbox, joined):
    sandbox.run("pull")
    other = sandbox.other_machine()
    other.write("files/.tmux.conf", "set -g mouse on\n")
    manifest = (other.path / "manifest.yaml").read_text()
    other.write("manifest.yaml", manifest + "\n  - source: files/.tmux.conf\n    dest: ~/.tmux.conf\n")
    other.commit_and_push("add tmux")

    sandbox.run("sync")
    assert not (joined / ".tmux.conf").exists()

    sandbox.run("pull", "~/.tmux.conf")
    assert (joined / ".tmux.conf").read_text() == "set -g mouse on\n"
