"""Profiles (branches) and .j2 templates."""

from conftest import git


def add_template(sandbox, source: str, dest: str, text: str, vars_yaml: str = "") -> None:
    """Put a template straight into the repo (as on another machine) and track it."""
    other = sandbox.other_machine()
    other.write(f"files/{source}", text)
    if vars_yaml:
        other.write("vars.yaml", vars_yaml)
    other.commit_and_push("add template")
    sandbox.run("checkout", source, "--local", dest)


def test_base_profile_uses_the_base_branch(sandbox):
    """Regression: profile `base` pointed at a nonexistent `profiles/base` branch."""
    sandbox.file(".vimrc", "v")

    sandbox.run("add", "~/.vimrc")

    assert sandbox.remote_file("files/.vimrc", branch="base") == "v"


def test_profile_list_includes_base(sandbox):
    sandbox.run("profile", "new", "laptop")

    out = sandbox.run("profile", "list").output

    assert "base" in out and "laptop" in out


def test_base_changes_merge_into_profile_and_profile_overrides_stick(sandbox):
    add_template(sandbox, ".gitconfig.j2", "~/.gitconfig", "email={{ email }}\n",
                 vars_yaml="email: base@example.com\n")
    sandbox.file(".vimrc", "v1\n")
    sandbox.run("add", "~/.vimrc")
    sandbox.run("profile", "new", "laptop")
    sandbox.run("profile", "set", "laptop")
    sandbox.run("var", "set", "email=laptop@example.com")

    other = sandbox.other_machine()
    other.write("files/.vimrc", "v2 from base\n")
    other.commit_and_push("base change")
    sandbox.run("sync")

    assert (sandbox.home / ".vimrc").read_text() == "v2 from base\n"
    assert (sandbox.home / ".gitconfig").read_text() == "email=laptop@example.com\n"
    assert sandbox.remote_file("vars.yaml", branch="base").strip() == "email: base@example.com"


def test_conflicting_base_merge_is_aborted_cleanly(sandbox):
    sandbox.file(".vimrc", "v1\n")
    sandbox.run("add", "~/.vimrc")
    sandbox.run("profile", "new", "laptop")
    sandbox.run("profile", "set", "laptop")
    (sandbox.home / ".vimrc").write_text("laptop line\n")
    sandbox.run("push")

    other = sandbox.other_machine()
    other.write("files/.vimrc", "base line\n")
    other.commit_and_push()
    result = sandbox.run("sync")

    assert "Could not merge base" in result.output
    assert sandbox.repo_dirty() == ""
    assert (sandbox.home / ".vimrc").read_text() == "laptop line\n"
    assert not (sandbox.repo / ".git" / "MERGE_HEAD").exists()
    assert git(sandbox.repo, "branch", "--show-current").strip() == "profiles/laptop"


def test_template_is_rendered_on_the_way_to_disk(sandbox):
    add_template(sandbox, ".gitconfig.j2", "~/.gitconfig", "[user]\n  email = {{ email }}\n",
                 vars_yaml="email: a@example.com\n")

    assert (sandbox.home / ".gitconfig").read_text() == "[user]\n  email = a@example.com\n"


def test_local_edit_to_rendered_file_never_overwrites_the_template(sandbox):
    """Regression: pushing a rendered file replaced {{ variables }} in the template."""
    add_template(sandbox, ".gitconfig.j2", "~/.gitconfig", "email={{ email }}\n",
                 vars_yaml="email: a@example.com\n")
    rendered = sandbox.home / ".gitconfig"
    rendered.write_text("email=a@example.com\nlocal=1\n")

    for cmd in (("sync",), ("push",), ("push", "~/.gitconfig")):
        result = sandbox.run(*cmd, ok=None)
        assert "edit the template in the repo" in result.output
    assert sandbox.remote_file("files/.gitconfig.j2") == "email={{ email }}\n"
    assert rendered.read_text().endswith("local=1\n")

    sandbox.run("pull", "~/.gitconfig")
    assert rendered.read_text() == "email=a@example.com\n"


def test_unchanged_template_is_left_alone_by_push(sandbox):
    add_template(sandbox, ".gitconfig.j2", "~/.gitconfig", "email={{ email }}\n",
                 vars_yaml="email: a@example.com\n")

    result = sandbox.run("push")

    assert "ERROR" not in result.output
    assert sandbox.remote_file("files/.gitconfig.j2") == "email={{ email }}\n"
