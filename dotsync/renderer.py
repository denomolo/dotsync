"""
renderer.py — Jinja2 template rendering with profile variable merging.

Variable loading order (later overrides earlier):
  1. base/vars/base.yaml          ← shared across all profiles
  2. profiles/<n>/vars/<n>.yaml   ← profile-specific overrides

Templates (.j2 files) are rendered with the merged variable set.
Plain files are returned as-is (no rendering).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound


class RenderError(Exception):
    pass


class Renderer:
    def __init__(self, repo_path: Path, profile: str):
        self.repo_path = repo_path
        self.profile = profile
        self._vars: dict[str, Any] | None = None

        # Jinja2 env — searches both base/files and profiles/<n>/files
        search_paths = [
            str(repo_path / "base" / "files"),
            str(repo_path / "profiles" / profile / "files"),
        ]
        self.env = Environment(
            loader=FileSystemLoader(search_paths),
            undefined=StrictUndefined,       # error on missing variables
            keep_trailing_newline=True,
        )

    # ── Variables ──────────────────────────────────────────────────────────────

    def _load_yaml(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        with path.open() as f:
            return yaml.safe_load(f) or {}

    @property
    def vars(self) -> dict[str, Any]:
        if self._vars is None:
            base_vars = self._load_yaml(self.repo_path / "base" / "vars" / "base.yaml")
            profile_vars = self._load_yaml(
                self.repo_path / "profiles" / self.profile / "vars" / f"{self.profile}.yaml"
            )
            self._vars = {**base_vars, **profile_vars}
        return self._vars

    # ── Rendering ─────────────────────────────────────────────────────────────

    def render(self, source_rel: str) -> bytes:
        """
        Render a source path (relative to repo root) to bytes.

        - If the file ends in .j2, renders as a Jinja2 template.
        - Otherwise, returns raw bytes.
        - Directories can't be rendered; use resolve_source() and copy them.

        Profile files take precedence over base files for templates
        (Jinja2 FileSystemLoader searches in order: base, then profile —
        but we reverse so profile wins by putting it last and using
        select_template logic below).
        """
        # Resolve absolute path: profile overrides base
        abs_path = self.resolve_source(source_rel)

        if abs_path.is_dir():
            raise RenderError(f"Cannot render a directory: {source_rel}")
        if source_rel.endswith(".j2") or abs_path.suffix == ".j2":
            return self._render_template(abs_path)
        else:
            return abs_path.read_bytes()

    def resolve_source(self, source_rel: str) -> Path:
        """
        Find the actual file on disk, preferring profile over base.
        source_rel is relative to the repo root.
        """
        # Try profile-specific path first
        # e.g. "base/files/dot_zshrc.j2" → check "profiles/<n>/files/dot_zshrc.j2"
        candidates = []

        if source_rel.startswith("base/files/"):
            filename = source_rel[len("base/files/"):]
            profile_path = self.repo_path / "profiles" / self.profile / "files" / filename
            if profile_path.exists():
                candidates.append(profile_path)

        candidates.append(self.repo_path / source_rel)

        for c in candidates:
            if c.exists():
                return c

        raise RenderError(f"Source file not found: {source_rel}")

    def _render_template(self, abs_path: Path) -> bytes:
        """Render a .j2 file using the merged variable set."""
        try:
            template_text = abs_path.read_text()
            from jinja2 import Template
            tmpl = self.env.from_string(template_text)
            rendered = tmpl.render(**self.vars)
            return rendered.encode()
        except Exception as e:
            raise RenderError(f"Template render failed for {abs_path}: {e}") from e

    # ── Manifest loading ───────────────────────────────────────────────────────

    def load_manifest(self) -> list[dict[str, str]]:
        """
        Load and return the list of file mappings from manifest.yaml.

        Each entry has:
          source: relative path in repo (e.g. "base/files/dot_zshrc.j2")
          dest:   absolute destination path (e.g. "~/.zshrc")
        """
        manifest_path = self.repo_path / "manifest.yaml"
        if not manifest_path.exists():
            raise RenderError(f"manifest.yaml not found at {manifest_path}")

        with manifest_path.open() as f:
            raw = yaml.safe_load(f) or {}

        entries = raw.get("files", [])
        if not isinstance(entries, list):
            raise RenderError("manifest.yaml: 'files' must be a list")

        result = []
        for entry in entries:
            if "source" not in entry or "dest" not in entry:
                raise RenderError(f"manifest.yaml: entry missing 'source' or 'dest': {entry}")
            result.append({
                "source": entry["source"],
                "dest": str(Path(entry["dest"]).expanduser()),
            })
        return result
