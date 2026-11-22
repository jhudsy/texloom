#!/usr/bin/env python3
"""Release a new version of the desktop app.

    uv run python scripts/release.py 0.4.41               # check, bump, commit, tag, push
    uv run python scripts/release.py 0.4.41 --no-push     # everything but the push
    uv run python scripts/release.py 0.4.41 --dry-run     # only the checks; changes nothing

It refuses unless: the version is newer than the current one; this is `main`, with nothing
uncommitted and level with GitHub; the tag doesn't exist yet; and the quick checks pass. Then it
sets the version in pyproject.toml, texloom/__init__.py and uv.lock (scripts/check_version.py
checks they agree), commits "Release X.Y.Z", tags vX.Y.Z and pushes both. The tag starts the app
builds (.github/workflows/app.yml), which attach the downloads to a GitHub release.

Last, it writes a stub for the release in the website's changelog (`--web`, by default
../texloom-web/changelog.html): its entry in the list, the "Latest" badge moved to it, and the
desktop app's commits since the last release as a comment, to turn into wording. The stub is
left uncommitted: the website is published separately.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from check_version import PLACES, versions

VERSION = re.compile(r"^\d+\.\d+\.\d+$")
QUICK_CHECKS = [  # the quick desktop checks CI runs on every push (.github/workflows/checks.yml)
    ["uv", "run", "python", "scripts/check_i18n.py"],
    ["uv", "run", "python", "scripts/check_listsync.py"],
    ["uv", "run", "python", "scripts/check_xr.py"],
    ["uv", "run", "python", "scripts/check_packing.py"],
    ["uv", "run", "python", "scripts/check_folder_deletions.py"],
    ["uv", "run", "python", "scripts/check_folder_reads.py"],
    ["uv", "run", "--with", "mypy", "python", "scripts/check_textpos.py"],
]
DESKTOP_PATHS = ["texloom", "pyproject.toml"]  # what the changelog describes: the desktop app


class Refused(Exception):
    pass


def run(cmd: list[str], cwd: Path = ROOT, quiet: bool = False) -> str:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if p.returncode != 0:
        raise Refused(f"`{' '.join(cmd)}` failed:\n{(p.stdout + p.stderr).strip()}")
    if not quiet and p.stdout.strip():
        print("   ", p.stdout.strip().splitlines()[-1])
    return p.stdout


def as_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def preflight(new: str, changelog: Path) -> str:
    """Everything that must hold before a release; returns the current version."""
    if not VERSION.match(new):
        raise Refused(f"{new!r} isn't a version like 0.4.41")
    found = versions()
    if None in found.values() or len(set(found.values())) != 1:
        raise Refused(f"the version's three places disagree already ({found}): fix that first")
    current = next(iter(found.values()))
    assert current is not None
    if as_tuple(new) <= as_tuple(current):
        raise Refused(f"{new} isn't newer than the current version, {current}")
    if run(["git", "rev-parse", "--abbrev-ref", "HEAD"], quiet=True).strip() != "main":
        raise Refused("not on main")
    if run(["git", "status", "--porcelain"], quiet=True).strip():
        raise Refused("there are uncommitted changes: commit or stash them first")
    run(["git", "fetch", "--quiet", "--tags", "origin"], quiet=True)
    head, remote = (run(["git", "rev-parse", r], quiet=True).strip() for r in ("HEAD", "origin/main"))
    if head != remote:
        raise Refused("main isn't level with GitHub (origin/main): pull or push first")
    if run(["git", "tag", "--list", f"v{new}"], quiet=True).strip():
        raise Refused(f"the tag v{new} exists already")
    if not changelog.is_file():
        raise Refused(f"no changelog at {changelog} (say where with --web)")
    if f'id="v{new}"' in changelog.read_text("utf-8"):
        raise Refused(f"the changelog has an entry for {new} already")
    if run(["git", "status", "--porcelain", "--", changelog.name], cwd=changelog.parent, quiet=True).strip():
        raise Refused(f"{changelog} has uncommitted changes")
    if not run(["git", "tag", "--list", f"v{current}"], quiet=True).strip():
        raise Refused(f"the current version's tag, v{current}, doesn't exist (the changelog lists commits since it)")
    with_stub(changelog.read_text("utf-8"), new, current, dt.date.today(), [])  # its layout is as expected
    return current


def bump(new: str) -> None:
    for name, (path, pattern) in PLACES.items():
        text = path.read_text("utf-8")
        m = pattern.search(text)
        assert m is not None, name
        path.write_text(text[:m.start(1)] + new + text[m.end(1):], "utf-8")


def desktop_commits(previous: str) -> list[str]:
    """The desktop app's commits since the previous release (their first lines)."""
    out = run(["git", "log", "--reverse", "--format=%s", f"v{previous}..HEAD", "--", *DESKTOP_PATHS], quiet=True)
    return [s for s in out.splitlines() if not s.startswith("Release ")]


