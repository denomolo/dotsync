# Changelog

All notable changes to dotsync are documented here. Versions follow
[Semantic Versioning](https://semver.org/); while on 0.x, minor releases may
include breaking changes.

## [0.4.3] - 2026-10-03

### Fixed
- Local edits to a file rendered from a `.j2` template are no longer pushed
  over the template (which replaced every `{{ variable }}` with its value).
  `sync` and `push` now report an error and leave both sides untouched; edit
  the template in the repo, or `dotsync pull` the file to discard the edit.

## [0.4.2] - 2026-10-03

### Added
- README with install instructions, a command reference and flowcharts of
  how sync works.

### Fixed
- `last-write-wins` no longer always picks the repo when both sides
  changed. It compared against the clone's file mtime, which `git pull`
  resets at the start of every sync, so local edits were overwritten. It
  now uses the time of the last commit that changed the file.
- Conflicts won by the machine during `sync` are now committed and pushed;
  before, they stayed uncommitted in the local clone until an unrelated
  push.

## [0.4.1] - 2026-10-03

### Fixed
- `dotsync status` and `--dry-run` summaries say what would happen
  ("↑ 1 to push") instead of reporting it as done ("↑ 1 pushed").

## [0.4.0] - 2026-10-03

### Added
- `dotsync push` and `dotsync pull` accept paths to act on just those
  tracked files or directories, e.g. `dotsync push ~/.vimrc`. Quoted globs
  are matched against tracked paths, so `dotsync pull '~/.config/foo/*'`
  works even for files missing from disk. Without paths they still act on
  everything.

## [0.3.0] - 2026-10-01

### Changed
- `dotsync add` without `--remote` keeps the path's hierarchy relative to
  your home directory: `~/.config/environment.d` is stored as
  `files/.config/environment.d` instead of `files/environment.d`, so a
  plain `dotsync checkout .config/environment.d` maps it back. Paths outside
  `~` now need `--remote`. Existing manifest entries are unaffected.

## [0.2.0] - 2026-09-30

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
