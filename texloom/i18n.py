"""The desktop app's language: English, or French when the system (or TEXLOOM_LANG) says so.

`_("English text", name=value)` returns the French from `locales_fr.FR` when the app runs in
French, then fills `{name}` placeholders; `ngettext(n, one, other, ...)` picks the plural form.
The English strings are the catalogue keys, so a string missing from it shows in English.
No Qt here: the language is decided from the environment and the `locale` module.
"""

from __future__ import annotations

import locale
import os
import re
import subprocess
import sys

_LANG: str | None = None
_FIELD = re.compile(r"\{(\w+)\}")


def _system_lang() -> str:
    for var in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        v = os.environ.get(var)
        if v:
            return v.split(":")[0]
    try:
        code = locale.getlocale()[0] or ""
    except (ValueError, TypeError):
        code = ""
    if not code and sys.platform == "darwin":  # apps opened from Finder get no LANG
        try:
            code = subprocess.run(["defaults", "read", "-g", "AppleLocale"], capture_output=True,
                                  text=True, timeout=2).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return code


def lang() -> str:
    """'fr' or 'en', decided once: TEXLOOM_LANG if set, else the system locale."""
    global _LANG
    if _LANG is None:
        forced = os.environ.get("TEXLOOM_LANG", "").strip().lower()
        code = forced or _system_lang().lower()
        _LANG = "fr" if code.startswith("fr") else "en"
    return _LANG


def _fill(s: str, vars: dict) -> str:
    """Replace `{name}` only for names given, leaving any other braces as they are."""
    if not vars:
        return s
    return _FIELD.sub(lambda m: str(vars[m[1]]) if m[1] in vars else m[0], s)


def _fr():
    from .locales_fr import FR
    return FR


def _(en: str, /, **vars) -> str:
    s = en
    if lang() == "fr":
        t = _fr().get(en)
        if isinstance(t, str):
            s = t
    return _fill(s, vars)


def ngettext(n: int, one: str, other: str, /, **vars) -> str:
    """`one` or `other` for the count `n` (French counts 0 and 1 as singular); keyed by `other`."""
    if lang() == "fr":
        t = _fr().get(other)
        if isinstance(t, tuple):
            return _fill(t[0] if n in (0, 1) else t[1], vars)
    return _fill(one if n == 1 else other, vars)
