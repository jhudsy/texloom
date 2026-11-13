#!/usr/bin/env python3
"""Property checks (Hypothesis) for the desktop's text handling: random texts and edits, including
Windows line endings, lone "\\r", accents, emoji and other line-like characters, against what must
always hold. A failure prints the smallest case Hypothesis found.

1. textpos: converting between characters, UTF-8 bytes and UTF-16 units round-trips, and editing a
   pycrdt text through it does exactly what the same edit does to a Python string.
2. apply_text turns a project text into exactly the target.
3. strip_cr takes every "\\r" out and nothing else; lf and on_disk round-trip, and a file written
   with Windows line endings has nothing but "\\r\\n".
4. merges.shift keeps a remembered range on the same text through any edits, local or merged in.
5. The "same rules" as the web app: merge3 and lf give the same answers here and in web/src
   (asked of web/scripts/check-props.mjs --same, so `npm ci` in web/ first).

  uv run --with hypothesis python scripts/check_props.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pycrdt import Doc, Map, Text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from texloom import textpos
from texloom.folder import apply_text, crlf, lf, merge3, on_disk, strip_cr
from texloom.merges import shift
from texloom.textpos import Byte, Char

PIECES = ["a", "b", "xyz", " ", "\n", "\n", "\r\n", "\r\n", "\r", "é", "naïve", "😀", "𝒜", "\\bottomrule",
          "{l|rr|}", "%", "&", " ", "\x0c", "\x85"]
texts = st.lists(st.sampled_from(PIECES), max_size=30).map("".join)
RUNS = settings(max_examples=int(os.environ.get("CHECK_RUNS", "300")), deadline=None,
                suppress_health_check=[HealthCheck.too_slow], derandomize=bool(os.environ.get("CHECK_SEED")))


def fresh(s: str) -> Text:
    d = Doc()
    d["files"] = m = Map()
    m["f"] = Text(s)
    return m["f"]


@RUNS
@given(texts, st.data())
def conversions_round_trip(s, data):
    i = Char(data.draw(st.integers(0, len(s))))
    assert textpos.char_of_byte(s, textpos.byte_of(s, i)) == i
    assert textpos.char_of_u16(s, textpos.u16_of(s, i)) == i
    assert textpos.length_u16(s) == textpos.u16_of(s, Char(len(s)))


@RUNS
@given(texts, st.lists(st.tuples(st.integers(0, 10**6), st.integers(0, 8), texts), max_size=15))
def textpos_edits_like_strings(s, edits):
    t = fresh(s)
    for at, n, new in edits:
        a = Char(at % (len(s) + 1))
        b = Char(min(len(s), a + n))
        textpos.replace(t, s, a, b, new)
        s = s[:a] + new + s[b:]
        assert str(t) == s


@RUNS
@given(texts, texts)
def apply_text_gives_the_target(old, new):
    t = fresh(old)
    apply_text(t, old, new)
    assert str(t) == new


@RUNS
@given(texts)
def strip_cr_takes_out_cr_only(s):
    t = fresh(s)
    strip_cr(t)
    assert str(t) == s.replace("\r", "")


@RUNS
@given(texts, st.booleans())
def line_endings_round_trip(s, windows):
    assert "\r" not in lf(s) and lf(lf(s)) == lf(s)
    data = on_disk(s, windows)
    back = data.decode("utf-8")
    assert lf(back) == lf(s)
    if windows:
        assert back.count("\r\n") == back.count("\n") and "\r\r" not in back
        assert not back.replace("\r\n", "").count("\r")
        if "\n" in back:
            assert crlf(data)
    else:
        assert "\r" not in back


@RUNS
@given(texts, st.lists(st.tuples(st.booleans(), st.integers(0, 10**6), st.integers(0, 5), texts), max_size=20))
def shift_keeps_a_range_on_its_text(s, edits):
    marked = "⟦" + "mark" + "⟧"
    s = s + marked + s
    t = fresh(s)
    doc = t.doc
    other = Doc()
    other.apply_update(doc.get_update())
    ot = other.get("files", type=Map)["f"]
    i = s.index(marked)
    rng = [Byte(len(s[:i].encode())), Byte(len(s[:i + len(marked)].encode()))]

    def follow(events):
        for e in events:
            rng[0], rng[1] = shift(rng[0], e.delta, True), shift(rng[1], e.delta, False)
    watching = doc.get("files", type=Map).observe_deep(follow)  # kept: pycrdt drops a subscription nobody holds
    for here, at, n, new in edits:
        tx, ts = (t, str(t)) if here else (ot, str(ot))
        a = Char(at % (len(ts) + 1))
        b = Char(min(len(ts), a + n))
        if marked in ts and ts.index(marked) < b and a < ts.index(marked) + len(marked):
            continue  # an edit to the marked text itself moves its ends: not what's checked
        textpos.replace(tx, ts, a, b, new.replace("⟦", "").replace("⟧", ""))
        if not here:
            doc.apply_update(other.get_update(doc.get_state()))
        other.apply_update(doc.get_update(other.get_state()))
    now = str(t).encode()
    assert now[rng[0]:rng[1]].decode() == marked, (now, rng)
    del watching


def same_rules() -> list[str]:
    """merge3 and lf give the same answers here and in the web app."""
    import random
    rng = random.Random(int(os.environ.get("CHECK_SEED", "0")) or None)
    words = PIECES + ["line one\n", "line two\n", "line three\n"]
    gen = lambda: "".join(rng.choice(words) for _ in range(rng.randint(0, 25)))
    cases = []
    for _ in range(800):
        base = gen()
        mutate = lambda s: "".join(c for c in s if rng.random() > 0.15) + gen() if rng.random() < 0.5 else gen() + s
        cases.append(("merge3", [base, mutate(base), mutate(base)]))
    cases += [("lf", [gen()]) for _ in range(200)]
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
        p = subprocess.run(["node", "scripts/check-props.mjs", "--same", f"{tmp}/cases.json", f"{tmp}/answers.json"],
                           cwd=ROOT / "web", capture_output=True, text=True, encoding="utf-8", check=False)
        if p.returncode:
            return [f"web/scripts/check-props.mjs --same failed: {p.stderr[-800:]}"]
        theirs = json.loads((Path(tmp) / "answers.json").read_text(encoding="utf-8"))
    ours = [merge3(*a) if fn == "merge3" else lf(*a) for fn, a in cases]
    return [f"{fn}{tuple(a)!r}: here {o!r}, web app {w!r}" for (fn, a), o, w in zip(cases, ours, theirs, strict=True) if o != w]


def main() -> None:
    checks = [conversions_round_trip, textpos_edits_like_strings, apply_text_gives_the_target, strip_cr_takes_out_cr_only,
              line_endings_round_trip, shift_keeps_a_range_on_its_text]
    bad = []
    for check in checks:
        try:
            check()
        except Exception as e:  # noqa: BLE001 - report every failing property
            bad.append(f"{check.__name__}: {str(e)[:1500]}")
    diff = same_rules()
    if diff:
        bad.append(f"same rules as the web app: {len(diff)} of 1000 cases differ, e.g.\n      " + "\n      ".join(diff[:3]))
    if bad:
        print("props: FAILED\n  " + "\n  ".join(bad))
        sys.exit(1)
    print(f"props: all {len(checks)} properties hold, and merge3 and lf agree with the web app on 1000 cases")


if __name__ == "__main__":
    main()
