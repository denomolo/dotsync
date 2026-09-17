"""
dotsync — bidirectional dotfile sync with profiles and templating.

Usage:
    dotsync install                  First-time setup
    dotsync sync [--dry-run]         Bidirectional sync (default)
    dotsync push [--dry-run]         Force disk → git
    dotsync pull [--dry-run]         Force git → disk
    dotsync status                   Show per-file state
    dotsync profile list             List available profiles
    dotsync profile set <name>       Switch active profile
    dotsync profile new <name>       Create a new profile branch
    dotsync service install          Install systemd user service
    dotsync service uninstall        Remove systemd user service
    dotsync service status           Show systemd service status
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from .config import Config, CONFIG_PATH
from .repo import Repo, GitError
from .sync import Action, Syncer
from . import systemd


# ── Helpers ────────────────────────────────────────────────────────────────────

ACTION_SYMBOLS = {
    Action.NOTHING:  click.style("·", fg="bright_black"),
    Action.PULL:     click.style("↓", fg="cyan"),
    Action.PUSH:     click.style("↑", fg="green"),
    Action.CONFLICT: click.style("!", fg="yellow"),
}


def print_results(results, verbose: bool = False) -> None:
    errors = 0
    for r in results:
        sym = ACTION_SYMBOLS[r.action]
        dest = str(r.dest).replace(str(Path.home()), "~")
        line = f"  {sym}  {dest}"
        if r.resolved_by:
            line += click.style(f"  [{r.resolved_by}]", fg="yellow")
        if r.error:
            line += click.style(f"  ERROR: {r.error}", fg="red")
            errors += 1
        if verbose or r.action != Action.NOTHING or r.error:
            click.echo(line)

    counts = {a: sum(1 for r in results if r.action == a) for a in Action}
    parts = []
    if counts[Action.PULL]:
        parts.append(click.style(f"↓ {counts[Action.PULL]} pulled", fg="cyan"))
    if counts[Action.PUSH]:
        parts.append(click.style(f"↑ {counts[Action.PUSH]} pushed", fg="green"))
    if counts[Action.CONFLICT]:
        parts.append(click.style(f"! {counts[Action.CONFLICT]} conflicts", fg="yellow"))
    if counts[Action.NOTHING] and not parts:
        parts.append(click.style("already in sync", fg="bright_black"))
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
@click.version_option("0.1.0", prog_name="dotsync")
def cli():
    """dotsync — bidirectional dotfile sync with profiles and templating."""


# ── install ────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--repo-url", prompt="GitHub repo URL", help="SSH or HTTPS URL of your dotfiles repo.")
@click.option("--profile", default="base", show_default=True, help="Initial profile name.")
@click.option("--clone/--init", default=True, help="Clone existing repo or init a new one.")
def install(repo_url: str, profile: str, clone: bool):
    """First-time setup: clone/init repo, write config, install systemd service."""
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
    click.echo(f"  {cfg.repo_path}/base/vars/base.yaml")


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
    print_results(results, verbose=verbose)


# ── push ───────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--dry-run", is_flag=True)
@click.option("-v", "--verbose", is_flag=True)
def push(dry_run: bool, verbose: bool):
    """Force disk → git for all files (machine-wins)."""
    cfg = load_config_or_exit()
    click.echo(f"Pushing disk → repo (profile: {click.style(cfg.profile, fg='cyan')}) …\n")
    syncer = Syncer(cfg, dry_run=dry_run)
    results = syncer.push_all()
    print_results(results, verbose=verbose)


# ── pull ───────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--dry-run", is_flag=True)
@click.option("-v", "--verbose", is_flag=True)
def pull(dry_run: bool, verbose: bool):
    """Force git → disk for all files (git-wins)."""
    cfg = load_config_or_exit()
    click.echo(f"Pulling repo → disk (profile: {click.style(cfg.profile, fg='cyan')}) …\n")
    syncer = Syncer(cfg, dry_run=dry_run)
    results = syncer.pull_all()
    print_results(results, verbose=verbose)


# ── status ─────────────────────────────────────────────────────────────────────

@cli.command()
def status():
    """Show per-file sync state without making any changes."""
    cfg = load_config_or_exit()
    click.echo(f"Status (profile: {click.style(cfg.profile, fg='cyan')}) …\n")
    syncer = Syncer(cfg, dry_run=True)
    results = syncer.status()
    print_results(results, verbose=True)


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
    profiles = [b.replace("profiles/", "").replace("remotes/origin/profiles/", "")
                for b in branches if "profiles/" in b]
    profiles = sorted(set(profiles))

    click.echo("Available profiles:\n")
    for p in profiles:
        marker = click.style(" ← active", fg="green") if p == cfg.profile else ""
        click.echo(f"  {p}{marker}")


@profile.command("set")
@click.argument("name")
def profile_set(name: str):
    """Switch the active profile."""
    cfg = load_config_or_exit()
    branch = f"profiles/{name}"
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
        click.echo(f"Add profile-specific vars to: profiles/{name}/vars/{name}.yaml")
        click.echo(f"Add profile-specific file overrides to: profiles/{name}/files/")
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
