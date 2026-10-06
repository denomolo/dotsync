"""
Shared fixtures: every test gets an isolated syncdot world.

  remote.git   bare repo standing in for GitHub
  home/        fake $HOME the files live in
  repo/        syncdot's local clone (initialised like `syncdot install --init`)
  bin/         first on $PATH; holds a fake `systemctl` that only logs its args

Nothing touches the real home directory, dotfiles repo or systemd session.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from click.testing import CliRunner

import syncdot.__main__ as cli_module
import syncdot.config as config_module
from syncdot.__main__ import cli
from syncdot.repo import Repo


def git(cwd: Path, *args: str, env: dict | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                            env={**os.environ, **(env or {})})
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout


@dataclass
class Machine:
    """Another clone of the remote, for simulating edits made elsewhere."""
    path: Path

    def write(self, rel: str, text: str) -> None:
        f = self.path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)

    def commit_and_push(self, message: str = "edit elsewhere", when: int | None = None) -> None:
        git(self.path, "pull", "-q", "origin", "base")
        git(self.path, "add", "-A")
        env = {}
        if when is not None:
            env = {"GIT_AUTHOR_DATE": f"@{when} +0000", "GIT_COMMITTER_DATE": f"@{when} +0000"}
        git(self.path, "commit", "-qm", message, env=env)
        git(self.path, "push", "-q", "origin", "base")


@dataclass
class Sandbox:
    root: Path
    home: Path
    repo: Path
    remote: Path
    config_path: Path
    systemctl_log: Path
    runner: CliRunner = field(default_factory=CliRunner)

    def run(self, *args: str, ok: bool | None = True):
        """Run the syncdot CLI. ok=True expects exit 0, False expects failure, None checks nothing."""
        result = self.runner.invoke(cli, list(args), catch_exceptions=False)
        if ok is True:
            assert result.exit_code == 0, result.output
        elif ok is False:
            assert result.exit_code != 0, result.output
        return result

    def file(self, rel: str, text: str = "", mode: int | None = None) -> Path:
        """Create a file under the fake home."""
        f = self.home / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        if mode is not None:
            f.chmod(mode)
        return f

    def other_machine(self) -> Machine:
        path = self.root / f"other-{len(list(self.root.glob('other-*')))}"
        git(self.root, "clone", "-q", "-b", "base", str(self.remote), str(path))
        return Machine(path)

    def remote_file(self, rel: str, branch: str = "base") -> str:
        return git(self.remote, "show", f"{branch}:{rel}")

    def remote_log(self, branch: str = "base") -> list[str]:
        return git(self.remote, "log", "--format=%s", branch).splitlines()

    def repo_dirty(self) -> str:
        return git(self.repo, "status", "--porcelain")

    def systemctl_calls(self) -> list[str]:
        return self.systemctl_log.read_text().splitlines() if self.systemctl_log.exists() else []

    def write_config(self, **extra) -> None:
        lines = [f"repo_url: {self.remote}", "profile: base",
                 f"repo_path: {self.repo}", f"state_path: {self.root / 'state' / 'state.json'}"]
        lines += [f"{k}: {v}" for k, v in extra.items()]
        self.config_path.write_text("\n".join(lines) + "\n")


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sandbox:
    home, bin_dir = tmp_path / "home", tmp_path / "bin"
    home.mkdir()
    bin_dir.mkdir()
    log = tmp_path / "systemctl.log"
    fake = bin_dir / "systemctl"
    fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\n')
    fake.chmod(0o755)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    for var in ("GIT_CONFIG_SYSTEM", "GIT_CONFIG_GLOBAL"):
        monkeypatch.setenv(var, os.devnull)
    for var in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(var, "Test")
    for var in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(var, "test@example.com")
    monkeypatch.setenv("GIT_DEFAULT_BRANCH", "base")
    monkeypatch.chdir(home)

    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "base", str(remote))
    repo = tmp_path / "repo"
    Repo.init(repo, str(remote)).push("base")

    config_path = tmp_path / "config.yaml"
    # CONFIG_PATH is read at import time, so point both modules at the sandbox
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    monkeypatch.setattr(cli_module, "CONFIG_PATH", config_path)

    box = Sandbox(root=tmp_path, home=home, repo=repo, remote=remote,
                  config_path=config_path, systemctl_log=log)
    box.write_config()
    return box
