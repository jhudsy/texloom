#!/usr/bin/env python3
"""Check texloom/listsync.py against scripts/listsync-vectors.json, the data that
web/scripts/check-listsync.mjs checks web/src/listcore.ts against. If the two clients ever
disagreed, lists would simply fail to open from the other one.

    uv run python scripts/check_listsync.py            check
    uv run python scripts/check_listsync.py --write    regenerate the file (only when the format changes on purpose)
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from texloom.listsync import (COMMON_PASSWORDS, ListKey, _canon, b64d, b64e, merge, merge_registry, merge_settings, merge_tags,  # noqa: E402
                              norm_username, padded, password_secret, password_strength, slot_hash, tag_colour, tidy_tags)

VECTORS = Path(__file__).with_name("listsync-vectors.json")
E = lambda f, n, at, w, **more: {"f": f, "n": n, "at": at, "w": w, **more}  # noqa: E731
doc = lambda projects=None, gone=None: {"v": 1, "projects": projects or {}, "gone": gone or {}}  # noqa: E731
MERGES = [
    ("a project's server goes with it, whichever copy is newer", doc({"a": E("p=a&k=1", "A", 5, True, s="https://other.example")}), doc({"a": E("p=a&k=1", "A", 9, True)})),
    ("projects on different servers side by side", doc({"a": E("p=a&k=1", "A", 5, True, s="https://other.example")}), doc({"b": E("p=b&k=2", "B", 6, False)})),
    ("newest wins", doc({"a": E("p=a&k=1", "Old", 5, False)}), doc({"a": E("p=a&k=2", "New", 7, False)})),
    ("an edit link is never downgraded", doc({"a": E("p=a&k=1&w=x", "A", 5, True)}), doc({"a": E("p=a&k=1", "A", 9, False)})),
    ("empty values don't replace real ones", doc({"a": E("f1", "Name", 5, True, nick="mine", t="Title")}), doc({"a": E("f2", "", 9, True, nick="")})),
    ("removal beats an older copy", doc({"a": E("f", "A", 5, True)}), doc(gone={"a": 6})),
    ("reopening beats an older removal", doc({"a": E("f", "A", 8, True)}), doc(gone={"a": 6})),
    ("a removal at the same moment wins", doc({"a": E("f", "A", 6, True)}), doc(gone={"a": 6})),
    ("the latest removal is kept", doc(gone={"a": 3, "b": 9}), doc(gone={"a": 7})),
    ("a tie is the same either way", doc({"a": E("f1", "Zoë", 5, True)}), doc({"a": E("f2", "Åsa", 5, True)})),
    ("the earliest creation and latest change are kept", doc({"a": E("f", "A", 9, True, c=3, m=4)}), doc({"a": E("f", "A", 5, True, c=2, m=8)})),
    ("a client without dates doesn't lose them", doc({"a": E("f", "A", 9, True)}), doc({"a": E("f", "A", 5, True, c=2, m=8)})),
    ("non-ASCII and extras survive", doc({"é": E("f", "日本語 €", 1, False, by="José", t="“Quoted”")}), doc()),
]
T = lambda tags, on=None: {"v": 1, "tags": tags, "on": on or {}}  # noqa: E731
TAG = lambda n, at, **more: {"n": n, "at": at, **more}  # noqa: E731
GONE = lambda at: {"n": "", "at": at, "gone": True}  # noqa: E731
TAG_COLOURS = [("aB3_-xyz", None), ("0000", None), ("Zz9-", None), ("q", None), ("Kf8wLm2P", None), ("aB3_-xyz", "pink"), ("aB3_-xyz", "chartreuse")]
TAG_MERGES = [
    ("the latest rename wins", T({"a": TAG("Old", 1)}), T({"a": TAG("New", 2)})),
    ("deleting beats a rename at the same moment", T({"a": TAG("X", 2)}), T({"a": GONE(2)})),
    ("assign and rename on two devices both survive", T({"a": TAG("New", 5)}, {"p": {"a": 1}}), T({"a": TAG("Old", 1)}, {"p": {"a": 9}})),
    ("taking a tag off later wins", T({"a": TAG("A", 1)}, {"p": {"a": 5}}), T({"a": TAG("A", 1)}, {"p": {"a": -7}})),
    ("assigning beats taking off at the same moment", T({}, {"p": {"a": 5}}), T({}, {"p": {"a": -5}})),
    ("a colour travels with the tag's latest change", T({"a": TAG("A", 1)}), T({"a": {**TAG("A", 2), "c": "red"}})),
    ("the same name made twice is one tag", T({"a": TAG("AAAI", 1)}, {"p": {"a": 3}}), T({"b": TAG("aaai ", 2)}, {"q": {"b": 4}})),
    ("names match across accents' case, spaces and ligatures", T({"a": TAG("Étude  ﬁnale", 1)}, {"p": {"a": 3}}), T({"b": TAG("étude finale", 2)}, {"p": {"b": -4}})),
    ("a tie is the same either way", T({"a": TAG("Zoë", 5)}), T({"a": TAG("Åsa", 5)})),
]
TAG_TIDIES = [
    ("old deletions, deleted tags and unlisted projects go",
     T({"a": TAG("A", 1), "b": GONE(32000000000000), "c": GONE(1)}, {"p": {"a": 5, "b": 32000000000000, "d": 32000000000000}, "q": {"a": 5}, "r": {"a": -1}}),
     ["p", "r"], 32000000001000),
]
SET = lambda **s: {"v": 1, "s": s}  # noqa: E731
SETTING_MERGES = [
    ("the latest code wins", SET(code={"v": "AAAA-BBBB", "at": 5}), SET(code={"v": "CCCC-DDDD", "at": 9})),
    ("a tie is the same either way", SET(code={"v": "AAAA", "at": 5}), SET(code={"v": "ZZZZ", "at": 5})),
    ("settings from either side are kept", SET(code={"v": "AAAA", "at": 5}), SET(other={"v": "x", "at": 1})),
    ("a cleared code is a setting too", SET(code={"v": "", "at": 9}), SET(code={"v": "AAAA", "at": 5})),
]
NOW = 1_800_000_000_000
# (what, a, b, now): a time more than ten minutes ahead of `now` counts as now + ten minutes
SETTING_MERGES_AT = [
    ("a setting dated far ahead counts as dated a little ahead, so a later change wins over it",
     SET(**{"copy:https://evil.example": {"v": "1", "at": NOW + 10**12}}), SET(**{"copy:https://evil.example": {"v": "", "at": NOW + 600_001}}), NOW + 600_001),
    ("two dated far ahead are both capped: the tie goes the same way on every device",
     SET(code={"v": "AAAA", "at": NOW + 10**12}), SET(code={"v": "ZZZZ", "at": NOW + 10**9}), NOW),
    ("one just within the skew keeps its time", SET(name={"v": "Ada", "at": NOW + 600_000}), SET(name={"v": "Bob", "at": NOW + 599_999}), NOW),
    ("now itself is fine", SET(name={"v": "Ada", "at": NOW}), SET(name={"v": "Bob", "at": NOW - 1}), NOW),
]
USERNAMES = ["ada@example.org", "  Ada@Example.ORG ", "\ufeffada\ufeff", "\x1cada\x1f", "\x85ada\u3000", "\u00a0ada\u2029", "\tAda\n",
             "ｗｉｄｅ", "İstanbul", "ΟΔΟΣ", "Straße", "\u200bada", "ada\u200b", "a d a"]
STRENGTHS = [("aaaaaaaaaaaa", ""), ("abcdefghijkl", ""), ("abcabcabcabc", ""), ("Password1234!", ""), ("P@ssw0rd2024!!", ""),
             ("qwertyuiop123", ""), ("ada@example.org!!", "ada@example.org"), ("short1!", ""), ("x7#Kp2!vQ9mL", ""),
             ("kqzmfwtrbnhx", ""), ("kqzmfwtrbnhx4", ""), ("correct horse battery staple", ""), ("correcthorsebatterystaple", ""),
             ("purple monkey dishwasher", ""), ("kofibu-tanemo-rilasu-pekodi", ""), ("9876543210ab", ""), ("aabbccddeeff", ""),
             ("Tr0ub4dor&3xyz", ""), ("日本語のパスワードです", ""), ("日本語のとても長いパスワードです", ""), ("𝔭𝔞𝔰𝔰𝔴𝔬𝔯𝔡𝔭𝔞𝔰𝔰", ""),
             ("ab12ab12ab12ab12", ""), ("monkeydragonshadowmaster", ""), ("Ｐａｓｓｗｏｒｄ１２３４５６", ""), ("jdk38-QQ!x z", "")]
PADDED = ["", '{"v":1}', "x" * 511, "x" * 512, "x" * 513, "é" * 300]
REG = lambda slots=None, gone=None: {"v": 1, "slots": slots or {}, **({"gone": gone} if gone else {})}  # noqa: E731
SLOT = lambda kind, at, seed="c2VlZA": {"kind": kind, "at": at, "seed": seed}  # noqa: E731
REGISTRY_MERGES = [
    ("ways in from either copy are kept", REG({"a": SLOT("passkey", 5)}), REG({"b": SLOT("password", 6)})),
    ("a removal beats an older copy", REG({"a": SLOT("password", 5)}), REG(gone={"a": 6})),
    ("a removal at the same moment wins", REG({"a": SLOT("password", 6)}), REG(gone={"a": 6})),
    ("a way in added again after its removal is back", REG({"a": SLOT("passkey", 9)}), REG(gone={"a": 6})),
    ("the latest removal is kept", REG(gone={"a": 3, "b": 9}), REG(gone={"a": 7})),
    ("the later record of one way in wins", REG({"a": SLOT("password", 5, "b2xk")}), REG({"a": SLOT("password", 8, "bmV3")})),
    ("nothing to merge", REG(), REG()),
    ("a way in's hash goes with it", REG({"a": {**SLOT("password", 5), "h": "aGFzaA"}}), REG({"b": SLOT("passkey", 6)})),
]
PASSWORDS = [("ada@example.org", "correct horse battery"), ("  Ada@Example.ORG ", "correct horse battery"), ("josé", "pässwörd ﬁne long"), ("ｗｉｄｅ", "Ａ full-width pass"),
             ("\ufeffada@example.org\x1c", "correct horse battery")]
SECRETS = [bytes(range(32)), bytes([0xff] * 32)]


def build() -> dict:
    keys = []
    for secret in SECRETS:
        key, data = ListKey(secret), b"encrypted bytes \x00\xff"
        plain = '{"v":1,"note":"déjà vu"}'
        keys.append({"secret": b64e(secret), "id": key.id, "seq": 7, "data": b64e(data), "signature": key.sign_put(7, data),
                     "ciphertext": b64e(key.encrypt(plain.encode())), "plain": plain,
                     "link": key.link("https://example.org/")})
    return {
        "keys": keys,
        "passwords": [{"username": u, "password": p, "secret": b64e(password_secret(u, p))} for u, p in PASSWORDS],
        "merges": [{"what": what, "a": a, "b": b, "merged": _canon(merge(a, b))} for what, a, b in MERGES],
        "tag_merges": [{"what": what, "a": a, "b": b, "merged": _canon(merge_tags(a, b))} for what, a, b in TAG_MERGES],
        "registry_merges": [{"what": what, "a": a, "b": b, "merged": _canon(merge_registry(a, b))} for what, a, b in REGISTRY_MERGES],
        "setting_merges": [*({"what": what, "a": a, "b": b, "merged": _canon(merge_settings(a, b))} for what, a, b in SETTING_MERGES),
                           *({"what": what, "a": a, "b": b, "now": now, "merged": _canon(merge_settings(a, b, now))} for what, a, b, now in SETTING_MERGES_AT)],
        "usernames": [{"username": u, "norm": norm_username(u)} for u in USERNAMES],
        "strengths": [{"password": pw, "username": u, "bits": password_strength(pw, u)[0], "problem": password_strength(pw, u)[1]} for pw, u in STRENGTHS],
        "common_passwords": list(COMMON_PASSWORDS),
        "padded": [{"json": j, "length": len(padded(j))} for j in PADDED],
        "slot_hashes": [{"data": b64e(d), "h": slot_hash(d)} for d in (b"", b"encrypted bytes \x00\xff", bytes(range(256)) * 3)],
        "tag_colours": [{"id": tid, "chosen": chosen, "colour": tag_colour(tid, chosen)} for tid, chosen in TAG_COLOURS],
        "tag_tidies": [{"what": what, "doc": doc, "listed": listed, "now": now, "tidied": _canon(tidy_tags(doc, set(listed), now))}
                       for what, doc, listed, now in TAG_TIDIES],
    }


def main() -> int:
    if "--write" in sys.argv:
        VECTORS.write_text(json.dumps(build(), ensure_ascii=False, indent=1) + "\n")
        print(f"wrote {VECTORS}")
        return 0
    v, bad = json.loads(VECTORS.read_text()), []
    def check(what, got, want):
        if got != want:
            bad.append(f"{what}: got {got!r}, want {want!r}")
    for k in v["keys"]:
        key = ListKey(b64d(k["secret"]))
        check("id", key.id, k["id"])
        check("signature", key.sign_put(k["seq"], b64d(k["data"])), k["signature"])
        check("decrypt", key.decrypt(b64d(k["ciphertext"])).decode(), k["plain"])
        check("link", key.link("https://example.org/"), k["link"])
    for p in v["passwords"]:
        check(f"password {p['username']!r}", b64e(password_secret(p["username"], p["password"])), p["secret"])
    for m in v["merges"]:
        check(m["what"], _canon(merge(m["a"], m["b"])), m["merged"])
        check(m["what"] + " (other way round)", _canon(merge(m["b"], m["a"])), m["merged"])
    for m in v["tag_merges"]:
        check(m["what"], _canon(merge_tags(m["a"], m["b"])), m["merged"])
        check(m["what"] + " (other way round)", _canon(merge_tags(m["b"], m["a"])), m["merged"])
    for m in v["registry_merges"]:
        check(m["what"], _canon(merge_registry(m["a"], m["b"])), m["merged"])
        check(m["what"] + " (other way round)", _canon(merge_registry(m["b"], m["a"])), m["merged"])
    for m in v["setting_merges"]:
        check(m["what"], _canon(merge_settings(m["a"], m["b"], m.get("now"))), m["merged"])
        check(m["what"] + " (other way round)", _canon(merge_settings(m["b"], m["a"], m.get("now"))), m["merged"])
    for u in v["usernames"]:
        check(f"username {u['username']!r}", norm_username(u["username"]), u["norm"])
    for p in v["strengths"]:
        check(f"strength of {p['password']!r}", [*password_strength(p["password"], p["username"])], [p["bits"], p["problem"]])
    check("common passwords", list(COMMON_PASSWORDS), v["common_passwords"])
    for p in v["padded"]:
        check(f"padded {p['json'][:20]!r}", len(padded(p["json"])), p["length"])
        if p["json"].startswith("{"):
            check(f"padded {p['json'][:20]!r} reads back", json.loads(padded(p["json"])), json.loads(p["json"]))
    for h in v["slot_hashes"]:
        check("slot hash", slot_hash(b64d(h["data"])), h["h"])
    for m in v["tag_colours"]:
        check(f"colour of {m['id']}", tag_colour(m["id"], m["chosen"]), m["colour"])
    for m in v["tag_tidies"]:
        check(m["what"], _canon(tidy_tags(m["doc"], set(m["listed"]), m["now"])), m["tidied"])
    print(*bad, sep="\n") if bad else print(f"listsync: {len(v['keys'])} keys, {len(v['passwords'])} passwords, {len(v['merges'])} merges, {len(v['registry_merges'])} ways-in merges, "
                                             f"{len(v['tag_merges'])} tag merges, {len(v['setting_merges'])} setting merges, {len(v['tag_tidies'])} tidies, "
                                             f"{len(v['usernames'])} usernames, {len(v['strengths'])} password strengths, {len(v['padded'])} paddings match")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
