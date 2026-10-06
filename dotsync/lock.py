"""
lock.py — Only one dotsync touches the repo clone at a time.

Commands take an exclusive flock on ~/.local/state/dotsync/lock (next to the
state file). A second dotsync, e.g. `dotsync add` while the login service is
syncing, waits for the first to finish and gives up after LOCK_TIMEOUT
seconds. The kernel drops the lock when the process exits, so a crashed run
never leaves it stuck.
"""

from __future__ import annotations

import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

LOCK_TIMEOUT = 120.0   # seconds; read at call time so tests can shorten it


class LockTimeout(Exception):
    pass


def _holder(path: Path) -> str:
    try:
        return path.read_text().strip() or "unknown"
    except OSError:
        return "unknown"


@contextmanager
def repo_lock(path: Path, on_wait: Callable[[str], None] | None = None) -> Iterator[None]:
    """Hold the dotsync lock for the duration of the block."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + LOCK_TIMEOUT
        waiting = False
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not waiting:
                    waiting = True
                    if on_wait:
                        on_wait(_holder(path))
                if time.monotonic() >= deadline:
                    raise LockTimeout(
                        f"Another dotsync is running (pid {_holder(path)}); gave up after "
                        f"{LOCK_TIMEOUT:g}s. Try again when it has finished."
                    ) from None
                time.sleep(0.1)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        yield
    finally:
        os.close(fd)   # closing the descriptor releases the lock
