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
        self.record_hash(dest, sha256(content))

    def forget(self, dest: Path) -> None:
        self.files.pop(str(dest), None)

    def record_hash(self, dest: Path, digest: str) -> None:
        self.files[str(dest)] = FileState(
            last_applied_hash=digest,
            last_applied_at=datetime.now(timezone.utc).isoformat(),
        )


# ── Hashing ────────────────────────────────────────────────────────────────────

# Directory entries never synced (nested git checkouts would become submodules)
IGNORED_NAMES = {".git"}

# Bytes sniffed for binary detection (same heuristic as git: a NUL byte → binary)
BINARY_SNIFF_BYTES = 8192


def is_binary(path: Path) -> bool:
    """True if the file looks binary (contains a NUL byte near the start)."""
    try:
        with path.open("rb") as f:
            return b"\0" in f.read(BINARY_SNIFF_BYTES)
    except (FileNotFoundError, IsADirectoryError):
        return False


def iter_dir_files(path: Path, include_binary: bool = False):
    """
    Yield regular files under path (recursive, sorted), skipping
    IGNORED_NAMES, symlinks and (unless include_binary) binary files.
    rglob doesn't descend into symlinked directories, so nothing reached
    through a link is yielded either.
    """
    for child in sorted(path.rglob("*")):
        rel = child.relative_to(path)
        if IGNORED_NAMES.intersection(rel.parts) or child.is_symlink():
            continue
        if child.is_file() and (include_binary or not is_binary(child)):
            yield child

def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> Optional[str]:
    """Return sha256 of file contents, or None if file doesn't exist."""
    try:
        return sha256(path.read_bytes())
    except FileNotFoundError:
        return None


def sha256_dir(path: Path, include_binary: bool = False) -> Optional[str]:
    """
    Return a stable hash of a directory's contents (recursive).
    Hash is computed over sorted relative paths + file contents.
    Returns None if directory doesn't exist.
    """
    if not path.is_dir():
        return None
    h = hashlib.sha256()
    for child in iter_dir_files(path, include_binary):
        h.update(str(child.relative_to(path)).encode())
        h.update(child.read_bytes())
    return h.hexdigest()


def hash_path(path: Path, include_binary: bool = False) -> Optional[str]:
    """Hash a file or directory."""
    if path.is_dir():
        return sha256_dir(path, include_binary)
    return sha256_file(path)
