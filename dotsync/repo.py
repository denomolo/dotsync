"""
repo.py — Git operations for dotsync.

Branch model:
  base              ← shared files, all profiles merge from here
  profiles/<name>   ← base plus profile-specific commits

Every branch has the same layout: files/ (the dotfiles), vars.yaml
(template variables) and manifest.yaml (what goes where on disk).

Deliberately shells out to git rather than using libgit2/pygit2,
because libgit2's merge support is incomplete.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


class GitError(Exception):
    pass


_HTTPS_GITHUB_RE = re.compile(r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$")
_SHORTHAND_RE = re.compile(r"^(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)$")


def normalize_github_url(url: str) -> str:
    """Prefer the existing GitHub SSH key over HTTPS auth prompts.

    Rewrites an https://github.com/... URL (or an owner/repo shorthand) to
    the equivalent git@github.com:owner/repo.git SSH form. Anything else
    (an existing ssh/git URL, a non-GitHub host, a local path) passes through
    unchanged.
    """
    m = _HTTPS_GITHUB_RE.match(url.strip())
    if not m:
        m = _SHORTHAND_RE.match(url.strip())
    if not m:
        return url
    return f"git@github.com:{m.group('owner')}/{m.group('repo')}.git"


class Repo:
    def __init__(self, path: Path):
        self.path = path

    # ── Internal ───────────────────────────────────────────────────────────────

    def _run(self, *args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(
            ["git", *args],
            cwd=self.path,
            text=True,
            capture_output=capture,
        )
        if check and result.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
        return result

    # ── Setup ──────────────────────────────────────────────────────────────────

    @classmethod
    def clone(cls, url: str, path: Path) -> "Repo":
        path.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["git", "clone", url, str(path)],
            text=True, capture_output=True,
        )
        if result.returncode != 0:
            raise GitError(f"git clone failed:\n{result.stderr.strip()}")
        return cls(path)

    @classmethod
    def init(cls, path: Path, url: str) -> "Repo":
        """Init a new repo and set the remote, creating base branch structure."""
        path.mkdir(parents=True, exist_ok=True)
        repo = cls(path)
        repo._run("init", "-b", "base")
        repo._run("remote", "add", "origin", url)

        (path / "files").mkdir(parents=True, exist_ok=True)
        (path / "files" / ".keep").touch()

        # Initial manifest and vars
        manifest = path / "manifest.yaml"
        if not manifest.exists():
            manifest.write_text(
                "# dotsync manifest\n"
                "# Each entry maps a source (relative to the repo root) to a destination.\n"
                "#\n"
                "# files:\n"
                "#   - source: files/dot_zshrc.j2\n"
                "#     dest: ~/.zshrc\n"
                "#   - source: files/dot_gitconfig\n"
                "#     dest: ~/.gitconfig\n"
                "files: []\n"
            )

        vars_file = path / "vars.yaml"
        if not vars_file.exists():
            vars_file.write_text(
                "# Template variables for .j2 files (profile branches can override them)\n"
                "# hostname: my-machine\n"
                "# email: you@example.com\n"
            )

        readme = path / "README.md"
        if not readme.exists():
            readme.write_text("# dotsync dotfiles\n\nManaged by [dotsync](https://github.com/you/dotsync).\n")

        repo._run("add", "-A")
        repo._run("commit", "-m", "init: scaffold dotsync repo")
        return repo

    # ── Branch management ──────────────────────────────────────────────────────

    def current_branch(self) -> str:
        return self._run("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

    def branch_exists(self, branch: str, remote: bool = False) -> bool:
        if remote:
            result = self._run("ls-remote", "--heads", "origin", branch, check=False)
            return bool(result.stdout.strip())
        result = self._run("branch", "--list", branch, check=False)
        return bool(result.stdout.strip())

    def checkout(self, branch: str, create: bool = False) -> None:
        if create:
            self._run("checkout", "-b", branch)
        else:
            self._run("checkout", branch)

    def create_profile_branch(self, profile: str) -> None:
        """Create profiles/<profile> branched from base."""
        branch = f"profiles/{profile}"
        if self.branch_exists(branch):
            raise GitError(f"Branch {branch!r} already exists.")
        self.checkout("base")
        self.checkout(branch, create=True)

    def merge_base(self) -> bool:
        """Merge base (local and origin) into the current branch. True if anything merged."""
        current = self.current_branch()
        if current == "base":
            raise GitError("Already on base branch — nothing to merge.")
        before = self._run("rev-parse", "HEAD").stdout.strip()
        for ref in ("origin/base", "base"):
            if self._run("rev-parse", "--verify", "--quiet", ref, check=False).returncode != 0:
                continue
            result = self._run("merge", ref, "--no-edit", check=False)
            if result.returncode != 0:
                self._run("merge", "--abort", check=False)
                raise GitError(
                    f"Merging {ref} into {current} conflicts; resolve it by hand in {self.path} "
                    f"(git merge {ref})"
                )
        return self._run("rev-parse", "HEAD").stdout.strip() != before

    # ── Remote sync ────────────────────────────────────────────────────────────

    def fetch(self) -> None:
        self._run("fetch", "--all", "--prune")

    def pull(self, branch: str) -> None:
        self._run("pull", "origin", branch)

    def push(self, branch: str) -> None:
        self._run("push", "--set-upstream", "origin", branch)

    # ── Staging and committing ─────────────────────────────────────────────────

    def add_all(self) -> None:
        self._run("add", "-A")

    def commit(self, message: str) -> bool:
        """Commit staged changes. Returns True if a commit was made."""
        result = self._run("diff", "--cached", "--quiet", check=False)
        if result.returncode == 0:
            return False  # nothing staged
        self._run("commit", "-m", message)
        return True

    def has_uncommitted(self) -> bool:
        result = self._run("status", "--porcelain")
        return bool(result.stdout.strip())

    def last_commit_time(self, path: str) -> float | None:
        """
        Unix time of the last commit touching `path` (file or directory), or
        None if it has uncommitted changes or was never committed.
        """
        if self._run("status", "--porcelain", "--", path).stdout.strip():
            return None
        out = self._run("log", "-1", "--format=%ct", "--", path, check=False).stdout.strip()
        return float(out) if out else None

    # ── Diff / status ──────────────────────────────────────────────────────────

    def status(self) -> str:
        return self._run("status", "--short").stdout

    def log_since(self, ref: str, branch: str | None = None) -> str:
        """Return commits on branch since ref."""
        target = branch or self.current_branch()
        return self._run("log", "--oneline", f"{ref}..{target}").stdout

    def remote_ahead(self, branch: str) -> bool:
        """Return True if origin has commits we don't have locally."""
        self.fetch()
        result = self._run(
            "rev-list", "--count", f"HEAD..origin/{branch}", check=False
        )
        try:
            return int(result.stdout.strip()) > 0
        except ValueError:
            return False
