"""TeXLoom relay server: the command line.

Stores and relays opaque, end-to-end encrypted Yjs updates and file blobs. It never
sees keys or plaintext, and verifies Ed25519 signatures so read-only link holders
can't write.

Storage. The server can't see what it stores and making a project needs no account,
so storage is governed by *allowances*:

  --codes-file FILE   one rule per line:   <code|default>  <allowance>  [until=YYYY-MM-DD]  [label]
                      (repeatable, or listed in TEXLOOM_CODES_FILES: codes are combined)
                      allowance: 0 (store nothing), a size (500M, 5G) or "unlimited".
                      A code's allowance is the total stored across every project
                      created with it; "default" applies to projects created without
                      a code (default 0 = such projects aren't stored on the server).
                      until= is the last day (UTC) a code is valid; after it, the code
                      counts as removed (its projects fall back to "default").
                      The file is re-read whenever it changes.
  --purge-expired-after DAYS
                      delete the stored copies of projects whose code expired more
                      than DAYS days ago (default: never)
  --max-bytes         total the server stores                (default unlimited)
  --min-free          keep this much disk free                (default 2G)
  --project-max-bytes cap per project, except for unlimited allowances (default 200M)

Without --codes-file every project may be stored (subject to the limits above). A
refused write is still relayed live to collaborators online but not acknowledged, so
the sender keeps it and retries later: nobody loses work.

Run:              texloom-server --port 8787 --data data --codes-file data/codes.txt
Add a code:       texloom-server --codes-file data/codes.txt --add-code alice --allowance 2G [--months 12]
Renew a code:     texloom-server --codes-file data/codes.txt --renew-code CODE --months 12 [--allowance 10G]
List codes/usage: texloom-server --codes-file data/codes.txt --list-codes --data data
Purge one code:   texloom-server --codes-file data/codes.txt --purge-code CODE --data data
Move to Postgres: texloom-server --database postgresql:///texloom --copy-database-from sqlite:///…/texloom.db
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from pathlib import Path

from aiohttp import web

from .bus import open_bus
from .files import Files, FilesError, open_files
from .policy import (
    MEMORY,
    UNLIMITED,
    CodeBook,
    GiB,
    Limits,
    MiB,
    Policy,
    add_months,
    fmt_size,
    parse_date,
    parse_size,
    today,
)
from .app import Settings, make_app
from .relay import LISTEN_BACKLOG, SHUTDOWN_WAIT
from .store import Store
from .wire import version_tuple

log = logging.getLogger("texloom.relay")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="TeXLoom relay server", epilog=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", type=Path, metavar="FILE",
                   help="settings file (TOML; default: TEXLOOM_CONFIG, else /etc/texloom/texloom.toml if it exists). "
                        "Every option below can be set there by name, and as TEXLOOM_<NAME> in the environment")
    p.add_argument("--host", default="127.0.0.1", help="interface to listen on (default: localhost only)")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--database", "--db", dest="database", default="", metavar="URL",
                   help="where edit logs, project records and project lists are kept: an SQLite file, which one "
                        "server uses (default: sqlite:DIR/texloom.db, in --data; or e.g. sqlite:///var/lib/texloom/texloom.db), "
                        "or a PostgreSQL database, which several can share: postgresql:///texloom (this machine, as "
                        "this user) or postgresql://texloom:PASSWORD@db.example.org/texloom")
    p.add_argument("--bus", default="", metavar="URL",
                   help="how several servers sharing a PostgreSQL database pass messages to each other: a Redis-compatible "
                        "server, redis://[USER:PASSWORD@]HOST[:PORT], rediss://… (TLS) or redis+sentinel://S1,S2,S3/NAME. "
                        "Unset (one server): in-process")
    p.add_argument("--copy-database-from", metavar="URL",
                   help="copy everything from this database (the one the server used until now) into --database, "
                        "which must be new, then exit. Stop the server first")
    p.add_argument("--data", type=Path, default=Path("data"), metavar="DIR",
                   help="this server's own folder: uploads are spooled here, --min-free is kept free here, and "
                        "stored files go in DIR/files unless --files says otherwise")
    p.add_argument("--files", default="", metavar="DIR or s3://BUCKET[/PREFIX]",
                   help="where stored files' content goes (default: DIR/files of --data)")
    p.add_argument("--s3-endpoint", default="", metavar="URL", help="for --files s3://…: the store's address")
    p.add_argument("--s3-region", default="auto", help="for --files s3://…: the store's region (Garage: garage; R2: auto)")
    p.add_argument("--s3-key-id", default="", help="for --files s3://…: the access key id")
    p.add_argument("--s3-secret", default="", help="for --files s3://…: the secret key (put it in the settings file)")
    p.add_argument("--static", type=Path, default=Path("web/dist"))
    p.add_argument("--tex-assets", type=Path, default=Path("tex-assets"),
                   help="directory containing busytex/ (in-browser TeX); optional")
    p.add_argument("--max-bytes", type=parse_size, default=UNLIMITED, help="total storage cap, e.g. 20G")
    p.add_argument("--min-free", type=parse_size, default=2 * GiB, help="keep this much disk free, e.g. 2G")
    p.add_argument("--project-max-bytes", type=parse_size, default=200 * MiB, help="per-project cap, e.g. 200M")
    p.add_argument("--memory", type=parse_size, default=MEMORY,
                   help="memory for big work at once (catch-ups, large messages, uploads, arXiv imports; default 512M); "
                        "the process needs about 500M more, so keep systemd's MemoryMax that far above it")
    p.add_argument("--codes-file", type=Path, action="append",
                   help="storage allowances per creation code (see above); repeatable. TEXLOOM_CODES_FILES adds "
                        "more (separated by ':'). --add-code writes to the first")
    p.add_argument("--add-code", metavar="LABEL", help="create a creation code with this label, print it, exit")
    p.add_argument("--allowance", help="allowance for --add-code (default 1G) or --renew-code (default: unchanged): "
                                       "0, a size, or unlimited")
    p.add_argument("--until", type=parse_date, metavar="YYYY-MM-DD",
                   help="for --add-code / --renew-code: the last day the code is valid")
    p.add_argument("--months", type=int, default=0,
                   help="for --add-code / --renew-code: valid for this many months (a renewal counts from the "
                        "later of today and the code's current expiry)")
    p.add_argument("--renew-code", metavar="CODE", help="change a code's expiry (--until/--months) and/or allowance, then exit")
    p.add_argument("--purge-code", metavar="CODE",
                   help="delete the stored copies of every project created with this code (expired or removed "
                        "codes only), then exit. Prefer --purge-expired-after, which does it inside the server")
    p.add_argument("--list-codes", action="store_true", help="list codes, allowances, expiry and usage, then exit")
    p.add_argument("--forget-codes-file", metavar="FILE",
                   help="with several servers sharing a database (each copies its codes files there, and takes every "
                        "server's): take a codes file no server uses any more out of it, so its codes stop counting "
                        "everywhere, then exit. (A server still using that file puts it back when the file next changes.)")
    p.add_argument("--files-for-backup", type=Path, metavar="DIR",
                   help="print the folder that holds the stored files, for a backup to include, then exit. With "
                        "a bucket, first make DIR a copy of it (copying only what's new), and print DIR")
    p.add_argument("--files-restore", type=Path, metavar="DIR",
                   help="put back into the file store every stored file it lacks, from DIR (a restored backup, "
                        "or the folder when moving to a bucket), then exit")
    p.add_argument("--failover", action="store_true",
                   help="take part in failover with a standby server (STANDBY.md): this server "
                        "holds a lease in the bucket while it is the primary, or waits as the standby and takes over "
                        "when it isn't renewed. Needs PostgreSQL (this server's own, the standby's a replica) and "
                        "--files s3://… with a store that does conditional writes. Off (the default): nothing of this runs")
    p.add_argument("--failover-name", default="", metavar="NAME",
                   help="this server's name in the lease; each of the two needs its own (default: its host name)")
    p.add_argument("--failover-ttl", type=float, default=15, metavar="SECONDS",
                   help="how long a primary may go without renewing its lease, on its own clock, before it stops (default "
                        "15; the standby waits twice as long, plus a margin, before it takes over)")
    p.add_argument("--failover-check", action="store_true",
                   help="check that the --files bucket does conditional writes properly (a failover needs them), then exit")
    p.add_argument("--purge-expired-after", type=float, metavar="DAYS", default=None,
                   help="delete the stored copies of projects whose creation code expired more than DAYS days "
                        "ago (checked hourly; default: never). Their files stay on people's devices")
    p.add_argument("--private", action="store_true",
                   help="only projects created with a valid creation code may use this server, even without "
                        "storage; people with such a project's link need nothing else (needs --codes-file)")
    p.add_argument("--min-version", default="", metavar="X.Y.Z",
                   help="the oldest TeXLoom app allowed to connect: older ones, and those too old to say, are asked "
                        "to update. The admin page shows which versions connect")
    p.add_argument("--codes-url", default="", metavar="URL",
                   help="a web page saying how to get a creation code for this server; the apps link to it where "
                        "they ask for a code (default: none, and they say to ask whoever runs it)")
    p.add_argument("--dropbox-app-key", default="", metavar="KEY",
                   help="the app key of a Dropbox app made for this server (Full Dropbox access; redirect URI "
                        "https://<this server>/dropbox-auth.html): lets the web app keep projects in step with Dropbox "
                        "folders. The key is public; the app secret isn't needed (default: no Dropbox)")
    p.add_argument("--no-project-lists", action="store_true",
                   help="don't store people's encrypted project lists (the sync link and passkey features)")
    p.add_argument("--no-arxiv", action="store_true", help="disable importing arXiv sources through this server")
    p.add_argument("--evict-idle-days", type=float, default=0,
                   help="drop the stored copy of projects nobody opened for this many days (0 = never); their "
                        "writers re-upload them on their next visit")
    p.add_argument("--admin-token", default="", help="enable /admin (read-only usage page) with this password "
                                                     "(at least 16 characters; put it in the settings file)")
    p.add_argument("--admin-token-file", type=Path, help="as --admin-token, read from this file")
    p.add_argument("--texlive", type=Path, metavar="DIR",
                   help="serve TeX Live files on demand to the in-browser engine, cached in DIR")
    p.add_argument("--texlive-init", action="store_true", help="download/refresh TeX Live's package index into --texlive, then exit")
    p.add_argument("--texlive-max", type=parse_size, default=4 * GiB, help="disk the TeX Live cache may use (default 4G)")
    p.add_argument("--texlive-mirror", action="append", default=[], metavar="URL",
                   help="TeX Live mirror to use first (…/systems/texlive/tlnet/), e.g. one in your country; "
                        "repeatable. Without it, mirror.ctan.org picks one near the server")
    return p


async def _failover_check(files: Files, check) -> None:
    """--failover-check, and the same before a server fails over."""
    try:
        await check(files)
    finally:
        await files.close()


async def _files_command(a: argparse.Namespace, files: Files) -> None:
    """--files-for-backup and --files-restore."""
    from .files import FolderFiles
    store = await Store.open(a.database, files, a.data)
    try:
        await files.check()
        if a.files_for_backup is not None:
            if isinstance(files, FolderFiles):
                print(files.root)  # back it up as it is
                return
            copied, removed = await store.files_to_folder(a.files_for_backup)
            print(a.files_for_backup.resolve())
            print(f"{copied} file(s) copied from {files.name}, {removed} removed", file=sys.stderr)
        else:
            copied, missing = await store.files_from_folder(a.files_restore)
            print(f"{copied} file(s) put back into {files.name}" + (f"; {missing} not in {a.files_restore}" if missing else ""))
            if missing:
                sys.exit(1)
    finally:
        await store.close()


async def _forget_codes(a: argparse.Namespace, files: Files) -> None:
    store = await Store.open(a.database, files, a.data)
    try:
        sources = {r[0] for r in await store.all_codes()}
        if a.forget_codes_file not in sources:
            sys.exit(f"no codes file named {a.forget_codes_file} in the database (it has: {', '.join(sorted(sources)) or 'none'})")
        await store.put_codes(a.forget_codes_file, [])
        print(f"{a.forget_codes_file}: its codes no longer count on any server (within seconds)")
    finally:
        await store.close()


async def _codes_command(a: argparse.Namespace, codes: CodeBook, files: Files) -> None:
    """--list-codes and --purge-code: they read (and purge) the store."""
    store = await Store.open(a.database, files, a.data)
    codes.set_shared(await store.all_codes())  # (other servers' codes files too)
    try:
        if a.purge_code is not None:
            c = CodeBook.normalize(a.purge_code)
            if c in codes.codes and not codes.expired(c):
                sys.exit(f"{a.purge_code} is still valid{f' (until {codes.until[c]})' if c in codes.until else ''}: "
                         "let it expire, or delete its line, first")
            n, freed, _ = await Policy(store, codes).purge(c)
            await store.empty_trash()
            print(f"purged {n} project(s), {fmt_size(freed)} freed. If the server is running, restart it so its "
                  "storage totals are up to date.")
            return

        def expiry(c: str) -> str:
            if c not in codes.until:
                return ""
            days = codes.lapsed_since(c)
            return f"  EXPIRED {codes.until[c]} ({days} day{'s' * (days != 1)} ago)" if days is not None else f"  until {codes.until[c]}"
        print(f"{'default':16}  {fmt_size(codes.default):>9}  used {fmt_size(store.owner_usage('default')):>7}  "
              f"{sum(1 for o in store.owners.values() if o == 'default')} project(s)")
        for c, (allowance, label) in codes.codes.items():
            n = sum(1 for o in store.owners.values() if o == c)
            print(f"{codes.display(c)}  {fmt_size(allowance):>9}  used {fmt_size(store.owner_usage(c)):>7}  "
                  f"{n} project(s){expiry(c)}  {label}")
        removed = sorted({o for o in store.owners.values() if o not in codes.codes and o != "default"})
        for c in removed:
            if store.owner_usage(c):
                print(f"{codes.display(c)}  {'removed':>9}  used {fmt_size(store.owner_usage(c)):>7}  "
                      f"{len(store.stored_by(c))} stored project(s)  (--purge-code to delete them)")
    finally:
        await store.close()


async def _copy_command(a: argparse.Namespace) -> None:
    """--copy-database-from."""
    from .db import redact
    from .store import copy_database
    print(f"copying {redact(a.copy_database_from)} into {redact(a.database)}", file=sys.stderr)
    counts = await copy_database(a.copy_database_from, a.database,
                                 lambda table, n: print(f"  {table}: {n} rows…", end="\r", file=sys.stderr))
    for table, n in counts.items():
        print(f"  {table}: {n} row(s)" + " " * 10)
    print("done: point the server at the new database (database = … in its settings), then start it")


def main() -> None:
    from .config import parse
    p = parser()
    a = parse(p)
    if not a.database:  # an SQLite file in the server's own folder
        a.database = f"sqlite:{(a.data / 'texloom.db').resolve()}"
    if a.copy_database_from:
        from .db import DatabaseError
        try:
            asyncio.run(_copy_command(a))
        except DatabaseError as e:
            sys.exit(str(e))
        return
    try:
        files = open_files(a.files, a.data / "files",
                           {"endpoint": a.s3_endpoint, "region": a.s3_region, "key_id": a.s3_key_id, "secret": a.s3_secret})
    except FilesError as e:
        p.error(str(e))
    if a.texlive_init:
        if not a.texlive:
            sys.exit("--texlive-init needs --texlive DIR")
        from .texlive import TexLive
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        tl = TexLive(a.texlive, extra_mirrors=a.texlive_mirror)
        asyncio.run(tl.build_index(tl.index_sources() if tl.ready or tl.extra_mirrors else None))
        return
    if a.files_for_backup is not None or a.files_restore is not None:
        asyncio.run(_files_command(a, files))
        return
    if a.failover_check:
        from .failover import check_conditional
        try:
            asyncio.run(_failover_check(files, check_conditional))
        except FilesError as e:
            sys.exit(str(e))
        print(f"{files.name} does conditional writes properly: it can hold a failover lease")
        return
    codes = CodeBook(a.codes_file or None)
    if a.until and a.months:
        sys.exit("give --until or --months, not both")
    if a.forget_codes_file:
        asyncio.run(_forget_codes(a, files))
        return
    if any(x is not None for x in (a.add_code, a.renew_code, a.purge_code)) or a.list_codes:
        if not a.codes_file:
            sys.exit("--codes-file is required")
        if a.add_code is not None:
            until = add_months(today(), a.months) if a.months else a.until
            print(codes.add(a.add_code, a.allowance or "1G", until))
            return
        if a.renew_code is not None:
            if not (a.until or a.months or a.allowance):
                sys.exit("--renew-code needs --until, --months and/or --allowance")
            try:
                label, until, size = codes.renew(a.renew_code, a.until, a.months, a.allowance)
            except (KeyError, FileNotFoundError):
                sys.exit(f"no such code in {', '.join(map(str, a.codes_file))}: {a.renew_code}")
            print(f"{label or a.renew_code}: {size}, {f'valid until {until}' if until else 'no expiry'}")
            return
        codes.load()
        asyncio.run(_codes_command(a, codes, files))
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    admin_token = (a.admin_token_file.read_text().strip() if a.admin_token_file else a.admin_token.strip())
    if admin_token and len(admin_token) < 16:
        sys.exit("the admin token must be at least 16 characters")
    if a.private and not codes.path:
        sys.exit("--private needs --codes-file: it's the creation codes that decide who may use the server")
    limits = Limits(a.max_bytes, a.min_free, a.project_max_bytes, a.memory)
    if a.config:
        log.info("settings from %s", a.config)
    if a.private:
        log.info("private server: only projects with a valid creation code may connect")
    log.info("limits: total %s, keep %s free, %s per project, %s of memory for big work; %s",
             fmt_size(limits.max_bytes), fmt_size(limits.min_free), fmt_size(limits.project_max), fmt_size(limits.memory),
             f"allowances from {', '.join(map(str, a.codes_file))}" if codes.path else "no codes file: every project may be stored")
    if a.min_version and not version_tuple(a.min_version):
        p.error("--min-version is a version such as 0.4.30")
    if a.codes_url and not re.match(r"https?://", a.codes_url, re.I):
        p.error("--codes-url must be an http:// or https:// address")
    if a.dropbox_app_key and not re.fullmatch(r"[a-z0-9]{8,32}", a.dropbox_app_key):
        p.error("--dropbox-app-key is the app key from the Dropbox App Console (letters and digits), not the secret")
    try:
        open_bus(a.bus)  # (only to check the address: the server opens it)
    except ValueError as e:
        p.error(str(e))
    failover = None
    if a.failover:
        from .db import sqlite_path
        from .failover import FailoverSettings, check_conditional
        if sqlite_path(a.database) is not None:
            p.error("--failover needs PostgreSQL: a standby follows the primary's PostgreSQL (SQLite can't have one)")
        if not a.files.startswith("s3://"):
            p.error("--failover needs --files s3://… : the lease lives in the bucket the two servers share")
        if a.failover_ttl < 2:
            p.error("--failover-ttl is at least 2 seconds")
        try:
            asyncio.run(_failover_check(files, check_conditional))
        except FilesError as e:
            p.error(str(e))
        failover = FailoverSettings(name=a.failover_name, ttl=a.failover_ttl, database=a.database)
    settings = Settings(
        a.database, bus=a.bus, static=a.static, data=a.data, files=files, tex=a.tex_assets, limits=limits, codes=codes,
        private=a.private, min_version=a.min_version, codes_url=a.codes_url, dropbox_app_key=a.dropbox_app_key,
        lists=not a.no_project_lists, arxiv=not a.no_arxiv, admin_token=admin_token, evict_days=a.evict_idle_days,
        purge_days=a.purge_expired_after, texlive=a.texlive, texlive_max=a.texlive_max, texlive_mirrors=a.texlive_mirror, failover=failover)
    web.run_app(make_app(settings), host=a.host, port=a.port, backlog=LISTEN_BACKLOG, shutdown_timeout=SHUTDOWN_WAIT)


if __name__ == "__main__":
    main()
