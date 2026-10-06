"""
systemd.py — Optional systemd user service that runs `syncdot sync` at login.

Unit files are written to ~/.config/systemd/user/.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

SERVICE_NAME = "syncdot.service"
# Units from before the rename (dotsync) and from 0.6.0's periodic sync;
# install and uninstall remove them
OLD_UNITS = {
    "dotsync.timer": "periodic sync is no longer supported",
    "dotsync.service": "replaced by syncdot.service",
}

SERVICE_TEMPLATE = """\
[Unit]
Description=syncdot — bidirectional dotfile sync
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart={executable} -m syncdot sync
StandardOutput=journal
StandardError=journal
# Give up after 3 minutes rather than blocking login indefinitely (syncdot
# itself waits up to 2 minutes for another running syncdot to finish)
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
    messages = _remove_old_units()
    service = directory / SERVICE_NAME
    service.write_text(SERVICE_TEMPLATE.format(executable=sys.executable))
    messages.append(f"Wrote {service}")
    _systemctl("daemon-reload")
    _systemctl("enable", SERVICE_NAME)
    messages.append("syncdot will sync at login (after the network is up).")
    return messages


def uninstall() -> list[str]:
    _require_systemctl()
    messages = _remove_old_units()
    _systemctl("disable", SERVICE_NAME, check=False)
    path = unit_dir() / SERVICE_NAME
    if path.exists():
        path.unlink()
        messages.append(f"Removed {path}")
    _systemctl("daemon-reload")
    messages.append("syncdot no longer syncs at login.")
    return messages


def _remove_old_units() -> list[str]:
    messages = []
    for name, why in OLD_UNITS.items():
        path = unit_dir() / name
        if path.exists():
            _systemctl("disable", "--now", name, check=False)
            path.unlink()
            messages.append(f"Removed {path} ({why})")
    return messages


def status() -> str:
    _require_systemctl()
    result = subprocess.run(
        ["systemctl", "--user", "status", "--no-pager", SERVICE_NAME],
        text=True, capture_output=True,
    )
    return result.stdout or result.stderr


def _require_systemctl() -> None:
    if not shutil.which("systemctl"):
        raise SystemdError("systemd isn't available here; run `syncdot sync` from cron or "
                           "your shell's startup files instead")


def _systemctl(*args: str, check: bool = True) -> None:
    result = subprocess.run(["systemctl", "--user", *args], text=True, capture_output=True)
    if check and result.returncode != 0:
        raise SystemdError(f"systemctl --user {' '.join(args)} failed: "
                           f"{(result.stderr or result.stdout).strip()}")
