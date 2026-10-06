"""dotsync var and dotsync env."""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ENV_FILE = ".config/environment.d/99-env.conf"
GENERATOR = Path("/usr/lib/systemd/user-environment-generators/30-systemd-environment-d-generator")


def vars_text(sandbox) -> str:
    return (sandbox.repo / "vars.yaml").read_text()


def track_template(sandbox, text: str = "name={{ fullname }}\nemail={{ email }}\n") -> Path:
    other = sandbox.other_machine()
    other.write("files/.gitconfig.j2", text)
    other.commit_and_push("add template")
    sandbox.run("var", "set", "fullname=Old", "email=old@example.com")
    sandbox.run("checkout", ".gitconfig.j2", "--local", "~/.gitconfig")
    return sandbox.home / ".gitconfig"


# ── var ────────────────────────────────────────────────────────────────────────

def test_var_set_rerenders_templates_and_pushes(sandbox):
    gitconfig = track_template(sandbox)

    sandbox.run("var", "set", "fullname=Ariel Shatil")

    assert gitconfig.read_text() == "name=Ariel Shatil\nemail=old@example.com\n"
    assert sandbox.remote_log()[0] == "var: set fullname"
    assert "fullname: Ariel Shatil" in sandbox.remote_file("vars.yaml")


def test_var_set_keeps_comments_and_values_are_strings(sandbox):
    (sandbox.repo / "vars.yaml").write_text("# my vars\nemail: a@b   # personal\n# section\nport: 1\n")

    sandbox.run("var", "set", "email=new@b", "port=22", "debug=true")

    text = vars_text(sandbox)
    assert "# my vars\n" in text and "# section\n" in text
    assert "email: new@b   # personal\n" in text
    assert yaml.safe_load(text) == {"email": "new@b", "port": "22", "debug": "true"}


def test_var_set_yaml_parses_values(sandbox):
    sandbox.run("var", "set", "--yaml", "retries=3", "hosts=[a, b]", "on=true")

    assert yaml.safe_load(vars_text(sandbox)) == {"retries": 3, "hosts": ["a", "b"], "on": True}


def test_var_unset_refuses_while_a_template_uses_it(sandbox):
    track_template(sandbox)

    result = sandbox.run("var", "unset", "email", ok=False)
    assert "Still used" in result.output and ".gitconfig.j2" in result.output

    sandbox.run("var", "unset", "email", "--force", ok=False)   # template can't render now
    assert "email" not in yaml.safe_load(vars_text(sandbox))


def test_var_rejects_bad_names_and_the_env_section(sandbox):
    assert "Invalid variable name" in sandbox.run("var", "set", "1x=y", ok=False).output
    assert "dotsync env" in sandbox.run("var", "set", "env=x", ok=False).output
    assert "Not set" in sandbox.run("var", "unset", "nope", ok=False).output


def test_var_dry_run_changes_nothing(sandbox):
    before = vars_text(sandbox)

    sandbox.run("var", "set", "x=1", "--dry-run")

    assert vars_text(sandbox) == before


# ── env ────────────────────────────────────────────────────────────────────────

def source_env(path: Path) -> dict[str, str]:
    """Environment a POSIX shell gets from the line `dotsync env hook` prints."""
    out = subprocess.run(["sh", "-c", f'[ -r "{path}" ] && {{ set -a; . "{path}"; set +a; }}; env -0'],
                         capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}).stdout
    return dict(kv.split("=", 1) for kv in out.split("\0") if "=" in kv)


TRICKY = {"Q": 'say "hi"', "A": "it's", "B": "back\\slash", "T": "run `id` now", "SP": "two  spaces"}


