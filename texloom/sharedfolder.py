"""A synced folder that is also shared through a file-sync service (Dropbox, iCloud Drive,
OneDrive, Google Drive, Syncthing…) with another computer that runs TeXLoom on it too.

Then each change would reach the other computer twice: through TeXLoom, and as the service copies
the file. The second copy would look like an edit made there, and be shared again: the same text
twice. So only one computer keeps such a folder in step at a time (it holds the turn); the others
stand by (FolderSync.standby): the service keeps their copy of the folder up to date, and they
only note which files already match the project, so that one of them can take over cleanly.

How computers find each other: the folder has an id, `.texloom/folder-id`, which the service copies
with it. Every computer that syncs the folder tells the project's other copies, every ANNOUNCE
seconds, "I sync folder <id>; I hold its turn, or not" (peer-to-peer, through the relay: encrypted,
and not kept in the project's history). Two computers naming the same id share one folder.

Who holds the turn: one that does keeps it; with two at once (both started before hearing of
each other), the lower computer id keeps it. When nobody does, the lowest computer id heard
from in the last EXPIRE seconds takes it, so a computer that stops (closed, offline) is replaced
after about EXPIRE. A folder once found shared is synced only while online: offline, a computer
can't know whether another holds the turn, and its service would still bring that one's edits.
A folder never found shared syncs as always, online or not.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from .paths import STATE, replace_file, state_dir, write_json
from .registry import computer_id

log = logging.getLogger("texloom.shared")

ANNOUNCE = 20.0  # seconds between "I sync this folder" messages
EXPIRE = 70.0  # seconds without one before a computer is taken to have stopped
if os.environ.get("TEXLOOM_CHECKS") == "1":  # the checks' own, shorter (scripts/check_shared_folder.py)
    ANNOUNCE = float(os.environ.get("TEXLOOM_TURN_ANNOUNCE", ANNOUNCE))
    EXPIRE = float(os.environ.get("TEXLOOM_TURN_EXPIRE", EXPIRE))
OFF = os.environ.get("TEXLOOM_CHECKS") == "1" and os.environ.get("TEXLOOM_NO_TURNS") == "1"  # to show what goes wrong without
FORGET = 30 * 86400  # a folder not found shared for this long syncs as an unshared one again


def folder_id(root: Path) -> str:
    """The folder's id, made by the first computer to sync it; a file-sync service copies it."""
    p = root / STATE / "folder-id"
    try:
        fid = p.read_text().strip()
        if len(fid) == 32 and all(c in "0123456789abcdef" for c in fid):
            return fid
    except OSError:
        pass
    fid = secrets.token_hex(16)
    p.parent.mkdir(parents=True, exist_ok=True)
    replace_file(p, fid.encode())
    return fid


@dataclass
class Peer:
    seen: float  # time.monotonic() when last heard
    holding: bool
    by: str


class SharedFolder:
    """Takes turns on a folder with other computers that sync the same copy of it."""

    def __init__(self, root: Path, fs, name: Callable[[], str]) -> None:
        self.root, self.fs, self.r, self.name = root, fs, fs.r, name
        self.me = computer_id()
        self.fid = folder_id(root)
        self.peers: dict[str, Peer] = {}
        self._flag = state_dir(root) / "shared.json"
        self.shared = fs.shared = self._was_shared()
        self.holding = not self.shared  # a folder known to be shared waits to hear the others first
        fs.standby = not self.holding
        self._online_since: float | None = None
        self._announced = 0.0
        self.r.on_folder = self._heard
        self.changed: Callable[[], Awaitable[None]] | None = None  # for the app's window

    # --- what others say ---
    def _was_shared(self) -> bool:
        try:
            return time.time() - float(json.loads(self._flag.read_text()).get("at", 0)) < FORGET
        except (OSError, ValueError, AttributeError):
            return False

    def _heard(self, p: dict) -> None:
        """A message from another copy of the project: {"k": "fold", "f": folder id, "m": computer, "h": holds, "by": name}."""
        m, f = p.get("m"), p.get("f")
        if not isinstance(m, str) or m == self.me or f != self.fid:
            return
        if m not in self.peers:
            log.warning("%s also syncs this folder (shared through a file-sync service such as Dropbox): "
                        "the two take turns, so changes aren't brought in twice", str(p.get("by") or "another computer")[:60])
        self.peers[m] = Peer(time.monotonic(), bool(p.get("h")), str(p.get("by") or "")[:60])
        if not self.shared or time.time() - self._flag_at() > 3600:
            self.shared = self.fs.shared = True
            try:
                write_json(self._flag, {"at": time.time()})
            except OSError:
                pass

    def _flag_at(self) -> float:
        try:
            return float(json.loads(self._flag.read_text()).get("at", 0))
        except (OSError, ValueError, AttributeError):
            return 0.0

    def live(self) -> dict[str, Peer]:
        now = time.monotonic()
        return {m: p for m, p in self.peers.items() if now - p.seen < EXPIRE}

    @property
    def holder(self) -> str | None:
        """Who keeps the folder in step now, when it isn't this computer (a name, or '')."""
        if self.holding:
            return None
        held = [p for p in self.live().values() if p.holding]
        return held[0].by if held else ""

    # --- deciding ---
    def should_hold(self) -> bool:
        if not self.shared or OFF:
            return True
        if not self.r.synced.is_set() or (not self.r.files and not self.r.bin):
            # not until this copy of the project has caught up and has its files: reading the
            # folder into an empty copy would add every file a second time
            return False
        if not self.r.connected.is_set():
            return False  # offline: another may hold the turn, and its edits still arrive through the service
        if self._online_since is None or time.monotonic() - self._online_since < ANNOUNCE + 5:
            return self.holding  # just online: hear from the others first
        live = self.live()
        holders = [m for m, p in live.items() if p.holding]
        if holders:
            return self.holding and all(self.me < m for m in holders)
        return all(self.me < m for m in live)

    async def tick(self) -> None:
        if self.r.connected.is_set():
            if self._online_since is None:
                self._online_since = time.monotonic()
                self._announced = 0.0  # say so straight away
        else:
            self._online_since = None
        self.fid = folder_id(self.root)  # (the service may have brought another computer's, made at the same time)
        hold = self.should_hold()
        if hold != self.holding:
            self.holding = hold
            await self._announce()
            if hold:
                log.info("this computer keeps the shared folder in step now")
                async with self.r.lock:
                    await self.fs.resume()
            else:
                log.info("standing by: %s keeps the shared folder in step", self.holder or "another computer")
                self.fs.standby = True
            if self.changed:
                await self.changed()
        elif time.monotonic() - self._announced >= ANNOUNCE:
            await self._announce()

    async def _announce(self) -> None:
        if not self.r.connected.is_set() or not self.r.synced.is_set():
            return
        self._announced = time.monotonic()
        await self.r.p2p({"k": "fold", "f": self.fid, "m": self.me, "h": self.holding, "by": self.name()})

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as e:  # noqa: BLE001 - keep taking turns whatever one round met
                log.warning("taking turns on the shared folder: %s", e)
            await asyncio.sleep(2)
