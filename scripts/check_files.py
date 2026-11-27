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
- in a folder, files written while others in the same project's folder are erased all land.

The database is a throwaway one (scripts/testdb.py: SQLite, or PostgreSQL).

    uv run --extra server python scripts/check_files.py            # folder, and Garage if installed
    uv run --extra server python scripts/check_files.py folder     # just one
    TEXLOOM_S3_ENDPOINT=… TEXLOOM_S3_KEY_ID=… TEXLOOM_S3_SECRET=… TEXLOOM_S3_REGION=… \\
      uv run --extra server python scripts/check_files.py s3://bucket/prefix   # a store of your own (it writes test files there)
"""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from checklib import check, free_port, summary  # noqa: E402
import testdb  # noqa: E402

from server.files import FolderFiles, S3Files, open_files  # noqa: E402
from server.store import Store  # noqa: E402


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


async def cases(make_files, tmp: Path, db: str) -> None:
    files = make_files()
    store = await Store.open(db, files, tmp)
    await store.start()

    p, a, b = project(), bid(), bid()
    data = secrets.token_bytes(700_000)
    stored = await store.put_blob(p, a, data, "file")
    check("a file is stored and counted", stored and await store.blob_size(p, a) == len(data) and store.usage[p] == len(data))
    check("it reads back whole (in pieces)", await read(store, p, a) == data)
    check("the same content again is stored once", not await store.put_blob(p, a, data, "file") and store.usage[p] == len(data))
    await store.put_blob(p, b, b"pdf" * 1000, "output")
    check("a project file can't be erased as output", not await store.delete_blob(p, a, any_kind=False))
    check("output is erased by any writer", await store.delete_blob(p, b, any_kind=False) and await store.blob_size(p, b) is None)
    check("and stops counting at once", store.usage[p] == len(data))
    check("the owner erases a project file", await store.delete_blob(p, a, any_kind=True) and store.usage[p] == 0)
    await store._reaper
    check("their content is erased from the store", await gone(files, f"{p}/{a}") and await gone(files, f"{p}/{b}"))

    # an upload counts while under way; two of the same at once are stored once
    q, c = project(), bid()
    big = secrets.token_bytes(3_000_000)
    t1 = asyncio.ensure_future(store.put_blob(q, c, big))
    t2 = asyncio.ensure_future(store.put_blob(q, c, big))
    await asyncio.sleep(0)
    check("uploads count from the moment they're accepted", store.usage[q] == 2 * len(big))
    r = await asyncio.gather(t1, t2)
    check("two uploads of the same content: stored once, counted once", sorted(r) == [False, True] and store.usage[q] == len(big))

    # a dropped project: erased in the background; a re-upload of content in the trash keeps it
    ids = [bid() for _ in range(5)]
    for i in ids:
        await store.put_blob(q, i, secrets.token_bytes(10_000))
    async with store.locked(q) as tx:
        await store._trash_files(q, tx)
    check("a dropped project's files stop counting at once", store.usage[q] == 0 and all([await store.blob_size(q, i) is None for i in ids + [c]]))
    keep = ids[0]
    again = await store.put_blob(q, keep, b"new copy of the same content")
    await store._reaper
    check("content uploaded again while in the trash is kept", again and await read(store, q, keep) == b"new copy of the same content")
    check("the rest is erased", all([await gone(files, f"{q}/{i}") for i in ids[1:] + [c]]))
    check("and the trash is empty", await store.db.fetchval("SELECT COUNT(*) FROM trash") == 0)
    await store.close()

    # a stop part way: what's left in the trash is erased on the next start
    store = await Store.open(db, make_files(), tmp)
    await store.start()
    r_, ids = project(), [bid() for _ in range(3)]
    for i in ids:
        await store.put_blob(r_, i, b"x" * 100)
    store._reaper = asyncio.get_running_loop().create_future()  # nothing erases: as if it stopped first
    async with store.locked(r_) as tx:
        await store._trash_files(r_, tx)
    store._reaper.set_result(None)
    await store.close()
    store = await Store.open(db, make_files(), tmp)
    await store.start()
    await store._reaper
    check("after a restart, the trash is emptied", all([await gone(store.files, f"{r_}/{i}") for i in ids]))

    # a backup copy in a folder, and a restore from it after the store loses files
    b_, ids = project(), [bid() for _ in range(4)]
    contents = {i: secrets.token_bytes(5000 + n) for n, i in enumerate(ids)}
    for i, d in contents.items():
        await store.put_blob(b_, i, d)
    copy = tmp / "backup-copy"
    copied, _ = await store.files_to_folder(copy)
    check("a backup copy in a folder holds every stored file",
          copied >= len(ids) and all((copy / b_ / i).read_bytes() == d for i, d in contents.items()))
    again, _ = await store.files_to_folder(copy)
    check("a second copy copies nothing new", again == 0)
    await store.delete_blob(b_, ids[0], any_kind=True)
    await store._reaper
    _, removed = await store.files_to_folder(copy)
    check("a file erased since is removed from the copy", removed == 1 and not (copy / b_ / ids[0]).exists())
    for i in ids[1:3]:
        await store.files.delete(f"{b_}/{i}")  # the store loses two files
    put_back, missing = await store.files_from_folder(copy)
    check("a restore puts back what the store lost, and only that",
          put_back == 2 and missing == 0 and all([await read(store, b_, i) == contents[i] for i in ids[1:]]))
    await store.close()


async def folder_race(root: Path) -> None:
    """Files written into a project's folder while others in it are erased, which removes the
    folder once it's empty: no write may land in a folder that's going away."""
    f = FolderFiles(root)
    await f.check()
    p = project()

    async def churn():
        for _ in range(1500):
            k = f"{p}/{bid()}"
            await f.put(k, b"x")
            await f.delete(k)
    try:
        await asyncio.gather(*(churn() for _ in range(8)))
        check("writes racing erases in the same folder: none lost", True)
    except OSError as e:
        check(f"writes racing erases in the same folder: {e!r}", False)


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
    run = lambda *a: subprocess.run(["garage", "-c", str(conf), *a], capture_output=True, text=True, check=True).stdout
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
        check("a wrong secret is refused at the start", False)
    except Exception as e:  # noqa: BLE001
        check("a wrong secret is refused at the start, saying why", "can't use the bucket" in str(e))
    finally:
        await f.close()


def main() -> None:
    wanted = sys.argv[1:] or ["folder", "garage"]
    for which in wanted:
        with tempfile.TemporaryDirectory(prefix="texloom-files-") as d:
            tmp = Path(d)
            if which == "folder":
                print("folder:")
                with testdb.running() as db:
                    asyncio.run(cases(lambda tmp=tmp: FolderFiles(tmp / "files"), tmp, db))
                asyncio.run(folder_race(tmp / "race"))
            elif which == "garage":
                if not shutil.which("garage"):
                    print("garage: not installed (brew install garage), skipped")
                    continue
                print("garage (S3):")
                (tmp / "garage").mkdir()
                with garage(tmp / "garage") as env:
                    asyncio.run(wrong_secret(env))
                    s3 = {"endpoint": env["TEXLOOM_S3_ENDPOINT"], "region": env["TEXLOOM_S3_REGION"],
                          "key_id": env["TEXLOOM_S3_KEY_ID"], "secret": env["TEXLOOM_S3_SECRET"]}
                    with testdb.running() as db:
                        asyncio.run(cases(lambda s3=s3: open_files("s3://texloom/check", Path(), s3), tmp, db))
            elif which.startswith("s3://"):
                print(f"{which}:")
                prefix = f"{which.rstrip('/')}/texloom-check-{secrets.token_hex(4)}"
                env = os.environ.get
                s3 = {"endpoint": env("TEXLOOM_S3_ENDPOINT", ""), "region": env("TEXLOOM_S3_REGION", ""),
                      "key_id": env("TEXLOOM_S3_KEY_ID", ""), "secret": env("TEXLOOM_S3_SECRET", "")}
                with testdb.running() as db:
                    asyncio.run(cases(lambda prefix=prefix, s3=s3: open_files(prefix, Path(), s3), tmp, db))
            else:
                sys.exit(f"unknown store {which!r}: folder, garage or s3://bucket/prefix")
    sys.exit(summary("file store"))


if __name__ == "__main__":
    main()
