"""
syncdot — bidirectional dotfile sync with profiles and templating.

Usage:
    syncdot init <repo>              Set syncdot up on this machine
    syncdot sync [--dry-run]         Bidirectional sync
    syncdot push [<path>...]         Force disk → git (all, or just <path>s)
    syncdot pull [<path>...]         Force git → disk (all, or just <path>s)
    syncdot status [<path>...]       Show per-file state (--diff for details)
    syncdot add <path>...            Track files/dirs, copying them to the repo (--remote R)
    syncdot checkout <remote>...     Track repo files, writing them to disk (--local L)
    syncdot remove <path>...         Stop tracking files/dirs (disk copies are kept)
    syncdot var list|set|unset       Manage template variables (vars.yaml)
    syncdot env list|set|unset|hook  Manage session environment variables
    syncdot profile list             List available profiles
    syncdot profile set <name>       Switch active profile
    syncdot profile new <name>       Create a new profile branch
    syncdot service install          Sync automatically at login (opt-in)
    syncdot service uninstall        Stop syncing at login
    syncdot service status           Show the login service status
"""

from __future__ import annotations

import functools
import glob
import shutil
import os
import sys
from pathlib import Path

import click
import yaml

from . import __version__
from .config import Config, CONFIG_PATH, profile_branch
from .lock import LockTimeout, repo_lock
from .migrate import migrate_from_dotsync
from .repo import Repo, GitError, normalize_github_url
from .renderer import RenderError
from .sync import NOT_SET_UP, Action, PullDeclined, Syncer, find_private
from . import systemd


# ── Helpers ────────────────────────────────────────────────────────────────────

# Exit codes (part of the stable interface): 0 success, 1 error, 2 usage error
# (from click), EXIT_PENDING from `status --exit-code` when something would change
EXIT_PENDING = 3

ACTION_SYMBOLS = {
    Action.NOTHING:  click.style("·", fg="bright_black"),
    Action.PULL:     click.style("↓", fg="cyan"),
    Action.PUSH:     click.style("↑", fg="green"),
    Action.CONFLICT: click.style("!", fg="yellow"),
    Action.UNTRACK:  click.style("−", fg="red"),
}


def print_results(results, verbose: bool = False, pending: bool = False) -> int:
    """Print per-file results and a summary (pending=True words it as not yet
    done). Returns the number of errors."""
    errors = 0
    for r in results:
        sym = ACTION_SYMBOLS[r.action]
        dest = str(r.dest).replace(str(Path.home()), "~")
        line = f"  {sym}  {dest}"
        if r.skipped:
            sym = click.style("-", fg="bright_black")
            line = f"  {sym}  {dest}" + click.style(f"  [skipped: {r.skipped}]", fg="bright_black")
        if r.resolved_by:
            line += click.style(f"  [{r.resolved_by}]", fg="yellow")
        if r.error:
            line += click.style(f"  ERROR: {r.error}", fg="red")
            errors += 1
        if verbose or r.action != Action.NOTHING or r.error or r.skipped:
            click.echo(line)

    counts = {a: sum(1 for r in results if r.action == a and not r.skipped and not r.error) for a in Action}
    skipped = sum(1 for r in results if r.skipped)
    parts = []
    if counts[Action.PULL]:
        parts.append(click.style(f"↓ {counts[Action.PULL]} " + ("to pull" if pending else "pulled"), fg="cyan"))
    if counts[Action.PUSH]:
        parts.append(click.style(f"↑ {counts[Action.PUSH]} " + ("to push" if pending else "pushed"), fg="green"))
    if counts[Action.CONFLICT]:
        parts.append(click.style(f"! {counts[Action.CONFLICT]} conflicts", fg="yellow"))
    if counts[Action.UNTRACK]:
        parts.append(click.style(f"− {counts[Action.UNTRACK]} " + ("to untrack" if pending else "untracked"), fg="red"))
    if counts[Action.NOTHING] and not parts:
        parts.append(click.style("already in sync", fg="bright_black"))
    if skipped:
        parts.append(click.style(f"{skipped} skipped", fg="bright_black"))
    if errors:
        parts.append(click.style(f"{errors} errors", fg="red"))

    click.echo("\n  " + "  ".join(parts))
    if any(r.skipped == NOT_SET_UP for r in results):
        click.echo(click.style(
            "\n  Files not synced on this machine yet are left alone. Run `syncdot pull <path>`\n"
            "  to take the repo's version or `syncdot push <path>` to keep this machine's\n"
            "  (`syncdot pull` alone takes the repo's version of everything).", fg="bright_black"))
    return errors