def test_env_set_writes_one_file_shells_read(sandbox):
    sandbox.run("var", "set", "email=ariel@example.com")

    sandbox.run("env", "set", "EDITOR=nvim", "GIT_AUTHOR_EMAIL={{ email }}",
                *(f"{k}={v}" for k, v in TRICKY.items()))

    env = source_env(sandbox.home / ENV_FILE)
    assert env["EDITOR"] == "nvim"
    assert env["GIT_AUTHOR_EMAIL"] == "ariel@example.com"
    for k, v in TRICKY.items():
        assert env[k] == v, k          # quotes, backslashes, backticks: all literal
    assert yaml.safe_load(vars_text(sandbox))["env"]["GIT_AUTHOR_EMAIL"] == "{{ email }}"


@pytest.mark.skipif(not GENERATOR.exists(), reason="systemd environment.d generator not installed")
def test_env_file_parses_identically_in_systemd(sandbox):
    sandbox.run("env", "set", *(f"{k}={v}" for k, v in TRICKY.items()))

    gen = subprocess.run([str(GENERATOR)], capture_output=True, text=True,
                         env={"XDG_CONFIG_HOME": str(sandbox.home / ".config"), "PATH": "/usr/bin:/bin"}).stdout
    # The generator prints shell-quoted assignments; let a shell decode them
    out = subprocess.run(["sh", "-c", 'set -a; eval "$1"; set +a; env -0', "_", gen],
                         capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}).stdout
    parsed = dict(kv.split("=", 1) for kv in out.split("\0") if "=" in kv)
    for k, v in TRICKY.items():
        assert parsed[k] == v, k


def test_env_follows_template_variable_changes(sandbox):
    sandbox.run("var", "set", "email=old@example.com")
    sandbox.run("env", "set", "GIT_AUTHOR_EMAIL={{ email }}")

    sandbox.run("var", "set", "email=new@example.com")

    assert source_env(sandbox.home / ENV_FILE)["GIT_AUTHOR_EMAIL"] == "new@example.com"
    assert "Still used" in sandbox.run("var", "unset", "email", ok=False).output


def test_env_refuses_protected_names_and_dollar_values(sandbox):
    assert "managed by your session" in sandbox.run("env", "set", "PATH=/x", ok=False).output
    assert "managed by your session" in sandbox.run("env", "set", "LD_PRELOAD=x", ok=False).output
    assert "can't contain $" in sandbox.run("env", "set", "X=$HOME/x", ok=False).output
    assert not (sandbox.home / ENV_FILE).exists()


def test_env_updates_running_systemd_session(sandbox):
    sandbox.run("env", "set", "EDITOR=nvim", "PAGER=less")
    sandbox.run("env", "unset", "PAGER")

    calls = sandbox.systemctl_calls()
    assert "--user set-environment EDITOR=nvim PAGER=less" in calls
    assert "--user unset-environment PAGER" in calls


def test_env_file_is_never_tracked_in_a_tracked_environment_d(sandbox):
    sandbox.file(".config/environment.d/qt.conf", "QT_QPA_PLATFORMTHEME=qt6ct\n")
    sandbox.run("add", "~/.config/environment.d")

    sandbox.run("env", "set", "EDITOR=nvim")
    sandbox.run("sync")
    shutil.rmtree(sandbox.home / ".config/environment.d")
    sandbox.run("pull")

    tracked = subprocess.run(["git", "ls-files", "files/.config"], cwd=sandbox.repo,
                             capture_output=True, text=True).stdout.split()
    assert tracked == ["files/.config/environment.d/qt.conf"]
    assert (sandbox.home / ".config/environment.d/qt.conf").exists()
    assert source_env(sandbox.home / ENV_FILE)["EDITOR"] == "nvim"
    assert "already in sync" in sandbox.run("status").output


def test_env_unset_all_removes_the_file(sandbox):
    sandbox.run("env", "set", "EDITOR=nvim")

    sandbox.run("env", "unset", "EDITOR")

    assert not (sandbox.home / ENV_FILE).exists()


def test_env_hook_prints_the_source_line(sandbox):
    out = sandbox.run("env", "hook").output

    assert "set -a; . ~/" + ENV_FILE + "; set +a;" in out
