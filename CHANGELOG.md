# Changelog

All notable changes to dotsync are documented here. Versions follow
[Semantic Versioning](https://semver.org/); while on 0.x, minor releases may
include breaking changes.

## [Unreleased]

### Added
- `dotsync status [PATH...]` shows the status of just those tracked files or
  directories, and `--diff` shows how each differing file differs (repo `-`,
  this machine `+`; directories file by file, templates rendered).

### Changed
- **Breaking:** `sync` (and the login service) only handles files that were
  already synced on this machine. Files that are missing here, differ from
  the repo, or were added by another machine are left alone and listed until
  you `pull`, `push` or `checkout` them; identical files start syncing
  quietly. Before, `sync` wrote missing files and treated differing ones as
  conflicts, so a first sync on a new machine could push the distribution's
  default `.bashrc` over yours.
- **Breaking:** the default `conflict_resolution` is now `machine-wins`: the
  local version wins and the repo's version stays in git history, and the
  sync output names the commit to recover it from. Existing configs that set
  `conflict_resolution` explicitly are unchanged.
- Automatic sync is opt-in and login-only: `install` asks with a default of
  no, and `service install` no longer adds the hourly timer from 0.6.0 (it
  removes it if present). The service is enabled for the next login rather
  than run immediately.
- `install` never changes files in your home directory, and its closing
  hints point to `status` and `pull` for a machine joining an existing repo.

### Fixed
- `install` against a repo whose default branch isn't `base` (e.g. a GitHub
  repo created with `main`) cloned without checking anything out, mistook it
  for an empty repo and re-initialised it, ignoring its history. It now checks
  out `base` (or the chosen profile), and refuses repos with no `base` branch.

## [0.6.0] - 2026-10-05

### Added
- Only one dotsync runs at a time: commands take a lock on
  `~/.local/state/dotsync/lock`, wait for a running dotsync (e.g. the login
  service) to finish, and give up after two minutes.
- `dotsync service install` also installs a `dotsync.timer` that syncs every
  hour while you're logged in, not just at login. `--interval` (e.g. `15min`)
  changes it, `--no-timer` keeps login-only sync; `uninstall` and `status`
  cover both units.
- `add --allow-private` warns that the file will arrive on other machines with
  default permissions (usually 644) and that its contents are in the repo.

### Fixed
- `dotsync install` pointed at an empty repo without `--init` reported
  success but left a broken clone (no `base` branch or manifest); it now sets
  the repo up as a new dotsync repo.
- Repo and template errors (e.g. a missing `manifest.yaml`) are shown as a
  message instead of a Python traceback in every command.
- The service's start timeout is 3 minutes (was 60 seconds), so a background
  sync waiting for another dotsync isn't killed by systemd.
- `--dry-run` no longer changes the local clone. `sync`, `pull`,
  `checkout` and `var`/`env` dry runs used to run `git pull` and merge
  `base` into the profile branch for real; they now fetch and preview the
  result in a temporary worktree, so incoming changes are still shown.

## [0.5.1] - 2026-10-05

### Added
- Test suite (`pytest`, run with `pip install -e '.[dev]'` then `pytest`)
  covering tracking, sync decisions, profiles, templates, permissions and
  `var`/`env`, plus a GitHub Actions workflow running it on Python 3.11–3.14.

### Fixed
- `dotsync var set` / `env set` failed for names YAML reads as booleans or
  null (`on`, `off`, `yes`, `no`, `y`, `n`, `true`, `null`); those keys are
  now quoted in `vars.yaml`.

## [0.5.0] - 2026-10-05

### Added
- Licensed under the GNU General Public License v3.0 or later (`LICENSE`),
  declared in the package metadata.
- `dotsync add` refuses private files (no group/other permissions, e.g.
  mode 600 or a 700 directory like `~/.ssh`, or a directory containing such
  a file), since they usually hold secrets. `--allow-private` overrides it.
- `dotsync var list | set NAME=VALUE... | unset NAME...` manages template
  variables in `vars.yaml` on the active profile's branch: it keeps comments,
  re-renders templates on disk, and commits and pushes. Values are strings
  unless `--yaml` is given; `unset` refuses variables a template still uses
  unless `--force`.
- `dotsync env list | set | unset | hook` manages session environment
  variables in a separate `env:` section of `vars.yaml`. Values may use
  template variables. dotsync writes them to
  `~/.config/environment.d/99-env.conf`, which the systemd user session
  reads (dotsync also updates the running session) and shells source via
  the line `env hook` prints. The file is never tracked.

### Fixed
- Pulling a single file keeps its executable bit; scripts used to come out
  as mode 644. (Files inside tracked directories already kept it.)

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
