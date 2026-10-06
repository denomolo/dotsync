"""add, checkout and remove: what gets tracked, where, and what is refused."""

import shutil

import yaml


def manifest(sandbox) -> list[dict]:
    return yaml.safe_load((sandbox.repo / "manifest.yaml").read_text())["files"]


def test_add_keeps_home_relative_hierarchy(sandbox):
    sandbox.file(".config/environment.d/10-a.conf", "A=1\n")
    sandbox.file(".vimrc", "set nu\n")

    sandbox.run("add", "~/.config/environment.d", "~/.vimrc")

    assert {e["source"]: e["dest"] for e in manifest(sandbox)} == {
        "files/.config/environment.d": "~/.config/environment.d",
        "files/.vimrc": "~/.vimrc",
    }
    assert sandbox.remote_file("files/.config/environment.d/10-a.conf") == "A=1\n"
    # Everything from one `add` lands in a single commit
    assert sandbox.remote_log()[0] == "add: 2 files"


def test_add_expands_quoted_globs_and_drops_duplicates(sandbox):
    sandbox.file(".config/foo/x.conf", "x")
    sandbox.file(".config/foo/y.conf", "y")

    sandbox.run("add", "~/.config/foo/*.conf", "~/.config/foo/x.conf")

    assert sorted(e["source"] for e in manifest(sandbox)) == [
        "files/.config/foo/x.conf", "files/.config/foo/y.conf"]


def test_add_remote_directory_keeps_names(sandbox):
    sandbox.file("a.conf", "a")
    sandbox.file("b.conf", "b")

    sandbox.run("add", "~/a.conf", "~/b.conf", "--remote", "misc")

    assert sorted(e["source"] for e in manifest(sandbox)) == ["files/misc/a.conf", "files/misc/b.conf"]


def test_add_reports_bad_paths_and_adds_the_rest(sandbox, tmp_path):
    sandbox.file(".good", "ok")
    sandbox.file("bin.dat").write_bytes(b"\0binary")
    sandbox.file(".netrc", "machine x password y", mode=0o600)
    sandbox.file("a/.dup", "1")
    sandbox.file("b/.dup", "2")
    outside = tmp_path / "outside.conf"
    outside.write_text("o")

    result = sandbox.run("add", "~/.good", "~/bin.dat", "~/.netrc", str(outside),
                         "~/a/.dup", "~/b/.dup", "--remote", "x/", ok=False)

    out = result.output
    assert "binary file" in out
    assert "is private (mode 600)" in out and "--allow-private" in out
    assert "would also be used by" in out
    assert {e["source"] for e in manifest(sandbox)} == {
        "files/x/.good", "files/x/outside.conf", "files/x/.dup"}


def test_add_outside_home_needs_remote(sandbox, tmp_path):
    outside = tmp_path / "outside.conf"
    outside.write_text("o")

    result = sandbox.run("add", str(outside), ok=False)

    assert "outside your home directory" in result.output
    assert not manifest(sandbox)


def test_add_refuses_private_directories_and_their_contents(sandbox):
    sandbox.file(".ssh/config", "Host x")
    (sandbox.home / ".ssh").chmod(0o700)
    sandbox.file(".config/app/conf", "c")
    sandbox.file(".config/app/token", "t", mode=0o600)

    result = sandbox.run("add", "~/.ssh", "~/.config/app", ok=False)

    assert "is private (mode 700)" in result.output
    assert "contains private file" in result.output
    sandbox.run("add", "~/.config/app", "--allow-private")
    assert sandbox.remote_file("files/.config/app/token") == "t"


def test_add_twice_is_refused(sandbox):
    sandbox.file(".vimrc", "v")
    sandbox.run("add", "~/.vimrc")

    result = sandbox.run("add", "~/.vimrc", ok=False)

    assert "already tracked" in result.output


def test_checkout_is_the_reverse_of_add(sandbox):
    sandbox.file(".config/kitty/kitty.conf", "font_size 12\n")
    sandbox.run("add", "~/.config/kitty")
    # New machine: same repo, nothing on disk, no manifest entries yet
    shutil.rmtree(sandbox.home / ".config/kitty")
    (sandbox.repo / "manifest.yaml").write_text("files: []\n")

    sandbox.run("checkout", ".config/kitty")

    assert (sandbox.home / ".config/kitty/kitty.conf").read_text() == "font_size 12\n"
    assert manifest(sandbox) == [{"source": "files/.config/kitty", "dest": "~/.config/kitty"}]


def test_checkout_refuses_to_overwrite_a_different_local_file(sandbox):
    other = sandbox.other_machine()
    other.write("files/.bashrc", "from repo\n")
    other.commit_and_push()
    local = sandbox.file(".bashrc", "local edits\n")

    result = sandbox.run("checkout", ".bashrc", ok=False)
    assert "--force" in result.output
    assert local.read_text() == "local edits\n"

    sandbox.run("checkout", ".bashrc", "--force")
    assert local.read_text() == "from repo\n"


def test_remove_untracks_but_keeps_disk_copy(sandbox):
    vimrc = sandbox.file(".vimrc", "v")
    sandbox.run("add", "~/.vimrc")

    sandbox.run("remove", "~/.vimrc")

    assert vimrc.read_text() == "v"
    assert not (sandbox.repo / "files/.vimrc").exists()
    assert not manifest(sandbox)
    assert sandbox.remote_log()[0] == "remove: files/.vimrc"
