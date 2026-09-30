# Changelog

All notable changes to dotsync are documented here. Versions follow
[Semantic Versioning](https://semver.org/); while on 0.x, minor releases may
include breaking changes.

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
