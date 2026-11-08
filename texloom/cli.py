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
  texloom mcp   <dir|link> [--name NAME] [--suggest-only]  MCP server: an agent edits/comments as a collaborator
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
from .daemon import CURRENT, FolderProblem, FolderTag, lock_folder
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
    """Sync one folder (until cancelled, or one round with once=True). Holds the folder's lock
    while running, released however it ends (a cancelled task may keep its frame alive)."""
    held = lock_folder(d)
    try:
        await _run(d, compile_, once, shell_escape, git)
    finally:
        held.close()


async def _run(d: Path, compile_: bool, once: bool, shell_escape: bool | str, git: bool) -> None:
    cfg = {**DEFAULT_CONFIG, **load_config(d)}
    keys, server = load(d)
    code = cfg["create_code"] or os.environ.get("TEXLOOM_CODE", "")
    replica = Replica(d / STATE, keys, server, code or None)
    import platform
    opts = {"engine": cfg["engine"], "latexmk_args": cfg["latexmk_args"],
            "shell_escape": cfg["shell_escape"] or shell_escape,
            "label": cfg["label"] or f"latexmk on {platform.node().split('.')[0]}"}
    fs = FolderSync(d, replica, compile_ or cfg["compile"], opts)
    if (state := CURRENT.get()) is not None:  # running under the app or `texloom start`
        state.replica, state.fs = replica, fs
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


async def clone_project(link: str, d: Path) -> None:
    """Check a project out into a new or empty folder, and add it to the folder list."""
    Keys.from_link(link)  # a usable link?
    if d.exists() and (not d.is_dir() or any(d.iterdir())):
        raise FolderProblem(f"{d} is not empty: choose a new or empty folder")
    d.mkdir(parents=True, exist_ok=True)
    save(d, link)
    await run(d, False, once=True)
    registry.add(d)


async def new_project(d: Path, server: str, name: str = "", code: str = "") -> Keys:
    """Share an existing folder as a new project, and add it to the folder list."""
    if config_path(d).exists():
        raise FolderProblem(f"{d} is already a texloom project")
    d.mkdir(parents=True, exist_ok=True)
    keys = Keys.generate()
    save(d, keys.link(server), code)
    r = Replica(d / STATE, keys, server, code or None)
    r.local_change(lambda: r.meta.__setitem__("name", name or d.resolve().name))
    r.save()
    await run(d, False, once=True)
    registry.add(d)
    return keys


async def peek(link: str, timeout: float = 15) -> dict:
    """Look at a project before joining it: its name, access and size (nothing is kept)."""
    import tempfile
    keys, server = Keys.from_link(link)
    with tempfile.TemporaryDirectory(prefix="texloom-peek-") as tmp:
        r = Replica(Path(tmp), keys, server)
        task = asyncio.create_task(r.run())
        try:
            await asyncio.wait_for(r.synced.wait(), timeout)
        except asyncio.TimeoutError:
            raise FolderProblem(f"couldn't reach {server}") from None
        finally:
            task.cancel()
        return {"name": str(r.meta.get("name") or ""), "writable": keys.writable, "server": server,
                "files": len(r.files) + len(r.bin), "member": keys.member[2] if keys.member else None}


def update_config(d: Path, **changes) -> None:
    """Change settings in a folder's .texloom/config.json (kept readable by you only)."""
    cfg = {**load_config(d), **changes}
    fd = os.open(config_path(d), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)


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


