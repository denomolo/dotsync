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

import fnmatch
import glob
import os
import re
import shutil
import textwrap
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum, auto
from pathlib import Path
from typing import Optional

import yaml

from .config import Config
from .renderer import Renderer, RenderError
from .repo import Repo, GitError
from .state import IGNORED_NAMES, State, hash_path, is_binary, iter_dir_files, sha256, sha256_dir


class Action(Enum):
    NOTHING   = auto()
    PUSH      = auto()   # disk → repo
    PULL      = auto()   # repo → disk
    CONFLICT  = auto()
    UNTRACK   = auto()   # removed from tracking (disk file left alone)


@dataclass
class FileResult:
    dest: Path
    source: str
    action: Action
    resolved_by: Optional[str] = None   # how a conflict was resolved
    error: Optional[str] = None
    skipped: Optional[str] = None       # why the file was skipped (e.g. "binary")


class Syncer:
    def __init__(self, config: Config, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run
        self.repo = Repo(config.repo_path)
        self.renderer = Renderer(config.repo_path)
        self.state = State.load(config.state_path, config.profile, config.conflict_resolution)
        self.include_binary = config.include_binary

    # ── Public API ─────────────────────────────────────────────────────────────

    def sync(self) -> list[FileResult]:
        """Full bidirectional sync: fetch → decide → act → commit → push."""
        branch = self.config.branch

        # 1. Pull remote changes into the repo
        self._git_pull(branch)

        # 2. Process each file in the manifest
        manifest = self.renderer.load_manifest()
        results = [self._process_entry(entry) for entry in manifest]

        # 3. Commit whatever changed in the repo: pushes, and conflicts the
        #    machine won (those are reported as CONFLICT, not PUSH)
        if not self.dry_run and self.repo.has_uncommitted():
            self._git_commit_and_push(branch)

        # 4. Save updated state
        if not self.dry_run:
            self.state.save()

        return results

    def push_all(self, patterns: list[str] | None = None) -> list[FileResult]:
        """Force disk → repo for all files, or only those matching `patterns` (machine-wins)."""
        branch = self.config.branch
        manifest = self._select_entries(self.renderer.load_manifest(), patterns)
        results = [self._push_file(entry["source"], Path(entry["dest"])) for entry in manifest]
        if not self.dry_run:
            message = None
            if patterns:
                pushed = [r.source for r in results if not r.error and not r.skipped]
                message = (f"push: {pushed[0]}" if len(pushed) == 1
                           else f"push: {len(pushed)} files\n\n" + "\n".join(pushed))
            self._git_commit_and_push(branch, message=message)
            self.state.save()
        return results

    def pull_all(self, patterns: list[str] | None = None) -> list[FileResult]:
        """Force repo → disk for all files, or only those matching `patterns` (git-wins)."""
        branch = self.config.branch
        self._git_pull(branch)
        manifest = self._select_entries(self.renderer.load_manifest(), patterns)
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

    def add(self, locals_: list[Path], remote: str | None = None) -> list[FileResult]:
        """
        Start tracking one or more local files or directories.

        Each path is copied into files/<remote> (remote defaults to the
        path relative to ~, e.g. ~/.config/foo → files/.config/foo), gets a manifest entry and
        state, then everything is committed and pushed in a single commit.
        With several paths, `remote` is always treated as a directory.

        Paths that can't be added (missing, already tracked, binary, …) are
        returned with `error` set; the rest are still added.
        """
        branch = self.config.branch
        as_dir = len(locals_) > 1
        manifest = self.renderer.load_manifest()

        results: list[FileResult] = []
        planned: list[tuple[str, Path]] = []
        for local in locals_:
            dest = local.expanduser().absolute()
            try:
                source = self._plan_add(dest, remote, as_dir, manifest, planned)
            except ValueError as e:
                results.append(FileResult(dest=dest, source="", action=Action.PUSH, error=str(e)))
                continue
            planned.append((source, dest))
            results.append(FileResult(dest=dest, source=source, action=Action.PUSH))

        if self.dry_run or not planned:
            return results

        self.repo.checkout(branch)
        added = []
        for i, r in enumerate(results):
            if r.error:
                continue
            results[i] = r = self._push_file(r.source, r.dest)
            if not r.error:
                self._append_manifest_entry(r.source, r.dest)
                added.append(r.source)
        if added:
            message = f"add: {added[0]}" if len(added) == 1 else f"add: {len(added)} files\n\n" + "\n".join(added)
            self._git_commit_and_push(branch, message=message)
            self.state.save()
        return results

    def _plan_add(self, dest: Path, remote: str | None, as_dir: bool,
                  manifest: list[dict], planned: list[tuple[str, Path]]) -> str:
        """Validate adding `dest` and return its repo source path, or raise ValueError."""
        if not dest.exists():
            raise ValueError(f"Local path not found: {dest}")
        if dest.is_symlink():
            raise ValueError(f"Symlinks are not tracked: {dest}")
        if dest.is_file() and not self.include_binary and is_binary(dest):
            raise ValueError(
                f"{dest} is a binary file; only text files are synced "
                "(set include_binary: true in config to allow binaries)"
            )

        files_root = self.config.repo_path / "files"
        if remote is None:
            # Mirror the path under ~ so checkout's default (~/<remote>) maps it back
            if not dest.is_relative_to(Path.home()) or dest == Path.home():
                raise ValueError(f"{dest} is outside your home directory; use --remote to place it")
            rel = dest.relative_to(Path.home()).as_posix()
        else:
            rel = remote.strip("/")
            if not rel or as_dir or remote.endswith("/") or (files_root / rel).is_dir():
                rel = f"{rel}/{dest.name}".lstrip("/")
        repo_source = (files_root / rel).resolve()
        if not repo_source.is_relative_to(files_root.resolve()):
            raise ValueError(f"Remote path escapes files/: {remote}")
        source = f"files/{repo_source.relative_to(files_root.resolve())}"

        for entry in manifest:
            if Path(entry["dest"]) == dest:
                raise ValueError(f"{dest} is already tracked (source: {entry['source']})")
            if entry["source"] == source:
                raise ValueError(f"{source} is already used by {entry['dest']}")
        for other_source, other_dest in planned:
            if other_dest == dest:
                raise ValueError(f"{dest} was given more than once")
            if other_source == source:
                raise ValueError(f"{source} would also be used by {other_dest}; use --remote to separate them")
        if repo_source.exists():
            raise ValueError(f"{source} already exists in the repo")
        return source

    def checkout(self, patterns: list[str], local: Path | None = None,
                 force: bool = False) -> list[FileResult]:
        """
        Start tracking files that already exist in the repo (the reverse of add).

        Each pattern is a path or glob relative to files/. The file is
        written to `local` (default: ~/<path>, minus any .j2 suffix), gets a
        manifest entry and state, then the manifest change is committed and
        pushed. With several files, `local` is always treated as a directory.
        An existing local file with different content is only overwritten
        when `force` is set.
        """
        branch = self.config.branch
        self._git_pull(branch)
        sources = self._expand_sources(patterns)
        as_dir = len(sources) > 1
        manifest = self.renderer.load_manifest()

        results: list[FileResult] = []
        planned: list[tuple[str, Path]] = []
        for source in sources:
            try:
                dest = self._plan_checkout(source, local, as_dir, force, manifest, planned)
            except (ValueError, RenderError) as e:
                results.append(FileResult(dest=local or Path(source), source=source,
                                          action=Action.PULL, error=str(e)))
                continue
            planned.append((source, dest))
            results.append(FileResult(dest=dest, source=source, action=Action.PULL))

        if self.dry_run or not planned:
            return results

        self.repo.checkout(branch)
        added = []
        for i, r in enumerate(results):
            if r.error:
                continue
            results[i] = r = self._pull_file(r.source, r.dest)
            if not r.error:
                self._append_manifest_entry(r.source, r.dest)
                added.append(r.source)
        if added:
            message = (f"checkout: {added[0]}" if len(added) == 1
                       else f"checkout: {len(added)} files\n\n" + "\n".join(added))
            self._git_commit_and_push(branch, message=message)
            self.state.save()
        return results

    def _expand_sources(self, patterns: list[str]) -> list[str]:
        """Expand repo paths/globs (relative to files/) into manifest sources."""
        files_root = (self.config.repo_path / "files").resolve()
        sources: dict[str, None] = {}
        for pattern in patterns:
            rel = pattern.strip("/").removeprefix("files/")
            if glob.has_magic(rel):
                matches = {Path(m).relative_to(files_root).as_posix()
                           for m in glob.glob(str(files_root / rel), include_hidden=True)}
                if not matches:
                    raise ValueError(f"Nothing in the repo matches {pattern}")
            else:
                matches = {rel}
            for m in sorted(matches):
                if not (files_root / m).resolve().is_relative_to(files_root):
                    raise ValueError(f"Repo path escapes files/: {pattern}")
                sources.setdefault(f"files/{m}", None)
        return list(sources)

    def _plan_checkout(self, source: str, local: Path | None, as_dir: bool, force: bool,
                       manifest: list[dict], planned: list[tuple[str, Path]]) -> Path:
        """Validate checking out `source` and return its disk path, or raise."""
        abs_source = self.renderer.resolve_source(source)
        if abs_source.is_file() and not self.include_binary and is_binary(abs_source):
            raise ValueError(
                f"{source} is a binary file; only text files are synced "
                "(set include_binary: true in config to allow binaries)"
            )

        name = source.removeprefix("files/").removesuffix(".j2")
        if local is None:
            dest = Path.home() / name
        else:
            dest = Path(os.path.abspath(local.expanduser()))
            if as_dir or str(local).endswith("/") or dest.is_dir() and not abs_source.is_dir():
                dest = dest / Path(name).name

        for entry in manifest:
            if entry["source"] == source:
                raise ValueError(f"{source} is already tracked (dest: {entry['dest']})")
            if Path(entry["dest"]) == dest:
                raise ValueError(f"{dest} is already tracked (source: {entry['source']})")
        for other_source, other_dest in planned:
            if other_dest == dest:
                raise ValueError(f"{dest} would also be written by {other_source}")

        if dest.is_symlink():
            raise ValueError(f"{dest} is a symlink; symlinks are not tracked")
        if dest.exists() and not force:
            if abs_source.is_dir() != dest.is_dir():
                raise ValueError(f"{dest} already exists and is a different kind of path (use --force)")
            if abs_source.is_dir():
                same = sha256_dir(abs_source, self.include_binary) == sha256_dir(dest, self.include_binary)
            else:
                same = self.renderer.render(source) == dest.read_bytes()
            if not same:
                raise ValueError(f"{dest} already exists with different content (use --force to overwrite)")
        return dest

    def _select_entries(self, manifest: list[dict], patterns: list[str] | None) -> list[dict]:
        """
        Manifest entries whose dest matches one of `patterns` (all if none given).

        Patterns are disk paths; globs are matched against tracked dests, not
        the filesystem, so files missing from disk can still be selected.
        Raises ValueError if any pattern matches no tracked entry.
        """
        if not patterns:
            return manifest
        selected: dict[int, dict] = {}
        for pattern in patterns:
            path = os.path.abspath(os.path.expanduser(pattern))
            if glob.has_magic(path):
                matches = [i for i, e in enumerate(manifest)
                           if fnmatch.fnmatchcase(os.path.abspath(e["dest"]), path)]
            else:
                matches = [i for i, e in enumerate(manifest) if os.path.abspath(e["dest"]) == path]
            if not matches:
                parent = next((e for e in manifest if Path(path).is_relative_to(e["dest"])), None)
                if parent:
                    raise ValueError(f"{path} is inside tracked directory {parent['dest']}; "
                                     "use that directory instead")
                raise ValueError(f"{pattern} is not tracked")
            for i in matches:
                selected.setdefault(i, manifest[i])
        return [selected[i] for i in sorted(selected)]

    def remove(self, local: Path) -> FileResult:
        """
        Stop tracking a file or directory.

        Drops its manifest entry, deletes its source from the repo, forgets
        its state, then commits and pushes. The file on disk is left untouched.
        """
        branch = self.config.branch
        dest = Path(os.path.abspath(local.expanduser()))

        manifest = self.renderer.load_manifest()
        entry = next((e for e in manifest if Path(os.path.abspath(e["dest"])) == dest), None)
        if entry is None:
            if not dest.exists() and not dest.is_symlink():
                raise ValueError(f"{dest} doesn't exist")
            parent = next((e for e in manifest if dest.is_relative_to(e["dest"])), None)
            if parent:
                raise ValueError(
                    f"{dest} is inside tracked directory {parent['dest']}; "
                    "remove that directory instead"
                )
            raise ValueError(f"{dest} is not tracked")

        source = entry["source"]
        result = FileResult(dest=Path(entry["dest"]), source=source, action=Action.UNTRACK)
        if self.dry_run:
            return result

        self.repo.checkout(branch)
        self._remove_manifest_entry(source)

        # Keep the source if another (hand-written) entry still points at it
        if not any(e["source"] == source for e in manifest if e is not entry):
            p = self.config.repo_path / source
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p)
            elif p.exists() or p.is_symlink():
                p.unlink()

        self.state.forget(result.dest)
        self._git_commit_and_push(branch, message=f"remove: {source}")
        self.state.save()
        return result

    def _remove_manifest_entry(self, source: str) -> None:
        """Remove the list item whose source matches, editing text to keep comments."""
        manifest_path = self.config.repo_path / "manifest.yaml"
        text = manifest_path.read_text()
        lines = text.splitlines(keepends=True)

        for i, line in enumerate(lines):
            m = re.match(r"^(\s*)-(\s|$)", line)
            if not m:
                continue
            indent = len(m.group(1))
            end = i + 1
            while end < len(lines):
                nxt = lines[end]
                if (not nxt.strip() or nxt.lstrip().startswith("#")
                        or len(nxt) - len(nxt.lstrip()) <= indent):
                    break
                end += 1
            try:
                item = yaml.safe_load(textwrap.dedent("".join(lines[i:end])))
            except yaml.YAMLError:
                continue
            if (isinstance(item, list) and len(item) == 1 and isinstance(item[0], dict)
                    and item[0].get("source") == source):
                # Also drop the blank separator line `add` puts before entries
                start = i - 1 if i > 0 and not lines[i - 1].strip() else i
                del lines[start:end]
                break
        else:
            raise ValueError(f"Could not find manifest entry for {source}")

        new_text = "".join(lines)
        # An emptied list parses as null, which the manifest loader rejects
        if (yaml.safe_load(new_text) or {}).get("files") is None:
            new_text = re.sub(r"^files:[ \t]*$", "files: []", new_text, count=1, flags=re.MULTILINE)
        remaining = (yaml.safe_load(new_text) or {}).get("files") or []
        if any(isinstance(e, dict) and e.get("source") == source for e in remaining):
            raise ValueError(f"Could not cleanly remove manifest entry for {source}")
        manifest_path.write_text(new_text)

    def _append_manifest_entry(self, source: str, dest: Path) -> None:
        """Append to manifest.yaml as text so existing comments are preserved."""
        manifest_path = self.config.repo_path / "manifest.yaml"
        text = manifest_path.read_text()
        home = str(Path.home())
        dest_str = "~" + str(dest)[len(home):] if str(dest).startswith(home + "/") else str(dest)

        # Scaffolded manifests start as `files: []`, which can't be appended to
        text = re.sub(r"^files:\s*\[\]\s*$", "files:", text, count=1, flags=re.MULTILINE)
        if not text.endswith("\n"):
            text += "\n"
        entry = yaml.safe_dump([{"source": source, "dest": dest_str}], sort_keys=False)
        text += "\n" + "".join(f"  {line}\n" for line in entry.splitlines())
        manifest_path.write_text(text)

    # ── Per-file logic ─────────────────────────────────────────────────────────

    def _process_entry(self, entry: dict) -> FileResult:
        source = entry["source"]
        dest = Path(entry["dest"])

        try:
            abs_source = self.renderer.resolve_source(source)
            if abs_source.is_dir():
                # Directories are copied verbatim, never rendered
                rendered = None
                repo_hash = sha256_dir(abs_source, self.include_binary)
            else:
                if self._is_skipped_binary(abs_source, dest):
                    return FileResult(dest=dest, source=source, action=Action.NOTHING, skipped="binary")
                rendered = self.renderer.render(source)
                repo_hash = sha256(rendered)
        except RenderError as e:
            return FileResult(dest=dest, source=source, action=Action.NOTHING, error=str(e))

        disk_hash    = hash_path(dest, self.include_binary)
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
                    self.state.record_hash(dest, repo_hash)
                return FileResult(dest=dest, source=source, action=Action.NOTHING)
            else:
                # Disk has content, repo has different content, no prior state
                # Treat as conflict, resolve per policy
                action = Action.CONFLICT
        elif not disk_changed and not repo_changed:
            action = Action.NOTHING
        elif disk_hash == repo_hash:
            # Both sides changed identically (e.g. binaries newly excluded
            # from directory hashes) — just record the new state
            if not self.dry_run:
                self.state.record_hash(dest, repo_hash)
            return FileResult(dest=dest, source=source, action=Action.NOTHING)
        elif disk_changed and not repo_changed:
            action = Action.PUSH
        elif repo_changed and not disk_changed:
            action = Action.PULL
        else:
            action = Action.CONFLICT

        return self._act(source, dest, rendered, action)

    def _act(self, source: str, dest: Path, rendered: bytes | None, action: Action) -> FileResult:
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
        """Write rendered repo content (or a mirrored directory) to disk."""
        try:
            abs_source = self.renderer.resolve_source(source)
            if not abs_source.is_dir() and self._is_skipped_binary(abs_source, dest):
                return FileResult(dest=dest, source=source, action=Action.NOTHING, skipped="binary")
            if rendered is None and not abs_source.is_dir():
                rendered = self.renderer.render(source)
        except RenderError as e:
            return FileResult(dest=dest, source=source, action=Action.PULL, error=str(e))

        if self.dry_run:
            return FileResult(dest=dest, source=source, action=Action.PULL)

        if abs_source.is_dir():
            try:
                _mirror_dir(abs_source, dest, self.include_binary)
            except Exception as e:
                return FileResult(dest=dest, source=source, action=Action.PULL, error=str(e))
            self.state.record_hash(dest, sha256_dir(abs_source, self.include_binary))
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
        if not dest.is_dir() and self._is_skipped_binary(repo_source, dest):
            return FileResult(dest=dest, source=source, action=Action.NOTHING, skipped="binary")
        if self.dry_run:
            return FileResult(dest=dest, source=source, action=Action.PUSH)

        repo_source.parent.mkdir(parents=True, exist_ok=True)
        try:
            if dest.is_dir():
                _mirror_dir(dest, repo_source, self.include_binary)
            else:
                if repo_source.is_dir():
                    shutil.rmtree(repo_source)
                shutil.copy2(dest, repo_source)
        except Exception as e:
            return FileResult(dest=dest, source=source, action=Action.PUSH, error=str(e))

        # Update state to reflect current disk content
        if repo_source.is_dir():
            self.state.record_hash(dest, sha256_dir(repo_source, self.include_binary))
        else:
            self.state.record(dest, repo_source.read_bytes())
        return FileResult(dest=dest, source=source, action=Action.PUSH)

    # ── Conflict resolution ────────────────────────────────────────────────────

    def _resolve_conflict(self, source: str, dest: Path, rendered: bytes | None) -> FileResult:
        mode = self.config.conflict_resolution

        if mode == "machine-wins":
            result = self._push_file(source, dest)
            result.resolved_by = "machine-wins"
            return result

        if mode == "git-wins":
            result = self._pull_file(source, dest, rendered)
            result.resolved_by = "git-wins"
            return result

        # last-write-wins: compare disk mtime vs when the repo side was
        # committed. The clone's own mtime is useless: git rewrites the file
        # when it pulls, so it would always look newer than a local edit.
        repo_source = self.config.repo_path / source
        disk_mtime = _mtime(dest, self.include_binary)
        repo_mtime = self.repo.last_commit_time(source)
        if repo_mtime is None:
            repo_mtime = _mtime(repo_source, self.include_binary)

        if disk_mtime >= repo_mtime:
            result = self._push_file(source, dest)
            result.resolved_by = "last-write-wins → machine"
        else:
            result = self._pull_file(source, dest, rendered)
            result.resolved_by = "last-write-wins → git"

        result.action = Action.CONFLICT
        return result

    def _is_skipped_binary(self, *paths: Path) -> bool:
        """True if binaries are excluded and any of the given files is binary."""
        return not self.include_binary and any(is_binary(p) for p in paths)

    # ── Git helpers ────────────────────────────────────────────────────────────

    def _git_pull(self, branch: str) -> None:
        """Pull the profile branch, then merge base into it so shared changes reach every profile."""
        try:
            self.repo.checkout(branch)
            if self.repo.remote_ahead(branch):
                self.repo.pull(branch)
        except GitError as e:
            # Non-fatal: offline sync still works
            print(f"  [warn] Could not pull from remote: {e}")
        if branch == "base":
            return
        try:
            if self.repo.merge_base() and self.config.auto_push and not self.dry_run:
                self.repo.push(branch)
        except GitError as e:
            print(f"  [warn] Could not merge base into {branch}: {e}")

    def _git_commit_and_push(self, branch: str, message: str | None = None) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        try:
            self.repo.checkout(branch)
            self.repo.add_all()
            committed = self.repo.commit(message or f"sync: {timestamp}")
            if committed and self.config.auto_push:
                self.repo.push(branch)
        except GitError as e:
            print(f"  [warn] Git commit/push failed: {e}")


