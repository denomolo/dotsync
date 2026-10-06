"""Carrying an existing setup over from dotsync (syncdot's name up to 0.8.0)."""

import yaml

import syncdot.__main__ as cli_module
from conftest import git
from syncdot.repo import Repo
from test_install_service import fresh_machine, new_remote  # noqa: F401  (fixture)


def old_setup(sandbox, home, remote, extra_config: dict | None = None):
    """A machine set up by dotsync 0.8: explicit default paths in the config, like `install` wrote."""
    old_repo = home / ".local/share/dotsync/repo"
    Repo.init(old_repo, str(remote)).push("base")
    (home / ".local/state/dotsync").mkdir(parents=True)
    (home / ".local/state/dotsync/state.json").write_text('{"profile": "base", "files": {}}')
    config = {"repo_url": str(remote), "profile": "base", "conflict_resolution": "machine-wins",
              "repo_path": str(old_repo), "state_path": str(home / ".local/state/dotsync/state.json"),
              **(extra_config or {})}
    (home / ".config/dotsync").mkdir(parents=True)
    (home / ".config/dotsync/config.yaml").write_text(yaml.dump(config))


def test_first_command_moves_a_dotsync_setup_over(sandbox, fresh_machine):
    home = fresh_machine("machine-a")
    remote = new_remote(sandbox)
    old_setup(sandbox, home, remote)
    (home / ".vimrc").write_text("set nu\n")

    result = sandbox.runner.invoke(cli_module.cli, ["add", "~/.vimrc"])

    assert result.exit_code == 0, result.output
    assert "syncdot was called dotsync" in result.output
    assert not (home / ".config/dotsync").exists()
    assert not (home / ".local/share/dotsync").exists() and not (home / ".local/state/dotsync").exists()
    config = yaml.safe_load((home / ".config/syncdot/config.yaml").read_text())
    assert config["repo_path"] == str(home / ".local/share/syncdot/repo")
    assert config["state_path"] == str(home / ".local/state/syncdot/state.json")
    assert config["conflict_resolution"] == "machine-wins"          # settings kept as they were
    assert git(remote, "show", "base:files/.vimrc") == "set nu\n"     # the moved clone still works

    again = sandbox.runner.invoke(cli_module.cli, ["status", "--no-fetch"])
    assert "dotsync" not in again.output                              # only happens once


def test_custom_paths_outside_the_old_defaults_are_left_alone(sandbox, fresh_machine):
    home = fresh_machine("machine-a")
    remote = new_remote(sandbox)
    custom = sandbox.root / "elsewhere" / "repo"
    Repo.init(custom, str(remote)).push("base")
    old_setup(sandbox, home, new_remote(sandbox, "unused.git"), {"repo_path": str(custom)})

    sandbox.runner.invoke(cli_module.cli, ["status", "--no-fetch"])

    config = yaml.safe_load((home / ".config/syncdot/config.yaml").read_text())
    assert config["repo_path"] == str(custom)
    assert custom.exists()


def test_old_login_service_gets_a_hint(sandbox, fresh_machine):
    home = fresh_machine("machine-a")
    old_setup(sandbox, home, new_remote(sandbox))
    units = home / ".config/systemd/user"
    units.mkdir(parents=True)
    (units / "dotsync.service").write_text("[Service]\nExecStart=python -m dotsync sync\n")

    result = sandbox.runner.invoke(cli_module.cli, ["status", "--no-fetch"])

    assert "run `syncdot service install` to replace it" in result.output


def test_nothing_happens_when_syncdot_is_already_set_up(sandbox, fresh_machine):
    home = fresh_machine("machine-a")
    old_setup(sandbox, home, new_remote(sandbox))
    repo = home / ".local/share/syncdot/repo"
    Repo.init(repo, str(new_remote(sandbox, "fresh.git")))
    (home / ".config/syncdot").mkdir(parents=True)
    (home / ".config/syncdot/config.yaml").write_text(f"profile: base\nrepo_path: {repo}\n")

    result = sandbox.runner.invoke(cli_module.cli, ["status", "--no-fetch"])

    assert "dotsync" not in result.output
    assert (home / ".config/dotsync/config.yaml").exists()            # left for the user to deal with
