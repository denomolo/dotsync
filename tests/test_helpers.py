"""Unit tests for pure helpers."""

import os
from pathlib import Path

import pytest
import yaml

from dotsync.__main__ import expand_paths
from dotsync.config import profile_branch
from dotsync.repo import normalize_github_url
from dotsync.sync import _env_edit, _vars_set, _vars_unset, _yaml_entry


@pytest.mark.parametrize("url, expected", [
    ("https://github.com/me/dots", "git@github.com:me/dots.git"),
    ("https://github.com/me/dots.git/", "git@github.com:me/dots.git"),
    ("me/dots", "git@github.com:me/dots.git"),
    ("git@github.com:me/dots.git", "git@github.com:me/dots.git"),
    ("https://gitlab.com/me/dots", "https://gitlab.com/me/dots"),
    ("/srv/git/dots.git", "/srv/git/dots.git"),
])
def test_normalize_github_url(url, expected):
    assert normalize_github_url(url) == expected


def test_profile_branch():
    assert profile_branch("base") == "base"
    assert profile_branch("laptop") == "profiles/laptop"


def test_expand_paths_globs_tilde_and_dedupes(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("a.conf", "b.conf", "c.txt"):
        (tmp_path / name).write_text("")

    got = expand_paths(("~/*.conf", str(tmp_path / "a.conf"), "~/c.txt"))

    assert got == [tmp_path / "a.conf", tmp_path / "b.conf", tmp_path / "c.txt"]
    with pytest.raises(ValueError, match="No files match"):
        expand_paths(("~/*.nope",))


@pytest.mark.parametrize("value, line", [
    ("Ariel Shatil", "n: Ariel Shatil\n"),
    ("true", "n: 'true'\n"),            # strings stay strings
    ("22", "n: '22'\n"),
    ("a: b", "n: 'a: b'\n"),
    (3, "n: 3\n"),
    (["a", "b"], "n: [a, b]\n"),
])
def test_yaml_entry(value, line):
    assert _yaml_entry("n", value) == line
    assert yaml.safe_load(line) == {"n": value}


@pytest.mark.parametrize("name", ["on", "off", "yes", "no", "y", "N", "true", "null"])
def test_yaml_entry_quotes_keys_yaml_would_misread(name):
    """Regression: `var set on=1` failed because YAML read the key as a boolean."""
    assert yaml.safe_load(_yaml_entry(name, "1")) == {name: "1"}


def test_vars_set_and_unset_keep_comments_and_order():
    text = ("# head\nemail: a@b   # personal\n# editor settings\neditor: vim\n"
            "hosts:\n  - a\n  - b\n\nurl: \"http://x/#f\"  # site\nenv:\n  EDITOR: nvim\n")

    text = _vars_set(text, {"email": "new@x", "url": "http://y/#z", "hosts": ["c"], "fullname": "F"})
    text = _vars_unset(text, ["editor"])

    assert text == ("# head\nemail: new@x   # personal\n# editor settings\nhosts: [c]\n\n"
                    "url: http://y/#z  # site\nenv:\n  EDITOR: nvim\nfullname: F\n")


def test_env_edit_add_change_remove_keeps_comments():
    text = "email: a@b\n"
    text = _env_edit(text, set_values={"EDITOR": "nvim", "MAIL_TO": "{{ email }}"})
    assert yaml.safe_load(text)["env"] == {"EDITOR": "nvim", "MAIL_TO": "{{ email }}"}

    text = text.replace("  EDITOR: nvim\n", "  EDITOR: nvim   # mine\n\n  # mail\n")
    text = _env_edit(text, set_values={"EDITOR": "hx", "PAGER": "less"})
    assert "  EDITOR: hx   # mine\n" in text and "  # mail\n" in text

    text = _env_edit(text, remove=["EDITOR"])
    assert yaml.safe_load(text)["env"] == {"MAIL_TO": "{{ email }}", "PAGER": "less"}
    assert yaml.safe_load(text)["email"] == "a@b"


def test_env_edit_converts_inline_section():
    text = _env_edit("a: 1\nenv: {X: '1'}\nb: 2\n", set_values={"Y": "2"})

    assert yaml.safe_load(text) == {"a": 1, "env": {"X": "1", "Y": "2"}, "b": 2}
