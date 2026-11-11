"""Keep a synced folder's git repository in step with the project (`texloom run --git`).

The folder is both the TeXLoom project (kept in sync by FolderSync) and a git clone:
- TeXLoom -> git: once the project files have been quiet for a while, commit them, and
  push to the branch's upstream (e.g. GitHub);
- git -> TeXLoom: fetch every so often; new upstream commits are merged into the folder,
  and FolderSync shares the changed files with collaborators.

Only project files are committed (what the helper syncs: no build output, no .texloom).
A merge that conflicts is aborted, leaving the project as it is: nothing with conflict
markers ever reaches collaborators. Git uses your own credentials and configuration; the
TeXLoom server is not involved, so end-to-end encryption is unaffected.

When the project is also linked to a GitHub repository from the web app (its `github`
setting) and this folder is a clone of that repository and branch, the bridge takes turns with
browsers through the project's `githubLease` (web/src/github.ts), so one commit is never
brought in twice: it brings in, commits and pushes only while it holds the lease, renews it
every RENEW seconds, and takes it over only when no renewal has been seen for EXPIRE. It
records the commit it has brought in or pushed as the link's `head`, for whoever syncs next.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path

from .folder import FolderSync
from .paths import safe_rel
from .scan import SCANNED, added

log = logging.getLogger("texloom.git")

MARKER = re.compile(r"^(<{7}|>{7})( |$)", re.M)  # an unresolved merge conflict

DEFAULTS = {"interval": 60, "quiet": 120, "push": True}
RENEW, EXPIRE = 30.0, 100.0  # seconds, as in web/src/github.ts
GITHUB = re.compile(r"^(?:https?://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", re.IGNORECASE)


def github_repo(url: str) -> str | None:
    """owner/name of a github.com remote address, lower-cased; None for anything else."""
    m = GITHUB.match(url.strip())
    return f"{m[1]}/{m[2]}".lower() if m else None


class GitError(Exception):
    pass


async def clone_of_link(root: Path, link: object) -> bool:
    """Whether the folder is a git clone of the GitHub repository the project is linked to."""
    if not isinstance(link, dict) or not isinstance(link.get("repo"), str) or not (root / ".git").exists():
        return False
    try:
        proc = await asyncio.create_subprocess_exec("git", "remote", "-v", cwd=root,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
    except OSError:
        return False
    want = link["repo"].lower()
    return any(len(p := line.split()) >= 2 and github_repo(p[1]) == want for line in out.decode(errors="replace").splitlines())


class GitBridge:
    def __init__(self, root: Path, fs: FolderSync, opts: dict | None = None):
        self.root = root
        self.fs = fs
        self.r = fs.r
        o = {**DEFAULTS, **(opts or {})}
        self.interval, self.quiet, self.push_enabled = float(o["interval"]), float(o["quiet"]), bool(o["push"])
        self._last_status: str | None = None
        self._status_since = time.monotonic()
        self._blocked: str | None = None  # upstream commit that didn't merge cleanly
        self._told: set[str] = set()  # warnings already shown
        self.name = "TeXLoom app"  # shown to others as who keeps the repository in step (set by cli)
        # taking turns with browsers that sync the same repository (see the module's notes)
        idf = root / ".texloom" / "git-device"
        try:
            self.lease_id = idf.read_text().strip() or ""
        except OSError:
            self.lease_id = ""
        if not self.lease_id:
            import secrets
            self.lease_id = "app:" + secrets.token_hex(8)
            try:
                idf.write_text(self.lease_id)
            except OSError:
                pass
        self._lease_seen = time.monotonic()
        self._renewed = 0.0
        self._claimed = 0.0
        self.r.meta.observe(lambda ev: self._saw_lease(ev))

    def _saw_lease(self, ev) -> None:
        if "githubLease" in ev.keys:
            self._lease_seen = time.monotonic()

    # --- running git -------------------------------------------------------------
    async def git(self, *args: str, check: bool = True, timeout: float = 120) -> str:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true", "LC_ALL": "C"}
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=self.root, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise GitError(f"git {args[0]} timed out")
        if check and proc.returncode:
            raise GitError(err.decode(errors="replace").strip() or f"git {args[0]} failed")
        return out.decode(errors="replace")

    def tell(self, key: str, msg: str, *a) -> None:
        """Warn once per distinct problem (the loop retries every minute)."""
        if key not in self._told:
            self._told.add(key)
            log.warning(msg, *a)

    async def check(self) -> str | None:
        """None if the folder can be bridged, else why not."""
        try:
            if (await self.git("rev-parse", "--show-toplevel")).strip() != str(self.root.resolve()):
                return "the folder isn't the top of a git repository (run `git init` in it)"
            await self.git("symbolic-ref", "-q", "HEAD")
        except GitError as e:
            return f"not usable as a git repository ({e})" if "symbolic-ref" not in str(e) else "HEAD is detached: check out a branch"
        except FileNotFoundError:
            return "git isn't installed"
        return None

    async def upstream(self) -> str | None:
        try:
            return (await self.git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")).strip() or None
        except GitError:
            return None

    # --- TeXLoom -> git ---------------------------------------------------------
    async def changed_paths(self) -> list[str]:
        """Project files that differ from HEAD (what the helper syncs; nothing else)."""
        out = await self.git("status", "--porcelain=v1", "-z", "--untracked-files=all")
        items, paths = out.split("\0"), []
        i = 0
        while i < len(items):
            entry = items[i]
            i += 1
            if len(entry) < 4:
                continue
            code, path = entry[:2], entry[3:]
            if code[0] in "RC" and i < len(items):  # rename/copy: the old name follows
                old = items[i]
                i += 1
                if safe_rel(old) and not self.fs.ignored(old):
                    paths.append(old)
            if safe_rel(path) and not self.fs.ignored(path):
                paths.append(path)
        return paths

    async def commit(self, paths: list[str], why: str) -> bool:
        if not paths:
            return False
        await self.git("add", "-A", "--", *paths)
        if not (await self.git("diff", "--cached", "--name-only")).strip():
            return False
        names = sorted({p for p in (await self.git("diff", "--cached", "--name-only", "-z")).split("\0") if p})
        shown = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
        await self.git("commit", "-q", "-m", f"TeXLoom: {why} {shown}", "-m",
                       "Committed by `texloom run --git` from the shared project.")
        log.info("git: committed %s", shown)
        return True

    # --- git -> TeXLoom -----------------------------------------------------------
    async def merge(self, upstream: str) -> bool:
        """Merge new upstream commits into the folder. False if it would conflict."""
        target = (await self.git("rev-parse", upstream)).strip()
        if target == self._blocked:
            return False
        before = (await self.git("rev-parse", "HEAD")).strip()
        try:
            await self.git("merge", "--no-edit", "-q", upstream)
        except GitError as e:
            conflicted = (await self.git("diff", "--name-only", "--diff-filter=U", check=False)).split()
            await self.git("merge", "--abort", check=False)
            self._blocked = target
            remote, _, branch = upstream.partition("/")
            log.warning("git: %s has changes that clash with the project's (%s). Not merged, so the project is "
                        "unchanged. To combine them, in a separate worktree of this repository:\n"
                        "  git -C %s worktree add --detach ../texloom-merge\n"
                        "  cd ../texloom-merge && git merge %s     # resolve, then: git commit\n"
                        "  git push %s HEAD:%s\n"
                        "The merged version then comes into the project on the next fetch.",
                        upstream, ", ".join(conflicted[:5]) or str(e)[:200], self.root, upstream, remote, branch)
            return False
        marked = await self.conflict_markers(before)
        if marked:  # someone pushed a "resolution" that still has conflict markers: don't share it
            await self.git("reset", "-q", "--keep", before)
            self._blocked = target
            log.warning("git: %s still has unresolved conflict markers (<<<<<<< / >>>>>>>) in %s. Not merged, "
                        "so the project is unchanged: fix them, push, and they come in on the next fetch.",
                        upstream, ", ".join(marked[:5]))
            return False
        self._blocked = None
        await self.note_program_lines(before)
        log.info("git: merged %s", upstream)
        return True

    async def conflict_markers(self, before: str) -> list[str]:
        """Project files changed since `before` that contain conflict markers."""
        out = []
        for p in (await self.git("diff", "--name-only", "-z", before, "HEAD", check=False)).split("\0"):
            if not p or not safe_rel(p) or self.fs.ignored(p):
                continue
            try:
                if MARKER.search((self.root / p).read_text("utf-8", "replace")):
                    out.append(p)
            except OSError:
                continue
        return out

    async def note_program_lines(self, before: str) -> None:
        """Point out lines that ask to run programs, newly brought in from git (as for collaborators)."""
        changed = (await self.git("diff", "--name-only", "-z", before, "HEAD", check=False)).split("\0")
        for p in changed:
            if not p or not p.lower().endswith(SCANNED) or not safe_rel(p):
                continue
            old = await self.git("show", f"{before}:{p}", check=False)
            try:
                new = (self.root / p).read_text("utf-8", "replace")
            except OSError:
                continue
            for f in added(p, old or None, new):
                log.warning("⚠ %s (from git) — if you compile with your own LaTeX tools, check it first.", f.describe())

    # --- taking turns with browsers ---------------------------------------------------
    async def shared_link(self, upstream: str | None) -> dict | None:
        """The project's GitHub link, if this folder is a clone of that repository and branch."""
        link = self.r.meta.get("github")
        if not isinstance(link, dict) or not upstream or link.get("dir"):
            return None
        remote, _, branch = upstream.partition("/")
        url = (await self.git("config", "--get", f"remote.{remote}.url", check=False)).strip()
        same = github_repo(url) == str(link.get("repo", "")).lower() and branch == link.get("branch")
        return link if same else None

    def _lease(self) -> dict | None:
        lease = self.r.meta.get("githubLease")
        return lease if isinstance(lease, dict) and isinstance(lease.get("id"), str) else None

    def _set_lease(self, n: int) -> None:
        lease = {"id": self.lease_id, "by": self.name, "n": n}
        self.r.local_change(lambda: self.r.meta.__setitem__("githubLease", lease))
        self._renewed = time.monotonic()

    def renew(self) -> None:
        """Keep the lease while holding it."""
        lease = self._lease()
        if lease and lease["id"] == self.lease_id and time.monotonic() - self._renewed >= RENEW and self.r.keys.writable:
            self._set_lease(int(lease.get("n", 0)) + 1)

    def my_turn(self) -> bool:
        """Whether this bridge may bring in and push now. Takes the lease when it's free."""
        lease = self._lease()
        if lease and lease["id"] == self.lease_id:
            self.renew()
            return time.monotonic() - self._claimed >= 10  # a fresh claim waits to meet any other first
        if not self.r.keys.writable or self.r.ws is None or not getattr(self.r, "_caught_up", False):
            return False  # can't tell who holds it
        if lease and time.monotonic() - self._lease_seen < EXPIRE:
            self.tell("lease:" + lease["id"], "git: %s keeps %s in step for this project now; this folder waits its turn "
                      "(so no commit comes in twice)", lease.get("by") or "someone", self.r.meta.get("github", {}).get("repo"))
            return False
        self._set_lease(0)
        self._claimed = time.monotonic()
        log.info("git: this folder now keeps the project's GitHub repository in step")
        self._told = {k for k in self._told if not k.startswith("lease:")}
        return False

    async def record_head(self, upstream: str) -> None:
        """Note the upstream commit the folder now matches, as the link's `head`."""
        head = (await self.git("rev-parse", "HEAD")).strip()
        if head != (await self.git("rev-parse", upstream)).strip():
            return
        link = self.r.meta.get("github")
        if isinstance(link, dict) and link.get("head") != head and self.r.keys.writable:
            new = {**link, "head": head, "blocked": None, "clash": [], "by": self.name, "at": int(time.time() * 1000)}
            self.r.local_change(lambda: self.r.meta.__setitem__("github", new))

    # --- one round ------------------------------------------------------------------
    async def cycle(self, force: bool = False) -> None:
        upstream = await self.upstream()
        shared = await self.shared_link(upstream)
        if shared is not None and not self.my_turn():
            return
        if upstream:
            try:
                await self.git("fetch", "-q", timeout=300)
            except GitError as e:
                self.tell("fetch:" + str(e)[:80], "git: can't fetch (%s); will keep trying", e)
                upstream = None
        elif self.push_enabled:
            self.tell("no-upstream", "git: this branch has no upstream, so commits stay local "
                      "(set one with `git push -u origin <branch>`)")
        behind = ahead = 0
        async with self.r.lock:  # no collaborator edits are written while git works on the folder
            paths = await self.changed_paths()
            status = "\0".join(paths)
            if status != self._last_status:
                self._last_status, self._status_since = status, time.monotonic()
            if upstream:
                ahead_s, behind_s = (await self.git("rev-list", "--left-right", "--count", f"HEAD...{upstream}")).split()
                behind = int(behind_s)
            quiet = time.monotonic() - self._status_since >= self.quiet
            if paths and (force or quiet or behind):  # before merging, always: the merge needs a clean base
                await self.commit(paths, "edits from collaborators:" if not behind else "edits before merging:")
                self._last_status = ""
            if behind and await self.merge(upstream):
                await self.fs.ingest_all()  # share what came in from git
        if upstream and self.push_enabled and not self._blocked:  # diverged: pushing would be refused
            ahead = int((await self.git("rev-list", "--count", f"{upstream}..HEAD")).strip() or 0)
            if ahead:
                try:
                    await self.git("push", "-q", timeout=300)
                    log.info("git: pushed %d commit(s) to %s", ahead, upstream)
                    self._told = {k for k in self._told if not k.startswith("push:")}
                except GitError as e:
                    self.tell("push:" + str(e)[:80], "git: push to %s failed (%s); will retry", upstream, e)
        if shared is not None and upstream:
            await self.record_head(upstream)

    async def loop(self) -> None:
        await self.r.synced.wait()  # start from the current project, not a stale copy
        while True:
            try:
                await self.cycle()
            except (GitError, OSError) as e:
                self.tell("cycle:" + str(e)[:80], "git: %s; will retry", e)
            until = time.monotonic() + self.interval
            while (left := until - time.monotonic()) > 0:  # renewing the lease meanwhile, if held
                await asyncio.sleep(min(left, RENEW / 2))
                try:
                    self.renew()
                except PermissionError:
                    pass
