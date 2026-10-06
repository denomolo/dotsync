"""--dry-run never changes the clone, and only one syncdot runs at a time."""

import subprocess
import sys
import time

import pytest

import syncdot.lock
from conftest import git


def snapshot(sandbox) -> dict:
    """Everything a dry run must leave alone in the local clone."""
    return {
        "branches": git(sandbox.repo, "for-each-ref", "--format=%(refname:short) %(objectname)",
                        "refs/heads"),
        "head": git(sandbox.repo, "symbolic-ref", "-q", "HEAD"),
        "status": sandbox.repo_dirty(),
        "worktrees": git(sandbox.repo, "worktree", "list", "--porcelain"),
    }


def test_dry_run_sync_previews_remote_changes_without_merging(sandbox):
    vimrc = sandbox.file(".vimrc", "v1\n")
    sandbox.run("add", "~/.vimrc")
    other = sandbox.other_machine()
    other.write("files/.vimrc", "from elsewhere\n")
    other.commit_and_push()
    before = snapshot(sandbox)

    result = sandbox.run("sync", "--dry-run")

    assert "↓ 1 to pull" in result.output          # the remote change is previewed
    assert snapshot(sandbox) == before              # ...but nothing was pulled
    assert vimrc.read_text() == "v1\n"

    sandbox.run("sync")
    assert vimrc.read_text() == "from elsewhere\n"


def test_dry_run_on_a_profile_does_not_merge_base(sandbox):
    sandbox.file(".vimrc", "v1\n")
    sandbox.run("add", "~/.vimrc")
    sandbox.run("profile", "new", "laptop")
    sandbox.run("profile", "set", "laptop")
    sandbox.run("sync")
    other = sandbox.other_machine()
    other.write("files/.vimrc", "base change\n")
    other.commit_and_push()
    before = snapshot(sandbox)
    remote_before = git(sandbox.remote, "for-each-ref")

    result = sandbox.run("sync", "--dry-run")

    assert "↓ 1 to pull" in result.output
    assert snapshot(sandbox) == before
    assert git(sandbox.remote, "for-each-ref") == remote_before   # no merge pushed


def test_dry_run_pull_and_var_set_leave_the_clone_alone(sandbox):
    sandbox.file(".vimrc", "v1\n")
    sandbox.run("add", "~/.vimrc")
    other = sandbox.other_machine()
    other.write("files/.vimrc", "remote\n")
    other.commit_and_push()
    before = snapshot(sandbox)

    sandbox.run("pull", "--dry-run")
    sandbox.run("var", "set", "x=1", "--dry-run")
    sandbox.run("env", "set", "EDITOR=nvim", "--dry-run")

    assert snapshot(sandbox) == before
    assert (sandbox.home / ".vimrc").read_text() == "v1\n"


def test_dry_run_reports_a_base_merge_conflict_without_starting_it(sandbox):
    sandbox.file(".vimrc", "v1\n")
    sandbox.run("add", "~/.vimrc")
    sandbox.run("profile", "new", "laptop")
    sandbox.run("profile", "set", "laptop")
    (sandbox.home / ".vimrc").write_text("laptop\n")
    sandbox.run("push")
    other = sandbox.other_machine()
    other.write("files/.vimrc", "base\n")
    other.commit_and_push()
    before = snapshot(sandbox)

    result = sandbox.run("sync", "--dry-run")

    assert "conflict" in result.output.lower()
    assert snapshot(sandbox) == before
    assert not (sandbox.repo / ".git" / "MERGE_HEAD").exists()


# ── locking ────────────────────────────────────────────────────────────────────

def hold_lock(path, seconds: float) -> subprocess.Popen:
    """Hold the syncdot lock from another process, like a running sync would."""
    code = (
        "import fcntl, os, sys, time\n"
        f"fd = os.open({str(path)!r}, os.O_RDWR | os.O_CREAT, 0o644)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "os.write(fd, str(os.getpid()).encode())\n"
        "print('locked', flush=True)\n"
        f"time.sleep({seconds})\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "locked"
    return proc


def lock_path(sandbox):
    return sandbox.root / "state" / "lock"


def test_command_gives_up_while_another_syncdot_holds_the_lock(sandbox, monkeypatch):
    monkeypatch.setattr(syncdot.lock, "LOCK_TIMEOUT", 0.5)
    sandbox.file(".vimrc", "v")
    lock_path(sandbox).parent.mkdir(parents=True, exist_ok=True)
    holder = hold_lock(lock_path(sandbox), seconds=30)
    try:
        result = sandbox.run("add", "~/.vimrc", ok=False)
    finally:
        holder.kill()
        holder.wait()

    assert f"Another syncdot is running (pid {holder.pid})" in result.output
    assert "gave up" in result.output
    assert not (sandbox.repo / "files/.vimrc").exists()


def test_command_waits_for_the_lock_then_runs(sandbox, monkeypatch):
    monkeypatch.setattr(syncdot.lock, "LOCK_TIMEOUT", 10)
    sandbox.file(".vimrc", "v")
    lock_path(sandbox).parent.mkdir(parents=True, exist_ok=True)
    holder = hold_lock(lock_path(sandbox), seconds=1)
    start = time.monotonic()
    try:
        result = sandbox.run("add", "~/.vimrc")
    finally:
        holder.wait()

    assert "waiting" in result.output
    assert time.monotonic() - start >= 0.5
    assert (sandbox.repo / "files/.vimrc").exists()


@pytest.mark.parametrize("args", [("status",), ("sync",), ("var", "list"), ("env", "list"),
                                  ("profile", "list")])
def test_every_repo_command_takes_the_lock(sandbox, monkeypatch, args):
    monkeypatch.setattr(syncdot.lock, "LOCK_TIMEOUT", 0.3)
    lock_path(sandbox).parent.mkdir(parents=True, exist_ok=True)
    holder = hold_lock(lock_path(sandbox), seconds=30)
    try:
        result = sandbox.run(*args, ok=False)
    finally:
        holder.kill()
        holder.wait()

    assert "Another syncdot is running" in result.output