def locked(command):
    """Run a command while holding the syncdot lock, so runs never overlap."""
    @functools.wraps(command)
    def wrapper(*args, **kwargs):
        cfg = load_config_or_exit()
        def on_wait(pid: str) -> None:
            click.echo(click.style(f"Another syncdot is running (pid {pid}); waiting for it…",
                                   fg="yellow"), err=True)
        try:
            with repo_lock(cfg.state_path.parent / "lock", on_wait=on_wait):
                return command(*args, **kwargs)
        except (LockTimeout, GitError, RenderError) as e:
            click.echo(click.style(str(e), fg="red"), err=True)
            sys.exit(1)
    return wrapper


def load_config_or_exit() -> Config:
    try:
        return Config.load()
    except FileNotFoundError as e:
        click.echo(click.style(str(e), fg="red"), err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(click.style(f"Config error: {e}", fg="red"), err=True)
        sys.exit(1)


# ── CLI root ───────────────────────────────────────────────────────────────────

@click.group()
@click.version_option(__version__, prog_name="syncdot")
def cli():
    """syncdot — bidirectional dotfile sync with profiles and templating."""
    for line in migrate_from_dotsync():
        click.echo(click.style(f"syncdot was called dotsync up to 0.8.0. {line}", fg="yellow"),
                   err=True)


# ── init ───────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("repo")
@click.option("--profile", default="base", show_default=True, help="Profile to use on this machine.")
@click.option("--new", is_flag=True,
              help="Start a new syncdot repo locally, for a GitHub repo that doesn't exist yet "
                   "(it's pushed on your first `syncdot add`).")
def init(repo: str, profile: str, new: bool):
    """Set syncdot up on this machine with REPO, your dotfiles repo.

    REPO is a git URL or a GitHub owner/repo shorthand; HTTPS GitHub URLs are
    converted to SSH so your existing key is used. An existing syncdot repo is
    cloned, an empty one is set up as a new syncdot repo. Only the clone and
    ~/.config/syncdot/config.yaml are written: nothing in your home directory
    changes until you pull, push or checkout.
    """
    _init(repo, profile, new)


@cli.command(hidden=True)
@click.option("--repo-url", prompt="GitHub repo URL")
@click.option("--profile", default="base")
@click.option("--clone/--init", default=True)
def install(repo_url: str, profile: str, clone: bool):
    """Old name for `syncdot init` (before 0.8.0)."""
    new_flag = "" if clone else " --new"
    click.echo(click.style(f"`syncdot install` is now `syncdot init <repo>{new_flag}`.", fg="yellow"),
               err=True)
    _init(repo_url, profile, new=not clone)


def _init(repo_url: str, profile: str, new: bool) -> None:
    normalized = normalize_github_url(repo_url)
    if normalized != repo_url:
        click.echo(f"Using SSH (existing GitHub key) instead of HTTPS: {normalized}")
        repo_url = normalized
    cfg = Config(profile=profile)

    if cfg.repo_path.exists() and (cfg.repo_path / ".git").exists():
        click.echo(f"Repo already exists at {cfg.repo_path}, keeping it.")
    elif new:
        click.echo(f"Starting a new syncdot repo at {cfg.repo_path} …")
        Repo.init(cfg.repo_path, repo_url)
    else:
        click.echo(f"Cloning {repo_url} → {cfg.repo_path} …")
        try:
            repo = Repo.clone(repo_url, cfg.repo_path)
        except GitError as e:
            click.echo(click.style(str(e), fg="red"), err=True)
            click.echo("If the repo doesn't exist on GitHub yet, create it empty, or run "
                       f"`syncdot init {repo_url} --new` to start locally.", err=True)
            sys.exit(1)
        branches = repo.remote_branches()
        if not branches:
            # A brand-new, empty GitHub repo: set it up instead of leaving a broken clone
            click.echo("The repo is empty, so setting it up as a new syncdot repo …")
            shutil.rmtree(cfg.repo_path)
            Repo.init(cfg.repo_path, repo_url)
        else:
            # The repo's default branch may not be base (e.g. GitHub's `main`),
            # in which case git checked out the wrong branch or nothing at all
            target = cfg.branch if cfg.branch in branches else "base"
            if target not in branches:
                shutil.rmtree(cfg.repo_path)
                click.echo(click.style(
                    f"{repo_url} has no `base` branch, so it doesn't look like a syncdot repo "
                    f"(branches: {', '.join(branches)}). Point syncdot at your dotfiles repo, "
                    "or at a new empty one.", fg="red"), err=True)
                sys.exit(1)
            repo.checkout(target)

    cfg.write_default(profile)

    if click.confirm("Sync automatically at login? (you can turn it on later with "
                     "`syncdot service install`)", default=False):
        try:
            for line in systemd.install():
                click.echo(line)
        except systemd.SystemdError as e:
            click.echo(click.style(f"Skipped the service: {e}", fg="yellow"), err=True)

    click.echo(click.style("\nsyncdot is set up. Nothing on disk was changed. Next:", fg="green"))
    click.echo("  syncdot add ~/.vimrc ~/.config/kitty   # start tracking files from this machine")
    click.echo("  syncdot status                         # joining an existing repo: see what's there,")
    click.echo("  syncdot pull                           # then take the repo's files (or `pull <path>`)")


# ── sync ───────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--dry-run", is_flag=True, help="Show what would happen without making changes.")
@click.option("-v", "--verbose", is_flag=True)
@locked
def sync(dry_run: bool, verbose: bool):
    """Bidirectional sync: pull remote changes, push local changes, resolve conflicts."""
    cfg = load_config_or_exit()
    label = "[DRY RUN] " if dry_run else ""
    click.echo(f"{label}Syncing profile {click.style(cfg.profile, fg='cyan')} …\n")
    syncer = Syncer(cfg, dry_run=dry_run)
    results = syncer.sync()
    errors = print_results(results, verbose=verbose, pending=dry_run)
    for note in syncer.notes:
        click.echo(f"\n  {note}")
    if errors:
        sys.exit(1)


# ── push ───────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("paths", nargs=-1)
@click.option("--dry-run", is_flag=True)
@click.option("-v", "--verbose", is_flag=True)
@locked
def push(paths: tuple[str, ...], dry_run: bool, verbose: bool):
    """Copy this machine's files to the repo: all of them, or only PATHS.

    PATHS are tracked files or directories on disk. Quoted glob patterns
    are matched against tracked paths, so '~/.config/foo/*' works even for
    files missing from disk.
    """
    cfg = load_config_or_exit()
    click.echo(f"Pushing disk → repo (profile: {click.style(cfg.profile, fg='cyan')}) …\n")
    syncer = Syncer(cfg, dry_run=dry_run)
    try:
        results = syncer.push_all(list(paths))
    except (ValueError, GitError, RenderError) as e:
        click.echo(click.style(str(e), fg="red"), err=True)
        sys.exit(1)
    errors = print_results(results, verbose=verbose or bool(paths), pending=dry_run)
    for note in syncer.notes:
        click.echo(f"\n  {note}")
    if errors:
        sys.exit(1)


# ── pull ───────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("paths", nargs=-1)
@click.option("-y", "--yes", is_flag=True, help="Overwrite local changes without asking.")
@click.option("--dry-run", is_flag=True)
@click.option("-v", "--verbose", is_flag=True)
@locked
def pull(paths: tuple[str, ...], yes: bool, dry_run: bool, verbose: bool):
    """Copy the repo's files to this machine: all of them, or only PATHS.

    PATHS are tracked files or directories on disk. Quoted glob patterns
    are matched against tracked paths, so '~/.config/foo/*' works even for
    files missing from disk. If this would overwrite local changes that
    aren't in the repo, pull lists those files and asks first (--yes skips
    the question).
    """
    cfg = load_config_or_exit()
    click.echo(f"Pulling repo → disk (profile: {click.style(cfg.profile, fg='cyan')}) …\n")
    syncer = Syncer(cfg, dry_run=dry_run)

    def confirm(at_risk: list[Path]) -> bool:
        if yes:
            return True
        click.echo(click.style("These local files have changes that aren't in the repo, and "
                               "pulling overwrites them:", fg="yellow"))
        for dest in at_risk:
            click.echo("  " + str(dest).replace(str(Path.home()), "~", 1))
        click.echo("(`syncdot status --diff` shows the changes; `syncdot push <path>` keeps one.)")
        try:
            return click.confirm("Overwrite them?", default=False)
        except click.Abort:
            click.echo()
            return False

    try:
        results = syncer.pull_all(list(paths), confirm=confirm)
    except PullDeclined as e:
        click.echo(click.style(f"{e} Run with --yes to overwrite without asking.", fg="red"), err=True)
        sys.exit(1)
    except (ValueError, GitError, RenderError) as e:
        click.echo(click.style(str(e), fg="red"), err=True)
        sys.exit(1)
    if print_results(results, verbose=verbose or bool(paths), pending=dry_run):
        sys.exit(1)


# ── status ─────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("paths", nargs=-1)
@click.option("--diff", "show_diff", is_flag=True, help="Show how each differing file differs.")
@click.option("--no-fetch", is_flag=True, help="Don't check the remote; compare with the local clone only.")
@click.option("--exit-code", is_flag=True,
              help=f"Exit with {EXIT_PENDING} if anything isn't in sync (0 if all is).")
@locked
def status(paths: tuple[str, ...], show_diff: bool, no_fetch: bool, exit_code: bool):
    """Show what `sync` would do for each file, without changing anything.

    Changes waiting on GitHub are fetched and included (--no-fetch skips
    that). PATHS limits it to those tracked files or directories (quoted
    globs are matched against tracked paths). --diff also shows how each
    differing file differs: `-` lines are the repo's version, `+` lines this
    machine's.
    """
    cfg = load_config_or_exit()
    click.echo(f"Status (profile: {click.style(cfg.profile, fg='cyan')}) …\n")
    syncer = Syncer(cfg, dry_run=True)
    try:
        results = syncer.status(list(paths), fetch=not no_fetch, with_diff=show_diff)
    except ValueError as e:
        click.echo(click.style(str(e), fg="red"), err=True)
        sys.exit(1)
    print_results(results, verbose=True, pending=True)
    for r in results:
        if r.diff:
            print_diff(r.diff)
    # Files that sync couldn't handle are reported, not a failure of status itself
    if exit_code and any(r.action != Action.NOTHING or r.skipped == NOT_SET_UP or r.error
                         for r in results):
        sys.exit(EXIT_PENDING)


def print_diff(lines: list[str]) -> None:
    if not lines:
        return
    click.echo()
    colours = {"+++": "bright_white", "---": "bright_white", "+": "green", "-": "red", "@": "cyan"}
    for line in lines:
        line = line.rstrip("\n")
        colour = next((c for prefix, c in colours.items() if line.startswith(prefix)), None)
        click.echo(click.style(line, fg=colour, bold=line.startswith(("+++", "---"))) if colour else line)


# ── add ────────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("paths", nargs=-1, required=True)
@click.option("--remote", metavar="REMOTE", help="Where to store it under files/.")
@click.option("--allow-private", is_flag=True,
              help="Track files only you can read (e.g. mode 600), which often hold secrets.")
@click.option("--dry-run", is_flag=True, help="Show what would happen without making changes.")
@locked
def add(paths: tuple[str, ...], remote: str | None, allow_private: bool, dry_run: bool):
    """Start tracking PATHS (files or directories), stored under files/ in the repo.

    Several paths can be given at once, and glob patterns are expanded
    (quote them to let syncdot expand them, e.g. '~/.config/foo/*.conf').
    Everything is added in a single commit; paths that can't be added are
    reported and the rest are still added.

    --remote defaults to the path relative to your home directory, so
    ~/.config/foo is stored as files/.config/foo (paths outside ~ need
    --remote). If --remote ends with /, is an existing directory, or several
    paths are given, each path's name is kept inside it. Directories are copied verbatim
    (no .j2 rendering).

    Symlinks are never copied: PATHS themselves can't be symlinks, and
    symlinks inside a directory (plus nested .git directories) are skipped
    and left untouched on every sync. Binary files are skipped unless
    include_binary: true is set in the config. Private files (no access for
    group or others, e.g. mode 600) are refused unless --allow-private is
    given, since they often hold secrets.
    """
    try:
        locals_ = expand_paths(paths)
    except ValueError as e:
        click.echo(click.style(str(e), fg="red"), err=True)
        sys.exit(1)
    cfg = load_config_or_exit()
    syncer = Syncer(cfg, dry_run=dry_run)
    try:
        results = syncer.add(locals_, remote, allow_private)
    except (ValueError, GitError) as e:
        click.echo(click.style(str(e), fg="red"), err=True)
        sys.exit(1)
    print_results(results, verbose=True, pending=dry_run)
    for r in results:
        if not r.error:
            dest = str(r.dest).replace(str(Path.home()), "~")
            click.echo(f"  Tracking {dest} as {click.style(r.source, fg='cyan')}")
    if allow_private:
        private = [r.dest for r in results if not r.error and find_private(r.dest) is not None]
        for dest in private:
            shown = str(dest).replace(str(Path.home()), "~")
            click.echo(click.style(
                f"  Warning: {shown} is private here, but git doesn't store permissions: on "
                "other machines it arrives with default permissions (usually 644), readable "
                "by other users. Its contents are also in your dotfiles repo.", fg="yellow"))
    if any(r.error for r in results):
        sys.exit(1)


def expand_paths(patterns: tuple[str, ...]) -> list[Path]:
    """Expand ~ and glob patterns, dropping duplicates while keeping order."""
    seen: dict[Path, None] = {}
    for pattern in patterns:
        expanded = os.path.expanduser(pattern)
        if glob.has_magic(expanded) and not os.path.lexists(expanded):
            matches = sorted(glob.glob(expanded, include_hidden=True))
            if not matches:
                raise ValueError(f"No files match {pattern}")
        else:
            matches = [expanded]
        for m in matches:
            seen.setdefault(Path(os.path.abspath(m)), None)
    return list(seen)


# ── checkout ───────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("remotes", nargs=-1, required=True)
@click.option("--local", type=click.Path(path_type=Path), help="Where to write it on disk.")
@click.option("--force", is_flag=True, help="Overwrite existing local files that differ.")
@click.option("--dry-run", is_flag=True, help="Show what would happen without making changes.")
@locked
def checkout(remotes: tuple[str, ...], local: Path | None, force: bool, dry_run: bool):
    """Start tracking REMOTES that are already in the repo (the reverse of add).

    REMOTES are paths or glob patterns relative to files/ (quote globs
    so syncdot matches them inside the repo). Each one is written to disk
    and added to the manifest in a single commit; ones that can't be
    checked out are reported and the rest are still checked out.

    --local defaults to ~/<remote> (minus any .j2 suffix). If it ends with
    /, is an existing directory, or several remotes are given, each
    remote's name is kept inside it. An existing local file with different
    content is left alone unless --force is given.
    """
    cfg = load_config_or_exit()
    syncer = Syncer(cfg, dry_run=dry_run)
    try:
        results = syncer.checkout(list(remotes), local, force)
    except (ValueError, GitError, RenderError) as e:
        click.echo(click.style(str(e), fg="red"), err=True)
        sys.exit(1)
    print_results(results, verbose=True, pending=dry_run)
    for r in results:
        if not r.error and not r.skipped:
            dest = str(r.dest).replace(str(Path.home()), "~")
            click.echo(f"  Tracking {click.style(r.source, fg='cyan')} as {dest}")
    if any(r.error for r in results):
        sys.exit(1)


# ── remove ─────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("paths", nargs=-1, required=True)
@click.option("--dry-run", is_flag=True, help="Show what would happen without making changes.")
@locked
def remove(paths: tuple[str, ...], dry_run: bool):
    """Stop tracking PATHS (files or directories).

    Removes their manifest entries and their copies in the repo, in one
    commit. The files on disk are left untouched, and may already be
    deleted. Quoted globs are matched against tracked paths.
    """
    cfg = load_config_or_exit()
    syncer = Syncer(cfg, dry_run=dry_run)
    try:
        results = syncer.remove(list(paths))
    except (ValueError, GitError) as e:
        click.echo(click.style(str(e), fg="red"), err=True)
        sys.exit(1)
    print_results(results, verbose=True, pending=dry_run)
    verb = "Would stop tracking" if dry_run else "No longer tracking"
    for r in results:
        click.echo(f"  {verb} {click.style(r.source, fg='cyan')} (disk copy kept)")


# ── var ────────────────────────────────────────────────────────────────────────

@cli.group()
def var():
    """Manage template variables (vars.yaml on the active profile's branch)."""


@var.command("list")
@locked
def var_list():
    """List variables for the active profile."""
    cfg = load_config_or_exit()
    variables = Syncer(cfg, dry_run=True).var_list()
    if not variables:
        click.echo("No variables set. Add one with `syncdot var set name=value`.")
        return
    width = max(len(n) for n in variables)
    for name, value in variables.items():
        shown = value if isinstance(value, str) else yaml.safe_dump(
            value, default_flow_style=True, width=float("inf")).strip().removesuffix("...").strip()
        click.echo(f"  {click.style(name.ljust(width), fg='cyan')} = {shown}")


@var.command("set")
@click.argument("assignments", nargs=-1, required=True, metavar="NAME=VALUE...")
@click.option("--yaml", "as_yaml", is_flag=True,
              help="Parse values as YAML (numbers, booleans, lists) instead of plain strings.")
@click.option("--dry-run", is_flag=True, help="Show what would happen without making changes.")
@locked
def var_set(assignments: tuple[str, ...], as_yaml: bool, dry_run: bool):
    """Set template variables and re-render the templates that use them.

    Values are stored as strings unless --yaml is given. Quote values with
    spaces for your shell: syncdot var set fullname="Ariel Shatil".
    """
    values = {}
    for a in assignments:
        name, sep, value = a.partition("=")
        if not sep or not name:
            click.echo(click.style(f"Expected NAME=VALUE, got {a!r}", fg="red"), err=True)
            sys.exit(1)
        if as_yaml:
            try:
                value = yaml.safe_load(value)
            except yaml.YAMLError as e:
                click.echo(click.style(f"Invalid YAML for {name}: {e}", fg="red"), err=True)
                sys.exit(1)
        values[name] = value
    _run_var_edit(lambda syncer: syncer.var_set(values), "Would set" if dry_run else "Set",
                  list(values), dry_run)


@var.command("unset")
@click.argument("names", nargs=-1, required=True)
@click.option("--force", is_flag=True, help="Unset even if a template still uses the variable.")
@click.option("--dry-run", is_flag=True, help="Show what would happen without making changes.")
@locked
def var_unset(names: tuple[str, ...], force: bool, dry_run: bool):
    """Remove template variables.

    Refuses if a tracked template still uses one of them, since it could no
    longer be rendered, unless --force is given.
    """
    _run_var_edit(lambda syncer: syncer.var_unset(list(names), force),
                  "Would unset" if dry_run else "Unset", list(names), dry_run)


def _run_var_edit(action, verb: str, names: list[str], dry_run: bool) -> None:
    cfg = load_config_or_exit()
    syncer = Syncer(cfg, dry_run=dry_run)
    try:
        results = action(syncer)
    except (ValueError, GitError, RenderError) as e:
        click.echo(click.style(str(e), fg="red"), err=True)
        sys.exit(1)
    click.echo(f"{verb} {', '.join(click.style(n, fg='cyan') for n in names)} "
               f"(profile: {click.style(cfg.profile, fg='cyan')}).")
    if results:
        click.echo("\nUpdated files:" if not dry_run else "\nFiles to update:")
        print_results(results, verbose=True, pending=dry_run)
    if any(r.error for r in results):
        sys.exit(1)


# ── env ────────────────────────────────────────────────────────────────────────

@cli.group()
def env():
    """Manage environment variables for your session (env: in vars.yaml)."""


@env.command("list")
@locked
def env_list():
    """List environment variables for the active profile."""
    cfg = load_config_or_exit()
    rows = Syncer(cfg, dry_run=True).env_list()
    if not rows:
        click.echo("No environment variables set. Add one with `syncdot env set NAME=value`.")
        return
    width = max(len(r[0]) for r in rows)
    for name, raw, rendered, error in rows:
        line = f"  {click.style(name.ljust(width), fg='cyan')} = {raw}"
        if error:
            line += click.style(f"  ERROR: {error}", fg="red")
        elif rendered != raw:
            line += click.style(f"  → {rendered}", fg="bright_black")
        click.echo(line)


@env.command("set")
@click.argument("assignments", nargs=-1, required=True, metavar="NAME=VALUE...")
@click.option("--dry-run", is_flag=True, help="Show what would happen without making changes.")
@locked
def env_set(assignments: tuple[str, ...], dry_run: bool):
    """Set environment variables and regenerate the session environment.

    Values may use template variables: syncdot env set GIT_AUTHOR_EMAIL='{{ email }}'
    (single quotes keep your shell from touching them).
    """
    values = {}
    for a in assignments:
        name, sep, value = a.partition("=")
        if not sep or not name:
            click.echo(click.style(f"Expected NAME=VALUE, got {a!r}", fg="red"), err=True)
            sys.exit(1)
        values[name] = value
    _run_var_edit(lambda syncer: syncer.env_set(values), "Would set" if dry_run else "Set",
                  list(values), dry_run)


@env.command("unset")
@click.argument("names", nargs=-1, required=True)
@click.option("--dry-run", is_flag=True, help="Show what would happen without making changes.")
@locked
def env_unset(names: tuple[str, ...], dry_run: bool):
    """Remove environment variables."""
    _run_var_edit(lambda syncer: syncer.env_unset(list(names)),
                  "Would unset" if dry_run else "Unset", list(names), dry_run)


@env.command("hook")
def env_hook():
    """Print the line that loads syncdot's environment in your shell."""
    cfg = load_config_or_exit()
    path = str(Syncer(cfg, dry_run=True).env_path).replace(str(Path.home()), "~", 1)
    click.echo(f"The systemd user session reads {path} by itself.")
    click.echo("For shells, add this line to ~/.profile (login shells, SSH), and to")
    click.echo("~/.bashrc or ~/.zshrc to have it in every interactive shell too:\n")
    click.echo(f"    [ -r {path} ] && {{ set -a; . {path}; set +a; }}\n")
    click.echo("Open a new shell to pick up changes.")


# ── profile ────────────────────────────────────────────────────────────────────

@cli.group()
def profile():
    """Manage profiles (git branches)."""


@profile.command("list")
@locked
def profile_list():
    """List available profiles."""
    cfg = load_config_or_exit()
    repo = Repo(cfg.repo_path)
    result = repo._run("branch", "-a")
    branches = [b.strip().lstrip("* ") for b in result.stdout.splitlines()]
    profiles = [b.removeprefix("remotes/origin/").removeprefix("profiles/") for b in branches
                if b.removeprefix("remotes/origin/") == "base" or "profiles/" in b]
    profiles = sorted(set(profiles), key=lambda p: (p != "base", p))

    click.echo("Available profiles:\n")
    for p in profiles:
        marker = click.style(" ← active", fg="green") if p == cfg.profile else ""
        click.echo(f"  {p}{marker}")


@profile.command("set")
@click.argument("name")
@locked
def profile_set(name: str):
    """Switch the active profile."""
    cfg = load_config_or_exit()
    branch = profile_branch(name)
    repo = Repo(cfg.repo_path)

    if not repo.branch_exists(branch) and not repo.branch_exists(branch, remote=True):
        click.echo(click.style(f"Profile {name!r} does not exist. Use `syncdot profile new {name}` to create it.", fg="red"), err=True)
        sys.exit(1)

    # Rewrite config with new profile
    with CONFIG_PATH.open() as f:
        data = yaml.safe_load(f)
    data["profile"] = name
    with CONFIG_PATH.open("w") as f:
        yaml.dump(data, f, default_flow_style=False)

    click.echo(f"Active profile set to {click.style(name, fg='cyan')}.")
    click.echo("Run `syncdot pull` to apply the new profile to disk.")


@profile.command("new")
@click.argument("name")
@locked
def profile_new(name: str):
    """Create a new profile branch from base."""
    cfg = load_config_or_exit()
    repo = Repo(cfg.repo_path)
    try:
        repo.create_profile_branch(name)
        click.echo(f"Created profile {click.style(name, fg='cyan')} (branch: profiles/{name}).")
        click.echo("Switch to it with `syncdot profile set` and edit files/ or vars.yaml on that")
        click.echo("branch to override base. Changes to base are merged in on every sync.")
    except GitError as e:
        click.echo(click.style(str(e), fg="red"), err=True)
        sys.exit(1)


# ── service ────────────────────────────────────────────────────────────────────

@cli.group()
def service():
    """Manage the systemd user service."""


@service.command("install")
def service_install():
    """Sync automatically at login (systemd user service)."""
    _run_systemd(systemd.install)


@service.command("uninstall")
def service_uninstall():
    """Stop syncing automatically at login."""
    _run_systemd(systemd.uninstall)


@service.command("status")
def service_status():
    """Show the status of the login service."""
    _run_systemd(lambda: [systemd.status()])


def _run_systemd(action) -> None:
    try:
        for line in action():
            click.echo(line)
    except systemd.SystemdError as e:
        click.echo(click.style(str(e), fg="red"), err=True)
        sys.exit(1)


if __name__ == "__main__":
    cli()
