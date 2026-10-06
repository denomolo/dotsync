"""
migrate.py — Carry an existing setup over from dotsync, syncdot's old name.

Up to 0.8.0 the tool was called dotsync and kept its files in
~/.config/dotsync, ~/.local/share/dotsync and ~/.local/state/dotsync. The
first syncdot command moves them to the syncdot locations, as long as
syncdot has no config of its own yet.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import yaml

from . import config as config_module


def _old_dirs() -> dict[str, Path]:
    home = Path.home()
    return {"config": home / ".config" / "dotsync", "data": home / ".local" / "share" / "dotsync",
            "state": home / ".local" / "state" / "dotsync"}


def _new_dirs() -> dict[str, Path]:
    home = Path.home()
    return {"config": home / ".config" / "syncdot", "data": home / ".local" / "share" / "syncdot",
            "state": home / ".local" / "state" / "syncdot"}


def migrate_from_dotsync() -> list[str]:
    """Move dotsync's config, clone and state to syncdot's locations. Returns messages."""
    new_config = config_module.CONFIG_PATH
    old_config = Path(os.environ.get("DOTSYNC_CONFIG", _old_dirs()["config"] / "config.yaml")).expanduser()
    if new_config.exists() or not old_config.exists():
        return []

    old, new = _old_dirs(), _new_dirs()
    messages = []
    for key in ("data", "state"):
        if old[key].is_dir() and not new[key].exists():
            new[key].parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old[key]), str(new[key]))
            messages.append(f"Moved {_tilde(old[key])} → {_tilde(new[key])}")

    raw = yaml.safe_load(old_config.read_text()) or {}
    for key in ("repo_path", "state_path"):
        if key in raw:
            raw[key] = str(_moved(Path(raw[key]).expanduser(), old, new))
    new_config.parent.mkdir(parents=True, exist_ok=True)
    new_config.write_text(yaml.dump(raw, default_flow_style=False))
    old_config.unlink()
    if old_config.parent == old["config"] and not any(old["config"].iterdir()):
        old["config"].rmdir()
    messages.append(f"Moved {_tilde(old_config)} → {_tilde(new_config)}")

    if (Path.home() / ".config" / "systemd" / "user" / "dotsync.service").exists():
        messages.append("The login service still runs the old `dotsync` command: run "
                        "`syncdot service install` to replace it.")
    return messages


def _moved(path: Path, old: dict[str, Path], new: dict[str, Path]) -> Path:
    """`path` rewritten from an old dotsync directory to the new one, if it was inside one."""
    for key in ("data", "state"):
        if path == old[key] or path.is_relative_to(old[key]):
            return new[key] / path.relative_to(old[key])
    return path


def _tilde(path: Path) -> str:
    return str(path).replace(str(Path.home()), "~", 1)
