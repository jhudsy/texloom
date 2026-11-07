"""texloom command-line interface.

  texloom new   <dir> --server http://host:8787   turn a folder into a shared project
  texloom clone <link> <dir>                      check a project out into a folder
  texloom run   <dir> [--compile]                 keep folder and project in sync
  texloom links <dir>                             print the edit / read-only links
  texloom start                                   sync every folder on this computer's list, in one process
  texloom autostart on|off                        run `texloom start` at login
  texloom list | add <dir> | leave <dir>          the folder list (clone and new add to it)
  texloom run   <dir> --git                       also keep the folder's git repository in step (e.g. GitHub)
  texloom allow <dir>                             review lines that run programs; allow them with --shell-escape
  texloom mcp   <dir|link> [--name NAME]          MCP server: an agent edits/comments as a collaborator
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from . import __version__
from .crypto import Keys
from .folder import STATE, FolderSync
from . import registry
from .daemon import FolderProblem, FolderTag, lock_folder
from .paths import safe_rel
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
    "git": False,              # true, or {"interval": 60, "quiet": 120, "push": true}: see `run --git`
    "shell_escape": False,     # false, "restricted" (TeX Live's safe programs only) or true (minted etc.;
                               # lines that run programs must be approved with `texloom allow`)
    "label": "",               # how your compiles are labelled for others
    "create_code": "",         # creation code, if the server requires one for new projects
}


def save(d: Path, link: str, create_code: str = "") -> None:
    (d / STATE).mkdir(parents=True, exist_ok=True)
    os.chmod(d / STATE, 0o700)
    fd = os.open(config_path(d), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # holds the project keys
    with os.fdopen(fd, "w") as f:
        json.dump({"link": link, **DEFAULT_CONFIG, "create_code": create_code}, f, indent=2)
    (d / STATE / ".gitignore").write_text("*\n")


def print_links(keys: Keys, server: str) -> None:
    print(f"Project code:   {keys.code}")
    if keys.writable:
        print(f"Edit link:      {keys.link(server, write=True)}")
    print(f"Read-only link: {keys.link(server, write=False)}")
    print("Anyone with a link has that access. Keep the edit link private.")


async def run(d: Path, compile_: bool, once: bool = False, shell_escape: bool | str = False, git: bool = False) -> None:
    cfg = {**DEFAULT_CONFIG, **load_config(d)}
    keys, server = load(d)
    held = lock_folder(d)  # noqa: F841 - released when this returns
    code = cfg["create_code"] or os.environ.get("TEXLOOM_CODE", "")
    replica = Replica(d / STATE, keys, server, code or None)
    import platform
    opts = {"engine": cfg["engine"], "latexmk_args": cfg["latexmk_args"],
            "shell_escape": cfg["shell_escape"] or shell_escape,
            "label": cfg["label"] or f"latexmk on {platform.node().split('.')[0]}"}
    fs = FolderSync(d, replica, compile_ or cfg["compile"], opts)
    bridge = None
    if git or cfg.get("git"):
        from .gitsync import GitBridge
        bridge = GitBridge(d, fs, cfg["git"] if isinstance(cfg.get("git"), dict) else None)
        if why := await bridge.check():
            raise FolderProblem(f"--git: {why}")
    async with replica.lock:
        await fs.ingest_all()  # local edits made while we were stopped
        await fs.write_out()
    conn = asyncio.create_task(replica.run())

    async def after_connect() -> None:  # retry anything that couldn't be fetched offline
        await replica.synced.wait()
        cid = str(replica.doc.client_id)
        name = cfg.get("name") or f"folder copy on {platform.node().split('.')[0]}"
        if keys.writable and replica.clients.get(cid) != name:  # for "who wrote this"
            replica.local_change(lambda: replica.clients.__setitem__(cid, name))
        async with replica.lock:
            await fs.write_out()
    later = asyncio.create_task(after_connect())
    if once:
        await asyncio.wait_for(replica.synced.wait(), 30)
        await asyncio.sleep(3)  # let acks and peers' replies (relay-only projects) arrive
        if bridge:
            async with replica.lock:
                await fs.write_out()
            await bridge.cycle(force=True)
            await asyncio.sleep(3)  # let what came in from git reach the server
        conn.cancel()
        later.cancel()
        return
    log.info("syncing %s ⇄ %s (%s)", d.resolve(), server, "edit" if keys.writable else "read-only")
    if bridge:
        up = await bridge.upstream()
        log.info(f"git: committing project changes after {bridge.quiet:g}s of quiet"
              + (f", fetching {up} every {bridge.interval:g}s and pushing to it." if up else " (no upstream: commits stay local)."))
    try:
        await asyncio.gather(conn, fs.watch(), *([bridge.loop()] if bridge else []))
    finally:
        later.cancel()
        replica.save()


def list_folders() -> None:
    have = registry.folders()
    if not have:
        print("No folders yet: `texloom clone <link> <dir>` or `texloom add <dir>`.")
        return
    for d in have:
        try:
            cfg = {**DEFAULT_CONFIG, **load_config(d)}
            keys, server = Keys.from_link(cfg["link"])
            name = str(Replica(d / STATE, keys, server).meta.get("name") or "") or d.name
            extras = [x for x, on in (("compile", cfg["compile"]), ("git", cfg.get("git"))) if on]
            print(f"{d}\n    “{name}” — {'edit' if keys.writable else 'read-only'}, {server}"
                  + (f", {' + '.join(extras)}" if extras else ""))
        except (OSError, ValueError, KeyError) as e:
            print(f"{d}\n    can't read it ({e})")


def allow_programs(d: Path, yes: bool) -> None:
    """Show every line that asks to run programs; on confirmation, allow exactly those."""
    from .scan import allow, allowed, scan
    keys, server = load(d)
    r = Replica(d / STATE, keys, server)  # the local copy, as last synced
    found = [f for f in scan({p: str(t) for p, t in r.files.items() if safe_rel(p)}) if f.kind == "shell"]
    if not found:
        allow(d, set())
        print("No lines in this project ask to run programs.")
        return
    before = allowed(d)
    print("With shell escape, these lines run programs on this computer:\n")
    for f in found:
        print(("  " if f.key in before else "* ") + f.describe())
    print("\n(* = new since you last allowed.) Anyone with an edit link can change them; macros can")
    print("also hide commands, so only allow this for projects whose editors you trust.")
    if not yes and input("Allow these lines to run? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Nothing changed.")
        return
    allow(d, {f.key for f in found})
    print(f"Allowed {len(found)} line(s). A running `texloom run` uses them from its next compile.")


def main() -> None:
    ap = argparse.ArgumentParser(prog="texloom", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=f"texloom {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("new", help="share an existing folder as a new project")
    p.add_argument("dir", type=Path)
    p.add_argument("--server", required=True)
    p.add_argument("--name")
    p.add_argument("--code", default=os.environ.get("TEXLOOM_CODE", ""),
                   help="creation code, if the server requires one to store new projects")
    p = sub.add_parser("clone", help="check out a project from a link")
    p.add_argument("link")
    p.add_argument("dir", type=Path)
    p = sub.add_parser("run", help="sync a folder continuously")
    p.add_argument("dir", type=Path, nargs="?", default=Path("."))
    p.add_argument("--compile", action="store_true", help="compile with latexmk on changes")
    p.add_argument("--git", action="store_true",
                   help="also commit project changes to the folder's git repository, push them, and merge "
                        "new upstream commits into the project")
    p.add_argument("--shell-restricted", action="store_true",
                   help="let TeX run only TeX Live's list of safe programs (e.g. for epstopdf)")
    p.add_argument("--shell-escape", action="store_true",
                   help="allow shell-escape (minted etc.); runs code collaborators write")
    sub.add_parser("start", help="sync every folder on this computer's list, in one process")
    p = sub.add_parser("autostart", help="run `texloom start` at login (on/off/status)")
    p.add_argument("state", choices=["on", "off", "status"])
    sub.add_parser("list", help="the folders this computer keeps in sync")
    p = sub.add_parser("add", help="add a texloom folder to the list")
    p.add_argument("dir", type=Path)
    p = sub.add_parser("leave", help="stop syncing a folder (its files stay)")
    p.add_argument("dir", type=Path)
    p = sub.add_parser("allow", help="review the lines that run programs, and let them run in shell-escape compiles")
    p.add_argument("dir", type=Path, nargs="?", default=Path("."))
    p.add_argument("-y", "--yes", action="store_true", help="don't ask")
    p = sub.add_parser("sync", help="sync once and exit")
    p.add_argument("dir", type=Path, nargs="?", default=Path("."))
    p.add_argument("--git", action="store_true", help="also commit, merge upstream commits and push, once")
    p = sub.add_parser("storage", help="server storage: 'server' keeps an encrypted copy; 'relay' keeps nothing")
    p.add_argument("dir", type=Path)
    p.add_argument("mode", choices=["server", "relay"])
    p = sub.add_parser("mcp", help="run an MCP server (stdio) so an agent can work on the project")
    p.add_argument("target", help="a texloom folder, or a project link")
    p.add_argument("--name", default="Claude (agent)", help="how the agent appears to collaborators")
    p = sub.add_parser("links", help="print share links")
    p.add_argument("dir", type=Path, nargs="?", default=Path("."))
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(folder)s%(message)s", datefmt="%H:%M:%S")
    for h in logging.getLogger().handlers:
        h.addFilter(FolderTag())
    logging.getLogger("watchfiles").setLevel(logging.WARNING)

    if a.cmd == "new":
        a.dir.mkdir(parents=True, exist_ok=True)
        if config_path(a.dir).exists():
            sys.exit(f"{a.dir} is already a texloom project")
        keys = Keys.generate()
        save(a.dir, keys.link(a.server), a.code)
        r = Replica(a.dir / STATE, keys, a.server, a.code or None)
        r.local_change(lambda: r.meta.__setitem__("name", a.name or a.dir.resolve().name))
        r.save()
        asyncio.run(run(a.dir, False, once=True))
        registry.add(a.dir)
        print_links(keys, a.server)
    elif a.cmd == "clone":
        keys, server = Keys.from_link(a.link)
        if a.dir.exists() and any(a.dir.iterdir()):
            sys.exit(f"{a.dir} is not empty")
        a.dir.mkdir(parents=True, exist_ok=True)
        save(a.dir, a.link)
        asyncio.run(run(a.dir, False, once=True))
        registry.add(a.dir)
        print(f"Cloned into {a.dir}. A running `texloom start` picks it up; or run `texloom run {a.dir}`.")
        print("Collaborators' files are written there. texloom won't write files that LaTeX tools run as\n"
              "programs and warns about lines that ask to run one, but compile the folder with your own\n"
              "tools only if you trust everyone who can edit this project.")
    elif a.cmd == "run":
        try:
            asyncio.run(run(a.dir, a.compile, shell_escape=True if a.shell_escape else "restricted" if a.shell_restricted else False,
                            git=a.git))
        except FolderProblem as e:
            sys.exit(str(e))
        except KeyboardInterrupt:
            pass
    elif a.cmd == "start":
        from .daemon import start
        try:
            asyncio.run(start(lambda d: run(d, False)))
        except KeyboardInterrupt:
            pass
    elif a.cmd == "autostart":
        from . import autostart
        print(autostart.status() if a.state == "status" else autostart.enable() if a.state == "on" else autostart.disable())
    elif a.cmd == "list":
        list_folders()
    elif a.cmd == "add":
        if not config_path(a.dir).exists():
            sys.exit(f"{a.dir} isn't a texloom folder: use `texloom clone <link> {a.dir}` or `texloom new {a.dir} --server …`")
        print("Added." if registry.add(a.dir) else "Already on the list.")
    elif a.cmd == "leave":
        print(f"No longer syncing {a.dir.resolve()} (its files stay; `texloom add` resumes)." if registry.remove(a.dir)
              else "That folder isn't on the list.")
    elif a.cmd == "allow":
        allow_programs(a.dir, a.yes)
    elif a.cmd == "sync":
        try:
            asyncio.run(run(a.dir, False, once=True, git=a.git))
        except FolderProblem as e:
            sys.exit(str(e))
    elif a.cmd == "storage":
        keys, server = load(a.dir)
        r = Replica(a.dir / STATE, keys, server)
        r.set_storage(a.mode)
        asyncio.run(run(a.dir, False, once=True))  # connecting aligns the server with the setting
        print(f"storage mode: {a.mode}")
    elif a.cmd == "mcp":
        from .mcp import serve
        if "#" in a.target:
            keys, server = Keys.from_link(a.target)
            code = os.environ.get("TEXLOOM_CODE", "")
        else:
            d = Path(a.target)
            keys, server = load(d)
            code = load_config(d).get("create_code") or os.environ.get("TEXLOOM_CODE", "")
        try:
            asyncio.run(serve(keys, server, a.name[:64], code or None))
        except KeyboardInterrupt:
            pass
    elif a.cmd == "links":
        keys, server = load(a.dir)
        print_links(keys, server)


if __name__ == "__main__":
    main()
