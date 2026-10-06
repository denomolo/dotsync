"""dotsync status [PATH...] [--diff]."""

import os


def track(sandbox, rel: str, text: str):
    f = sandbox.file(rel, text)
    sandbox.run("add", f"~/{rel}")
    return f


def test_status_of_a_single_file(sandbox):
    vimrc = track(sandbox, ".vimrc", "v1\n")
    bashrc = track(sandbox, ".bashrc", "b1\n")
    vimrc.write_text("v2\n")
    bashrc.write_text("b2\n")

    out = sandbox.run("status", "~/.vimrc").output

    assert "~/.vimrc" in out and "~/.bashrc" not in out
    assert "↑ 1 to push" in out


def test_status_glob_and_errors(sandbox):
    track(sandbox, ".config/foo/x.conf", "x")
    track(sandbox, ".config/foo/y.conf", "y")

    out = sandbox.run("status", "~/.config/foo/*").output
    assert "x.conf" in out and "y.conf" in out

    assert "not tracked" in sandbox.run("status", "~/.nope", ok=False).output


def test_status_diff_shows_repo_minus_and_local_plus(sandbox):
    vimrc = track(sandbox, ".vimrc", "set nu\nset ts=4\n")
    vimrc.write_text("set nu\nset ts=2\n")

    out = sandbox.run("status", "~/.vimrc", "--diff").output

    assert "--- repo: files/.vimrc" in out
    assert "+++ here: ~/.vimrc" in out
    assert "-set ts=4" in out and "+set ts=2" in out
    assert " set nu" in out


def test_status_diff_is_quiet_for_files_in_sync(sandbox):
    track(sandbox, ".vimrc", "v\n")

    out = sandbox.run("status", "--diff").output

    assert "@@" not in out and "---" not in out


def test_status_diff_of_a_directory_compares_file_by_file(sandbox):
    sandbox.file(".config/app/a.conf", "a1\n")
    sandbox.run("add", "~/.config/app")
    sandbox.file(".config/app/a.conf", "a2\n")
    sandbox.file(".config/app/new.conf", "n\n")

    out = sandbox.run("status", "~/.config/app", "--diff").output

    assert "--- repo: files/.config/app/a.conf" in out and "-a1" in out and "+a2" in out
    assert "--- /dev/null" in out and "+++ here: ~/.config/app/new.conf" in out


def test_status_diff_of_a_template_uses_the_rendered_version(sandbox):
    other = sandbox.other_machine()
    other.write("files/.gitconfig.j2", "email={{ email }}\n")
    other.write("vars.yaml", "email: a@example.com\n")
    other.commit_and_push()
    sandbox.run("checkout", ".gitconfig.j2", "--local", "~/.gitconfig")
    (sandbox.home / ".gitconfig").write_text("email=local@example.com\n")

    out = sandbox.run("status", "~/.gitconfig", "--diff").output

    assert "-email=a@example.com" in out and "+email=local@example.com" in out
    assert "{{" not in out


def test_status_diff_shows_files_not_set_up_here(sandbox):
    track(sandbox, ".bashrc", "repo version\n")
    os.remove(sandbox.root / "state" / "state.json")       # as on a machine that never synced it
    (sandbox.home / ".bashrc").write_text("distro default\n")

    out = sandbox.run("status", "--diff").output

    assert "not synced on this machine yet" in out
    assert "-repo version" in out and "+distro default" in out


def test_status_diff_reports_binary_files(sandbox):
    sandbox.write_config(include_binary="true")
    blob = sandbox.file("data.bin")
    blob.write_bytes(b"\0one")
    sandbox.run("add", "~/data.bin")
    blob.write_bytes(b"\0two")

    out = sandbox.run("status", "--diff").output

    assert "Binary files repo: files/data.bin and here: ~/data.bin differ" in out
