"""Checks the desktop app's translations (texloom/locales) against the code.

Every string passed to _() or ngettext() in texloom/*.py needs an entry in each language;
counts need the forms that language uses (see PLURAL_RULES in texloom/i18n.py); `{name}`
placeholders must match. French also gets a typography check (non-breaking spaces, « »).

    python scripts/check_i18n.py                 every language
    python scripts/check_i18n.py de pl           just these
    python scripts/check_i18n.py --allow-missing report untranslated text without failing
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from texloom.i18n import plural_category  # noqa: E402
from texloom.locales import CATALOGS  # noqa: E402

fields = lambda s: set(re.findall(r"\{(\w+)\}", s))  # noqa: E731


def used_strings():
    singles, plurals, nonliteral = {}, {}, []
    for path in sorted((ROOT / "texloom").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            where = f"{path.name}:{node.lineno}"
            lit = lambda a: a.value if isinstance(a, ast.Constant) and isinstance(a.value, str) else None  # noqa: E731
            if node.func.id == "_" and node.args:
                s = lit(node.args[0])
                (singles.setdefault(s, where) if s is not None else nonliteral.append(where))
            elif node.func.id == "ngettext" and len(node.args) >= 3:
                one, other = lit(node.args[1]), lit(node.args[2])
                (plurals.setdefault(other, (one, where)) if one is not None and other is not None
                 else nonliteral.append(where))
    return singles, plurals, nonliteral


def categories(lang: str) -> list[str]:
    """The plural categories the language's rule produces for counts 0–200."""
    return sorted({plural_category(lang, n) for n in range(201)})


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    allow_missing = "--allow-missing" in sys.argv
    singles, plurals, nonliteral = used_strings()
    print(f"texts in code: {len(singles) + len(plurals)}")
    if nonliteral:
        print("calls with a non-literal text (check by hand):", ", ".join(nonliteral))
    failed = False
    for lang in args or sorted(CATALOGS):
        cat = CATALOGS.get(lang)
        if cat is None:
            print(f"\n== {lang}: no catalogue (add it to texloom/locales/__init__.py)")
            failed = True
            continue
        cats = categories(lang)
        missing, shape, holes = [], [], []
        for s, where in singles.items():
            t = cat.get(s)
            if t is None:
                missing.append(f"{where}  {s!r}")
            elif not isinstance(t, str):
                shape.append(f"{where}  {s!r}: not a count, give one text")
            elif fields(t) != fields(s):
                holes.append(f"{where}  {s!r} → {t!r}")
        for other, (one, where) in plurals.items():
            t = cat.get(other)
            if t is None:
                missing.append(f"{where}  {other!r}")
                continue
            if isinstance(t, str):
                shape.append(f"{where}  {other!r}: a count needs its forms")
                continue
            if isinstance(t, tuple):
                forms = list(t)
                if len(t) != 2:
                    shape.append(f"{where}  {other!r}: (one, other) needs two forms")
                elif set(cats) - {"one", "other"}:
                    shape.append(f"{where}  {other!r}: {lang} uses {', '.join(cats)}: write a dict with those keys")
            else:
                forms = list(t.values())
                if set(cats) - set(t) or set(t) - set(cats):
                    shape.append(f"{where}  {other!r}: {lang} needs exactly {', '.join(cats)}")
            for f in forms:
                if not fields(f) <= fields(one) | fields(other):
                    holes.append(f"{where}  {other!r} → {f!r}")
        unused = [k for k in cat if k not in singles and k not in plurals]
        typo = []
        if lang.startswith("fr"):
            for v in cat.values():
                for t in (v if isinstance(v, tuple) else v.values() if isinstance(v, dict) else (v,)):
                    if re.search(r" [:;!?»]|« |[“”]", t) or re.search(r"[^\s  (]([;!?»])|«[^  ]", t.replace("https://", "")):
                        typo.append(repr(t))
        n = len(singles) + len(plurals)
        print(f"\n== {lang}: {n - len(missing)} of {n} translated ({'/'.join(cats)} plurals)")
        for title, items in (("missing", missing), ("plural forms wrong", shape), ("placeholder mismatch", holes),
                             ("unused entries", unused), ("typography (use non-breaking spaces and « »)", typo)):
            if items:
                print(f"  {title} ({len(items)}):")
                for i in items[:40]:
                    print("    " + i)
        if (missing and not allow_missing) or shape or holes or typo:
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
