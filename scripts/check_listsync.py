#!/usr/bin/env python3
"""Check texloom/listsync.py against scripts/listsync-vectors.json, the data that
web/scripts/check-listsync.mjs checks web/src/listcore.ts against. If the two clients ever
disagreed, lists would simply fail to open from the other one.

    python scripts/check_listsync.py            check
    python scripts/check_listsync.py --write    regenerate the file (only when the format changes on purpose)
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from texloom.listsync import ListKey, _canon, b64d, b64e, merge, password_secret  # noqa: E402

VECTORS = Path(__file__).with_name("listsync-vectors.json")
E = lambda f, n, at, w, **more: {"f": f, "n": n, "at": at, "w": w, **more}  # noqa: E731
doc = lambda projects=None, gone=None: {"v": 1, "projects": projects or {}, "gone": gone or {}}  # noqa: E731
MERGES = [
    ("newest wins", doc({"a": E("p=a&k=1", "Old", 5, False)}), doc({"a": E("p=a&k=2", "New", 7, False)})),
    ("an edit link is never downgraded", doc({"a": E("p=a&k=1&w=x", "A", 5, True)}), doc({"a": E("p=a&k=1", "A", 9, False)})),
    ("empty values don't replace real ones", doc({"a": E("f1", "Name", 5, True, nick="mine", t="Title")}), doc({"a": E("f2", "", 9, True, nick="")})),
    ("removal beats an older copy", doc({"a": E("f", "A", 5, True)}), doc(gone={"a": 6})),
    ("reopening beats an older removal", doc({"a": E("f", "A", 8, True)}), doc(gone={"a": 6})),
    ("a removal at the same moment wins", doc({"a": E("f", "A", 6, True)}), doc(gone={"a": 6})),
    ("the latest removal is kept", doc(gone={"a": 3, "b": 9}), doc(gone={"a": 7})),
    ("a tie is the same either way", doc({"a": E("f1", "Zoë", 5, True)}), doc({"a": E("f2", "Åsa", 5, True)})),
    ("non-ASCII and extras survive", doc({"é": E("f", "日本語 €", 1, False, by="José", t="“Quoted”")}), doc()),
]
PASSWORDS = [("ada@example.org", "correct horse battery"), ("  Ada@Example.ORG ", "correct horse battery"), ("josé", "pässwörd ﬁne long"), ("ｗｉｄｅ", "Ａ full-width pass")]
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
    print(*bad, sep="\n") if bad else print(f"listsync: {len(v['keys'])} keys, {len(v['passwords'])} passwords, {len(v['merges'])} merges match")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
