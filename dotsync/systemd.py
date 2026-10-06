"""
systemd.py — systemd user units that run `dotsync sync` automatically.

  dotsync.service   runs `dotsync sync` once; enabled for login (after network is up)
  dotsync.timer     re-runs the service every INTERVAL while you're logged in

Unit files are written to ~/.config/systemd/user/.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

SERVICE_NAME = "dotsync.service"
TIMER_NAME = "dotsync.timer"
DEFAULT_INTERVAL = "1h"

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

TIMER_TEMPLATE = """\
[Unit]
Description=Run dotsync sync every {interval}

[Timer]
OnActiveSec={interval}
OnUnitActiveSec={interval}
RandomizedDelaySec=2min

[Install]
WantedBy=timers.target
"""

# A plain systemd time span such as 30min, 1h or 1d
_INTERVAL_RE = re.compile(r"\d+\s*(s|sec|m|min|h|hr|d|day|w|week)s?")


class SystemdError(Exception):
    pass


def unit_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def install(interval: str | None = DEFAULT_INTERVAL) -> list[str]:
    """Write and enable the units. interval=None installs the login-only service."""
    if interval is not None and not _INTERVAL_RE.fullmatch(interval.strip()):
        raise SystemdError(f"Invalid interval {interval!r}: use e.g. 30min, 1h or 1d")
    _require_systemctl()
    directory = unit_dir()
    directory.mkdir(parents=True, exist_ok=True)
    messages = []

    service = directory / SERVICE_NAME
    service.write_text(SERVICE_TEMPLATE.format(executable=sys.executable))
    messages.append(f"Wrote {service}")
    timer = directory / TIMER_NAME
    if interval is not None:
        timer.write_text(TIMER_TEMPLATE.format(interval=interval.strip()))
        messages.append(f"Wrote {timer}")
    elif timer.exists():
        _systemctl("disable", "--now", TIMER_NAME, check=False)
        timer.unlink()
        messages.append(f"Removed {timer}")

    _systemctl("daemon-reload")
    _systemctl("enable", "--now", SERVICE_NAME)
    if interval is not None:
        _systemctl("enable", "--now", TIMER_NAME)
        messages.append(f"dotsync will sync at login (after network is up) and every {interval.strip()}.")
    else:
        messages.append("dotsync will sync at login (after network is up).")
    return messages


def uninstall() -> list[str]:
    _require_systemctl()
    messages = []
    for name in (TIMER_NAME, SERVICE_NAME):
        _systemctl("disable", "--now", name, check=False)
        path = unit_dir() / name
        if path.exists():
            path.unlink()
            messages.append(f"Removed {path}")
    _systemctl("daemon-reload")
    messages.append("dotsync no longer syncs automatically.")
    return messages


def status() -> str:
    _require_systemctl()
    result = subprocess.run(
        ["systemctl", "--user", "status", "--no-pager", SERVICE_NAME, TIMER_NAME],
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
