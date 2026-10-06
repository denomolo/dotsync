"""
config.py — Load and validate ~/.config/dotsync/config.yaml
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

CONFIG_PATH = Path(os.environ.get("DOTSYNC_CONFIG", "~/.config/dotsync/config.yaml")).expanduser()

ConflictResolution = Literal["last-write-wins", "machine-wins", "git-wins"]


def profile_branch(profile: str) -> str:
    """Git branch for a profile: "base" lives on the base branch itself."""
    return "base" if profile == "base" else f"profiles/{profile}"


@dataclass
class Config:
    repo_url: str
    profile: str = "base"
    conflict_resolution: ConflictResolution = "machine-wins"
    auto_push: bool = True
    include_binary: bool = False   # sync binary files too (default: text only)
    repo_path: Path = field(default_factory=lambda: Path("~/.local/share/dotsync/repo").expanduser())
    state_path: Path = field(default_factory=lambda: Path("~/.local/state/dotsync/state.json").expanduser())

    @property
    def branch(self) -> str:
        return profile_branch(self.profile)

    @classmethod
    def load(cls) -> "Config":
        if not CONFIG_PATH.exists():
            raise FileNotFoundError(
                f"Config not found at {CONFIG_PATH}.\n"
                "Run `dotsync install` to create an initial config."
            )
        with CONFIG_PATH.open() as f:
            raw = yaml.safe_load(f) or {}

        repo_path = Path(raw.get("repo_path", "~/.local/share/dotsync/repo")).expanduser()
        state_path = Path(raw.get("state_path", "~/.local/state/dotsync/state.json")).expanduser()
        conflict = raw.get("conflict_resolution", "machine-wins")

        if conflict not in ("last-write-wins", "machine-wins", "git-wins"):
            raise ValueError(f"Invalid conflict_resolution: {conflict!r}")

        return cls(
            repo_url=raw["repo_url"],
            profile=raw.get("profile", "base"),
            conflict_resolution=conflict,
            auto_push=raw.get("auto_push", True),
            include_binary=raw.get("include_binary", False),
            repo_path=repo_path,
            state_path=state_path,
        )

    def write_default(self, repo_url: str, profile: str) -> None:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "repo_url": repo_url,
            "profile": profile,
            "conflict_resolution": "machine-wins",
            "auto_push": True,
            "include_binary": False,
            "repo_path": str(self.repo_path),
            "state_path": str(self.state_path),
        }
        with CONFIG_PATH.open("w") as f:
            yaml.dump(data, f, default_flow_style=False)
        print(f"Config written to {CONFIG_PATH}")
