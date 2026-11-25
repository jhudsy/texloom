#!/usr/bin/env python3
"""A PostgreSQL of its own for checks and development, so nothing needs installing as a service.

It runs from a folder (a throwaway one, or one you keep), listens only on 127.0.0.1 on a free
port, and lets this user in without a password. It needs PostgreSQL's programs (initdb, pg_ctl):
on PATH, in PGBIN, or where Homebrew, Debian/Ubuntu or the Windows installer put them.

    uv run python scripts/pg.py data/pg -- uv run texloom-server --data data
        # keep a database in data/pg, start it, run the command with TEXLOOM_DATABASE set, stop it
    uv run python scripts/pg.py data/pg
        # just run it (Ctrl+C stops it), printing the address to use

From Python (the checks): `with pg.running() as url: …` starts a throwaway one.
"""

from __future__ import annotations

import contextlib
import glob
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

USER = "texloom"


def bindir() -> Path:
    """Where PostgreSQL's programs are."""
    if found := shutil.which("initdb"):
        return Path(found).parent
    candidates = [os.environ.get("PGBIN", "")]
    candidates += sorted(glob.glob("/opt/homebrew/opt/postgresql@*/bin") + glob.glob("/usr/local/opt/postgresql@*/bin"), reverse=True)
    candidates += sorted(glob.glob("/usr/lib/postgresql/*/bin"), key=lambda p: int(Path(p).parent.name), reverse=True)
    candidates += sorted(glob.glob(r"C:\Program Files\PostgreSQL\*\bin"), reverse=True)
    for c in candidates:
        if c and (Path(c) / ("initdb.exe" if os.name == "nt" else "initdb")).exists():
            return Path(c)
    sys.exit("PostgreSQL's programs (initdb, pg_ctl) aren't installed: brew install postgresql@17, "
             "apt install postgresql, or set PGBIN")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run(*args: str | Path, output: Path | None = None) -> None:
    """Run one of PostgreSQL's programs; its error output is in the exception's stderr, or, with
    `output`, in that file. (`pg_ctl start` must write to a file: the server it leaves running
    inherits its output, and on Windows a pipe would then never close, so waiting for it hangs.)"""
    # a locale it can use: on macOS, PostgreSQL won't start without one (and "C" is the same everywhere)
    env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
    if output is None:
        subprocess.run([str(a) for a in args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=env)
        return
    with open(output, "w") as out:
        subprocess.run([str(a) for a in args], check=True, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env)


def start(folder: Path) -> tuple[str, int]:
    """Start (creating it if new) the database kept in `folder`. Returns its address and port."""
    bin_ = bindir()
    folder = folder.resolve()
    if not (folder / "PG_VERSION").exists():
        # initdb makes the folder itself: on Windows it can't set the permissions of one made by us
        folder.parent.mkdir(parents=True, exist_ok=True)
        if folder.exists() and not any(folder.iterdir()):
            folder.rmdir()
        try:
            _run(bin_ / "initdb", "-D", folder, "-U", USER, "--auth=trust", "-E", "UTF8", "--no-locale")
        except subprocess.CalledProcessError as e:
            sys.exit(f"initdb failed: {e.stderr}")
    port = free_port()
    opts = f"-p {port} -c listen_addresses=127.0.0.1 -c unix_socket_directories= -c fsync=off -c synchronous_commit=off"
    said = folder.parent / f"{folder.name}-pg_ctl.txt"
    try:
        _run(bin_ / "pg_ctl", "-D", folder, "-o", opts, "-l", folder / "server.log", "-w", "-t", "60", "start", output=said)
    except subprocess.CalledProcessError:
        log = (folder / "server.log").read_text(errors="replace")[-2000:] if (folder / "server.log").exists() else ""
        sys.exit(f"PostgreSQL didn't start: {said.read_text(errors='replace')}\n{log}")
    return f"postgresql://{USER}@127.0.0.1:{port}/postgres", port


def stop(folder: Path, mode: str = "fast") -> None:
    """Stop it; `mode` "immediate" is a crash (nothing flushed, as when the machine is lost)."""
    folder = folder.resolve()
    with contextlib.suppress(subprocess.CalledProcessError):
        _run(bindir() / "pg_ctl", "-D", folder, "-m", mode, "-w", "stop", output=folder.parent / f"{folder.name}-pg_ctl.txt")


def standby_of(folder: Path, primary_port: int) -> None:
    """Make `folder` a standby of the (running) database on `primary_port`, as INSTALL.md's "A
    standby server" does: a base backup streamed from it, with the connection settings written
    (-R) and standby.signal. Start it with start(folder) afterwards."""
    folder = folder.resolve()
    folder.parent.mkdir(parents=True, exist_ok=True)
    try:
        _run(bindir() / "pg_basebackup", "-h", "127.0.0.1", "-p", primary_port, "-U", USER, "-D", folder,
             "-R", "-X", "stream", "--checkpoint=fast")
    except subprocess.CalledProcessError as e:
        sys.exit(f"pg_basebackup failed: {e.stderr}")


def promote(folder: Path) -> None:
    """Promote the standby in `folder` to a primary (it must be running), and wait for it."""
    _run(bindir() / "pg_ctl", "-D", folder.resolve(), "-w", "-t", "60", "promote",
         output=folder.resolve().parent / f"{folder.name}-pg_ctl.txt")


@contextlib.contextmanager
def running(folder: Path | None = None):
    """A running database (throwaway unless `folder` is given); yields its address."""
    tmp = None
    if folder is None:
        tmp = tempfile.mkdtemp(prefix="texloom-pg-")
        folder = Path(tmp) / "pg"
    url, _ = start(folder)
    try:
        yield url
    finally:
        stop(folder)
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return
    folder = Path(args[0])
    command = args[args.index("--") + 1:] if "--" in args else []
    with running(folder) as url:
        if command:
            env = {**os.environ, "TEXLOOM_DATABASE": url}
            proc = subprocess.Popen(command, env=env)
            try:
                sys.exit(proc.wait())
            except KeyboardInterrupt:
                proc.send_signal(signal.SIGINT)
                proc.wait()
        print(f"PostgreSQL running: TEXLOOM_DATABASE={url}  (Ctrl+C stops it)")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
