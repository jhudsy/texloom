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
  texloom mcp   <dir|link> [--name NAME] [--suggest-only]  MCP server: an agent edits/comments as a collaborator
  texloom lsp [dir]                               language server: the Review tab's comments in your editor
  texloom comments <dir> [--mine] [--all]         the Review tab's comments; also: reply <dir> ID TEXT, resolve <dir> ID
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import time
import logging
import os
import sys
from pathlib import Path

from . import __version__
from .crypto import Keys
from .folder import FolderSync
from . import registry, tls
from .daemon import CURRENT, FolderProblem, FolderTag, lock_folder
from .paths import STATE, state_dir
from .replica import Replica

log = logging.getLogger("texloom")


def config_path(d: Path) -> Path:
    return state_dir(d) / "config.json"


def load(d: Path) -> tuple[Keys, str]:
    return Keys.from_link(load_config(d)["link"])


def load_config(d: Path) -> dict:
    return json.loads(config_path(d).read_text())


def browser_link(d: Path, extra: str = "") -> str:
    """A folder's project link for opening in the browser, marked as opened by this app, so the web
    app doesn't ask where the files should live: they're in this folder already. `extra`: more of the
    query, such as "tab=review" (web/src/main.ts reads it)."""
    cfg = load_config(d)
    page, _hash, keys = cfg["link"].partition("#")
    mark = "from=app" + ("&compile=1" if cfg.get("compile") else "") + (f"&{extra}" if extra else "")  # the web app then compiles here by default
    return f"{page}{'&' if '?' in page else '?'}{mark}#{keys}"


DEFAULT_CONFIG = {
    # Local, per-folder settings (never shared with collaborators).
    "compile": False,          # run latexmk after changes (same as --compile)
    "engine": None,            # override the project's engine on this machine
    "latexmk_args": [],        # extra arguments, e.g. ["-pdfxe"]
    "git": False,              # true, or {"interval": 60, "quiet": 120, "push": true}: see `run --git`
    "shell_escape": False,     # false, "restricted" (TeX Live's safe programs only) or true (minted etc.;
                               # lines that run programs must be approved with `texloom allow`)
    "confine": True,           # compiles may read only the project, TeX's files and fonts (false: anything
                               # you can read, for projects whose collaborators you trust)
    "label": "",               # how your compiles are labelled for others
    "create_code": "",         # creation code, if the server requires one for new projects
}


def save(d: Path, link: str, create_code: str = "") -> None:
    state_dir(d).mkdir(parents=True, exist_ok=True)
    os.chmod(d / STATE, 0o700)
    fd = os.open(config_path(d), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # holds the project keys
    with os.fdopen(fd, "w") as f:
        json.dump({"link": link, **DEFAULT_CONFIG, "create_code": create_code}, f, indent=2)
    (d / STATE / ".gitignore").write_text("*\n")


def adopt(d: Path) -> bool:
    """A folder another computer syncs, shared with this one through Dropbox or the like (so its
    `.texloom` has that computer's records, not this one's): set it up here with the same project
    link. It starts out known to be shared (sharedfolder.py), so this computer stands by until it has
    the project and has heard from the others. False if no other computer's records are there."""
    for other in sorted((d / STATE / "computers").glob("*/config.json")):
        try:
            cfg = json.loads(other.read_text())
            Keys.from_link(cfg["link"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        save(d, cfg["link"], str(cfg.get("create_code") or ""))
        (state_dir(d) / "shared.json").write_text(json.dumps({"at": time.time()}))
        return True
    return False


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
    cfg = {**DEFAULT_CONFIG, **load_config(d)}
    keys, server = load(d)
    code = cfg["create_code"]  # never the environment's: a project's link decides which server hears it
    replica = Replica(state_dir(d), keys, server, code or None)
    import platform
    opts = {"engine": cfg["engine"], "latexmk_args": cfg["latexmk_args"],
            "shell_escape": cfg["shell_escape"] or shell_escape,
            "confine": cfg["confine"] is not False,
            "label": cfg["label"] or f"latexmk on {platform.node().split('.')[0]}"}
    fs = FolderSync(d, replica, compile_ or cfg["compile"], opts)
    from .listsync import my_name
    name = cfg.get("name") or my_name()[0] or f"folder copy on {platform.node().split('.')[0]}"
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
        cid = str(replica.doc.client_id)
        if replica.can_write and replica.clients.get(cid) != name:  # for "who wrote this"
            replica.local_change(lambda: replica.clients.__setitem__(cid, name))
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
        merges._save()
        replica.save()


def comments_command(a: argparse.Namespace) -> None:
    """`texloom comments DIR` lists from this computer's copy of the project (as up to date as its
    last sync); `reply` and `resolve` connect with a copy of their own, as `texloom mcp` does, so
    they work while the app or `texloom run` is syncing the folder."""
    from .comments import read, reply, resolve
    from .listsync import my_name
    act = a.args[0] if a.args[0] in ("reply", "resolve") else "list"
    rest = a.args[1:] if act != "list" else a.args
    if not rest or (act == "reply" and len(rest) < 3) or (act == "resolve" and len(rest) != 2):
        sys.exit("usage: texloom comments DIR | texloom comments reply DIR ID TEXT | texloom comments resolve DIR ID")
    d = Path(rest[0])
    keys, server = load(d)
    cfg = {**DEFAULT_CONFIG, **load_config(d)}
    name = cfg.get("name") or my_name()[0] or getpass.getuser()
    if act == "list":
        r = Replica(state_dir(d), keys, server)
        items = [c for c in read(r, a.all) if not a.mine or c.mentions(name)]
        if not items:
            print("Nothing open." if not a.mine else f"Nothing mentions @{name}.")
        for c in items:
            where = f"{c.file}:{c.line}" if c.line else f"{c.file} (text changed)"
            state = " [resolved]" if c.resolved else ""
            print(f"{where}: {c.kind} by {c.by}{' ✓' if c.verified else ''}{state}  (id {c.id})")
            body = c.text if c.kind == "comment" else (f"replace {c.quote!r} with {c.text!r}" if c.quote else f"insert {c.text!r}")
            print("    " + body.replace("\n", "\n    "))
            for rp in c.replies:
                print(f"    ↳ {rp.by}{' ✓' if rp.verified else ''}: {rp.text}")
        return
    if not keys.writable:
        sys.exit("this folder has a read-only link: it can't reply or resolve")

    async def change() -> None:
        state = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "texloom" / "comments" / keys.project
        r = Replica(state, keys, server, cfg["create_code"] or None)
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
        from .mcp import serve
        if "#" in a.target:
            keys, server = Keys.from_link(a.target)
            code = ""
        else:
            d = Path(a.target)
            keys, server = load(d)
            code = load_config(d).get("create_code", "")
        try:
            asyncio.run(serve(keys, server, a.name[:64], code or None, a.suggest_only))
        except KeyboardInterrupt:
            pass
    elif a.cmd == "lsp":
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
