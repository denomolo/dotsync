"""
systemd.py — Optional systemd user service that runs `dotsync sync` at login.

Unit files are written to ~/.config/systemd/user/.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

SERVICE_NAME = "dotsync.service"
# Installed by dotsync 0.6.0; removed again by install and uninstall
OLD_TIMER_NAME = "dotsync.timer"

SERVICE_TEMPLATE = """\
[Unit]
Description=dotsync — bidirectional dotfile sync
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart={executable} -m dotsync sync
StandardOutput=journal
StandardError=journal
# Give up after 3 minutes rather than blocking login indefinitely (dotsync
# itself waits up to 2 minutes for another running dotsync to finish)
TimeoutStartSec=180

[Install]
WantedBy=default.target
"""


class SystemdError(Exception):
    pass


def unit_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def install() -> list[str]:
    """Write and enable the login service."""
    _require_systemctl()
    directory = unit_dir()
    directory.mkdir(parents=True, exist_ok=True)
    messages = _remove_old_timer()
    service = directory / SERVICE_NAME
    service.write_text(SERVICE_TEMPLATE.format(executable=sys.executable))
    messages.append(f"Wrote {service}")
    _systemctl("daemon-reload")
    _systemctl("enable", SERVICE_NAME)
    messages.append("dotsync will sync at login (after the network is up).")
    return messages


def uninstall() -> list[str]:
    _require_systemctl()
    messages = _remove_old_timer()
    _systemctl("disable", SERVICE_NAME, check=False)
    path = unit_dir() / SERVICE_NAME
    if path.exists():
        path.unlink()
        messages.append(f"Removed {path}")
    _systemctl("daemon-reload")
    messages.append("dotsync no longer syncs at login.")
    return messages


def _remove_old_timer() -> list[str]:
    timer = unit_dir() / OLD_TIMER_NAME
    if not timer.exists():
        return []
    _systemctl("disable", "--now", OLD_TIMER_NAME, check=False)
    timer.unlink()
    return [f"Removed {timer} (periodic sync is no longer supported)"]


def status() -> str:
    _require_systemctl()
    result = subprocess.run(
        ["systemctl", "--user", "status", "--no-pager", SERVICE_NAME],
        text=True, capture_output=True,
    )
    return result.stdout or result.stderr


def _require_systemctl() -> None:
    if not shutil.which("systemctl"):
        raise SystemdError("systemd isn't available here; run `dotsync sync` from cron or "
                           "your shell's startup files instead")


def _systemctl(*args: str, check: bool = True) -> None:
    result = subprocess.run(["systemctl", "--user", *args], text=True, capture_output=True)
    if check and result.returncode != 0:
        raise SystemdError(f"systemctl --user {' '.join(args)} failed: "
                           f"{(result.stderr or result.stdout).strip()}")