# ── Directory helpers ──────────────────────────────────────────────────────────

def _mtime(path: Path, include_binary: bool = False) -> float:
    """mtime of a file, or the newest file mtime inside a directory (0 if missing)."""
    if path.is_dir():
        return max((f.stat().st_mtime for f in iter_dir_files(path, include_binary)), default=0)
    return path.stat().st_mtime if path.exists() else 0


def _mirror_dir(src: Path, dst: Path, include_binary: bool = False) -> None:
    """
    Make dst's contents match src: copy every file over, then delete files
    and emptied directories in dst that src doesn't have. Entries in
    IGNORED_NAMES, symlinks and (unless include_binary) binary files are
    neither copied nor deleted, and are never written through.
    """
    if dst.exists() and not dst.is_dir():
        dst.unlink()
    dst.mkdir(parents=True, exist_ok=True)

    wanted = set()
    for f in iter_dir_files(src, include_binary):
        rel = f.relative_to(src)
        wanted.add(rel)
        target = dst / rel
        if target.is_symlink():
            continue
        if target.is_dir():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)

    # Deepest paths first so directories are empty by the time we reach them
    for child in sorted(dst.rglob("*"), reverse=True):
        rel = child.relative_to(dst)
        if rel in wanted or IGNORED_NAMES.intersection(rel.parts) or child.is_symlink():
            continue
        if child.is_file():
            if include_binary or not is_binary(child):
                child.unlink()
        elif child.is_dir() and not (src / rel).is_dir() and not any(child.iterdir()):
            child.rmdir()
