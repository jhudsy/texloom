"""texloom command-line interface.

  texloom new   <dir> --server http://host:8787   turn a folder into a shared project
  texloom clone <link> <dir>                      check a project out into a folder
  texloom run   <dir> [--compile]                 keep folder and project in sync
  texloom links <dir>                             print the edit / read-only links
  texloom start                                   sync every folder on this computer's list, in one process
  texloom autostart on|off                        run `texloom start` at login
  texloom list | add <dir> | leave <dir>          the folder list (clone and new add to it)
  texloom run   <dir> --git                       also keep the folder's git repository in step (any host)
  texloom allow <dir>                             review lines that run programs; allow them with --shell-escape
  texloom settings <dir> [KEY=VALUE ...]          this computer's settings for a folder (compile, engine, …)
  texloom mcp   <dir|link> [--name NAME] [--suggest-only]  MCP server: an agent edits/comments as a collaborator
  texloom lsp [dir]                               language server: the Review tab's comments in your editor
  texloom comments <dir> [--mine] [--all]         the Review tab's comments; also: reply <dir> ID TEXT, resolve <dir> ID
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys
from pathlib import Path

from . import __version__
from .crypto import Keys
from .folder import FolderSync
from . import registry, tls
# the folder's settings, from config.py (still imported from here by scripts)
from .config import (DEFAULT_CONFIG, LOCAL_SETTINGS, adopt, browser_link, config_path, load, load_config, save,  # noqa: F401
                     settings_path, side_replica, update_config)
from .daemon import CURRENT, FolderProblem, FolderTag, lock_folder
from .paths import state_dir
from .replica import Replica

log = logging.getLogger("texloom")


def print_links(keys: Keys, server: str) -> None:
    print(f"Project code:   {keys.code}")
    if keys.writable:
        print(f"Edit link:      {keys.link(server, write=True)}")
    print(f"Read-only link: {keys.link(server, write=False)}")
    print("Anyone with a link has that access. Keep the edit link private.")


OVERVIEW_EVERY = 5  # seconds between looks at the comments, for .texloom/comments.md


async def run(d: Path, compile_: bool, once: bool = False, shell_escape: bool | str = False, git: bool = False) -> None:
    """Sync one folder (until cancelled, or one round with once=True). Holds the folder's lock
    while running, released however it ends (a cancelled task may keep its frame alive)."""
    held = lock_folder(d)
    try:
        await _run(d, compile_, once, shell_escape, git)
    finally:
        held.close()


async def _run(d: Path, compile_: bool, once: bool, shell_escape: bool | str, git: bool) -> None:
    cfg = load_config(d)
    keys, server = load(d)
    code = cfg["create_code"]  # never the environment's: a project's link decides which server hears it
    replica = Replica(state_dir(d), keys, server, code or None)
    import platform
    opts = {"engine": cfg["engine"], "latexmk_args": cfg["latexmk_args"],
            "shell_escape": cfg["shell_escape"] or shell_escape,
            "confine": cfg["confine"] is not False,
            "label": cfg["label"] or f"latexmk on {platform.node().split('.')[0]}"}
    fs = FolderSync(d, replica, compile_ or cfg["compile"], opts)
    from .listsync import name_in
    name = name_in(d) or f"folder copy on {platform.node().split('.')[0]}"
    from .merges import MergeWatch
    merges = MergeWatch(replica, lambda: name, state_dir(d))  # before ingesting: edits made while stopped count as apart
    if (state := CURRENT.get()) is not None:  # running under the app or `texloom start`
        state.replica, state.fs = replica, fs
    bridge = None
    if git or cfg.get("git"):
        from .gitsync import GitBridge
        bridge = GitBridge(d, fs, cfg["git"] if isinstance(cfg.get("git"), dict) else None)
        bridge.name = name
        if why := await bridge.check():
            raise FolderProblem(f"--git: {why}")
    from .sharedfolder import SharedFolder
    turns = SharedFolder(d, fs, lambda: name)  # a folder also shared through Dropbox or the like: take turns
    if state is not None:
        state.turns = turns
    async with replica.lock:
        await fs.ingest_all()  # local edits made while we were stopped (standing by: only noted)
        await fs.write_out()
    conn = asyncio.create_task(replica.run())

    async def after_connect() -> None:  # retry anything that couldn't be fetched offline
        await replica.synced.wait()
        replica.name_client(name)
        if not bridge:
            from .gitsync import clone_of_link
            if await clone_of_link(d, replica.meta.get("github")):
                log.warning("This folder is a git clone of %s, which the project is kept in step with from the web app. Pulling "
                            "here by hand while that runs can bring the same changes in twice: turn on “Keep this "
                            "folder’s git repository in step” (texloom run --git) instead, so the app takes turns.",
                            replica.meta.get("github", {}).get("repo"))
        async with replica.lock:
            await fs.write_out()
    later = asyncio.create_task(after_connect())

    async def keep_overview() -> None:  # .texloom/comments.md: the Review tab, for reading in any editor
        from .comments import write_overview
        await replica.synced.wait()
        while True:
            try:
                write_overview(d, replica, name)
            except OSError as e:
                log.debug("comments overview not written: %s", e)
            await asyncio.sleep(OVERVIEW_EVERY)
    if once:
        await asyncio.wait_for(replica.synced.wait(), 30)
        await asyncio.sleep(3)  # let acks and peers' replies (relay-only projects) arrive
        from .comments import write_overview
        write_overview(d, replica, name)
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
        await asyncio.gather(conn, fs.watch(), merges.run(), turns.run(), keep_overview(), *([bridge.loop()] if bridge else []))
    finally:
        later.cancel()
        merges.save()
        replica.save()


def refuse_if_off(pref: str, what: str) -> None:
    """The app's Settings can turn a server off for this computer: it then won't start."""
    from .i18n import app_pref
    if not app_pref(pref, True):
        sys.exit(f"TeXLoom: {what} is turned off on this computer. Turn it on in the TeXLoom app's Settings.")


def comments_command(a: argparse.Namespace) -> None:
    """`texloom comments DIR` lists from this computer's copy of the project (as up to date as its
    last sync); `reply` and `resolve` connect with a copy of their own, as `texloom mcp` does, so
    they work while the app or `texloom run` is syncing the folder."""
    from .comments import author, read, reply, resolve
    from .listsync import name_in
    act = a.args[0] if a.args[0] in ("reply", "resolve") else "list"
    rest = a.args[1:] if act != "list" else a.args
    if not rest or (act == "reply" and len(rest) < 3) or (act == "resolve" and len(rest) != 2):
        sys.exit("usage: texloom comments DIR | texloom comments reply DIR ID TEXT | texloom comments resolve DIR ID")
    d = Path(rest[0])
    keys, server = load(d)
    name = name_in(d) or getpass.getuser()
    if act == "list":
        r = Replica(state_dir(d), keys, server)
        items = [c for c in read(r, a.all) if not a.mine or c.mentions(name)]
        if not items:
            print("Nothing open." if not a.mine else f"Nothing mentions @{name}.")
        for c in items:
            where = f"{c.file}:{c.line}" if c.line else f"{c.file} (text changed)"
            state = " [resolved]" if c.resolved else ""
            print(f"{where}: {c.kind} by {author(c.by, c.verified)}{state}  (id {c.id})")
            body = c.text if c.kind == "comment" else (f"replace {c.quote!r} with {c.text!r}" if c.quote else f"insert {c.text!r}")
            print("    " + body.replace("\n", "\n    "))
            for rp in c.replies:
                print(f"    ↳ {author(rp.by, rp.verified)}: {rp.text}")
        return
    if not keys.writable:
        sys.exit("this folder has a read-only link: it can't reply or resolve")

    async def change() -> None:
        r = side_replica("comments", keys, server, load_config(d)["create_code"] or None)
        conn = asyncio.create_task(r.run())
        try:
            await asyncio.wait_for(r.synced.wait(), 60)
            iid = rest[1]
            if act == "reply":
                reply(r, iid, " ".join(rest[2:]), name)
            else:
                resolve(r, iid)
            for _ in range(200):  # until the server has it
                if not r.outbox:
                    break
                await asyncio.sleep(0.1)
            if r.outbox:
                sys.exit("not confirmed by the server yet: it's sent when this copy next connects (run the command again)")
        finally:
            r.save()
            conn.cancel()
    try:
        asyncio.run(change())
    except KeyError as e:
        sys.exit(f"no comment with id {e.args[0]}")
    except ValueError as e:
        sys.exit(str(e))
    print("Replied." if act == "reply" else "Resolved.")


async def clone_project(link: str, d: Path) -> None:
    """Check a project out into a new or empty folder, and add it to the folder list."""
    Keys.from_link(link)  # a usable link?
    if d.exists() and (not d.is_dir() or any(d.iterdir())):
        raise FolderProblem(f"{d} is not empty: choose a new or empty folder")
    d.mkdir(parents=True, exist_ok=True)
    save(d, link)
    await run(d, False, once=True)
    registry.add(d)


async def refuse_if_private(server: str, code: str) -> dict:
    """A private server only takes new projects from people with a valid creation code. If it
    can't be asked, carry on: the server still decides when the project connects. Returns
    what the server says about itself ({} if it couldn't be asked)."""
    import aiohttp
    info = await server_info(server)
    if not info.get("private"):
        return info
    try:
        if await code_ok(server, code):
            return info
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return info
    raise FolderProblem(f"{server.rstrip('/')} is a private server: a new project needs a valid creation code from its owner")


async def server_info(server: str) -> dict:
    """What the server says about itself (/api/info), or {} if it can't be asked."""
    import aiohttp
    try:
        async with aiohttp.ClientSession(connector=tls.connector(), timeout=aiohttp.ClientTimeout(total=10)) as http:
            async with http.get(f"{server.rstrip('/')}/api/info") as r:
                info = (await r.json()) if r.ok else {}
                return info if isinstance(info, dict) else {}
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return {}


async def code_ok(server: str, code: str) -> bool:
    """Whether the server takes this creation code."""
    import aiohttp
    async with aiohttp.ClientSession(connector=tls.connector(), timeout=aiohttp.ClientTimeout(total=10)) as http:
        async with http.post(f"{server.rstrip('/')}/api/code", json={"code": code}) as r:
            return r.ok and (await r.json()).get("ok") is True


async def new_project(d: Path, server: str, name: str = "", code: str = "", settings: dict | None = None) -> Keys:
    """Share an existing folder as a new project, and add it to the folder list.
    `settings` are further shared project settings (the main file, engine…)."""
    if config_path(d).exists():
        raise FolderProblem(f"{d} is already a texloom project")
    info = await refuse_if_private(server, code)
    d.mkdir(parents=True, exist_ok=True)
    keys = Keys.generate()
    save(d, keys.link(server), code)
    r = Replica(state_dir(d), keys, server, code or None)

    def describe() -> None:
        r.meta["name"] = name or d.resolve().name
        for k, v in (settings or {}).items():
            r.meta[k] = v

    r.local_change(describe)
    if not code and info.get("creation") == "code":  # the server wouldn't keep it: sync-only from the start
        log.info("no creation code: this project syncs only between computers that are online at the same time")
        r.set_storage("relay")
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
        if r.deleted_at is not None:
            raise FolderProblem("the owner deleted this project for everyone")
        return {"name": str(r.meta.get("name") or ""), "writable": keys.writable, "server": server,
                "files": len(r.files) + len(r.bin), "member": keys.member[2] if keys.member else None}


def list_folders() -> None:
    have = registry.folders()
    if not have:
        print("No folders yet: `texloom clone <link> <dir>` or `texloom add <dir>`.")
        return
    for d in have:
        try:
            cfg = load_config(d)
            keys, server = Keys.from_link(cfg["link"])
            name = str(Replica(state_dir(d), keys, server).meta.get("name") or "") or d.name
            extras = [x for x, on in (("compile", cfg["compile"]), ("git", cfg.get("git"))) if on]
            print(f"{d}\n    “{name}” — {'edit' if keys.writable else 'read-only'}, {server}"
                  + (f", {' + '.join(extras)}" if extras else ""))
        except (OSError, ValueError, KeyError) as e:
            print(f"{d}\n    can't read it ({e})")


def allow_programs(d: Path, yes: bool) -> None:
    """Show every line that asks to run programs; on confirmation, allow exactly those."""
    from .scan import allow, allowed, binaries_on_disk, shell_lines
    keys, server = load(d)
    r = Replica(state_dir(d), keys, server)  # the local copy, as last synced
    found = shell_lines(r.files, binaries_on_disk(d, r.bin))
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


def settings_command(d: Path, changes: list[str]) -> None:
    """Show this computer's settings for a folder, or change them (KEY=VALUE, the value as JSON:
    compile=true, engine="xelatex", latexmk_args=["-pdfxe"]; a bare word is taken as a string)."""
    import json
    if not config_path(d).exists():
        sys.exit(f"{d} isn't a texloom folder")
    if changes:
        new = {}
        for c in changes:
            k, eq, v = c.partition("=")
            if not eq or k not in LOCAL_SETTINGS or k == "allowed":
                sys.exit(f"not a setting: {c!r} (settings: {', '.join(k for k in LOCAL_SETTINGS if k != 'allowed')}; "
                         "use `texloom allow` for the lines that may run programs)")
            try:
                new[k] = json.loads(v)
            except ValueError:
                new[k] = v
        update_config(d, **new)
        print("Changed. A running texloom picks them up when the folder restarts (the app restarts it).")
    cfg = load_config(d)
    for k in LOCAL_SETTINGS:
        if k != "allowed":
            print(f"{k} = {json.dumps(cfg[k])}")
    print(f"({len(cfg['allowed'])} line(s) allowed to run programs; kept in {settings_path(d)})")


def projects_command(a) -> None:
    from .listsync import ListError, ListKey, explain, local_folders, not_here, save_link, sync, sync_this_computer, unlock_with_password
    code = a.code or (os.environ.get("TEXLOOM_CODE") if a.action == "new" else None) or None
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
            print("\nThis can't be recovered: nobody can reset it for you. If you lose this link, every passkey and any\n"
                  "password, the list is gone for good. Your projects aren't deleted, but you'll need each one's own link.")
            return
        if a.action == "use" and a.password:
            if not a.server:
                sys.exit("`texloom projects use --password` needs --server URL")
            username = input("Username or email: ")
            server = a.server.rstrip("/")
            save_link(unlock_with_password(server, username, getpass.getpass("Password: ")).link(server))
        elif a.action == "use":
            if not a.link:
                sys.exit("`texloom projects use` needs the sync link (quote it: it contains '#')")
            ListKey.from_link(a.link)
            save_link(a.link)
        result = sync_this_computer(code)
    except ValueError as e:
        sys.exit(str(e))
    except ListError as e:
        sys.exit(explain(e.code) + (" Use --code." if e.code == "needs-code" else ""))
    if result is None:
        print("No project list yet. Start one with `texloom projects new --server URL`,\n"
              "or use an existing one with `texloom projects use '<sync link>'`.")
        return
    server, doc = result
    away = {p for p, _ in not_here(doc, server)}
    for p, e in sorted(doc["projects"].items(), key=lambda x: -x[1].get("at", 0)):
        print(f"{e.get('nick') or e.get('n') or '(untitled)'}  [{'edit' if e.get('w') else 'read-only'}]"
              f"{'  not on this computer' if p in away else ''}")
        if a.links:
            print(f"    {server}/#{e['f']}")


def main() -> None:
    from .paths import use_tex_on_path
    use_tex_on_path()  # started at login or by another program: TeX may not be on PATH
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
    p.add_argument("--password", action="store_true", help="for `use`: unlock with a username and password (set in the web app)")
    p = sub.add_parser("add", help="add a texloom folder to the list")
    p.add_argument("dir", type=Path)
    p = sub.add_parser("leave", help="stop syncing a folder (its files stay)")
    p.add_argument("dir", type=Path)
    p = sub.add_parser("allow", help="review the lines that run programs, and let them run in shell-escape compiles")
    p.add_argument("dir", type=Path, nargs="?", default=Path("."))
    p.add_argument("-y", "--yes", action="store_true", help="don't ask")
    p = sub.add_parser("settings", help="this computer's settings for a folder: show, or change with KEY=VALUE")
    p.add_argument("dir", type=Path)
    p.add_argument("changes", nargs="*", metavar="KEY=VALUE")
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
    p = sub.add_parser("lsp", help="run a language server (stdio) that shows the Review tab's comments in your editor")
    p.add_argument("dir", type=Path, nargs="?", help="a texloom folder (default: the editor's workspace folder)")
    p = sub.add_parser("comments", help="list the Review tab's comments, or reply to or resolve one")
    p.add_argument("args", nargs="+", metavar="[reply|resolve] DIR [ID [TEXT]]",
                   help="DIR to list; `reply DIR ID TEXT`; `resolve DIR ID`")
    p.add_argument("--mine", action="store_true", help="only those that mention you (@ and your name)")
    p.add_argument("--all", action="store_true", help="resolved ones too")
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
        if not config_path(a.dir).exists() and not adopt(a.dir):
            sys.exit(f"{a.dir} isn't a texloom folder: use `texloom clone <link> {a.dir}` or `texloom new {a.dir} --server …`")
        print("Added." if registry.add(a.dir) else "Already on the list.")
    elif a.cmd == "leave":
        print(f"No longer syncing {a.dir.resolve()} (its files stay; `texloom add` resumes)." if registry.remove(a.dir)
              else "That folder isn't on the list.")
    elif a.cmd == "allow":
        allow_programs(a.dir, a.yes)
    elif a.cmd == "settings":
        settings_command(a.dir, a.changes)
    elif a.cmd == "sync":
        try:
            asyncio.run(run(a.dir, False, once=True, git=a.git))
        except FolderProblem as e:
            sys.exit(str(e))
    elif a.cmd == "storage":
        keys, server = load(a.dir)
        if keys.seed is None:
            sys.exit("Only the project's owner can change where it's kept (this folder has an edit link, not the owner link).")
        r = Replica(state_dir(a.dir), keys, server)
        r.set_storage(a.mode)
        asyncio.run(run(a.dir, False, once=True))  # connecting aligns the server with the setting
        print(f"storage mode: {a.mode}")
    elif a.cmd == "mcp":
        refuse_if_off("allow_mcp", "AI tools connecting to your projects (MCP)")
        from .mcp import serve
        if "#" in a.target:
            keys, server = Keys.from_link(a.target)
            code = ""
        else:
            d = Path(a.target)
            keys, server = load(d)
            code = load_config(d)["create_code"]
        try:
            asyncio.run(serve(keys, server, a.name[:64], code or None, a.suggest_only))
        except KeyboardInterrupt:
            pass
    elif a.cmd == "lsp":
        refuse_if_off("allow_lsp", "editors showing comments (the language server)")
        from .lsp import serve as serve_lsp
        try:
            asyncio.run(serve_lsp(a.dir))
        except KeyboardInterrupt:
            pass
    elif a.cmd == "comments":
        comments_command(a)
    elif a.cmd == "projects":
        projects_command(a)
    elif a.cmd == "links":
        keys, server = load(a.dir)
        print_links(keys, server)


if __name__ == "__main__":
    main()
