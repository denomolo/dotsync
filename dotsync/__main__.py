"""
dotsync — bidirectional dotfile sync with profiles and templating.

Usage:
    dotsync install                  First-time setup
    dotsync sync [--dry-run]         Bidirectional sync (default)
    dotsync push [<path>...]         Force disk → git (all, or just <path>s)
    dotsync pull [<path>...]         Force git → disk (all, or just <path>s)
    dotsync status                   Show per-file state
    dotsync add <path>...            Track files/dirs, copying them to the repo (--remote R)
    dotsync checkout <remote>...     Track repo files, writing them to disk (--local L)
    dotsync remove <local>           Stop tracking a file/dir (disk copy is kept)
    dotsync profile list             List available profiles
    dotsync profile set <name>       Switch active profile
    dotsync profile new <name>       Create a new profile branch
    dotsync service install          Install systemd user service
    dotsync service uninstall        Remove systemd user service
    dotsync service status           Show systemd service status
"""

from __future__ import annotations

import glob
import os
import sys
from pathlib import Path

import click

from . import __version__
from .config import Config, CONFIG_PATH, profile_branch
from .repo import Repo, GitError, normalize_github_url
from .renderer import RenderError
from .sync import Action, Syncer
from . import systemd


# ── Helpers ────────────────────────────────────────────────────────────────────

ACTION_SYMBOLS = {
    Action.NOTHING:  click.style("·", fg="bright_black"),
    Action.PULL:     click.style("↓", fg="cyan"),
    Action.PUSH:     click.style("↑", fg="green"),
    Action.CONFLICT: click.style("!", fg="yellow"),
    Action.UNTRACK:  click.style("−", fg="red"),
}


def print_results(results, verbose: bool = False, pending: bool = False) -> None:
    """Print per-file results and a summary; pending=True words it as not yet done."""
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
@click.version_option(__version__, prog_name="dotsync")
def cli():
    """dotsync — bidirectional dotfile sync with profiles and templating."""


# ── install ────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--repo-url", prompt="GitHub repo URL", help="SSH/HTTPS URL or owner/repo shorthand; HTTPS GitHub URLs are converted to SSH to use your existing key.")
@click.option("--profile", default="base", show_default=True, help="Initial profile name.")
@click.option("--clone/--init", default=True, help="Clone existing repo or init a new one.")
def install(repo_url: str, profile: str, clone: bool):
    """First-time setup: clone/init repo, write config, install systemd service."""
    normalized = normalize_github_url(repo_url)
    if normalized != repo_url:
        click.echo(f"Using SSH (existing GitHub key) instead of HTTPS: {normalized}")
        repo_url = normalized
    cfg = Config(repo_url=repo_url, profile=profile)

    if cfg.repo_path.exists() and (cfg.repo_path / ".git").exists():
        click.echo(f"Repo already exists at {cfg.repo_path}, skipping clone/init.")
        repo = Repo(cfg.repo_path)
    elif clone:
        click.echo(f"Cloning {repo_url} → {cfg.repo_path} …")
        try:
            repo = Repo.clone(repo_url, cfg.repo_path)
        except GitError as e:
            click.echo(click.style(str(e), fg="red"), err=True)
            sys.exit(1)
    else:
        click.echo(f"Initialising new repo at {cfg.repo_path} …")
        repo = Repo.init(cfg.repo_path, repo_url)

    cfg.write_default(repo_url, profile)

    if click.confirm("Install systemd user service (auto-sync on login)?", default=True):
        systemd.install()

    click.echo(click.style("\ndotsync installed. Edit your manifest:", fg="green"))
    click.echo(f"  {cfg.repo_path}/manifest.yaml")
    click.echo(f"  {cfg.repo_path}/vars.yaml")


# ── sync ───────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--dry-run", is_flag=True, help="Show what would happen without making changes.")
@click.option("-v", "--verbose", is_flag=True)
def sync(dry_run: bool, verbose: bool):
    """Bidirectional sync: pull remote changes, push local changes, resolve conflicts."""
    cfg = load_config_or_exit()
    label = "[DRY RUN] " if dry_run else ""
    click.echo(f"{label}Syncing profile {click.style(cfg.profile, fg='cyan')} …\n")
    syncer = Syncer(cfg, dry_run=dry_run)
    results = syncer.sync()
    print_results(results, verbose=verbose, pending=dry_run)


