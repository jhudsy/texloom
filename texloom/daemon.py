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
import time
from dataclasses import dataclass, field
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


@dataclass
class FolderState:
    """What a running folder is doing, for the app's window."""
    path: Path
    replica: object | None = None  # the folder's Replica, once run() has set it up
    fs: object | None = None  # its FolderSync
    problem: str = ""  # why it isn't syncing (empty when fine)
    started: float = field(default_factory=time.time)


# Set by supervise() for the folder's task; run() fills in replica and fs.
CURRENT: contextvars.ContextVar[FolderState | None] = contextvars.ContextVar("current", default=None)


async def supervise(d: Path, run: Callable[[Path], Awaitable[None]], state: FolderState | None = None) -> None:
    FOLDER.set(d.name)  # tasks this one creates inherit it
    CURRENT.set(state)
    delay = 5.0
    while True:
        try:
            if state:
                state.problem = ""
            await run(d)
            return
        except asyncio.CancelledError:
            raise
        except FolderProblem as e:
            log.warning("not syncing: %s (retrying in 5 minutes)", e)
            if state:
                state.problem = str(e)
            delay = 300
        except Exception as e:  # noqa: BLE001 - one folder's failure mustn't stop the others
            log.warning("stopped with an error (%s: %s); retrying in %ds", type(e).__name__, e, int(delay))
            if state:
                state.problem = f"{type(e).__name__}: {e}"
        if state:
            state.replica = state.fs = None
        await asyncio.sleep(delay)
        delay = min(delay * 2, 300)


class Daemon:
    """Keeps every folder on the list in sync; the app drives it and reads `states`."""

    def __init__(self, run: Callable[[Path], Awaitable[None]]):
        self.run = run
        self.tasks: dict[Path, asyncio.Task] = {}
        self.stopping: dict[Path, asyncio.Task] = {}  # cancelled, still letting go of the folder
        self.states: dict[Path, FolderState] = {}
        self.paused = False
        self._told: set[Path] = set()
        self._wake = asyncio.Event()

    def wanted(self) -> set[Path]:
        want = set()
        for d in registry.folders():
            if (d / ".texloom" / "config.json").is_file():
                want.add(d)
            elif d not in self._told:
                self._told.add(d)
                log.warning("%s is on the folder list but isn't a texloom folder (moved or deleted?): skipped", d)
        return want

    def reconcile(self) -> None:
        want = set() if self.paused else self.wanted()
        self.stopping = {d: t for d, t in self.stopping.items() if not t.done()}
        for d in self.tasks.keys() - want:
            self.stop(d)
            log.info("stopped syncing %s", d)
        for d in sorted(want - self.tasks.keys()):
            if d in self.stopping:
                continue  # started again once the old task has released the folder
            log.info("syncing %s", d)
            self.states[d] = st = FolderState(d)
            self.tasks[d] = asyncio.create_task(supervise(d, self.run, st))

    def stop(self, d: Path) -> None:
        if (t := self.tasks.pop(d, None)) is not None:
            t.cancel()
            self.stopping[d] = t
            t.add_done_callback(lambda _t: self.poke())
        self.states.pop(d, None)

    def restart(self, d: Path) -> None:
        """E.g. after its settings changed: stop it; it starts again once it has let go."""
        self.stop(d)
        self.poke()

    def poke(self) -> None:
        self._wake.set()

    async def loop(self, poll: float = 5) -> None:
        first = True
        while True:
            self.reconcile()
            if first and not self.tasks and not self.paused:
                log.info("no folders to sync yet: add one with `texloom clone <link> <dir>` or `texloom add <dir>`")
            first = False
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), poll)
            except asyncio.TimeoutError:
                pass


async def start(run: Callable[[Path], Awaitable[None]], poll: float = 5) -> None:
    await Daemon(run).loop(poll)
