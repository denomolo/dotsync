"""
config.py — Load and validate ~/.config/syncdot/config.yaml
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

CONFIG_PATH = Path(os.environ.get("SYNCDOT_CONFIG", "~/.config/syncdot/config.yaml")).expanduser()

ConflictResolution = Literal["local-wins", "repo-wins", "newer-wins"]

# Names used before 0.8.0, still accepted
POLICY_ALIASES = {"machine-wins": "local-wins", "git-wins": "repo-wins",
                  "last-write-wins": "newer-wins"}


def profile_branch(profile: str) -> str:
    """Git branch for a profile: "base" lives on the base branch itself."""
    return "base" if profile == "base" else f"profiles/{profile}"


@dataclass
class Config:
    profile: str = "base"
    conflict_resolution: ConflictResolution = "local-wins"
    auto_push: bool = True
    include_binary: bool = False   # sync binary files too (default: text only)
    repo_path: Path = field(default_factory=lambda: Path("~/.local/share/syncdot/repo").expanduser())
    state_path: Path = field(default_factory=lambda: Path("~/.local/state/syncdot/state.json").expanduser())

    @property
    def branch(self) -> str:
        return profile_branch(self.profile)

    @classmethod
    def load(cls) -> "Config":
        if not CONFIG_PATH.exists():
            raise FileNotFoundError(
                f"Config not found at {CONFIG_PATH}.\n"
                "Run `syncdot init <repo>` to set syncdot up."
            )
        with CONFIG_PATH.open() as f:
            raw = yaml.safe_load(f) or {}

        repo_path = Path(raw.get("repo_path", "~/.local/share/syncdot/repo")).expanduser()
        state_path = Path(raw.get("state_path", "~/.local/state/syncdot/state.json")).expanduser()
        conflict = raw.get("conflict_resolution", "local-wins")
        conflict = POLICY_ALIASES.get(conflict, conflict)
        if conflict not in ("local-wins", "repo-wins", "newer-wins"):
            raise ValueError(f"Invalid conflict_resolution {conflict!r} in {CONFIG_PATH}: "
                             "use local-wins, repo-wins or newer-wins")

        # repo_url (written before 0.8.0) is ignored: the clone's git remote is
        # what syncdot pushes to
        return cls(
            profile=raw.get("profile", "base"),
            conflict_resolution=conflict,
            auto_push=raw.get("auto_push", True),
            include_binary=raw.get("include_binary", False),
            repo_path=repo_path,
            state_path=state_path,
        )

    def write_default(self, profile: str) -> None:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "profile": profile,
            "conflict_resolution": "local-wins",
            "auto_push": True,
            "include_binary": False,
            "repo_path": str(self.repo_path),
            "state_path": str(self.state_path),
        }
        with CONFIG_PATH.open("w") as f:
            yaml.dump(data, f, default_flow_style=False)
        print(f"Config written to {CONFIG_PATH}")