# ── push ───────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("paths", nargs=-1)
@click.option("--dry-run", is_flag=True)
@click.option("-v", "--verbose", is_flag=True)
def push(paths: tuple[str, ...], dry_run: bool, verbose: bool):
    """Force disk → git for all files, or only PATHS (machine-wins).

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
    print_results(results, verbose=verbose or bool(paths), pending=dry_run)


# ── pull ───────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("paths", nargs=-1)
@click.option("--dry-run", is_flag=True)
@click.option("-v", "--verbose", is_flag=True)
def pull(paths: tuple[str, ...], dry_run: bool, verbose: bool):
    """Force git → disk for all files, or only PATHS (git-wins).

    PATHS are tracked files or directories on disk. Quoted glob patterns
    are matched against tracked paths, so '~/.config/foo/*' works even for
    files missing from disk.
    """
    cfg = load_config_or_exit()
    click.echo(f"Pulling repo → disk (profile: {click.style(cfg.profile, fg='cyan')}) …\n")
    syncer = Syncer(cfg, dry_run=dry_run)
    try:
        results = syncer.pull_all(list(paths))
    except (ValueError, GitError, RenderError) as e:
        click.echo(click.style(str(e), fg="red"), err=True)
        sys.exit(1)
    print_results(results, verbose=verbose or bool(paths), pending=dry_run)


# ── status ─────────────────────────────────────────────────────────────────────

@cli.command()
def status():
    """Show per-file sync state without making any changes."""
    cfg = load_config_or_exit()
    click.echo(f"Status (profile: {click.style(cfg.profile, fg='cyan')}) …\n")
    syncer = Syncer(cfg, dry_run=True)
    results = syncer.status()
    print_results(results, verbose=True, pending=True)


# ── add ────────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("paths", nargs=-1, required=True)
@click.option("--remote", metavar="REMOTE", help="Where to store it under files/.")
@click.option("--dry-run", is_flag=True, help="Show what would happen without making changes.")
def add(paths: tuple[str, ...], remote: str | None, dry_run: bool):
    """Start tracking PATHS (files or directories), stored under files/ in the repo.

    Several paths can be given at once, and glob patterns are expanded
    (quote them to let dotsync expand them, e.g. '~/.config/foo/*.conf').
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
    include_binary: true is set in the config.
    """
    try:
        locals_ = expand_paths(paths)
    except ValueError as e:
        click.echo(click.style(str(e), fg="red"), err=True)
        sys.exit(1)
    cfg = load_config_or_exit()
    syncer = Syncer(cfg, dry_run=dry_run)
    try:
        results = syncer.add(locals_, remote)
    except (ValueError, GitError) as e:
        click.echo(click.style(str(e), fg="red"), err=True)
        sys.exit(1)
    print_results(results, verbose=True, pending=dry_run)
    for r in results:
        if not r.error:
            dest = str(r.dest).replace(str(Path.home()), "~")
            click.echo(f"  Tracking {dest} as {click.style(r.source, fg='cyan')}")
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
def checkout(remotes: tuple[str, ...], local: Path | None, force: bool, dry_run: bool):
    """Start tracking REMOTES that are already in the repo (the reverse of add).

    REMOTES are paths or glob patterns relative to files/ (quote globs
    so dotsync matches them inside the repo). Each one is written to disk
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
@click.argument("local", type=click.Path(path_type=Path))
@click.option("--dry-run", is_flag=True, help="Show what would happen without making changes.")
def remove(local: Path, dry_run: bool):
    """Stop tracking LOCAL (a file or directory).

    Removes its manifest entry and its copy in the repo. The file on disk
    is left untouched. LOCAL may already be deleted from disk as long as
    it is still tracked.
    """
    cfg = load_config_or_exit()
    syncer = Syncer(cfg, dry_run=dry_run)
    try:
        result = syncer.remove(local)
    except (ValueError, GitError) as e:
        click.echo(click.style(str(e), fg="red"), err=True)
        sys.exit(1)
    print_results([result], verbose=True, pending=dry_run)
    verb = "Would stop tracking" if dry_run else "No longer tracking"
    click.echo(f"  {verb} {click.style(result.source, fg='cyan')} (disk copy kept)")


# ── profile ────────────────────────────────────────────────────────────────────

@cli.group()
def profile():
    """Manage profiles (git branches)."""


@profile.command("list")
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
def profile_set(name: str):
    """Switch the active profile."""
    cfg = load_config_or_exit()
    branch = profile_branch(name)
    repo = Repo(cfg.repo_path)

    if not repo.branch_exists(branch) and not repo.branch_exists(branch, remote=True):
        click.echo(click.style(f"Profile {name!r} does not exist. Use `dotsync profile new {name}` to create it.", fg="red"), err=True)
        sys.exit(1)

    # Rewrite config with new profile
    import yaml
    with CONFIG_PATH.open() as f:
        data = yaml.safe_load(f)
    data["profile"] = name
    with CONFIG_PATH.open("w") as f:
        yaml.dump(data, f, default_flow_style=False)

    click.echo(f"Active profile set to {click.style(name, fg='cyan')}.")
    click.echo("Run `dotsync pull` to apply the new profile to disk.")


@profile.command("new")
@click.argument("name")
def profile_new(name: str):
    """Create a new profile branch from base."""
    cfg = load_config_or_exit()
    repo = Repo(cfg.repo_path)
    try:
        repo.create_profile_branch(name)
        click.echo(f"Created profile {click.style(name, fg='cyan')} (branch: profiles/{name}).")
        click.echo("Switch to it with `dotsync profile set` and edit files/ or vars.yaml on that")
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
    """Install the systemd user service (auto-sync on login)."""
    systemd.install()


@service.command("uninstall")
def service_uninstall():
    """Remove the systemd user service."""
    systemd.uninstall()


@service.command("status")
def service_status():
    """Show systemd service status."""
    click.echo(systemd.status())


if __name__ == "__main__":
    cli()
