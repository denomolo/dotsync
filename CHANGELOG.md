# Changelog

All notable changes to dotsync are documented here. Versions follow
[Semantic Versioning](https://semver.org/); while on 0.x, minor releases may
include breaking changes.

## [Unreleased]

### Added
- `dotsync add` accepts several paths and glob patterns (quoted patterns
  are expanded by dotsync, e.g. `'~/.config/foo/*.conf'`). Everything is
  added in one commit; paths that can't be added are reported while the
  rest are still added.
- `dotsync checkout <remote>... [--local PATH]`, the reverse of `add`:
  starts tracking files already in the repo (paths or globs relative to
  `files/`) by writing them to disk and adding manifest entries — for
  setting up a new machine. `--local` defaults to `~/<remote>` minus any
  `.j2` suffix; existing local files with different content are only
  overwritten with `--force`.

### Changed
- **Breaking:** profiles are now purely git branches, and every branch uses
  one flat layout: `files/` (dotfiles), `vars.yaml` (template variables) and
  `manifest.yaml`. The `base/files/`, `base/vars/base.yaml` and
  `profiles/<name>/{files,vars}/` override directories are gone; to override
  something for a profile, edit it on that profile's branch. To migrate a
  repo, on each branch: `git mv base/files files`,
  `git mv base/vars/base.yaml vars.yaml`, and change `source: base/files/`
  to `source: files/` in `manifest.yaml`.
- **Breaking:** the storage location for `dotsync add` moved from a second
  positional argument to `--remote REMOTE`. With several paths, `--remote` is
  treated as a directory.

### Fixed
- Changes on `base` now reach profile branches: sync merges `base` into the
  active profile branch (and pushes the merge). On a merge conflict the
  merge is aborted with a warning, leaving the branch untouched.
- `dotsync profile list` now includes `base`.
- Result summaries no longer count failed files as pushed/pulled.

## [0.1.1] - 2026-09-30

### Fixed
- The `base` profile now uses the `base` branch instead of a nonexistent
  `profiles/base`. Previously `dotsync add` failed with "pathspec
  'profiles/base' did not match", and `sync`/`push` skipped committing
  with only a warning.

## [0.1.0] - 2026-09-30

First versioned (pre-stable) release.

### Added
- Bidirectional sync with profiles (git branches) and Jinja2 templating.
- `dotsync install`, `sync`, `push`, `pull`, `status`, `profile`, `service`.
- `dotsync add` to track a file or directory.
- `dotsync remove` to stop tracking a file or directory; the disk copy is
  kept. Reports when the path doesn't exist or isn't tracked.
- Binary files are skipped by default — only text files are synced. Set
  `include_binary: true` in `~/.config/dotsync/config.yaml` to sync them.
- `dotsync --version`, with the version single-sourced from
  `dotsync/__init__.py`.
