"""
systemd.py — Install/remove a systemd user service that runs `dotsync sync` on login.

Unit files are written to ~/.config/systemd/user/.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

UNIT_DIR = Path("~/.config/systemd/user").expanduser()
SERVICE_NAME = "dotsync.service"
SERVICE_PATH = UNIT_DIR / SERVICE_NAME

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
# Give up after 60s rather than blocking login indefinitely
TimeoutStartSec=60

[Install]
WantedBy=default.target
"""


def install() -> None:
    executable = sys.executable
    UNIT_DIR.mkdir(parents=True, exist_ok=True)

    SERVICE_PATH.write_text(SERVICE_TEMPLATE.format(executable=executable))
    print(f"Wrote {SERVICE_PATH}")

    _systemctl("daemon-reload")
    _systemctl("enable", "--now", SERVICE_NAME)
    print(f"Enabled and started {SERVICE_NAME}")
    print("dotsync will now sync automatically on login (after network is up).")


def uninstall() -> None:
    _systemctl("disable", "--now", SERVICE_NAME, check=False)
    if SERVICE_PATH.exists():
        SERVICE_PATH.unlink()
        print(f"Removed {SERVICE_PATH}")
    _systemctl("daemon-reload")
    print(f"Removed {SERVICE_NAME}")


def status() -> str:
    result = subprocess.run(
        ["systemctl", "--user", "status", SERVICE_NAME],
        text=True, capture_output=True,
    )
    return result.stdout or result.stderr


def _systemctl(*args: str, check: bool = True) -> None:
    subprocess.run(["systemctl", "--user", *args], check=check)
