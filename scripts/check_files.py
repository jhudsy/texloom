#!/usr/bin/env python3
"""Checks the relay's file store (server/files.py) and how the relay's database uses it.

Each case runs against a folder and, if the `garage` program is installed (brew install garage),
against a throwaway Garage, a real S3-compatible store started for the purpose:

- a file is stored, counted, read back whole, and erased (only output by any writer; a project
  file only by the owner);
- a dropped project's files stop counting at once and are erased in the background, and a stop
  part way leaves the rest for the next start;
- uploading content that is waiting to be erased keeps it;
- an upload counts while it's under way, and the same content uploaded twice at once is stored once;
- a database from before the file store (content in a table `blob`) is moved out on start, and
  files of deleted projects aren't carried over.

    uv run python scripts/check_files.py            # folder, and Garage if installed
    uv run python scripts/check_files.py folder     # just one
    TEXLOOM_S3_ENDPOINT=… TEXLOOM_S3_KEY_ID=… TEXLOOM_S3_SECRET=… TEXLOOM_S3_REGION=… \\
      uv run python scripts/check_files.py s3://bucket/prefix   # a store of your own (it writes test files there)
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.files import FolderFiles, S3Files, open_files  # noqa: E402
from server.relay import Store  # noqa: E402

failures: list[str] = []


def ok(cond: bool, what: str) -> None:
    print(("  ✓ " if cond else "  ✗ ") + what)
    if not cond:
        failures.append(what)


def project() -> str:
    return secrets.token_urlsafe(32)[:43]


def bid() -> str:
    return secrets.token_hex(16)


async def read(store: Store, p: str, b: str) -> bytes | None:
    try:
        return b"".join([x async for x in store.read_blob(p, b)])
    except FileNotFoundError:
        return None


async def gone(files, key: str) -> bool:
    try:
        async for _ in files.get(key):
            return False
    except FileNotFoundError:
        return True
    return False


async def cases(make_files, tmp: Path) -> None:
    db = tmp / "texloom.db"
    files = make_files()
    store = Store(db, files)
    await store.start()

    p, a, b = project(), bid(), bid()
    data = secrets.token_bytes(700_000)
    stored = await store.put_blob(p, a, data, "file")
    ok(stored and store.blob_size(p, a) == len(data) and store.usage[p] == len(data), "a file is stored and counted")
    ok(await read(store, p, a) == data, "it reads back whole (in pieces)")
    ok(not await store.put_blob(p, a, data, "file") and store.usage[p] == len(data), "the same content again is stored once")
    await store.put_blob(p, b, b"pdf" * 1000, "output")
    ok(not store.delete_blob(p, a, any_kind=False), "a project file can't be erased as output")
    ok(store.delete_blob(p, b, any_kind=False) and store.blob_size(p, b) is None, "output is erased by any writer")
    ok(store.usage[p] == len(data), "and stops counting at once")
    ok(store.delete_blob(p, a, any_kind=True) and store.usage[p] == 0, "the owner erases a project file")
    await asyncio.sleep(0)
    await store._reaper
    ok(await gone(files, f"{p}/{a}") and await gone(files, f"{p}/{b}"), "their content is erased from the store")

    # an upload counts while under way; two of the same at once are stored once
    q, c = project(), bid()
    big = secrets.token_bytes(3_000_000)
    t1 = asyncio.ensure_future(store.put_blob(q, c, big))
    t2 = asyncio.ensure_future(store.put_blob(q, c, big))
    await asyncio.sleep(0)
    ok(store.usage[q] == 2 * len(big), "uploads count from the moment they're accepted")
    r = await asyncio.gather(t1, t2)
    ok(sorted(r) == [False, True] and store.usage[q] == len(big), "two uploads of the same content: stored once, counted once")

    # a dropped project: erased in the background; a re-upload of content in the trash keeps it
    ids = [bid() for _ in range(5)]
    for i in ids:
        await store.put_blob(q, i, secrets.token_bytes(10_000))
    store._drop_files(q)
    ok(store.usage[q] == 0 and all(store.blob_size(q, i) is None for i in ids + [c]), "a dropped project's files stop counting at once")
    keep = ids[0]
    again = await store.put_blob(q, keep, b"new copy of the same content")
    await store._reaper
    ok(again and await read(store, q, keep) == b"new copy of the same content", "content uploaded again while in the trash is kept")
    ok(all([await gone(files, f"{q}/{i}") for i in ids[1:] + [c]]), "the rest is erased")
    ok(store.db.execute("SELECT COUNT(*) FROM trash").fetchone()[0] == 0, "and the trash is empty")
    await store.files.close()
    store.db.close()

    # a stop part way: what's left in the trash is erased on the next start
    store = Store(db, make_files())
    await store.start()
    r_, ids = project(), [bid() for _ in range(3)]
    for i in ids:
        await store.put_blob(r_, i, b"x" * 100)
    store._reaper = asyncio.get_running_loop().create_future()  # nothing erases: as if it stopped first
    store._drop_files(r_)
    store._reaper.set_result(None)
    await store.files.close()
    store.db.close()
    store = Store(db, make_files())
    await store.start()
    await store._reaper
    ok(all([await gone(store.files, f"{r_}/{i}") for i in ids]), "after a restart, the trash is emptied")
    await store.files.close()
    store.db.close()

    # a database from before the file store
    old = tmp / "old.db"
    con = sqlite3.connect(old)
    con.executescript("""
        CREATE TABLE blob (project TEXT NOT NULL, id TEXT NOT NULL, data BLOB NOT NULL, kind TEXT NOT NULL DEFAULT 'file',
                           PRIMARY KEY (project, id));
        CREATE TABLE deleted (project TEXT PRIMARY KEY, at REAL NOT NULL, sig BLOB NOT NULL, ts INTEGER NOT NULL);""")
    live, dead = project(), project()
    contents = {bid(): secrets.token_bytes(n) for n in (10, 2_000_000, 50_000)}
    for i, d in contents.items():
        con.execute("INSERT INTO blob VALUES (?,?,?,?)", (live, i, d, "file"))
    con.execute("INSERT INTO blob VALUES (?,?,?,?)", (dead, bid(), b"secret", "file"))
    con.execute("INSERT INTO deleted VALUES (?,?,?,?)", (dead, time.time(), b"s", 1))
    con.commit()
    con.close()
    store = Store(old, make_files())
    before = store.usage[live]
    await store.start()
    ok(before == sum(map(len, contents.values())) == store.usage[live], "an old database's files keep counting the same")
    ok(all([await read(store, live, i) == d for i, d in contents.items()]), "its files are moved to the store, whole")
    ok(store.db.execute("SELECT 1 FROM sqlite_master WHERE name='blob'").fetchone() is None, "and the old table is gone")
    ok(not store.db.execute("SELECT 1 FROM stored WHERE project=?", (dead,)).fetchone(), "a deleted project's files aren't carried over")
    await store.files.close()
    store.db.close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def garage(tmp: Path):
    """A throwaway single-node Garage with a bucket and a key; yields the environment to reach it."""
    s3, rpc, admin = free_port(), free_port(), free_port()
    conf = tmp / "garage.toml"
    conf.write_text(f"""
