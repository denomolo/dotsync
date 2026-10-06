# dotsync

Bidirectional dotfile sync backed by a git repo, with per-machine profiles
(git branches) and Jinja2 templating.

Edit a dotfile on any machine and `dotsync sync` pushes it to your dotfiles
repo; edit it in the repo (or on another machine) and the next sync pulls it
down. dotsync remembers what it last wrote to each file, so it can tell
which side changed and only asks a conflict policy to decide when both did.

## Contents

- [Install](#install)
- [Quick start](#quick-start)
- [Commands](#commands)
- [Automatic sync](#automatic-sync)
- [Dotfiles repo layout](#dotfiles-repo-layout)
- [Profiles](#profiles)
- [Templates](#templates)
- [Environment variables](#environment-variables)
- [Conflicts](#conflicts)
- [Configuration](#configuration)
- [What is not synced](#what-is-not-synced)
- [Development](#development)
- [Changelog](#changelog)
- [License](#license)
- [How it works](#how-it-works)

## Install

**Requirements:** Python 3.11+, [pipx](https://pipx.pypa.io/), git, and an
SSH key on your GitHub account (`ssh -T git@github.com` should greet you).
The optional auto-sync service needs systemd.

### 1. Install the tool

This repository is private, so you need collaborator access. Install a
tagged release straight from GitHub:

```sh
pipx install git+ssh://git@github.com/denomolo/dotsync.git@v0.7.0
dotsync --version
```

To upgrade later, run the same command with `--force` and the new tag. To
install from a local checkout instead: `pipx install --force .`

### 2. Create your dotfiles repo

Your dotfiles live in **your own** repo, separate from this one. Create a
new private repo on GitHub (e.g. `<you>/dotfiles`) and leave it **completely
empty** — no README, license or .gitignore — because dotsync creates the
first commit itself.

### 3. Set up dotsync

On your first machine, initialise the repo:

```sh
dotsync install --repo-url <you>/dotfiles --init
```

On every other machine, clone it instead (the default):

```sh
dotsync install --repo-url <you>/dotfiles
```

`--repo-url` accepts `owner/repo`, an HTTPS GitHub URL (both converted to
SSH so your key is used) or any git URL. `install` writes
`~/.config/dotsync/config.yaml`, and asks whether to sync automatically at
login (off unless you say yes; see [Automatic sync](#automatic-sync)). It
never touches the files in your home directory. If the repo you point it at
is still empty, `install` sets it up as a new dotsync repo even without
`--init`. Use `--profile <name>` to start on a profile other than `base`.

## Quick start

```sh
# Start tracking files (one commit, pushed to GitHub)
dotsync add ~/.vimrc ~/.bashrc ~/.config/kitty

# See what changed
dotsync status

# Sync both ways
dotsync sync
```

On another machine, after `dotsync install`, nothing is synced until you
say so. `sync` only handles files that were already synced on that
machine; everything else is listed by `status` and left alone:

```sh
dotsync status                       # what's in the repo, and what differs here
dotsync pull                         # take the repo's version of everything
dotsync pull ~/.config/kitty         # ...or just some files
dotsync push ~/.bashrc               # keep this machine's version instead
```

After a file has been pulled or pushed once on a machine, `sync` keeps it
in step from then on. The same goes for files that another machine adds to
the repo later: `sync` reports them until you `pull` them.

## Commands

| Command | What it does |
| --- | --- |
| `dotsync install` | First-time setup: clone (or `--init`) your dotfiles repo, write the config, optionally install the systemd service. |
| `dotsync sync` | Two-way sync of every tracked file (see [How it works](#how-it-works)). |
| `dotsync status [PATH...] [--diff]` | Show what `sync` would do for each file (or just `PATH`s), without changing anything; `--diff` shows how files differ. |
| `dotsync push [PATH...]` | Force disk → repo, for every file or just `PATH`s. Commits and pushes. |
| `dotsync pull [PATH...]` | Force repo → disk, for every file or just `PATH`s. |
| `dotsync add PATH... [--remote R] [--allow-private]` | Start tracking files or directories from disk. |
| `dotsync checkout REMOTE... [--local L] [--force]` | Start tracking files that are already in the repo, writing them to disk. |
| `dotsync remove PATH` | Stop tracking a file or directory. The copy on disk is kept. |
| `dotsync var list \| set NAME=VALUE... \| unset NAME...` | Manage [template variables](#variables). |
| `dotsync env list \| set NAME=VALUE... \| unset NAME... \| hook` | Manage [environment variables](#environment-variables) for your session. |
| `dotsync profile list \| set NAME \| new NAME` | Manage [profiles](#profiles). |
| `dotsync service install \| uninstall \| status` | Opt in to syncing automatically at login ([details](#automatic-sync)). |
| `dotsync --version` | Print the installed version. |

`sync`, `push`, `pull`, `add`, `checkout`, `remove`, `var set|unset` and
`env set|unset` accept `--dry-run` to show what would happen without
changing anything: incoming changes from GitHub are fetched and previewed
(including merging `base` into a profile) in a temporary copy, so your
files, the local clone's branches and its working tree stay exactly as
they were. `sync`, `push` and `pull` accept `-v` to list unchanged files
too.

Only one dotsync runs at a time. If you start a command while another one
is running (for example the background `sync`), it waits for it to
finish, and gives up after two minutes.

### Output symbols

| Symbol | Meaning |
| --- | --- |
| `↑` | disk → repo (pushed) |
| `↓` | repo → disk (pulled) |
| `!` | conflict, resolved by the configured policy (shown in brackets) |
| `−` | no longer tracked |
| `·` | already in sync |
| `-` | skipped (e.g. binary file) |

### `add`

```sh
dotsync add ~/.vimrc ~/.config/environment.d
dotsync add ~/.config/foo/*.conf
dotsync add '~/.config/foo/*.conf'          # quoted: dotsync expands it
dotsync add /etc/hosts --remote system/      # outside ~ needs --remote
```

- Each path is stored under `files/` at its location relative to your home
  directory: `~/.config/environment.d` → `files/.config/environment.d`.
- `--remote R` picks the location under `files/` instead. If `R` ends with
  `/`, is an existing directory, or several paths are given, each path keeps
  its name inside `R`.
- Everything is added in one commit. Paths that can't be added (missing,
  already tracked, binary, private, a symlink, or two paths that would land
  on the same repo path) are reported, the rest are still added, and the
  command exits with status 1.
- Private files are refused: a file only you can access (no group or other
  permissions, e.g. mode `600`), a directory like that (e.g. `~/.ssh` at
  `700`), or a directory containing such a file. They usually hold secrets,
  and their contents would end up in your dotfiles repo. Pass
  `--allow-private` to track them anyway.

### `checkout`

The reverse of `add`, for setting up a new machine from a populated repo.

```sh
dotsync checkout .vimrc                              # files/.vimrc → ~/.vimrc
dotsync checkout '.config/*'                         # glob, matched in the repo
dotsync checkout nvim --local ~/.config/nvim         # choose where it goes
dotsync checkout .bashrc --force                     # overwrite a different local file
```

- `REMOTE` paths are relative to `files/` (a leading `files/` is accepted).
- `--local` defaults to `~/<REMOTE>`, with any `.j2` suffix dropped.
- An existing local file with **the same** content is simply tracked. One
  with **different** content is left alone and reported, unless you pass
  `--force`.

### `status`

```sh
dotsync status                       # every tracked file
dotsync status ~/.vimrc              # just one (or several, or a quoted glob)
dotsync status ~/.config/kitty --diff
```

`--diff` prints a unified diff for each file that differs, from the repo's
version (rendered, for templates) to this machine's: `-` lines are only in
the repo, `+` lines only here. Directories are compared file by file, and
binary files are only reported as different. `status` compares against the
local clone and doesn't fetch; `dotsync sync --dry-run` also shows changes
waiting on GitHub.

### `push` and `pull`

```sh
dotsync push ~/.config/starship.toml
dotsync pull '~/.config/foo/*'      # also restores files deleted from disk
```

Without paths they act on every tracked file. Paths are files or
directories on disk as listed in the manifest; quoted globs are matched
against tracked paths rather than the filesystem. A file *inside* a tracked
directory is rejected — use the directory.

## Automatic sync

Automatic sync is off unless you opt in, and then it only runs at login:

```sh
dotsync service install      # sync at every login, after the network is up
dotsync service status
dotsync service uninstall
```

This installs a systemd user unit, `~/.config/systemd/user/dotsync.service`,
that runs `dotsync sync` once per login; it doesn't run a sync right away.
Output goes to the journal: `journalctl --user -u dotsync`. Without systemd,
run `dotsync sync` from your shell's startup files instead. (dotsync 0.6.0
also installed an hourly `dotsync.timer`; `service install` and `uninstall`
remove it.)

## Dotfiles repo layout

Every branch of your dotfiles repo has the same layout:

```
files/           # the dotfiles themselves
  .vimrc
  .config/kitty/kitty.conf
  .gitconfig.j2  # rendered as a template
vars.yaml        # variables for templates
manifest.yaml    # which repo path goes where on disk
README.md
```

`manifest.yaml` maps each repo path (`source`) to its location on disk
(`dest`). `add`, `checkout` and `remove` maintain it for you, keeping any
comments, but you can edit it by hand:

```yaml
files:
  - source: files/.vimrc
    dest: ~/.vimrc

  - source: files/.config/kitty
    dest: ~/.config/kitty
```

See [`manifest.yaml.example`](manifest.yaml.example) and
[`vars.yaml.example`](vars.yaml.example).

## Profiles

A profile is a git branch. `base` holds what every machine shares; each
other profile is a `profiles/<name>` branch that starts as a copy of `base`
and adds its own commits on top.

```mermaid
%%{init: {"gitGraph": {"mainBranchName": "base", "rotateCommitLabel": false, "parallelCommits": false}}}%%
gitGraph
    commit id: "add .vimrc"
    commit id: "add kitty"
    branch profiles/laptop
    commit id: "laptop: bigger font"
    checkout base
    commit id: "add starship"
    checkout profiles/laptop
    merge base id: "sync merges base"
```

```sh
dotsync profile new laptop     # create profiles/laptop from base
dotsync profile set laptop     # make it this machine's active profile
dotsync pull                   # apply it to disk
dotsync profile list           # base, laptop ← active
```

- To change a file or variable for one profile, edit it on that profile's
  branch: anything you push while the profile is active is committed there.
- Every `sync` (and `pull`/`checkout`) merges `base` into the active profile
  branch, so shared changes reach every profile.
- If the merge conflicts (both `base` and the profile changed the same
  lines), dotsync aborts it, warns you with the `git merge` command to run
  in the repo clone, and leaves the branch and your files untouched until
  you resolve it.

## Templates

Files ending in `.j2` are rendered with [Jinja2](https://jinja.palletsprojects.com/)
using the variables in `vars.yaml` before being written to disk. Undefined
variables are an error rather than silently empty.

```yaml
# vars.yaml
email: you@example.com
editor: nvim
```

```ini
# files/.gitconfig.j2
[user]
    email = {{ email }}
[core]
    editor = {{ editor }}
```

Directories are always copied verbatim, never rendered.

### Variables

Manage `vars.yaml` with `dotsync var` instead of editing it in the repo:

```sh
dotsync var set fullname="Ariel Shatil" email=ariel@example.com
dotsync var list
dotsync var unset editor
```

- `set` and `unset` change `vars.yaml` on the **active profile's branch**,
  re-render the templates on disk, and commit and push, so other machines
  get the change on their next sync. Comments in `vars.yaml` are kept.
- Values are stored as strings: `port=22` and `debug=true` stay `"22"` and
  `"true"`. Add `--yaml` to parse values as YAML instead, for numbers,
  booleans or lists (`--yaml retries=3 hosts="[a, b]"`).
- `unset` refuses a variable that a tracked template still uses, since the
  template could no longer render; `--force` unsets it anyway.
- Both take `--dry-run`. Names are letters, digits and `_`, not starting
  with a digit.
- On a profile, `set` overrides the `base` value for that profile only, and
  later changes to `base` still merge in. `unset` on a profile removes the
  variable for that profile; it doesn't fall back to `base`'s value.

Templated files only flow **repo → disk**. If you edit the rendered file on
disk (e.g. `~/.gitconfig`), `sync` and `push` report an error instead of
copying it over the template, and leave both sides alone. Make the change in
the `.j2` file in the repo, or run `dotsync pull <path>` to discard the
local edit. See
[`dot_gitconfig.j2.example`](dot_gitconfig.j2.example).

## Environment variables

`dotsync env` manages environment variables for your session, such as
`EDITOR` or `GIT_AUTHOR_EMAIL`. They live in their own `env:` section of
`vars.yaml`, separate from template variables, so only what you list there
is exported:

```sh
dotsync env set EDITOR=nvim BROWSER=firefox
dotsync env set GIT_AUTHOR_EMAIL='{{ email }}'   # reuse a template variable
dotsync env list
dotsync env unset BROWSER
```

```yaml
# vars.yaml
email: ariel@example.com
env:
  EDITOR: nvim
  GIT_AUTHOR_EMAIL: '{{ email }}'
```

On every `sync`, `pull`, `checkout` and `var`/`env` change, dotsync writes
them to **`~/.config/environment.d/99-env.conf`**, one `KEY="value"` per
line, a format both systemd and POSIX shells read:

- **systemd user session:** reads the file by itself, so user services and
  the graphical session on desktops started through systemd (e.g. GNOME,
  KDE Plasma) get the variables. dotsync also updates the running session,
  so newly started services see changes straight away.
- **Shells** (TTYs, SSH, systems without systemd): add the line printed by
  `dotsync env hook` to `~/.profile`, and to `~/.bashrc` / `~/.zshrc` to
  have it in every interactive shell:

```sh
[ -r ~/.config/environment.d/99-env.conf ] && { set -a; . ~/.config/environment.d/99-env.conf; set +a; }
```

- Values may use template variables (`'{{ email }}'`); quote them with
  single quotes so your shell leaves the braces alone. `var unset` refuses
  a template variable that an env value still uses.
- Values can't contain newlines or `$` (systemd would expand it), so
  `PATH`-style expansion isn't supported. Variables your session manages
  (`PATH`, `HOME`, `USER`, `SHELL`, `DISPLAY`, `XDG_RUNTIME_DIR`, `LD_*`
  and similar) are refused.
- systemd applies `environment.d` files in name order, later ones winning.
  Many distributions link `/etc/environment` in as `99-environment.conf`,
  which sorts *after* `99-env.conf`, so for any name set in both (often
  `EDITOR`, `VISUAL`) the systemd session uses `/etc/environment`'s value
  from the next login on. Until then, the running session has dotsync's
  value, since dotsync pushes changes into it directly. Shells that source
  the hook always get dotsync's value.
- `99-env.conf` belongs to dotsync: don't edit it, and if you track
  `~/.config/environment.d` itself, dotsync leaves this file out of the
  repo.
- Like everything in `vars.yaml`, env variables are per profile.
- Shells that are already open keep their old environment; open a new one
  (or re-run the hook line) to pick up changes. Graphical sessions pick up
  `environment.d` changes at the next login.

## Conflicts

A conflict means a file changed both on disk and in the repo since it was
last synced on this machine. (A file that was never synced on this machine
isn't a conflict: `sync` leaves it alone until you `pull` or `push` it.)
`conflict_resolution` in the config decides what happens:

| Mode | Behaviour |
| --- | --- |
| `machine-wins` (default) | This machine's version wins and is pushed. |
| `last-write-wins` | The newer side wins: the file's modification time on disk vs the time of the last commit that changed it in the repo. |
| `git-wins` | The repo's version wins and overwrites the local file. |

`machine-wins` is the default because it never loses anything: the repo's
version it replaces stays in git history, while a local file that gets
overwritten is gone. The sync output shows where to find it:

```
!  ~/.vimrc  [machine-wins; repo version kept in history at 3f2c1ab]
```
```sh
git -C ~/.local/share/dotsync/repo show 3f2c1ab:files/.vimrc   # view it
dotsync push ~/.vimrc                                           # after restoring the file
```

`dotsync push` and `dotsync pull` are one-off overrides equivalent to
`machine-wins` and `git-wins`.

## Configuration

`~/.config/dotsync/config.yaml` (override the location with the
`DOTSYNC_CONFIG` environment variable):

```yaml
repo_url: git@github.com:you/dotfiles.git
profile: base                          # active profile
conflict_resolution: machine-wins      # or last-write-wins / git-wins
auto_push: true                        # push to GitHub after each commit
include_binary: false                  # sync binary files too
repo_path: ~/.local/share/dotsync/repo
state_path: ~/.local/state/dotsync/state.json
```

## What is not synced

- **Binary files** (a NUL byte in the first 8 KB) are skipped unless
  `include_binary: true`.
- **Symlinks** are never tracked or followed; inside a tracked directory
  they are left untouched on both sides.
- **Nested `.git` directories** inside a tracked directory are ignored, so
  they don't turn into submodules.
- **Private files** (e.g. mode `600`) are refused by `add` unless you pass
  `--allow-private`.
- **Permissions**, apart from the executable bit: git records only whether
  a file is executable, so pulled files get your default permissions
  (usually `644`, or `755` for executables). A file tracked with
  `--allow-private` is therefore *not* kept at `600` on other machines. A
  change to the executable bit alone, with no content change, isn't synced.

## Development

```sh
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

Each test runs the real CLI in an isolated sandbox: a bare git repo standing
in for GitHub, a temporary home directory and config, and a fake
`systemctl` that only records its arguments. Tests never touch your real
files, dotfiles repo or systemd session. The suite runs on every push and
pull request (`.github/workflows/tests.yml`).

## Changelog

See [CHANGELOG.md](CHANGELOG.md). dotsync is pre-1.0: minor releases may
include breaking changes, which are always called out there.

## License

Copyright (C) 2026 Ariel Shatil

dotsync is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option)
any later version. It is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See [LICENSE](LICENSE)
for the full text.

## How it works

Three places are involved: your files on disk, a local clone of your
dotfiles repo, and GitHub. A machine-local state file records the hash of
what was last synced for each file, which is how dotsync tells which side
changed. Templates (`.j2`) are rendered on the way to disk.

```mermaid
%%{init: {"flowchart": {"nodeSpacing": 18, "rankSpacing": 28, "padding": 6, "diagramPadding": 4}}}%%
flowchart LR
    state["state.json<br/>last-synced hashes"]
    disk["Disk<br/>~/.vimrc …"]
    clone["Local clone<br/>~/.local/share/dotsync/repo"]
    gh[("GitHub")]
    state -.- disk
    disk -- "push / add" --> clone
    clone -- "pull / checkout" --> disk
    clone -- "git push" --> gh
    gh -- "git pull" --> clone
```

### What `dotsync sync` does

On a profile other than `base`, sync first merges `base` into the
profile branch. It then decides each file as below, commits and pushes if
anything in the repo changed (plain pushes or conflicts the disk won), and
saves the state file.

```mermaid
%%{init: {"flowchart": {"nodeSpacing": 18, "rankSpacing": 28, "padding": 6, "diagramPadding": 4}}}%%
flowchart LR
    start(["sync"]) --> pull["git pull<br/>profile branch"]
    pull --> merge["merge base in<br/>(profiles only)"]
    merge --> each["each file:<br/>decide + act"]
    each --> commit["commit + push<br/>if repo changed"]
    commit --> save(["save state"])
```

### How each file is decided

For every tracked file dotsync computes three hashes: the file on disk, the
file in the repo (after rendering templates), and the one it recorded the
last time it synced that file.

```mermaid
%%{init: {"flowchart": {"nodeSpacing": 18, "rankSpacing": 28, "padding": 6, "diagramPadding": 4}}}%%
flowchart LR
    f(["file"]) --> known{"synced<br/>before?"}
    known -- no --> samenew{"disk =<br/>repo?"}
    samenew -- yes --> recnew["· record"]
    samenew -- no --> wait["- skip until<br/>pull / push"]
    known -- yes --> changed{"changed<br/>since?"}
    changed -- neither --> nothing["· in sync"]
    changed -- disk --> push["↑ push"]
    changed -- repo --> pull["↓ pull"]
    changed -- both --> same{"disk =<br/>repo?"}
    same -- yes --> rec["· record"]
    same -- no --> conf["! conflict"]
```

*record*: nothing to copy, just remember the hash. *skip*: a file never
synced on this machine (missing here, or different) is left alone until you
`pull` or `push` it. *conflict*: settled by `conflict_resolution` (default
`machine-wins`; see [Conflicts](#conflicts)).
