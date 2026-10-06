"""Interface decisions made in the 0.8.0 review: init, remove PATH..., policy
names, config without repo_url, status fetching, pull confirmation, exit codes."""

import pytest
import yaml

import dotsync.__main__ as cli_module
from conftest import git
from test_install_service import fresh_machine, new_remote  # noqa: F401  (fixture)


# ── init ───────────────────────────────────────────────────────────────────────

def test_init_takes_the_repo_as_an_argument(sandbox, fresh_machine):
    home = fresh_machine("machine-a")
    remote = new_remote(sandbox)

    result = sandbox.runner.invoke(cli_module.cli, ["init", str(remote)], input="n\n")

    assert result.exit_code == 0, result.output
    assert "repo is empty" in result.output
    config = yaml.safe_load((home / ".config/dotsync/config.yaml").read_text())
    assert "repo_url" not in config and config["conflict_resolution"] == "local-wins"


def test_init_new_works_before_the_remote_exists(sandbox, fresh_machine):
    home = fresh_machine("machine-a")
    remote = sandbox.root / "not-created-yet.git"

    result = sandbox.runner.invoke(cli_module.cli, ["init", str(remote), "--new"], input="n\n")

    assert result.exit_code == 0, result.output
    assert (home / ".local/share/dotsync/repo/manifest.yaml").exists()


def test_init_suggests_new_when_the_repo_cannot_be_cloned(sandbox, fresh_machine):
    fresh_machine("machine-a")

    result = sandbox.runner.invoke(cli_module.cli, ["init", str(sandbox.root / "missing.git")], input="n\n")

    assert result.exit_code == 1
    assert "--new" in result.output


def test_install_still_works_as_an_alias(sandbox, fresh_machine):
    fresh_machine("machine-a")
    remote = new_remote(sandbox)

    result = sandbox.runner.invoke(cli_module.cli, ["install", "--repo-url", str(remote), "--init"],
                                   input="n\n")

    assert result.exit_code == 0, result.output
    assert "`dotsync install` is now `dotsync init <repo> --new`" in result.output
    assert "install" not in sandbox.run("--help").output.split("Commands:")[1]


def test_bare_dotsync_shows_help():
    from click.testing import CliRunner
    result = CliRunner().invoke(cli_module.cli, [])

    assert "Commands:" in result.output and "Syncing" not in result.output


# ── config ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("old, new", [("machine-wins", "local-wins"), ("git-wins", "repo-wins"),
                                      ("last-write-wins", "newer-wins")])
def test_old_policy_names_still_load(sandbox, old, new):
    sandbox.write_config(conflict_resolution=old)

    from dotsync.config import Config
    assert Config.load().conflict_resolution == new


def test_config_without_repo_url_and_with_a_stale_one_both_load(sandbox):
    from dotsync.config import Config
    sandbox.config_path.write_text(sandbox.config_path.read_text().replace(
        f"repo_url: {sandbox.remote}", "repo_url: git@example.com:moved/elsewhere.git"))
    assert Config.load().profile == "base"
    sandbox.config_path.write_text("\n".join(l for l in sandbox.config_path.read_text().splitlines()
                                             if not l.startswith("repo_url")))
    assert Config.load().profile == "base"


def test_invalid_policy_gives_a_clear_message(sandbox):
    sandbox.write_config(conflict_resolution="coin-flip")

    out = sandbox.run("status", ok=False).output

    assert "use local-wins, repo-wins or newer-wins" in out


# ── remove ─────────────────────────────────────────────────────────────────────

def test_remove_several_paths_and_globs_in_one_commit(sandbox):
    for rel in (".vimrc", ".config/foo/x.conf", ".config/foo/y.conf"):
        sandbox.file(rel, "x")
    sandbox.run("add", "~/.vimrc", "~/.config/foo/x.conf", "~/.config/foo/y.conf")
    (sandbox.home / ".config/foo/y.conf").unlink()        # already gone from disk is fine

    sandbox.run("remove", "~/.vimrc", "~/.config/foo/*")

    assert not yaml.safe_load((sandbox.repo / "manifest.yaml").read_text())["files"]
    assert sandbox.remote_log()[0] == "remove: 3 files"
    assert (sandbox.home / ".vimrc").exists()
    assert "not tracked" in sandbox.run("remove", "~/.nope", ok=False).output


# ── status fetches ─────────────────────────────────────────────────────────────

def remote_edit(sandbox, rel="files/.vimrc", text="from elsewhere\n"):
    other = sandbox.other_machine()
    other.write(rel, text)
    other.commit_and_push()


def test_status_shows_changes_waiting_on_the_remote(sandbox):
    """Regression: status only compared with the local clone, so it said
    'already in sync' while GitHub had changes."""
    sandbox.file(".vimrc", "v1\n")
    sandbox.run("add", "~/.vimrc")
    remote_edit(sandbox)
    head_before = git(sandbox.repo, "rev-parse", "HEAD")

    out = sandbox.run("status", "--diff").output

    assert "↓ 1 to pull" in out and "+v1" in out and "-from elsewhere" in out
    assert git(sandbox.repo, "rev-parse", "HEAD") == head_before      # nothing merged
    assert "already in sync" in sandbox.run("status", "--no-fetch").output


