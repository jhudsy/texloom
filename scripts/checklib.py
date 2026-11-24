"""What the checks against a real server share: ✓ and ✗ lines, waiting for something to happen,
running `texloom`, a throwaway `texloom-server`, and computers that each run `texloom run` on a
folder of their own.

    from checklib import check, server, summary, until
    with server("comments") as srv:     # a throwaway server (its database: scripts/testdb.py)
        ...srv.url, srv.tmp...
    sys.exit(summary("comments"))

The server's folder (its log, database and files, and whatever the check puts there) is removed
afterwards, unless a check failed, the check stopped with an error, or KEEP=1 is set: then it
says where it is.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "scripts", ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import testdb  # noqa: E402
from pg import free_port  # noqa: E402

PY = [sys.executable, "-c"]
SERVER = "import sys; from server.cli import main; sys.argv[0] = 'texloom-server'; main()"
CLI = "import sys; from texloom.cli import main; sys.argv[0] = 'texloom'; main()"

__all__ = ["CLI", "PY", "ROOT", "SERVER", "Computer", "Failed", "Server", "background", "check", "failed", "free_port", "home_env",
           "invariants", "lf", "server", "summary", "texloom", "until", "wait"]


# ---- ✓ and ✗ ------------------------------------------------------------------------------------

_ok = True


def check(what: str, cond: object, detail: object = "") -> None:
    """One ✓ or ✗ line; a ✗ says `detail` and makes the check fail."""
    global _ok
    _ok &= bool(cond)
    print(("✓ " if cond else "✗ ") + what + ("" if cond else f": {detail}"), flush=True)


def failed() -> bool:
    return not _ok


def summary(name: str) -> int:
    """The last line, and the exit status."""
    print(f"{name}: " + ("all checks passed" if _ok else "FAILED"), flush=True)
    return 0 if _ok else 1


async def until(cond, secs: float = 20) -> bool:
    """Whether `cond()` comes true within `secs`."""
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.1)
    return False


@contextlib.asynccontextmanager
async def background():
    """A list to put asyncio tasks in (replicas' run(), say), all cancelled at the end."""
    tasks: list[asyncio.Task] = []
    try:
        yield tasks
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


# ---- texloom, and a throwaway server ----------------------------------------------------------

def home_env(home: Path) -> dict[str, str]:
    """The environment for `texloom` with its settings (TEXLOOM_HOME) in `home`."""
    return {**os.environ, "TEXLOOM_HOME": str(home), "XDG_CACHE_HOME": str(home / "cache"), "PYTHONIOENCODING": "utf-8"}


def texloom(home: Path, *args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    """`texloom ARGS` with its settings in `home`; its output is in the result."""
    return subprocess.run([sys.executable, "-m", "texloom.cli", *args], cwd=ROOT, env=home_env(home),
                          capture_output=True, text=True, timeout=timeout, check=False)


class Server:
    """A `texloom-server` on a free port, its files in `tmp` (its log: tmp/server.log). It can be
    stopped and started again, on the same address."""

    def __init__(self, tmp: Path, database: str, *args: str):
        self.tmp, self.database, self.args = tmp, database, args
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.keep = False  # keep its folder afterwards, whatever happens
        self.proc: subprocess.Popen | None = None
        self._log = None

    def start(self, secs: float = 60) -> None:
        self._log = open(self.tmp / "server.log", "a")  # noqa: SIM115 - open while it runs
        self.proc = subprocess.Popen(PY + [SERVER, "--port", str(self.port), "--database", self.database,
                                           "--data", str(self.tmp / "relay"), *self.args],
                                     cwd=ROOT, stdout=self._log, stderr=subprocess.STDOUT)
        end = time.monotonic() + secs
        while True:
            try:
                if urllib.request.urlopen(self.url + "/api/info", timeout=2).status == 200:
                    return
            except OSError:
                pass
            if self.proc.poll() is not None or time.monotonic() > end:
                raise RuntimeError(f"the server didn't start: see {self.tmp / 'server.log'}")
            time.sleep(0.1)

    def stop(self) -> None:
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
        if self._log:
            self._log.close()
            self._log = None


@contextlib.contextmanager
def server(name: str, *args: str, tmp: Path | str | None = None, codes: str | None = None, kind: str = testdb.KIND):
    """A throwaway server, started, in a new folder (or `tmp`), with a database of its own
    (scripts/testdb.py; `kind` sqlite or postgresql); `codes`, if given, are its creation codes
    (--codes-file). Yields the Server. Afterwards it and its database are stopped and the folder
    removed, unless something failed (a ✗, an exception, srv.keep set) or KEEP=1 is set."""
    folder = Path(tmp or tempfile.mkdtemp(prefix=f"texloom-{name}-"))
    folder.mkdir(parents=True, exist_ok=True)
    if codes is not None:
        (folder / "codes.txt").write_text(codes)
        args = (*args, "--codes-file", str(folder / "codes.txt"))
    srv = Server(folder, testdb.start(folder / "db", kind), *args)
    clean = False
    try:
        srv.start()
        yield srv
        clean = True
    finally:
        srv.stop()
        testdb.stop(folder / "db", kind)
        if clean and _ok and not srv.keep and not os.environ.get("KEEP"):
            shutil.rmtree(folder, ignore_errors=True)
        else:
            print(f"logs, folders and the server's database: {folder}", flush=True)


# ---- computers: each `texloom run` on a folder of its own ------------------------------------
# (scripts/check_synconly.py and check_shared_folder.py)

class Failed(Exception):
    pass


def wait(what: str, cond, secs: float = 60) -> None:
    """Wait for `cond()`, or fail saying `what` didn't happen."""
    end = time.monotonic() + secs
    while time.monotonic() < end:
        try:
            if cond():
                return
        except (OSError, ValueError, UnicodeDecodeError):
            pass
        time.sleep(0.25)
    raise Failed(f"timed out after {secs:g}s: {what}")


def lf(s: str) -> str:
    return s.replace("\r\n", "\n")


class Computer:
    """A computer: `texloom run` (its own process) on its own folder, with its own TEXLOOM_HOME.
    Its `system` is the line endings its editor saves with: "windows" (\\r\\n) or "unix"."""

    def __init__(self, name: str, system: str, tmp: Path):
        self.name, self.system = f"{name} ({system})", system
        self.home, self.dir = tmp / f"home-{name}", tmp / name
        self.home.mkdir()
        self.log = tmp / f"run-{name}.log"
        self.proc: subprocess.Popen | None = None
        self.saved: set[str] = set()  # text files this computer's editor saved (or made)

    @property
    def env(self) -> dict:
        env = {**os.environ, "TEXLOOM_HOME": str(self.home), "PYTHONIOENCODING": "utf-8", "TEXLOOM_CHECKS": "1"}
        if env.get("CHECK_REFUSE_WRITES"):
            env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOT / "scripts" / "refuse_writes"), env.get("PYTHONPATH")]))
        return env

    def texloom(self, *args: str, timeout: float = 180) -> str:
        p = subprocess.run(PY + [CLI, *args], cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=timeout, check=False)
        if p.returncode:
            raise Failed(f"{self.name}: texloom {args[0]} failed:\n{p.stdout}{p.stderr}")
        return p.stdout + p.stderr

    def start(self) -> None:
        self.proc = subprocess.Popen(PY + [CLI, "run", str(self.dir)], cwd=ROOT, env=self.env,
                                     stdout=open(self.log, "a"), stderr=subprocess.STDOUT)  # noqa: SIM115 - open while it runs
        wait(f"{self.name} connects", lambda: "connected to" in self.log.read_text(errors="replace"), 30)

    def stop(self) -> None:
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None

    def file(self, rel: str) -> Path:
        return self.dir / rel

    def broken(self) -> list[str]:
        try:
            return [line.split("INVARIANT BROKEN: ", 1)[1] for line in self.log.read_text(errors="replace").splitlines() if "INVARIANT BROKEN: " in line]
        except OSError:
            return []

    def read(self, rel: str) -> str:
        return self.file(rel).read_bytes().decode("utf-8")

    def save(self, rel: str, content: str) -> None:
        """Save a text file as this computer's editor would: \\r\\n line endings on "windows"."""
        content = lf(content)
        self.file(rel).write_bytes((content.replace("\n", "\r\n") if self.system == "windows" else content).encode("utf-8"))
        self.saved.add(rel)

    def edit(self, rel: str, old: str, new: str) -> None:
        s = lf(self.read(rel))
        if old not in s:
            raise Failed(f"{self.name}: {rel}: {old!r} isn't there to change")
        self.save(rel, s.replace(old, new, 1))

    def has(self, rel: str, *bits: str) -> bool:
        return self.file(rel).exists() and all(b in self.read(rel) for b in bits)


def invariants(*cs) -> None:
    """No computer has found one of its own invariants broken (texloom/invariants.py, web/src/invariants.ts)."""
    for c in cs:
        found = c.broken()
        if found:
            raise Failed(f"{c.name} found an invariant broken: {found[0]}" + (f" (and {len(found) - 1} more)" if len(found) > 1 else ""))
