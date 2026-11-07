"""One process that keeps every registered folder in sync (`texloom start`).

Each folder runs as its own task with its own connection, watcher and settings (from its
`.texloom/config.json`). The folder list is re-read every few seconds, so folders added
or left while it runs are picked up. A folder that fails is retried on its own; the
others carry on. Log lines are prefixed with the folder's name.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from . import registry

log = logging.getLogger("texloom")

FOLDER: contextvars.ContextVar[str] = contextvars.ContextVar("folder", default="")


class FolderProblem(Exception):
    """A folder that can't be synced as configured (e.g. --git on something that isn't a repository)."""


def lock_folder(d: Path):
    """Hold an exclusive lock on the folder for as long as the returned file stays open, so
    two texloom processes never sync the same folder. Raises FolderProblem if taken."""
    import os
    f = open(d / ".texloom" / "lock", "a+")  # noqa: SIM115 - kept open on purpose
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise FolderProblem("another texloom is already syncing this folder") from None
    return f


class FolderTag(logging.Filter):
    """Adds %(folder)s to log records: "[name] " inside a folder's task, else ""."""

    def filter(self, record: logging.LogRecord) -> bool:
        f = FOLDER.get()
        record.folder = f"[{f}] " if f else ""
        return True


async def supervise(d: Path, run: Callable[[Path], Awaitable[None]]) -> None:
    FOLDER.set(d.name)  # tasks this one creates inherit it
    delay = 5.0
    while True:
        try:
            await run(d)
            return
        except asyncio.CancelledError:
            raise
        except FolderProblem as e:
            log.warning("not syncing: %s (retrying in 5 minutes)", e)
            delay = 300
        except Exception as e:  # noqa: BLE001 - one folder's failure mustn't stop the others
            log.warning("stopped with an error (%s: %s); retrying in %ds", type(e).__name__, e, int(delay))
        await asyncio.sleep(delay)
        delay = min(delay * 2, 300)


async def start(run: Callable[[Path], Awaitable[None]], poll: float = 5) -> None:
    tasks: dict[Path, asyncio.Task] = {}
    told: set[Path] = set()
    first = True
    while True:
        want = set()
        for d in registry.folders():
            if (d / ".texloom" / "config.json").is_file():
                want.add(d)
            elif d not in told:
                told.add(d)
                log.warning("%s is on the folder list but isn't a texloom folder (moved or deleted?): skipped", d)
        for d in sorted(want - tasks.keys()):
            log.info("syncing %s", d)
            tasks[d] = asyncio.create_task(supervise(d, run))
        for d in tasks.keys() - want:
            tasks.pop(d).cancel()
            log.info("stopped syncing %s", d)
        if first and not want:
            log.info("no folders to sync yet: add one with `texloom clone <link> <dir>` or `texloom add <dir>`")
        first = False
        await asyncio.sleep(poll)
