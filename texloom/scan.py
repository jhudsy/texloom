"""Find lines in a project that ask to run programs on the computer that compiles it.

TeX can't run programs unless shell escape is on, and texloom keeps it off by default, so
these lines only matter
- when a compile has shell escape on (minted and similar need it): "shell" findings;
- when someone compiles a synced folder with their own tools, which may follow arara
  directives or editor "magic comments" naming the program to run: "tools" findings.

This catches what is written out plainly. TeX can assemble any command from macros, so
with full shell escape a careful author can still hide one: the guarantee is shell
escape off (or restricted, which only allows TeX Live's list of safe programs).

Keep the patterns in sync with programRequests() in web/src/texscan.ts.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

# In TeX code (outside comments).
SHELL = [
    (re.compile(r"\\write\s*18\b"), "runs a shell command (\\write18)"),
    (re.compile(r"\\(?:Delayed)?ShellEscape\b"), "runs a shell command (\\ShellEscape)"),
    (re.compile(r"\\input\s*\{?\s*\|"), "reads the output of a command (\\input|)"),
    (re.compile(r"\b(?:os\.execute|io\.popen|os\.spawn|os\.exec)\b"), "runs a program from Lua"),
    (re.compile(r"\\(?:usepackage|RequirePackage)\s*(?:\[[^\]]{0,300}\])?\s*\{[^}]{0,300}"
                r"\b(?:minted|pythontex|sagetex|gnuplottex|svg|auto-pst-pdf|shellesc|bashful|pyluatex)\b"),
     "loads a package that runs programs"),
    (re.compile(r"\\tikzexternalize\b|\\usetikzlibrary\s*\{[^}]{0,300}\bexternal\b"),
     "TikZ externalisation runs LaTeX as a command"),
]
# In comments: instructions for tools other than TeX.
TOOLS = [
    (re.compile(r"^\s*%\s*arara\s*:"), "tells arara which programs to run"),
    (re.compile(r"^\s*%\s*!\s*(?:TeX|TEX|tex|BIB|bib)\s+(?:TS-|TXS-)?program\b"),
     "sets the program your LaTeX editor runs"),
]
SCANNED = (".tex", ".sty", ".cls", ".ltx", ".dtx", ".def", ".cfg", ".lua", ".tikz")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str  # "shell" or "tools"
    what: str
    text: str  # the line, trimmed

    @property
    def key(self) -> str:
        """Identifies the line by file and content (not number), for approvals."""
        return hashlib.sha256(f"{self.path}\0{self.text}".encode()).hexdigest()[:24]

    def describe(self) -> str:
        return f"{self.path}:{self.line} {self.what}: {self.text}"


def code_part(line: str) -> str:
    """The line up to its comment: a '%' after an even number of backslashes."""
    run = 0
    for i, c in enumerate(line):
        if c == "\\":
            run += 1
            continue
        if c == "%" and run % 2 == 0:
            return line[:i]
        run = 0
    return line


def scan_text(path: str, text: str) -> list[Finding]:
    if not path.lower().endswith(SCANNED) or len(text) > 4 * 1024 * 1024:
        return []
    lua = path.lower().endswith(".lua")
    out = []
    for n, line in enumerate(text.split("\n"), 1):
        shown = line.strip()[:160]
        code = line if lua else code_part(line)
        for rx, what in SHELL:
            if rx.search(code):
                out.append(Finding(path, n, "shell", what, shown))
                break
        else:
            if not lua:
                for rx, what in TOOLS:
                    if rx.search(line):
                        out.append(Finding(path, n, "tools", what, shown))
                        break
    return out


def scan(files: dict[str, str]) -> list[Finding]:
    return [f for p, t in sorted(files.items()) for f in scan_text(p, t)]


def added(path: str, old: str | None, new: str) -> list[Finding]:
    """Findings in `new` whose line wasn't in `old` (what a change brought in)."""
    before = {f.text for f in scan_text(path, old or "")}
    return [f for f in scan_text(path, new) if f.text not in before]


# --- approvals for shell-escape compiles (per folder, never shared) -------------
def _allowed_file(root: Path) -> Path:
    return root / ".texloom" / "allowed.json"


def allowed(root: Path) -> set[str]:
    try:
        return set(json.loads(_allowed_file(root).read_text()))
    except (OSError, ValueError):
        return set()


def allow(root: Path, keys: set[str]) -> None:
    p = _allowed_file(root)
    p.write_text(json.dumps(sorted(keys)))
