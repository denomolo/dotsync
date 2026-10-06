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
  machine-wins     — disk wins (default): the repo's version stays in git
                     history, while a local file that's overwritten is gone
  last-write-wins  — newer side wins (disk mtime vs last commit time)
  git-wins         — repo always wins (good for recovering from local mess)
"""

from __future__ import annotations

import dataclasses
import difflib
import fnmatch
import functools
import glob
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
import stat
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
from .state import ENV_D_NAME, IGNORED_NAMES, State, hash_path, is_binary, iter_dir_files, sha256, sha256_dir


class Action(Enum):
    NOTHING   = auto()
    PUSH      = auto()   # disk → repo
    PULL      = auto()   # repo → disk
    CONFLICT  = auto()
    UNTRACK   = auto()   # removed from tracking (disk file left alone)


# Skip reason for tracked files that were never synced on this machine
NOT_SET_UP = "not synced on this machine yet"


@dataclass
class FileResult:
    dest: Path
    source: str
    action: Action
    resolved_by: Optional[str] = None   # how a conflict was resolved
    error: Optional[str] = None
    skipped: Optional[str] = None       # why the file was skipped (e.g. "binary")


class _PreviewRepo(Repo):
    """A throwaway worktree used by --dry-run: it never switches branches."""

    def checkout(self, branch: str, create: bool = False) -> None:
        pass


def _previewable(method):
    """In --dry-run, run `method` against a preview worktree instead of the clone."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        if not self.dry_run or self._preview_root is not None:
            return method(self, *args, **kwargs)
        with self._preview():
            return method(self, *args, **kwargs)
    return wrapper