metadata_dir = "{tmp}/meta"
data_dir = "{tmp}/data"
db_engine = "sqlite"
replication_factor = 1
rpc_bind_addr = "127.0.0.1:{rpc}"
rpc_public_addr = "127.0.0.1:{rpc}"
rpc_secret = "{secrets.token_hex(32)}"
[s3_api]
s3_region = "garage"
api_bind_addr = "127.0.0.1:{s3}"
[admin]
api_bind_addr = "127.0.0.1:{admin}"
""")
    run = lambda *a: subprocess.run(["garage", "-c", str(conf), *a], capture_output=True, text=True, check=True).stdout  # noqa: E731
    proc = subprocess.Popen(["garage", "-c", str(conf), "server"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                node = run("node", "id", "-q").strip().split("@")[0]
                break
            except subprocess.CalledProcessError:
                time.sleep(0.1)
        else:
            raise RuntimeError("garage didn't start")
        run("layout", "assign", "-z", "dc1", "-c", "1G", node)
        run("layout", "apply", "--version", "1")
        run("bucket", "create", "texloom")
        out = run("key", "create", "texloom-check")
        key_id = next(ln.split(":", 1)[1].strip() for ln in out.splitlines() if ln.strip().lower().startswith("key id"))
        secret = next(ln.split(":", 1)[1].strip() for ln in out.splitlines() if ln.strip().lower().startswith("secret key"))
        run("bucket", "allow", "--read", "--write", "--owner", "texloom", "--key", "texloom-check")
        yield {"TEXLOOM_S3_ENDPOINT": f"http://127.0.0.1:{s3}", "TEXLOOM_S3_REGION": "garage",
               "TEXLOOM_S3_KEY_ID": key_id, "TEXLOOM_S3_SECRET": secret}
    finally:
        proc.terminate()
        proc.wait(10)


async def wrong_secret(env: dict[str, str]) -> None:
    f = S3Files(env["TEXLOOM_S3_ENDPOINT"], "texloom", "", env["TEXLOOM_S3_REGION"], env["TEXLOOM_S3_KEY_ID"], "wrong")
    try:
        await f.check()
        ok(False, "a wrong secret is refused at the start")
    except Exception as e:  # noqa: BLE001
        ok("can't use the bucket" in str(e), "a wrong secret is refused at the start, saying why")
    finally:
        await f.close()


def main() -> None:
    wanted = sys.argv[1:] or ["folder", "garage"]
    for which in wanted:
        with tempfile.TemporaryDirectory(prefix="texloom-files-") as d:
            tmp = Path(d)
            if which == "folder":
                print("folder:")
                asyncio.run(cases(lambda: FolderFiles(tmp / "files"), tmp))
            elif which == "garage":
                if not shutil.which("garage"):
                    print("garage: not installed (brew install garage), skipped")
                    continue
                print("garage (S3):")
                (tmp / "garage").mkdir()
                with garage(tmp / "garage") as env:
                    os.environ.update(env)
                    asyncio.run(wrong_secret(env))
                    asyncio.run(cases(lambda: open_files("s3://texloom/check", Path()), tmp))
            elif which.startswith("s3://"):
                print(f"{which}:")
                prefix = f"{which.rstrip('/')}/texloom-check-{secrets.token_hex(4)}"
                asyncio.run(cases(lambda: open_files(prefix, Path()), tmp))
            else:
                sys.exit(f"unknown store {which!r}: folder, garage or s3://bucket/prefix")
    print(json.dumps({"failures": failures}) if failures else "file store: all checks passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
