"""sync, push, pull and status: which side wins, and that every win is committed."""

import os
import re
import time

from conftest import git


def track(sandbox, rel: str, text: str, mode: int | None = None):
    f = sandbox.file(rel, text, mode)
    sandbox.run("add", f"~/{rel}")
    return f


def set_mtime(path, when: int) -> None:
    os.utime(path, (when, when))


def test_local_edit_is_pushed(sandbox):
    vimrc = track(sandbox, ".vimrc", "v1\n")
    vimrc.write_text("v2\n")

    result = sandbox.run("sync")

    assert "↑ 1 pushed" in result.output
    assert sandbox.remote_file("files/.vimrc") == "v2\n"
    assert sandbox.repo_dirty() == ""


def test_remote_edit_is_pulled(sandbox):
    vimrc = track(sandbox, ".vimrc", "v1\n")
    other = sandbox.other_machine()
    other.write("files/.vimrc", "from elsewhere\n")
    other.commit_and_push()

    result = sandbox.run("sync")

    assert "↓ 1 pulled" in result.output
    assert vimrc.read_text() == "from elsewhere\n"


def test_nothing_changed_is_in_sync(sandbox):
    track(sandbox, ".vimrc", "v1\n")

    assert "already in sync" in sandbox.run("sync").output


def test_last_write_wins_newer_local_edit_wins_and_is_pushed(sandbox):
    """Regression: the repo side used the clone's mtime, which `git pull`
    resets, so git always won and local edits were overwritten. And a
    machine-side win was left uncommitted in the clone."""
    sandbox.write_config(conflict_resolution="last-write-wins")
    vimrc = track(sandbox, ".vimrc", "v1\n")
    now = int(time.time())
    other = sandbox.other_machine()
    other.write("files/.vimrc", "remote edit\n")
    other.commit_and_push(when=now - 3600)
    vimrc.write_text("local edit\n")
    set_mtime(vimrc, now)

    result = sandbox.run("sync")

    assert "last-write-wins → machine" in result.output
    assert vimrc.read_text() == "local edit\n"
    assert sandbox.remote_file("files/.vimrc") == "local edit\n"
    assert sandbox.repo_dirty() == ""


def test_last_write_wins_newer_remote_edit_wins(sandbox):
    sandbox.write_config(conflict_resolution="last-write-wins")
    vimrc = track(sandbox, ".vimrc", "v1\n")
    now = int(time.time())
    vimrc.write_text("local edit\n")
    set_mtime(vimrc, now - 3600)
    other = sandbox.other_machine()
    other.write("files/.vimrc", "remote edit\n")
    other.commit_and_push(when=now)

    result = sandbox.run("sync")

    assert "last-write-wins → git" in result.output
    assert vimrc.read_text() == "remote edit\n"


def test_machine_wins_is_the_default_and_keeps_the_repo_version_in_history(sandbox):
    vimrc = track(sandbox, ".vimrc", "v1\n")
    other = sandbox.other_machine()
    other.write("files/.vimrc", "remote edit\n")
    other.commit_and_push(when=int(time.time()) + 3600)   # newer, but disk still wins
    vimrc.write_text("local edit\n")

    result = sandbox.run("sync")

    assert sandbox.remote_file("files/.vimrc") == "local edit\n"
    assert vimrc.read_text() == "local edit\n"
    sha = re.search(r"repo version kept in history at (\w+)", result.output).group(1)
    assert git(sandbox.repo, "show", f"{sha}:files/.vimrc") == "remote edit\n"


def test_git_wins_policy(sandbox):
    sandbox.write_config(conflict_resolution="git-wins")
    vimrc = track(sandbox, ".vimrc", "v1\n")
    other = sandbox.other_machine()
    other.write("files/.vimrc", "remote edit\n")
    other.commit_and_push()
    vimrc.write_text("local edit\n")

    sandbox.run("sync")

    assert vimrc.read_text() == "remote edit\n"


def test_status_reports_pending_without_changing_anything(sandbox):
    vimrc = track(sandbox, ".vimrc", "v1\n")
    vimrc.write_text("v2\n")
    before = sandbox.remote_log()

    result = sandbox.run("status")

    assert "↑ 1 to push" in result.output
    assert sandbox.remote_log() == before
    assert sandbox.remote_file("files/.vimrc") == "v1\n"


def test_dry_run_sync_changes_nothing(sandbox):
    vimrc = track(sandbox, ".vimrc", "v1\n")
    vimrc.write_text("v2\n")

    result = sandbox.run("sync", "--dry-run")

    assert "to push" in result.output
    assert sandbox.remote_file("files/.vimrc") == "v1\n"


def test_push_with_path_only_pushes_that_file(sandbox):
    vimrc = track(sandbox, ".vimrc", "v1\n")
    bashrc = track(sandbox, ".bashrc", "b1\n")
    vimrc.write_text("v2\n")
    bashrc.write_text("b2\n")

    sandbox.run("push", "~/.vimrc")

    assert sandbox.remote_file("files/.vimrc") == "v2\n"
    assert sandbox.remote_file("files/.bashrc") == "b1\n"
    assert sandbox.remote_log()[0] == "push: files/.vimrc"


def test_pull_glob_restores_files_missing_from_disk(sandbox):
    x = track(sandbox, ".config/foo/x.conf", "x\n")
    y = track(sandbox, ".config/foo/y.conf", "y\n")
    x.unlink()
    y.unlink()

    sandbox.run("pull", "~/.config/foo/*")

    assert x.read_text() == "x\n" and y.read_text() == "y\n"


def test_push_rejects_untracked_and_nested_paths(sandbox):
    sandbox.file(".config/env/a.conf", "a")
    sandbox.run("add", "~/.config/env")

    assert "not tracked" in sandbox.run("push", "~/.nope", ok=False).output
    assert "inside tracked directory" in sandbox.run("push", "~/.config/env/a.conf", ok=False).output


def test_pull_keeps_the_executable_bit(sandbox):
    """Regression: single files were rewritten as 644, so scripts lost +x."""
    script = track(sandbox, "bin/hello", "#!/bin/sh\necho hi\n", mode=0o755)
    plain = track(sandbox, ".bashrc", "b\n", mode=0o644)
    other = sandbox.other_machine()
    other.write("files/bin/hello", "#!/bin/sh\necho there\n")
    other.write("files/.bashrc", "b2\n")
    other.commit_and_push()

    sandbox.run("sync")

    assert script.stat().st_mode & 0o777 == 0o755
    assert plain.stat().st_mode & 0o777 == 0o644


def test_executable_bit_survives_a_fresh_pull_inside_directories(sandbox):
    track(sandbox, ".local/scripts/run", "#!/bin/sh\n", mode=0o755)
    track(sandbox, "bin/tool", "#!/bin/sh\n", mode=0o755)
    for f in (sandbox.home / ".local/scripts/run", sandbox.home / "bin/tool"):
        f.unlink()

    sandbox.run("pull")

    assert (sandbox.home / ".local/scripts/run").stat().st_mode & 0o111
    assert (sandbox.home / "bin/tool").stat().st_mode & 0o111


def test_binary_files_inside_directories_are_skipped(sandbox):
    sandbox.file(".config/app/conf", "c")
    sandbox.file(".config/app/blob.bin").write_bytes(b"\0\1\2")

    sandbox.run("add", "~/.config/app")

    assert (sandbox.repo / "files/.config/app/conf").exists()
    assert not (sandbox.repo / "files/.config/app/blob.bin").exists()
