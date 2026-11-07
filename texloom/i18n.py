"""The desktop app's language: English, or one of the translations in texloom/locales.

`_("English text", name=value)` returns the text in the app's language, then fills `{name}`
placeholders; `ngettext(n, one, other, ...)` picks the form for the count. The English
strings are the catalogue keys, so a string missing from a catalogue shows in English.
The language is TEXLOOM_LANG if set, else the system's. No Qt here.
"""

from __future__ import annotations

import locale
import os
import re
import subprocess
import sys
from typing import Callable

from .locales import CATALOGS

_LANG: str | None = None
_FIELD = re.compile(r"\{(\w+)\}")

# Plural categories by language (CLDR names: zero, one, two, few, many, other), for counts.
# A catalogue entry for a count is (one, other), or a dict with one text per category the
# language uses. Languages not listed use English's: "one" for 1, else "other".


def _slavic_east(n: int) -> str:  # Russian, Ukrainian, Belarusian
    if n % 10 == 1 and n % 100 != 11:
        return "one"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "few"
    return "many"


def _polish(n: int) -> str:
    if n == 1:
        return "one"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "few"
    return "many"


PLURAL_RULES: dict[str, Callable[[int], str]] = {
    "fr": lambda n: "one" if n in (0, 1) else "other",
    "pt_BR": lambda n: "one" if n in (0, 1) else "other",
    "pl": _polish,
    "ru": _slavic_east, "uk": _slavic_east, "be": _slavic_east,
    "cs": lambda n: "one" if n == 1 else "few" if 2 <= n <= 4 else "other",
    "sk": lambda n: "one" if n == 1 else "few" if 2 <= n <= 4 else "other",
    "ja": lambda n: "other", "zh": lambda n: "other", "ko": lambda n: "other",
}


def plural_category(lang: str, n: int) -> str:
    rule = PLURAL_RULES.get(lang) or PLURAL_RULES.get(lang.split("_")[0])
    return rule(n) if rule else ("one" if n == 1 else "other")


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


def _match(code: str) -> str:
    """The catalogue for a locale code such as fr_FR.UTF-8 or pt-BR: the exact region if
    there is one (pt_BR), else the language (pt), else English."""
    code = code.split(".")[0].split("@")[0].replace("-", "_")
    lang, _sep, region = code.partition("_")
    lang = lang.lower()
    full = f"{lang}_{region.upper()}" if region else lang
    for c in (full, lang):
        if c in CATALOGS:
            return c
    return "en"


def lang() -> str:
    """The app's language ('en' or a key of CATALOGS), decided once."""
    global _LANG
    if _LANG is None:
        _LANG = _match(os.environ.get("TEXLOOM_LANG", "").strip() or _system_lang())
    return _LANG


def _fill(s: str, vars: dict) -> str:
    """Replace `{name}` only for names given, leaving any other braces as they are."""
    if not vars:
        return s
    return _FIELD.sub(lambda m: str(vars[m[1]]) if m[1] in vars else m[0], s)


def _(en: str, /, **vars) -> str:
    t = CATALOGS.get(lang(), {}).get(en)
    return _fill(t if isinstance(t, str) else en, vars)


def ngettext(n: int, one: str, other: str, /, **vars) -> str:
    """The form of `one`/`other` for the count `n` in the app's language; keyed by `other`."""
    t = CATALOGS.get(lang(), {}).get(other)
    cat = plural_category(lang(), n)
    if isinstance(t, tuple):
        return _fill(t[0] if cat == "one" else t[1], vars)
    if isinstance(t, dict):
        return _fill(t.get(cat, t.get("other", other)), vars)
    return _fill(one if n == 1 else other, vars)
