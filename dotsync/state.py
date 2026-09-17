"""
state.py — Per-machine state file: tracks last-applied hashes for 3-way merge detection.

State lives at ~/.local/state/dotsync/state.json (gitignored, machine-local).

Schema:
{
  "profile": "personal",
  "conflict_resolution": "last-write-wins",
  "files": {
    "/home/user/.zshrc": {
      "last_applied_hash": "abc123...",
      "last_applied_at": "2026-03-14T10:00:00"
    }
  }
}
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class FileState:
    last_applied_hash: str        # sha256 of the rendered content last written to disk
    last_applied_at: str          # ISO8601 timestamp


@dataclass
class State:
    path: Path
    profile: str = "base"
    conflict_resolution: str = "last-write-wins"
    files: dict[str, FileState] = field(default_factory=dict)

    # ── Persistence ────────────────────────────────────────────────────────────

    @classmethod
    def load(cls, path: Path, profile: str, conflict_resolution: str) -> "State":
        if path.exists():
            with path.open() as f:
                raw = json.load(f)
            files = {
                k: FileState(**v)
                for k, v in raw.get("files", {}).items()
            }
            return cls(
                path=path,
                profile=raw.get("profile", profile),
                conflict_resolution=raw.get("conflict_resolution", conflict_resolution),
                files=files,
            )
        return cls(path=path, profile=profile, conflict_resolution=conflict_resolution)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "profile": self.profile,
            "conflict_resolution": self.conflict_resolution,
            "files": {
                k: {"last_applied_hash": v.last_applied_hash, "last_applied_at": v.last_applied_at}
                for k, v in self.files.items()
            },
        }
        with self.path.open("w") as f:
            json.dump(data, f, indent=2)

    # ── File state helpers ─────────────────────────────────────────────────────

    def get(self, dest: Path) -> Optional[FileState]:
        return self.files.get(str(dest))

    def record(self, dest: Path, content: bytes) -> None:
        self.files[str(dest)] = FileState(
            last_applied_hash=sha256(content),
            last_applied_at=datetime.now(timezone.utc).isoformat(),
        )


# ── Hashing ────────────────────────────────────────────────────────────────────

def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> Optional[str]:
    """Return sha256 of file contents, or None if file doesn't exist."""
    try:
        return sha256(path.read_bytes())
    except FileNotFoundError:
        return None


def sha256_dir(path: Path) -> Optional[str]:
    """
    Return a stable hash of a directory's contents (recursive).
    Hash is computed over sorted relative paths + file contents.
    Returns None if directory doesn't exist.
    """
    if not path.is_dir():
        return None
    h = hashlib.sha256()
    for child in sorted(path.rglob("*")):
        if child.is_file():
            rel = str(child.relative_to(path))
            h.update(rel.encode())
            h.update(child.read_bytes())
    return h.hexdigest()


def hash_path(path: Path) -> Optional[str]:
    """Hash a file or directory."""
    if path.is_dir():
        return sha256_dir(path)
    return sha256_file(path)