def with_stub(text: str, new: str, previous: str, today: dt.date, commits: list[str]) -> str:
    """The changelog with a stub for the release: its entry in the list, its article with the
    "Latest" badge (taken off the one before), and the commits as a comment to turn into wording."""
    notes = "\n".join(f"           - {html.escape(c, quote=False).replace('--', '- -')}" for c in commits) or "           (none)"
    short, long = f"{today.day} {today:%b}", f"{today.day} {today:%B %Y}"

    toc = '    <nav class="cl-toc" aria-label="Releases">\n'
    if text.count(toc) != 1:
        raise Refused("can't find the list of releases in the changelog")
    text = text.replace(toc, toc + f'      <a href="#v{new}">{new} <small>{short}</small></a>\n')

    badge = re.compile(r'(<article class="release" id="v[^"]+">\s*<header>.*?</time>)<span class="badge">Latest</span>', re.DOTALL)
    text, n = badge.subn(r"\1", text, count=1)
    if n != 1:
        raise Refused("can't find the release with the \"Latest\" badge in the changelog")

    first = text.find('      <article class="release" id="v')
    if first < 0:
        raise Refused("can't find the first release in the changelog")
    article = f'''      <article class="release" id="v{new}">
        <header><h2>{new}</h2><time datetime="{today.isoformat()}">{long}</time><span class="badge">Latest</span></header>
        <p class="summary">TODO: one line on what this release is about.</p>
        <!-- The desktop app's commits since {previous}, to turn into wording (then delete this):
{notes}
        -->
        <h3>New</h3>
        <ul>
          <li><strong>TODO</strong></li>
        </ul>
        <h3>Fixed</h3>
        <ul>
          <li><strong>TODO</strong></li>
        </ul>
        <div class="links">
          <a href="https://github.com/jhudsy/texloom/releases/tag/v{new}">Downloads and commits on GitHub</a>
        </div>
      </article>

'''
    return text[:first] + article + text[first:]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("version", help="the new version, e.g. 0.4.41")
    ap.add_argument("--web", type=Path, default=ROOT.parent / "texloom-web", help="the website repository (for its changelog)")
    ap.add_argument("--no-push", action="store_true", help="commit and tag, but don't push")
    ap.add_argument("--dry-run", action="store_true", help="only the checks; change nothing")
    ap.add_argument("--skip-checks", action="store_true", help="don't run the quick checks (they ran just now)")
    a = ap.parse_args()
    new, changelog = a.version, (a.web / "changelog.html").resolve()

    try:
        print(f"==> Checking a release of {a.version} can be made")
        current = preflight(new, changelog)
        print(f"    {current} → {new}, from main at {run(['git', 'rev-parse', '--short', 'HEAD'], quiet=True).strip()}")
        if not a.skip_checks:
            print("==> The quick checks")
            for cmd in QUICK_CHECKS:
                run(cmd)
        if a.dry_run:
            print("==> Dry run: nothing changed")
            return 0

        print(f"==> Setting the version to {new}")
        bump(new)
        try:
            run(["uv", "lock", "--check"], quiet=True)
            run(["uv", "run", "python", "scripts/check_version.py"])
        except Refused:
            run(["git", "checkout", "--", *(str(p.relative_to(ROOT)) for p, _ in PLACES.values())], quiet=True)
            raise
        run(["git", "commit", "--quiet", "-m", f"Release {new}", "--", *(str(p.relative_to(ROOT)) for p, _ in PLACES.values())], quiet=True)
        run(["git", "tag", f"v{new}"], quiet=True)
        print(f"    committed and tagged v{new}")

        if a.no_push:
            print(f"==> Not pushed (--no-push): git push origin main && git push origin v{new}")
        else:
            print("==> Pushing main and the tag (the tag starts the app builds)")
            run(["git", "push", "--quiet", "origin", "main"], quiet=True)
            run(["git", "push", "--quiet", "origin", f"v{new}"], quiet=True)
            print("    https://github.com/jhudsy/texloom/actions/workflows/app.yml")

        print("==> The website's changelog")
        changelog.write_text(with_stub(changelog.read_text("utf-8"), new, current, dt.date.today(), desktop_commits(current)), "utf-8")
        print(f"    a stub for {new} is in {changelog}: write its wording (the TODOs), then commit and publish")
        return 0
    except Refused as e:
        print(f"release: refused: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