class Syncer:
    def __init__(self, config: Config, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run
        self.repo = Repo(config.repo_path)
        self.renderer = Renderer(config.repo_path)
        self.state = State.load(config.state_path, config.profile, config.conflict_resolution)
        self.include_binary = config.include_binary
        self._preview_root: Path | None = None

    # ── Public API ─────────────────────────────────────────────────────────────

    @_previewable
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

        # 5. Regenerate the session environment from vars.yaml (env:)
        return results + self._env_results()

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

    @_previewable
    def pull_all(self, patterns: list[str] | None = None) -> list[FileResult]:
        """Force repo → disk for all files, or only those matching `patterns` (git-wins)."""
        branch = self.config.branch
        self._git_pull(branch)
        manifest = self._select_entries(self.renderer.load_manifest(), patterns)
        results = [self._pull_file(entry["source"], Path(entry["dest"])) for entry in manifest]
        if not self.dry_run:
            self.state.save()
        return results + (self._env_results() if not patterns else [])

    def status(self, patterns: list[str] | None = None) -> list[FileResult]:
        """Report what sync would do for each file (or those matching `patterns`), changing nothing."""
        old_dry = self.dry_run
        self.dry_run = True
        try:
            manifest = self._select_entries(self.renderer.load_manifest(), patterns)
            results = [self._process_entry(entry) for entry in manifest]
            return results + (self._env_results() if not patterns else [])
        finally:
            self.dry_run = old_dry

    def diff(self, source: str, dest: Path) -> list[str]:
        """
        Unified diff from the repo's version (rendered, for templates) to the
        file on this machine. Directories are compared file by file. Empty if
        they match.
        """
        try:
            abs_source = self.renderer.resolve_source(source)
        except RenderError:
            abs_source = self.config.repo_path / source
        shown = str(dest).replace(str(Path.home()), "~", 1)

        if abs_source.is_dir() or dest.is_dir():
            def files(root: Path) -> dict[str, Path]:
                if not root.is_dir():
                    return {}
                return {f.relative_to(root).as_posix(): f for f in iter_dir_files(root, include_binary=True)}
            repo_files, disk_files = files(abs_source), files(dest)
            lines = []
            for rel in sorted(repo_files.keys() | disk_files.keys()):
                lines += _diff_files(repo_files.get(rel), disk_files.get(rel),
                                     f"repo: {source}/{rel}", f"here: {shown}/{rel}")
            return lines

        try:
            repo_bytes = self.renderer.render(source) if abs_source.exists() else None
        except RenderError as e:
            return [f"(can't render {source}: {e})\n"]
        disk_bytes = dest.read_bytes() if dest.is_file() else None
        return _diff_bytes(repo_bytes, disk_bytes, f"repo: {source}", f"here: {shown}")

    def add(self, locals_: list[Path], remote: str | None = None,
            allow_private: bool = False) -> list[FileResult]:
        """
        Start tracking one or more local files or directories.

        Each path is copied into files/<remote> (remote defaults to the
        path relative to ~, e.g. ~/.config/foo → files/.config/foo), gets a manifest entry and
        state, then everything is committed and pushed in a single commit.
        With several paths, `remote` is always treated as a directory.

        Paths that can't be added (missing, already tracked, binary, private
        unless `allow_private`, …) are returned with `error` set; the rest are
        still added.
        """
        branch = self.config.branch
        as_dir = len(locals_) > 1
        manifest = self.renderer.load_manifest()

        results: list[FileResult] = []
        planned: list[tuple[str, Path]] = []
        for local in locals_:
            dest = local.expanduser().absolute()
            try:
                source = self._plan_add(dest, remote, as_dir, manifest, planned, allow_private)
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
            results[i] = r = self._push_file(r.source, r.dest, allow_template=True)
            if not r.error:
                self._append_manifest_entry(r.source, r.dest)
                added.append(r.source)
        if added:
            message = f"add: {added[0]}" if len(added) == 1 else f"add: {len(added)} files\n\n" + "\n".join(added)
            self._git_commit_and_push(branch, message=message)
            self.state.save()
        return results

    def _plan_add(self, dest: Path, remote: str | None, as_dir: bool,
                  manifest: list[dict], planned: list[tuple[str, Path]],
                  allow_private: bool = False) -> str:
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
        if not allow_private:
            private = find_private(dest, self.include_binary)
            if private is not None:
                where = "is private" if private == dest else f"contains private file {private}"
                raise ValueError(
                    f"{dest} {where} (mode {stat.S_IMODE(private.stat().st_mode):o}), which "
                    "usually means it holds secrets; its contents would be pushed to your "
                    "dotfiles repo (use --allow-private to track it anyway)"
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

    @_previewable
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
        return results + self._env_results()

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

    # ── Variables ──────────────────────────────────────────────────────────────

    def var_list(self) -> dict:
        """Template variables in vars.yaml on the active profile's branch."""
        self.repo.checkout(self.config.branch)
        return {k: v for k, v in self.renderer.vars.items() if k != "env"}

    @_previewable
    def var_set(self, values: dict[str, object]) -> list[FileResult]:
        """
        Set variables in vars.yaml on the active profile's branch, re-render
        the templates on disk, then commit and push. Comments are preserved.
        Returns the results for templates whose output changed.
        """
        for name in values:
            _check_var_name(name)
        return self._edit_vars(
            lambda text: _vars_set(text, values),
            message="var: set " + ", ".join(values),
        )

    @_previewable
    def var_unset(self, names: list[str], force: bool = False) -> list[FileResult]:
        """
        Remove variables from vars.yaml on the active profile's branch.
        Refuses if a tracked template still uses one, unless `force`.
        """
        branch = self.config.branch
        self._git_pull(branch)
        current = self.renderer.vars
        missing = [n for n in names if n not in current]
        if missing:
            raise ValueError(f"Not set: {', '.join(missing)}")
        if "env" in names:
            raise ValueError("`env` holds environment variables; use `dotsync env unset`")
        if not force:
            used = {}
            for entry in self.renderer.load_manifest():
                for name in self.renderer.template_variables(entry["source"]) & set(names):
                    used.setdefault(name, []).append(entry["source"])
            for env_name, value in _env_section(current).items():
                for name in self.renderer.string_variables(_env_str(value)) & set(names):
                    used.setdefault(name, []).append(f"env {env_name}")
            if used:
                details = "; ".join(f"{n} by {', '.join(srcs)}" for n, srcs in used.items())
                raise ValueError(f"Still used: {details} (use --force to unset anyway)")
        return self._edit_vars(
            lambda text: _vars_unset(text, names),
            message="var: unset " + ", ".join(names),
            pulled=True,
        )

    def _edit_vars(self, edit, message: str, pulled: bool = False) -> list[FileResult]:
        """Apply `edit` to vars.yaml text, re-render templates, commit and push."""
        branch = self.config.branch
        if not pulled:
            self._git_pull(branch)
        self.repo.checkout(branch)
        vars_path = self.config.repo_path / "vars.yaml"
        old_text = vars_path.read_text() if vars_path.exists() else ""
        _load_vars_text(old_text)
        new_text = edit(old_text)
        new_vars = _load_vars_text(new_text)

        # Templates are rendered from the new values whether or not we write them
        self.renderer._vars = new_vars
        env = self._render_env(new_vars)   # refuse edits that would break the env
        templates = [e for e in self.renderer.load_manifest() if e["source"].endswith(".j2")]
        results = [r for r in (self._process_entry(e) for e in templates)
                   if r.action != Action.NOTHING or r.error]
        if self.dry_run:
            return results + self._apply_env(env)

        vars_path.write_text(new_text)
        self._git_commit_and_push(branch, message=message)
        self.state.save()
        return results + self._apply_env(env)

    # ── Environment variables ──────────────────────────────────────────────────

    def env_list(self) -> list[tuple[str, str, str | None, str | None]]:
        """(name, raw value, rendered value, error) for each env: entry."""
        self.repo.checkout(self.config.branch)
        variables = self.renderer.vars
        rows = []
        for name, value in _env_section(variables).items():
            raw = _env_str(value)
            try:
                rows.append((name, raw, self.renderer.render_string(raw, variables), None))
            except RenderError as e:
                rows.append((name, raw, None, str(e)))
        return rows

    @_previewable
    def env_set(self, values: dict[str, str]) -> list[FileResult]:
        """Set environment variables in the env: section of vars.yaml."""
        for name, value in values.items():
            _check_env_name(name)
            if "\n" in value:
                raise ValueError(f"{name}: env values can't contain newlines")
        return self._edit_vars(lambda text: _env_edit(text, set_values=values),
                               message="env: set " + ", ".join(values))

    @_previewable
    def env_unset(self, names: list[str]) -> list[FileResult]:
        """Remove environment variables from the env: section of vars.yaml."""
        self._git_pull(self.config.branch)
        missing = [n for n in names if n not in _env_section(self.renderer.vars)]
        if missing:
            raise ValueError(f"Not set: {', '.join(missing)}")
        return self._edit_vars(lambda text: _env_edit(text, remove=names),
                               message="env: unset " + ", ".join(names), pulled=True)

    @property
    def env_path(self) -> Path:
        return Path.home() / ".config" / "environment.d" / ENV_D_NAME

    def _render_env(self, variables: dict) -> dict[str, str]:
        """Rendered env: section, or raise ValueError if it can't be exported."""
        env = {}
        for name, value in _env_section(variables).items():
            _check_env_name(name)
            try:
                rendered = self.renderer.render_string(_env_str(value), variables)
            except RenderError as e:
                raise ValueError(f"env {name}: {e}") from e
            for bad, what in (("\n", "newlines"), ("$", "$ (systemd would expand it)")):
                if bad in rendered:
                    raise ValueError(f"env {name}: values can't contain {what}")
            env[name] = rendered
        return env

    def _env_results(self) -> list[FileResult]:
        """Regenerate the env files from the current vars, reporting what changed."""
        try:
            env = self._render_env(self.renderer.vars)
        except (ValueError, RenderError) as e:
            return [FileResult(dest=self.env_path, source="vars.yaml env:",
                               action=Action.PULL, error=str(e))]
        return self._apply_env(env)

    def _apply_env(self, env: dict[str, str]) -> list[FileResult]:
        """
        Write ~/.config/environment.d/99-env.conf, read by the systemd user
        session and sourced by shells (see `dotsync env hook`), then update
        the running systemd user manager. Returns a result if it changed.
        """
        path = self.env_path
        text = ("# Generated by dotsync from the env: section of vars.yaml; do not edit.\n"
                "# Shells load it with: set -a; . <this file>; set +a\n"
                + "".join(f'{k}="{_env_quote(v)}"\n' for k, v in env.items())) if env else None
        old = path.read_text() if path.exists() else None
        if text == old:
            return []
        result = FileResult(dest=path, source="vars.yaml env:",
                            action=Action.PULL if text is not None else Action.UNTRACK)
        if self.dry_run:
            return [result]
        old_names = set(re.findall(r"^([A-Za-z_][A-Za-z0-9_]*)=", old or "", flags=re.M))
        try:
            if text is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(path.name + ".dotsync_tmp")
                tmp.write_text(text)
                tmp.replace(path)
        except OSError as e:
            result.error = str(e)
            return [result]
        _update_systemd_env(env, old_names - env.keys())
        return [result]

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

        # Never synced on this machine: sync doesn't bring it into play by
        # itself. pull, push or checkout does that explicitly.
        if last_hash is None:
            if disk_hash == repo_hash:
                # Identical already, so nothing changes: just start tracking state
                if not self.dry_run:
                    self.state.record_hash(dest, repo_hash)
                return FileResult(dest=dest, source=source, action=Action.NOTHING)
            return FileResult(dest=dest, source=source, action=Action.NOTHING, skipped=NOT_SET_UP)
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
            # Carry over the executable bit git recorded; other bits stay at
            # the umask default (modes like 600 aren't stored in git)
            if abs_source.stat().st_mode & stat.S_IXUSR:
                mode = tmp.stat().st_mode
                tmp.chmod(mode | (mode & 0o444) >> 2)
            tmp.replace(dest)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            return FileResult(dest=dest, source=source, action=Action.PULL, error=str(e))

        self.state.record(dest, rendered)
        return FileResult(dest=dest, source=source, action=Action.PULL)

    def _push_file(self, source: str, dest: Path, allow_template: bool = False) -> FileResult:
        """
        Copy disk file back into the repo source location.

        A .j2 source is never overwritten (that would replace the template
        with its rendered output) unless `allow_template` is set, as add does
        when creating a new source.
        """
        if not dest.exists():
            return FileResult(dest=dest, source=source, action=Action.PUSH,
                              error=f"Disk file not found: {dest}")

        repo_source = self.config.repo_path / source
        if source.endswith(".j2") and repo_source.is_file() and not allow_template:
            try:
                rendered = self.renderer.render(source)
            except RenderError as e:
                return FileResult(dest=dest, source=source, action=Action.PUSH, error=str(e))
            if dest.is_file() and dest.read_bytes() == rendered:
                return FileResult(dest=dest, source=source, action=Action.NOTHING)
            shown = str(dest).replace(str(Path.home()), "~")
            return FileResult(
                dest=dest, source=source, action=Action.PUSH,
                error=f"rendered from template {source}; edit the template in the repo "
                      f"instead (`dotsync pull {shown}` discards the local change)",
            )
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
            return self._machine_wins(source, dest, "machine-wins")

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
            result = self._machine_wins(source, dest, "last-write-wins → machine")
        else:
            result = self._pull_file(source, dest, rendered)
            result.resolved_by = "last-write-wins → git"

        result.action = Action.CONFLICT
        return result

    def _machine_wins(self, source: str, dest: Path, how: str) -> FileResult:
        """Push the disk version, noting where the repo's version can be recovered."""
        previous = self.repo.last_commit_id(source)
        result = self._push_file(source, dest)
        result.resolved_by = how
        if previous and not result.error and not self.dry_run:
            result.resolved_by += f"; repo version kept in history at {previous}"
        result.action = Action.CONFLICT
        return result

    def _is_skipped_binary(self, *paths: Path) -> bool:
        """True if binaries are excluded and any of the given files is binary."""
        return not self.include_binary and any(is_binary(p) for p in paths)

    # ── Git helpers ────────────────────────────────────────────────────────────

    def _git_pull(self, branch: str) -> None:
        """Pull the profile branch, then merge base into it so shared changes reach every profile."""
        if self._preview_root is not None or self.dry_run:
            return   # dry runs never pull; a preview already contains what this would
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
            if self.repo.merge_base() and self.config.auto_push:
                self.repo.push(branch)
        except GitError as e:
            print(f"  [warn] Could not merge base into {branch}: {e}")

    @contextmanager
    def _preview(self):
        """
        For --dry-run: build what the clone would look like after pulling (the
        profile branch, plus origin, plus base merged in) in a temporary git
        worktree, and point this Syncer at it. The clone's branches, HEAD and
        files are never touched; only `git fetch` updates remote-tracking refs.
        """
        branch = self.config.branch
        try:
            self.repo.fetch()
        except GitError as e:
            print(f"  [warn] Could not fetch from remote (previewing local state): {e}")
        self.repo._run("worktree", "prune", check=False)

        tmp = Path(tempfile.mkdtemp(prefix="dotsync-preview-"))
        worktree = tmp / "repo"
        start = branch if self.repo.branch_exists(branch) else f"origin/{branch}"
        if not _ref_exists(self.repo, start):
            start = "HEAD"
        self.repo._run("worktree", "add", "--detach", str(worktree), start)
        preview = _PreviewRepo(worktree)
        try:
            refs = [f"origin/{branch}"] + (["origin/base", "base"] if branch != "base" else [])
            for ref in refs:
                if not _ref_exists(preview, ref):
                    continue
                merged = preview._run("-c", "user.name=dotsync", "-c", "user.email=dotsync@localhost",
                                      "merge", "--no-edit", ref, check=False)
                if merged.returncode != 0:
                    preview._run("merge", "--abort", check=False)
                    what = "pulling" if ref == f"origin/{branch}" else f"merging {ref} into {branch}"
                    print(f"  [warn] {what} would conflict; sync would stop there and leave it "
                          f"for you to resolve. Previewing without it.")

            saved = (self.config, self.repo, self.renderer)
            self.config = dataclasses.replace(self.config, repo_path=worktree)
            self.repo, self.renderer = preview, Renderer(worktree)
            self._preview_root = worktree
            try:
                yield
            finally:
                self.config, self.repo, self.renderer = saved
                self._preview_root = None
        finally:
            self.repo._run("worktree", "remove", "--force", str(worktree), check=False)
            shutil.rmtree(tmp, ignore_errors=True)
            self.repo._run("worktree", "prune", check=False)

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

def _diff_files(repo_file: Path | None, disk_file: Path | None, repo_label: str,
                disk_label: str) -> list[str]:
    return _diff_bytes(repo_file.read_bytes() if repo_file else None,
                       disk_file.read_bytes() if disk_file else None, repo_label, disk_label)


def _diff_bytes(repo: bytes | None, disk: bytes | None, repo_label: str, disk_label: str) -> list[str]:
    """Unified diff lines from the repo's content to the disk's (None = missing)."""
    if repo == disk:
        return []
    if b"\0" in (repo or b"")[:8192] or b"\0" in (disk or b"")[:8192]:
        return [f"Binary files {repo_label} and {disk_label} differ\n"]
    def text(data: bytes | None) -> list[str]:
        return [] if data is None else data.decode("utf-8", errors="replace").splitlines(keepends=True)
    lines = list(difflib.unified_diff(text(repo), text(disk),
                                      repo_label if repo is not None else "/dev/null",
                                      disk_label if disk is not None else "/dev/null"))
    # Mark a missing final newline the way git does, so the line breaks stay readable
    return [l if l.endswith("\n") else l + "\n\\ No newline at end of file\n" for l in lines]


def _ref_exists(repo: Repo, ref: str) -> bool:
    return repo._run("rev-parse", "--verify", "--quiet", ref, check=False).returncode == 0


_VAR_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _check_var_name(name: str) -> None:
    if not _VAR_NAME_RE.fullmatch(name):
        raise ValueError(f"Invalid variable name {name!r}: use letters, digits and _, "
                         "not starting with a digit")
    if name == "env":
        raise ValueError("`env` holds environment variables; use `dotsync env set`")


# Exporting these would break the session or hijack programs
_PROTECTED_ENV = {
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "PWD", "OLDPWD", "SHLVL", "TERM",
    "DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XDG_SESSION_ID",
    "DBUS_SESSION_BUS_ADDRESS", "SSH_AUTH_SOCK", "SSH_CONNECTION", "MAIL", "IFS",
}


def _check_env_name(name: str) -> None:
    if not _VAR_NAME_RE.fullmatch(name):
        raise ValueError(f"Invalid environment variable name {name!r}: use letters, digits "
                         "and _, not starting with a digit")
    if name in _PROTECTED_ENV or name.startswith(("LD_", "BASH_FUNC_")):
        raise ValueError(f"{name} is managed by your session and can't be set by dotsync")


def _env_section(variables: dict) -> dict:
    env = variables.get("env") or {}
    if not isinstance(env, dict):
        raise ValueError("env: in vars.yaml must be a mapping of names to values")
    return env


def _env_str(value: object) -> str:
    """Hand-written YAML values (true, 3) as the strings they'd be in a shell."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _env_quote(value: str) -> str:
    r"""
    Escape for a double-quoted value that systemd's environment.d parser and
    POSIX shells read identically: both unescape \\ \" and \` there.
    ($ is refused earlier: systemd would expand it.)
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`")


def _update_systemd_env(env: dict[str, str], removed: set[str]) -> None:
    """Best effort: make newly started user services see the change without re-login."""
    if not shutil.which("systemctl"):
        return
    for args in ((["set-environment", *(f"{k}={v}" for k, v in env.items())] if env else None),
                 (["unset-environment", *sorted(removed)] if removed else None)):
        if args:
            try:
                subprocess.run(["systemctl", "--user", *args], capture_output=True, timeout=10)
            except (OSError, subprocess.SubprocessError):
                pass


def _deeper(line: str, indent: str) -> bool:
    """True for a non-blank line indented further than `indent`."""
    return bool(line.strip()) and line.startswith(indent) and line[len(indent):][:1] in (" ", "\t")


def _var_block(lines: list[str], name: str, indent: str = "",
               start: int = 0, stop: int | None = None) -> tuple[int, int] | None:
    """Line range [start, end) of key `name` at `indent` in vars.yaml, or None."""
    stop = len(lines) if stop is None else stop
    n = re.escape(name)
    key = re.compile(rf"""^{re.escape(indent)}(?:{n}|"{n}"|'{n}')\s*:""")
    for i in range(start, stop):
        if key.match(lines[i]):
            end = i + 1
            # The value continues on deeper lines; blank and comment lines in
            # between belong to it, trailing ones don't
            while end < stop and (_deeper(lines[end], indent) or not lines[end].strip()
                                  or lines[end].lstrip().startswith("#")):
                end += 1
            while end > i + 1 and not _deeper(lines[end - 1], indent):
                end -= 1
            return i, end
    return None


def _replace_entry(lines: list[str], block: tuple[int, int], entry: str) -> None:
    """Replace a key's lines, keeping a trailing comment on a one-line value."""
    old_line = lines[block[0]].rstrip("\n")
    m = re.search(r"\s+#[^\n]*$", old_line)
    if (block[1] - block[0] == 1 and m and entry.count("\n") == 1
            and _safe_load_or_none(old_line) == _safe_load_or_none(old_line[:m.start()])):
        entry = entry.rstrip("\n") + m.group(0) + "\n"
    lines[block[0]:block[1]] = [entry]


def _safe_load_or_none(text: str):
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return None


def _load_vars_text(text: str) -> dict:
    try:
        parsed = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"vars.yaml is not valid YAML: {e}") from e
    if not isinstance(parsed, dict):
        raise ValueError("vars.yaml must be a mapping of names to values")
    return parsed


def _yaml_entry(name: str, value: object) -> str:
    """One `name: value` line (lists/maps inline), or a block if the value needs it."""
    # YAML reads bare keys like `on`, `no` or `y` as booleans; quote those
    key = name if yaml.safe_load(f"{name}: 0") == {name: 0} else f"'{name}'"
    inline = yaml.safe_dump(value, default_flow_style=True, allow_unicode=True, width=float("inf"))
    inline = inline.removesuffix("\n").removesuffix("\n...").rstrip("\n")
    if "\n" not in inline:
        return f"{key}: {inline}\n"
    return yaml.safe_dump({name: value}, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _vars_set(text: str, values: dict[str, object]) -> str:
    """Set top-level keys in vars.yaml text, keeping comments and order."""
    lines = text.splitlines(keepends=True)
    for name, value in values.items():
        entry = _yaml_entry(name, value)
        block = _var_block(lines, name)
        if block:
            _replace_entry(lines, block, entry)
        else:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(entry)
    new_text = "".join(lines)
    parsed = _load_vars_text(new_text)
    if any(parsed.get(n) != v for n, v in values.items()):
        raise ValueError("Could not cleanly edit vars.yaml; edit it by hand in the repo")
    return new_text


def _env_edit(text: str, set_values: dict[str, str] | None = None,
              remove: list[str] | None = None) -> str:
    """Set or remove keys in the env: section of vars.yaml text, keeping comments."""
    set_values, remove = set_values or {}, remove or []
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    block = _var_block(lines, "env")
    if block and lines[block[0]].split(":", 1)[1].split("#", 1)[0].strip():
        # Inline form (`env: {}` or `env: {A: b}`): rewrite as a block
        current = _env_section(_load_vars_text(lines[block[0]]))
        lines[block[0]:block[1]] = ["env:\n"] + ["  " + _yaml_entry(k, _env_str(v))
                                                 for k, v in current.items()]
        block = _var_block(lines, "env")
    if block is None:
        if not set_values:
            return text
        lines.append("env:\n")
        block = (len(lines) - 1, len(lines))

    start, end = block
    indent = next((re.match(r"[ \t]+", l).group(0) for l in lines[start + 1:end]
                   if _deeper(l, "") and not l.lstrip().startswith("#")), "  ")
    for name in remove:
        b = _var_block(lines, name, indent, start + 1, end)
        if b:
            del lines[b[0]:b[1]]
            end -= b[1] - b[0]
    for name, value in set_values.items():
        entry = indent + _yaml_entry(name, value)
        b = _var_block(lines, name, indent, start + 1, end)
        if b:
            _replace_entry(lines, b, entry)
            end -= b[1] - b[0] - 1
        else:
            lines.insert(end, entry)
            end += 1

    new_text = "".join(lines)
    env = _env_section(_load_vars_text(new_text))
    if any(_env_str(env.get(n)) != v or n not in env for n, v in set_values.items()) \
            or any(n in env for n in remove):
        raise ValueError("Could not cleanly edit vars.yaml; edit it by hand in the repo")
    return new_text


def _vars_unset(text: str, names: list[str]) -> str:
    """Remove top-level keys from vars.yaml text, keeping everything else."""
    lines = text.splitlines(keepends=True)
    for name in names:
        block = _var_block(lines, name)
        if block:
            del lines[block[0]:block[1]]
    new_text = "".join(lines)
    if any(n in _load_vars_text(new_text) for n in names):
        raise ValueError("Could not cleanly edit vars.yaml; edit it by hand in the repo")
    return new_text


def find_private(path: Path, include_binary: bool = False) -> Path | None:
    """
    First path that group and others can't access at all (e.g. 600, 700):
    `path` itself, or a synced file inside it if it's a directory.
    """
    if not path.stat().st_mode & 0o077:
        return path
    if path.is_dir():
        for f in iter_dir_files(path, include_binary):
            if not f.stat().st_mode & 0o077:
                return f
    return None


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
