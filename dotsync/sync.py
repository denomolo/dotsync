"""
sync.py — Core bidirectional sync logic.

For each file in the manifest, the decision matrix is:

  disk_changed | repo_changed | action
  -------------|--------------|------------------------------------------
  False        | False        | nothing (already in sync)
  True         | False        | push: copy disk → repo
  False        | True         | pull: copy repo → disk
  True         | True         | CONFLICT → resolve per conflict_resolution

Conflict resolution modes:
  last-write-wins  — compare disk mtime vs repo file mtime; winner overwrites
  machine-wins     — disk always wins (good for initial propagation)
  git-wins         — repo always wins (good for recovering from local mess)
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum, auto
from pathlib import Path
from typing import Optional

from .config import Config
from .renderer import Renderer, RenderError
from .repo import Repo, GitError
from .state import State, hash_path, sha256


class Action(Enum):
    NOTHING   = auto()
    PUSH      = auto()   # disk → repo
    PULL      = auto()   # repo → disk
    CONFLICT  = auto()


@dataclass
class FileResult:
    dest: Path
    source: str
    action: Action
    resolved_by: Optional[str] = None   # how a conflict was resolved
    error: Optional[str] = None
    skipped: bool = False


class Syncer:
    def __init__(self, config: Config, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run
        self.repo = Repo(config.repo_path)
        self.renderer = Renderer(config.repo_path, config.profile)
        self.state = State.load(config.state_path, config.profile, config.conflict_resolution)

    # ── Public API ─────────────────────────────────────────────────────────────

    def sync(self) -> list[FileResult]:
        """Full bidirectional sync: fetch → decide → act → commit → push."""
        branch = f"profiles/{self.config.profile}"

        # 1. Pull remote changes into the repo
        self._git_pull(branch)

        # 2. Process each file in the manifest
        manifest = self.renderer.load_manifest()
        results = [self._process_entry(entry) for entry in manifest]

        # 3. Commit any files that were pushed (disk → repo)
        pushed = [r for r in results if r.action == Action.PUSH and not r.error]
        if pushed and not self.dry_run:
            self._git_commit_and_push(branch)

        # 4. Save updated state
        if not self.dry_run:
            self.state.save()

        return results

    def push_all(self) -> list[FileResult]:
        """Force disk → repo for all files (machine-wins override)."""
        branch = f"profiles/{self.config.profile}"
        manifest = self.renderer.load_manifest()
        results = [self._push_file(entry["source"], Path(entry["dest"])) for entry in manifest]
        if not self.dry_run:
            self._git_commit_and_push(branch)
            self.state.save()
        return results

    def pull_all(self) -> list[FileResult]:
        """Force repo → disk for all files (git-wins override)."""
        branch = f"profiles/{self.config.profile}"
        self._git_pull(branch)
        manifest = self.renderer.load_manifest()
        results = [self._pull_file(entry["source"], Path(entry["dest"])) for entry in manifest]
        if not self.dry_run:
            self.state.save()
        return results

    def status(self) -> list[FileResult]:
        """Dry-run: report per-file action without making changes."""
        old_dry = self.dry_run
        self.dry_run = True
        try:
            manifest = self.renderer.load_manifest()
            return [self._process_entry(entry) for entry in manifest]
        finally:
            self.dry_run = old_dry

    # ── Per-file logic ─────────────────────────────────────────────────────────

    def _process_entry(self, entry: dict) -> FileResult:
        source = entry["source"]
        dest = Path(entry["dest"])

        try:
            rendered = self.renderer.render(source)
        except RenderError as e:
            return FileResult(dest=dest, source=source, action=Action.NOTHING, error=str(e))

        repo_hash    = sha256(rendered)
        disk_hash    = hash_path(dest)
        state_entry  = self.state.get(dest)
        last_hash    = state_entry.last_applied_hash if state_entry else None

        disk_changed = disk_hash != last_hash
        repo_changed = repo_hash != last_hash

        # New file: no state yet
        if last_hash is None:
            if disk_hash is None:
                # File doesn't exist on disk — pull from repo
                action = Action.PULL
            elif disk_hash == repo_hash:
                # Already in sync, just record state
                if not self.dry_run:
                    self.state.record(dest, rendered)
                return FileResult(dest=dest, source=source, action=Action.NOTHING)
            else:
                # Disk has content, repo has different content, no prior state
                # Treat as conflict, resolve per policy
                action = Action.CONFLICT
        elif not disk_changed and not repo_changed:
            action = Action.NOTHING
        elif disk_changed and not repo_changed:
            action = Action.PUSH
        elif repo_changed and not disk_changed:
            action = Action.PULL
        else:
            action = Action.CONFLICT

        return self._act(source, dest, rendered, action)

    def _act(self, source: str, dest: Path, rendered: bytes, action: Action) -> FileResult:
        if action == Action.NOTHING:
            return FileResult(dest=dest, source=source, action=action)

        if action == Action.PULL:
            return self._pull_file(source, dest, rendered)

        if action == Action.PUSH:
            return self._push_file(source, dest)

        if action == Action.CONFLICT:
            return self._resolve_conflict(source, dest, rendered)

        raise ValueError(f"Unknown action: {action}")

    # ── Atomic file operations ─────────────────────────────────────────────────

    def _pull_file(self, source: str, dest: Path, rendered: bytes | None = None) -> FileResult:
        """Write rendered repo content to disk."""
        if rendered is None:
            try:
                rendered = self.renderer.render(source)
            except RenderError as e:
                return FileResult(dest=dest, source=source, action=Action.PULL, error=str(e))

        if self.dry_run:
            return FileResult(dest=dest, source=source, action=Action.PULL)

        dest.parent.mkdir(parents=True, exist_ok=True)
        # Write atomically via temp file
        tmp = dest.with_suffix(dest.suffix + ".dotsync_tmp")
        try:
            tmp.write_bytes(rendered)
            tmp.replace(dest)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            return FileResult(dest=dest, source=source, action=Action.PULL, error=str(e))

        self.state.record(dest, rendered)
        return FileResult(dest=dest, source=source, action=Action.PULL)

    def _push_file(self, source: str, dest: Path) -> FileResult:
        """Copy disk file back into the repo source location."""
        if not dest.exists():
            return FileResult(dest=dest, source=source, action=Action.PUSH,
                              error=f"Disk file not found: {dest}")

        repo_source = self.config.repo_path / source
        if self.dry_run:
            return FileResult(dest=dest, source=source, action=Action.PUSH)

        repo_source.parent.mkdir(parents=True, exist_ok=True)
        try:
            if dest.is_dir():
                if repo_source.exists():
                    shutil.rmtree(repo_source)
                shutil.copytree(dest, repo_source)
            else:
                shutil.copy2(dest, repo_source)
        except Exception as e:
            return FileResult(dest=dest, source=source, action=Action.PUSH, error=str(e))

        # Update state to reflect current disk content
        rendered = repo_source.read_bytes() if repo_source.is_file() else b""
        self.state.record(dest, rendered)
        return FileResult(dest=dest, source=source, action=Action.PUSH)

    # ── Conflict resolution ────────────────────────────────────────────────────

    def _resolve_conflict(self, source: str, dest: Path, rendered: bytes) -> FileResult:
        mode = self.config.conflict_resolution

        if mode == "machine-wins":
            result = self._push_file(source, dest)
            result.resolved_by = "machine-wins"
            return result

        if mode == "git-wins":
            result = self._pull_file(source, dest, rendered)
            result.resolved_by = "git-wins"
            return result

        # last-write-wins: compare disk mtime vs repo file mtime
        repo_source = self.config.repo_path / source
        disk_mtime = dest.stat().st_mtime if dest.exists() else 0
        repo_mtime = repo_source.stat().st_mtime if repo_source.exists() else 0

        if disk_mtime >= repo_mtime:
            result = self._push_file(source, dest)
            result.resolved_by = "last-write-wins → machine"
        else:
            result = self._pull_file(source, dest, rendered)
            result.resolved_by = "last-write-wins → git"

        result.action = Action.CONFLICT
        return result

    # ── Git helpers ────────────────────────────────────────────────────────────

    def _git_pull(self, branch: str) -> None:
        try:
            if self.repo.remote_ahead(branch):
                self.repo.checkout(branch)
                self.repo.pull(branch)
        except GitError as e:
            # Non-fatal: offline sync still works
            print(f"  [warn] Could not pull from remote: {e}")

    def _git_commit_and_push(self, branch: str) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        try:
            self.repo.checkout(branch)
            self.repo.add_all()
            committed = self.repo.commit(f"sync: {timestamp}")
            if committed and self.config.auto_push:
                self.repo.push(branch)
        except GitError as e:
            print(f"  [warn] Git commit/push failed: {e}")
