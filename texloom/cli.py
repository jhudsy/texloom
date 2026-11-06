"""texloom command-line interface.

  texloom new   <dir> --server http://host:8787   turn a folder into a shared project
  texloom clone <link> <dir>                      check a project out into a folder
  texloom run   <dir> [--compile]                 keep folder and project in sync
  texloom links <dir>                             print the edit / read-only links
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from .crypto import Keys
from .folder import STATE, FolderSync
from .replica import Replica

log = logging.getLogger("texloom")


def config_path(d: Path) -> Path:
    return d / STATE / "config.json"


def load(d: Path) -> tuple[Keys, str]:
    return Keys.from_link(load_config(d)["link"])


def load_config(d: Path) -> dict:
    return json.loads(config_path(d).read_text())


DEFAULT_CONFIG = {
    # Local, per-folder settings (never shared with collaborators).
    "compile": False,          # run latexmk after changes (same as --compile)
    "engine": None,            # override the project's engine on this machine
    "latexmk_args": [],        # extra arguments, e.g. ["-pdfxe"]
    "shell_escape": False,     # allow \write18 (minted etc.). Runs code from collaborators!
    "label": "",               # how your compiles are labelled for others
}


def save(d: Path, link: str) -> None:
    (d / STATE).mkdir(parents=True, exist_ok=True)
    config_path(d).write_text(json.dumps({"link": link, **DEFAULT_CONFIG}, indent=2))
    (d / STATE / ".gitignore").write_text("*\n")


def print_links(keys: Keys, server: str) -> None:
    if keys.writable:
        print(f"Edit link:      {keys.link(server, write=True)}")
    print(f"Read-only link: {keys.link(server, write=False)}")
    print("Anyone with a link has that access. Keep the edit link private.")


async def run(d: Path, compile_: bool, once: bool = False, shell_escape: bool = False) -> None:
    cfg = {**DEFAULT_CONFIG, **load_config(d)}
    keys, server = load(d)
    replica = Replica(d / STATE, keys, server)
    import platform
    opts = {"engine": cfg["engine"], "latexmk_args": cfg["latexmk_args"],
            "shell_escape": cfg["shell_escape"] or shell_escape,
            "label": cfg["label"] or f"latexmk on {platform.node().split('.')[0]}"}
    fs = FolderSync(d, replica, compile_ or cfg["compile"], opts)
    async with replica.lock:
        await fs.ingest_all()  # local edits made while we were stopped
        await fs.write_out()
    conn = asyncio.create_task(replica.run())

    async def after_connect() -> None:  # retry anything that couldn't be fetched offline
        await replica.synced.wait()
        async with replica.lock:
            await fs.write_out()
    asyncio.create_task(after_connect())
    if once:
        await asyncio.wait_for(replica.synced.wait(), 30)
        await asyncio.sleep(3)  # let acks and peers' replies (relay-only projects) arrive
        conn.cancel()
        return
    print(f"Syncing {d} ⇄ {server} ({'edit' if keys.writable else 'read-only'}). Ctrl-C to stop.")
    try:
        await asyncio.gather(conn, fs.watch())
    finally:
        replica.save()


def main() -> None:
    ap = argparse.ArgumentParser(prog="texloom", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("new", help="share an existing folder as a new project")
    p.add_argument("dir", type=Path)
    p.add_argument("--server", required=True)
    p.add_argument("--name")
    p = sub.add_parser("clone", help="check out a project from a link")
    p.add_argument("link")
    p.add_argument("dir", type=Path)
    p = sub.add_parser("run", help="sync a folder continuously")
    p.add_argument("dir", type=Path, nargs="?", default=Path("."))
    p.add_argument("--compile", action="store_true", help="compile with latexmk on changes")
    p.add_argument("--shell-escape", action="store_true",
                   help="allow shell-escape (minted etc.); runs code collaborators write")
    p = sub.add_parser("sync", help="sync once and exit")
    p.add_argument("dir", type=Path, nargs="?", default=Path("."))
    p = sub.add_parser("storage", help="server storage: 'server' keeps an encrypted copy; 'relay' keeps nothing")
    p.add_argument("dir", type=Path)
    p.add_argument("mode", choices=["server", "relay"])
    p = sub.add_parser("links", help="print share links")
    p.add_argument("dir", type=Path, nargs="?", default=Path("."))
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("watchfiles").setLevel(logging.WARNING)

    if a.cmd == "new":
        a.dir.mkdir(parents=True, exist_ok=True)
        if config_path(a.dir).exists():
            sys.exit(f"{a.dir} is already a texloom project")
        keys = Keys.generate()
        save(a.dir, keys.link(a.server))
        r = Replica(a.dir / STATE, keys, a.server)
        r.local_change(lambda: r.meta.__setitem__("name", a.name or a.dir.resolve().name))
        r.save()
        asyncio.run(run(a.dir, False, once=True))
        print_links(keys, a.server)
    elif a.cmd == "clone":
        keys, server = Keys.from_link(a.link)
        if a.dir.exists() and any(a.dir.iterdir()):
            sys.exit(f"{a.dir} is not empty")
        a.dir.mkdir(parents=True, exist_ok=True)
        save(a.dir, a.link)
        asyncio.run(run(a.dir, False, once=True))
        print(f"Cloned into {a.dir}. Run `texloom run {a.dir}` to keep it in sync.")
    elif a.cmd == "run":
        try:
            asyncio.run(run(a.dir, a.compile, shell_escape=a.shell_escape))
        except KeyboardInterrupt:
            pass
    elif a.cmd == "sync":
        asyncio.run(run(a.dir, False, once=True))
    elif a.cmd == "storage":
        keys, server = load(a.dir)
        r = Replica(a.dir / STATE, keys, server)
        r.set_storage(a.mode)
        asyncio.run(run(a.dir, False, once=True))  # connecting aligns the server with the setting
        print(f"storage mode: {a.mode}")
    elif a.cmd == "links":
        keys, server = load(a.dir)
        print_links(keys, server)


if __name__ == "__main__":
    main()