def test_status_offline_warns_and_shows_the_local_view(sandbox):
    sandbox.file(".vimrc", "v1\n")
    sandbox.run("add", "~/.vimrc")
    git(sandbox.repo, "remote", "set-url", "origin", str(sandbox.root / "unreachable.git"))

    out = sandbox.run("status").output

    assert "Could not fetch" in out and "already in sync" in out


def test_dry_run_and_status_see_uncommitted_edits_in_the_clone(sandbox):
    other = sandbox.other_machine()
    other.write("files/.gitconfig.j2", "email={{ email }}\n")
    other.write("vars.yaml", "email: old@example.com\n")
    other.commit_and_push()
    sandbox.run("checkout", ".gitconfig.j2", "--local", "~/.gitconfig")
    (sandbox.repo / "vars.yaml").write_text("email: new@example.com\n")   # hand edit, not committed

    out = sandbox.run("sync", "--dry-run").output

    assert "↓ 1 to pull" in out
    assert (sandbox.home / ".gitconfig").read_text() == "email=old@example.com\n"
    assert sandbox.repo_dirty().strip() == "M vars.yaml"


def test_status_exit_code(sandbox):
    vimrc = sandbox.file(".vimrc", "v1\n")
    sandbox.run("add", "~/.vimrc")
    assert sandbox.run("status", "--exit-code").exit_code == 0

    vimrc.write_text("v2\n")

    assert sandbox.run("status", "--exit-code", ok=None).exit_code == cli_module.EXIT_PENDING
    assert sandbox.run("status").exit_code == 0


# ── pull asks before overwriting local changes ─────────────────────────────────

def edited_and_stale(sandbox):
    """.vimrc edited locally; .bashrc merely out of date (changed on the remote)."""
    vimrc = sandbox.file(".vimrc", "v1\n")
    bashrc = sandbox.file(".bashrc", "b1\n")
    sandbox.run("add", "~/.vimrc", "~/.bashrc")
    vimrc.write_text("my unsynced edit\n")
    remote_edit(sandbox, "files/.bashrc", "b2\n")
    return vimrc, bashrc


def test_pull_asks_and_declining_changes_nothing(sandbox):
    vimrc, bashrc = edited_and_stale(sandbox)

    result = sandbox.runner.invoke(cli_module.cli, ["pull"], input="n\n")

    assert result.exit_code == 1
    assert "~/.vimrc" in result.output and "~/.bashrc" not in result.output.split("Overwrite")[0]
    assert vimrc.read_text() == "my unsynced edit\n"
    assert bashrc.read_text() == "b1\n"


def test_pull_overwrites_after_confirming_or_with_yes(sandbox):
    vimrc, bashrc = edited_and_stale(sandbox)

    result = sandbox.runner.invoke(cli_module.cli, ["pull"], input="y\n")

    assert result.exit_code == 0, result.output
    assert vimrc.read_text() == "v1\n" and bashrc.read_text() == "b2\n"
    assert "[overwrites local changes]" in result.output


def test_pull_without_local_changes_does_not_ask(sandbox):
    sandbox.file(".bashrc", "b1\n")
    sandbox.run("add", "~/.bashrc")
    remote_edit(sandbox, "files/.bashrc", "b2\n")

    result = sandbox.runner.invoke(cli_module.cli, ["pull"], input="")

    assert result.exit_code == 0, result.output
    assert "Overwrite" not in result.output


def test_pull_in_a_script_without_yes_refuses(sandbox):
    edited_and_stale(sandbox)

    result = sandbox.runner.invoke(cli_module.cli, ["pull"], input="")   # stdin closed

    assert result.exit_code == 1
    assert "--yes" in result.output


def test_pull_dry_run_marks_what_it_would_overwrite_without_asking(sandbox):
    vimrc, _ = edited_and_stale(sandbox)

    result = sandbox.run("pull", "--dry-run")

    assert "[overwrites local changes]" in result.output and "Overwrite them?" not in result.output
    assert vimrc.read_text() == "my unsynced edit\n"


# ── exit codes ─────────────────────────────────────────────────────────────────

def test_sync_and_push_exit_1_when_a_file_fails(sandbox):
    other = sandbox.other_machine()
    other.write("files/.gitconfig.j2", "email={{ email }}\n")
    other.write("vars.yaml", "email: a@example.com\n")
    other.commit_and_push()
    sandbox.run("checkout", ".gitconfig.j2", "--local", "~/.gitconfig")
    (sandbox.home / ".gitconfig").write_text("edited\n")

    assert sandbox.run("sync", ok=None).exit_code == 1
    assert sandbox.run("push", ok=None).exit_code == 1