def projects_command(a) -> None:
    from .listsync import ListError, ListKey, local_folders, not_here, save_link, saved_link, sync, sync_this_computer
    code = a.code or os.environ.get("TEXLOOM_CODE") or None
    try:
        if a.action == "off":
            save_link(None)
            print("This computer no longer uses a project list. The list itself stays on the server.")
            return
        if a.action == "new":
            if not a.server:
                sys.exit("`texloom projects new` needs --server URL")
            key = ListKey.generate()
            sync(key, a.server.rstrip("/"), local_folders(a.server), code)
            save_link(key.link(a.server))
            print("Your sync link (keep it in a password manager: it opens every project on the list):\n")
            print(key.link(a.server))
            print("\nThis can't be recovered: nobody can reset it for you. If you lose this link and every passkey,\n"
                  "the list is gone for good. Your projects aren't deleted, but you'll need each one's own link.")
            return
        if a.action == "use":
            if not a.link:
                sys.exit("`texloom projects use` needs the sync link (quote it: it contains '#')")
            ListKey.from_link(a.link)
            save_link(a.link)
        result = sync_this_computer(code)
    except ValueError as e:
        sys.exit(str(e))
    except ListError as e:
        sys.exit({"needs-code": "This server needs a creation code to store a list: use --code or TEXLOOM_CODE.",
                  "code-expired": "The creation code this list was created with has expired.",
                  "list-full": "The server has no room for another list."}.get(e.code, e.code))
    if result is None:
        print("No project list yet. Start one with `texloom projects new --server URL`,\n"
              "or use an existing one with `texloom projects use '<sync link>'`.")
        return
    _, server, doc = result
    away = {p for p, _ in not_here(doc, server)}
    for p, e in sorted(doc["projects"].items(), key=lambda x: -x[1].get("at", 0)):
        print(f"{e.get('nick') or e.get('n') or '(untitled)'}  [{'edit' if e.get('w') else 'read-only'}]"
              f"{'  not on this computer' if p in away else ''}")
        if a.links:
            print(f"    {server}/#{e['f']}")


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
    p = sub.add_parser("app", help="the TeXLoom app: your synced folders in a window and the tray (needs texloom[app])")
    p.add_argument("--hidden", action="store_true", help="start in the tray, without opening the window")
    p.add_argument("url", nargs="?", help="a texloom://join?link=… URL")
    p = sub.add_parser("autostart", help="run `texloom start` at login (on/off/status)")
    p.add_argument("state", choices=["on", "off", "status"])
    sub.add_parser("list", help="the folders this computer keeps in sync")
    p = sub.add_parser("projects", help="your encrypted project list on the server (show/new/use/off)")
    p.add_argument("action", nargs="?", default="show", choices=["show", "new", "use", "off"])
    p.add_argument("link", nargs="?", help="for `use`: the sync link")
    p.add_argument("--server", help="for `new`: the server, e.g. https://texloom.example.org")
    p.add_argument("--code", help="creation code, if the server needs one to store a new list")
    p.add_argument("--links", action="store_true", help="also print each project's link")
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
    p.add_argument("--suggest-only", action="store_true",
                   help="the agent can comment and suggest changes for people to accept, but not edit files directly")
    p = sub.add_parser("links", help="print share links")
    p.add_argument("dir", type=Path, nargs="?", default=Path("."))
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(folder)s%(message)s", datefmt="%H:%M:%S")
    for h in logging.getLogger().handlers:
        h.addFilter(FolderTag())
    logging.getLogger("watchfiles").setLevel(logging.WARNING)

    if a.cmd == "new":
        try:
            keys = asyncio.run(new_project(a.dir, a.server, a.name or "", a.code))
        except FolderProblem as e:
            sys.exit(str(e))
        print_links(keys, a.server)
    elif a.cmd == "clone":
        try:
            asyncio.run(clone_project(a.link, a.dir))
        except FolderProblem as e:
            sys.exit(str(e))
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
    elif a.cmd == "app":
        try:
            from .app import main as app_main
        except ImportError:
            sys.exit("The app needs Qt: install it with `uv tool install 'texloom[app]'` (or pip install 'texloom[app]').")
        app_main((["--hidden"] if a.hidden else []) + ([a.url] if a.url else []))
        return
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
            asyncio.run(serve(keys, server, a.name[:64], code or None, a.suggest_only))
        except KeyboardInterrupt:
            pass
    elif a.cmd == "projects":
        projects_command(a)
    elif a.cmd == "links":
        keys, server = load(a.dir)
        print_links(keys, server)


if __name__ == "__main__":
    main()
