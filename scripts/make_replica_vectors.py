#!/usr/bin/env python3
"""Writes scripts/replica-vectors.json: what a replica must do with the messages it receives, the
cases both scripts/check_replica.py (texloom/replica.py) and web/scripts/check-replica.mjs
(web/src/replica.ts) run their replica through.

Each case starts a fresh replica opened with one of the case file's links (the owner's, a
personal edit link, or a read-only one), sets where it caught up to (`since`), and gives it the
server's messages in order. After a step, and at the end, what can be seen must be as written:
the files, where it caught up to, revocations, deletion, refusals, what it sent. The expected
values are written here by hand, from what the protocol says (DESIGN.md), not taken from either
app, so the two are held to the same thing rather than to each other.

The messages are real: encrypted and signed with the links' keys. Run this again only when the
cases change (encryption is random, so every run writes different bytes):

    uv run python scripts/make_replica_vectors.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pycrdt import Doc, Map, Text

from texloom.crypto import Keys, b64e, revoke_msg
from texloom.packing import pack

REF = b"\x00texloom-ref\x00"

owner = Keys.generate()
_, _, member, _ = owner.personal("Ann")
_, _, rex, _ = owner.personal("Rex")  # revoked in some cases
reader = Keys(owner.project, owner.key, None)
stranger = Keys.generate()  # another project's owner: signs, but not for this project


# ---- the document's changes ------------------------------------------------------------------
def doc_with(**texts: str) -> Doc:
    d = Doc()
    files = d.get("files", type=Map)
    for name, text in texts.items():
        files[name] = Text(text)
    return d


a = doc_with(**{"main.tex": "Hello\n"})
U1 = a.get_update()  # main.tex = "Hello\n"
before = a.get_state()
text = a.get("files", type=Map)["main.tex"]
text.insert(len(text), "World\n")
U2 = a.get_update(before)  # then "Hello\nWorld\n" (needs U1)
U3 = doc_with(**{"b.tex": "B\n"}).get_update()  # b.tex, independent of the others
snap = Doc()
for u in (U1, U2, U3):
    snap.apply_update(u)
SNAP = snap.get_update()  # everything: a snapshot
has = Doc()
has.apply_update(U1)
HAS_U1 = has.get_state()  # the state of a copy that has U1


# ---- messages -------------------------------------------------------------------------------
def entry(q: int, update: bytes, by: Keys = owner, *, snap: bool = False, prefix: bytes | None = None, data: bytes | None = None) -> dict:
    d = owner.encrypt(update) if data is None else data
    p = prefix if prefix is not None else (f"snap:{q}:".encode() if snap else b"upd:")
    e = {"q": q, "d": b64e(d), "s": b64e(by.sign(p + d))}
    if snap:
        e["snap"] = True
    return e


def log(head: int, entries: list, **extra) -> dict:
    return {"t": "log", "head": head, "count": len(entries), "entries": entries, "mode": "server", **extra}


def up(e: dict) -> dict:
    return {"t": "up", **e}


def p2p(payload: dict, by: Keys | None = None, frm: int = 7) -> dict:
    data = owner.encrypt(json.dumps(payload).encode())
    m = {"t": "p2p", "d": b64e(data), "from": frm}
    if by is not None:
        m["s"] = b64e(by.sign(b"p2p:" + data))
    return m


def p2p_parts(payload: dict, by: Keys, n: int, frm: int = 7) -> list[dict]:
    """A peer-to-peer message sent in pieces (as a big one would be), signed once it's whole."""
    data = owner.encrypt(json.dumps(payload).encode())
    d, sig = b64e(data), b64e(by.sign(b"p2p:" + data))
    size = -(-len(d) // n)
    out = []
    for i in range(n):
        part = {"k": "part", "id": "0123456789abcdef", "i": i, "n": n, "c": d[i * size:(i + 1) * size]}
        if i == n - 1:
            part["s"] = sig
        out.append({"t": "p2p", "d": b64e(owner.encrypt(json.dumps(part).encode())), "from": frm})
    return out


def revocation(k: Keys, by: Keys = owner) -> dict:
    pub = k.member_pub
    assert pub
    return {"p": pub, "s": b64e(by.sign(revoke_msg(pub)))}


def upload(content: bytes) -> tuple[str, dict]:
    """A change kept as an upload: its id, and what the server answers for it."""
    bid = owner.blob_id(content)
    return bid, {"status": 200, "body": owner.encrypt(content).hex()}


def ref(bid: str) -> bytes:
    return REF + json.dumps({"b": bid}).encode()


bid_missing = owner.blob_id(b"an upload the server can't give just now")
HELLO = {"main.tex": "Hello\n"}
BOTH = {"main.tex": "Hello\nWorld\n"}
ALL = {"main.tex": "Hello\nWorld\n", "b.tex": "B\n"}

cases: list[dict] = []


def case(what: str, steps: list, expect: dict, *, as_: str = "reader", since: int = 0, uploads: dict | None = None, put: dict | None = None) -> None:
    cases.append({"what": what, "as": as_, "since": since, "uploads": uploads or {}, **({"put": put} if put else {}),
                  "steps": [{"msg": s} if "t" in s else s for s in steps], "expect": expect})


# -- catching up from the server's log
case("a log's changes are applied, and it has caught up to the head",
     [log(3, [entry(1, U1), entry(2, U2), entry(3, U3)])],
     {"files": ALL, "since": 3, "sent": ["p2p:sv1"]})
case("a change with a bad signature is left out, the rest applied",
     [log(2, [entry(1, U1, by=stranger), entry(2, U3)])],
     {"files": {"b.tex": "B\n"}, "since": 2})
case("a change signed as a snapshot is left out (the prefix is part of what's signed)",
     [log(2, [entry(1, U1, prefix=b"snap:1:"), entry(2, U3)])],
     {"files": {"b.tex": "B\n"}, "since": 2})
case("a snapshot is applied",
     [log(4, [entry(4, SNAP, snap=True)])],
     {"files": ALL, "since": 4})
case("a snapshot signed for another position is left out",
     [log(4, [{**entry(4, SNAP, prefix=b"snap:3:"), "snap": True}])],
     {"files": {}, "since": 4})
case("a change that doesn't decrypt is left out, the rest applied",
     [log(2, [entry(1, b"", data=os.urandom(60)), entry(2, U3)])],
     {"files": {"b.tex": "B\n"}, "since": 2})
case("a compressed change is applied",
     [log(1, [entry(1, pack(U1))])],
     {"files": HELLO, "since": 1})
case("something that isn't a change is left out, the rest applied",
     [log(2, [entry(1, b"not a change at all"), entry(2, U3)])],
     {"files": {"b.tex": "B\n"}, "since": 2})
case("a malformed entry is left out, the rest applied",
     [log(2, [{"q": 1, "d": entry(1, U1)["d"]}, entry(2, U3)])],
     {"files": {"b.tex": "B\n"}, "since": 2})
case("a change from a revoked personal link, stored before it was revoked, is applied",
     [log(1, [entry(1, U1, by=rex)], rv=[revocation(rex)])],
     {"files": HELLO, "since": 1})
case("a log in pages: caught up only after the last",
     [{"msg": log(2, [entry(1, U1)], more=True), "expect": {"files": HELLO, "since": 0}},
      {"t": "logmore", "entries": [entry(2, U2)], "more": False}],
     {"files": BOTH, "since": 2, "sent": ["p2p:sv1"]})
case("a page that comes without a log first is ignored",
     [{"t": "logmore", "entries": [entry(1, U1)], "more": False}],
     {"files": {}, "since": 0, "sent": []})

# -- changes arriving live, and where it has caught up to
case("changes out of order: caught up only as far as nothing is missing",
     [log(0, []),
      {"msg": up(entry(2, U3)), "expect": {"files": {"b.tex": "B\n"}, "since": 0}},
      up(entry(1, U1))],
     {"files": {"main.tex": "Hello\n", "b.tex": "B\n"}, "since": 2})
case("an acknowledgement counts as received",
     [log(0, []), {"t": "ack", "id": "x", "q": 1}],
     {"since": 1})
case("the server lost its copy (its log ends before ours): caught up to its head",
     [log(2, [entry(1, U1), entry(2, U3)])],
     {"files": {"main.tex": "Hello\n", "b.tex": "B\n"}, "since": 2}, since=5)
case("the server lost its copy (we're at or below what it dropped): caught up to its head",
     [log(5, [entry(5, U3)], floor=4)],
     {"files": {"b.tex": "B\n"}, "since": 5}, since=3)
case("an editor whose server lost the copy sends the whole document again",
     [log(0, [])],
     {"since": 0, "sent": ["p2p:sv1", "up"]}, as_="member", since=4)
case("a project that's only relayed: nothing is lost, and it stays caught up where it was",
     [{**log(0, []), "mode": "relay"}],
     {"since": 5, "serverMode": "relay"}, since=5)

# -- snapshots: an editor replaces a long log with one
LONG = 300  # entries in the log before an editor uploads a snapshot (COMPACT_AFTER in both apps)
case("an editor replaces a long log with a snapshot",
     [{**log(LONG, [entry(LONG, U1)]), "count": LONG}],
     {"files": HELLO, "since": LONG, "sent": ["p2p:sv1", "snap"]}, as_="member", since=LONG - 1)
case("a read-only link doesn't make snapshots",
     [{**log(LONG, [entry(LONG, U1)]), "count": LONG}],
     {"since": LONG, "sent": ["p2p:sv1"]}, since=LONG - 1)
case("a short log isn't replaced",
     [{**log(LONG, [entry(LONG, U1)]), "count": LONG - 1}],
     {"since": LONG, "sent": ["p2p:sv1"]}, as_="member", since=LONG - 1)
case("a project that's only relayed has no log to replace",
     [{**log(0, [entry(1, U1)]), "count": LONG, "mode": "relay"}],
     {"sent": ["p2p:sv1"]}, as_="member")
case("no snapshot while a change's upload is missing (it would leave the change out)",
     [{**log(2, [entry(1, U3), entry(2, ref(bid_missing))]), "count": LONG}],
     {"since": 1, "sent": ["p2p:sv1"]}, as_="member", uploads={bid_missing: {"status": 503}})

# -- changes kept as uploads
bid1, ok1 = upload(U1)
case("a change kept as an upload is fetched and applied",
     [log(1, [entry(1, ref(bid1))])],
     {"files": HELLO, "since": 1}, uploads={bid1: ok1})
case("a change whose upload is gone: catch up again from the start",
     [log(2, [entry(1, ref(bid1)), entry(2, U3)])],
     {"since": 0, "closed": True}, uploads={bid1: {"status": 404}})
case("a change whose upload can't be fetched just now: caught up only to just before it",
     [log(3, [entry(1, U3), entry(2, ref(bid1)), entry(3, U1)])],
     {"files": {"main.tex": "Hello\n", "b.tex": "B\n"}, "since": 1}, uploads={bid1: {"status": 503}})
bid2, _ = upload(U2)
case("an upload that isn't what the change names counts as gone",
     [log(1, [entry(1, ref(bid2))])],
     {"since": 0, "closed": True}, uploads={bid2: ok1})

# -- revocations, deletion, refusals
case("a revocation signed by the owner is kept",
     [log(0, [], rv=[revocation(rex)])],
     {"revoked": [rex.member_pub]})
case("a revocation arriving on its own is kept",
     [log(0, []), {"t": "rv", **revocation(rex)}],
     {"revoked": [rex.member_pub]})
case("a revocation not signed by the owner is ignored",
     [log(0, [], rv=[revocation(rex, by=member)]), {"t": "rv", **revocation(rex, by=stranger)}],
     {"revoked": []})
case("a deletion signed by the owner: the project is deleted here",
     [log(0, []), {"t": "deleted", "ts": 1700000000, "at": 1700000000000, "s": b64e(owner.sign(b"delete:1700000000"))}],
     {"deleted": True})
case("a deletion not signed by the owner is ignored",
     [log(0, []), {"t": "deleted", "ts": 1700000000, "at": 1700000000000, "s": b64e(member.sign(b"delete:1700000000"))}],
     {"deleted": False})
case("a server that turns away old apps: refused, with the version it needs",
     [{"t": "err", "code": "update-needed", "denied": True, "min": "9.9.9", "m": "update needed"}],
     {"denied": "update-needed", "min": "9.9.9"})
case("a log clears an earlier refusal",
     [{"t": "err", "code": "needs-code", "denied": True, "m": "needs a code"}, log(0, [])],
     {"denied": None})
case("the server not storing changes is remembered, and cleared once one is stored",
     [log(0, []),
      {"msg": {"t": "err", "code": "quota-full", "id": "x", "m": "full"}, "expect": {"storeProblem": "quota-full"}},
      {"t": "ack", "id": "y", "q": 1}],
     {"storeProblem": None, "since": 1})
case("the server saying it only relays the project clears a storage problem",
     [log(0, []), {"t": "err", "code": "needs-code", "id": "x", "m": "needs a code"}, {"t": "mode", "m": "relay"}],
     {"storeProblem": None, "serverMode": "relay"})
case("a project that's only relayed needs no creation code",
     [{**log(0, []), "mode": "relay"}, {"t": "err", "code": "needs-code", "id": "x", "m": "needs a code"}],
     {"storeProblem": None, "serverMode": "relay"})

# -- peer to peer
case("a signed change from a peer is applied once caught up",
     [log(0, []), p2p({"k": "upd", "u": b64e(U1)}, by=member)],
     {"files": HELLO})
case("a compressed change from a peer is applied",
     [log(0, []), p2p({"k": "upd", "u": b64e(pack(U1))}, by=member)],
     {"files": HELLO})
case("a change from a peer before catching up is ignored (revocations aren't in yet)",
     [p2p({"k": "upd", "u": b64e(U1)}, by=member)],
     {"files": {}})
case("an unsigned change from a peer is ignored",
     [log(0, []), p2p({"k": "upd", "u": b64e(U1)})],
     {"files": {}})
case("a change from a peer signed by a revoked personal link is ignored",
     [log(0, [], rv=[revocation(rex)]), p2p({"k": "upd", "u": b64e(U1)}, by=rex)],
     {"files": {}})
case("a change from a peer signed by another project's key is ignored",
     [log(0, []), p2p({"k": "upd", "u": b64e(U1)}, by=stranger)],
     {"files": {}})
case("a change from a peer in pieces is put together and applied",
     [log(0, []), *p2p_parts({"k": "upd", "u": b64e(U1)}, by=member, n=3)],
     {"files": HELLO})
case("a peer's message that doesn't decrypt is ignored",
     [log(0, []), {"t": "p2p", "d": b64e(os.urandom(80)), "from": 7}],
     {"files": {}, "sent": ["p2p:sv1"]})
case("an editor answers a peer that just connected: its state, and what the peer lacks",
     [log(1, [entry(1, U1)]), p2p({"k": "sv1", "sv": b64e(b"\x00")})],
     {"sent": ["p2p:sv1", "p2p:sv2", "p2p:upd"]}, as_="member")
case("a read-only link sends a peer its state, but no changes (it can't sign them)",
     [log(1, [entry(1, U1)]), p2p({"k": "sv1", "sv": b64e(b"\x00")})],
     {"sent": ["p2p:sv1", "p2p:sv2"]})
case("a peer that already has everything is sent nothing more",
     [log(1, [entry(1, U1)]), p2p({"k": "sv2", "sv": b64e(HAS_U1)})],
     {"sent": ["p2p:sv1"]}, as_="member")

# -- edits made here: out to the server, kept until it has them
def edit(file: str, text: str, **more) -> dict:
    return {"edit": {"file": file, "append": text}, **more}


case("an edit is sent, and kept until the server acknowledges it",
     [log(0, []), edit("main.tex", "Hi\n", expect={"pending": 1, "sent": ["p2p:sv1", "up"], "sentFiles": {"main.tex": "Hi\n"}}),
      {"ackLast": 1}],
     {"pending": 0, "since": 1}, as_="member")
case("an edit made while catching up waits, and goes once caught up",
     [edit("main.tex", "Hi\n", expect={"pending": 1, "sent": []}), log(0, [])],
     {"pending": 1, "sent": ["p2p:sv1", "up"], "sentFiles": {"main.tex": "Hi\n"}}, as_="member")
case("several edits waiting go as one change",
     [edit("main.tex", "One\n"), edit("main.tex", "Two\n"), edit("b.tex", "B\n"), log(0, [])],
     {"sent": ["p2p:sv1", "up"], "sentFiles": {"main.tex": "One\nTwo\n", "b.tex": "B\n"}}, as_="owner")
case("an edit not acknowledged is sent again after reconnecting",
     [log(0, []), edit("main.tex", "Hi\n"), {"reconnect": True}, log(0, [])],
     {"pending": 1, "sent": ["p2p:sv1", "p2p:sv1", "up", "up", "hello"], "sentFiles": {"main.tex": "Hi\n"}}, as_="member")
case("an acknowledged edit isn't sent again after reconnecting",
     [log(0, []), edit("main.tex", "Hi\n"), {"ackLast": 1}, {"reconnect": True}, log(1, [])],
     {"pending": 0, "since": 1, "sent": ["p2p:sv1", "p2p:sv1", "up", "hello"]}, as_="member")
case("an edit the server won't store is kept, and the reason shown",
     [log(0, []), edit("main.tex", "Hi\n"), {"refuseLast": "quota-full"}],
     {"pending": 1, "storeProblem": "quota-full"}, as_="member")
case("an edit and a change from someone else both end up in the document",
     [log(0, []), edit("main.tex", "Mine\n"), up(entry(1, U3))],
     {"files": {"main.tex": "Mine\n", "b.tex": "B\n"}, "sentFiles": {"main.tex": "Mine\n"}}, as_="member")

# -- files: uploaded signed, fetched and checked, asked of peers
FILE = bytes(range(256)) * 3
FID = owner.blob_id(FILE)
OTHER = b"some other content"
OID = owner.blob_id(OTHER)
case("a file added here is uploaded, signed, as a project file",
     [log(0, []), {"putFile": {"hex": FILE.hex()}}],
     {"uploaded": [{"id": FID, "output": False, "ok": True}], "pendingFiles": 0}, as_="member")
case("compiled output is uploaded marked as output",
     [log(0, []), {"putFile": {"hex": FILE.hex(), "kind": "output"}}],
     {"uploaded": [{"id": FID, "output": True, "ok": True}], "pendingFiles": 0}, as_="member")
case("a file added while catching up is uploaded once caught up",
     [{"putFile": {"hex": FILE.hex()}}, log(0, [])],
     {"uploaded": [{"id": FID, "output": False, "ok": True}], "pendingFiles": 0}, as_="member")
case("a file the server won't store is kept to try again, and the reason shown",
     [log(0, []), {"putFile": {"hex": FILE.hex()}}],
     {"pendingFiles": 1, "storeProblem": "quota-full"}, as_="member", put={"status": 507, "code": "quota-full"})
case("a file the server can't take just now is kept to try again",
     [log(0, []), {"putFile": {"hex": FILE.hex()}}],
     {"pendingFiles": 1, "storeProblem": None}, as_="member", put={"status": 503})
case("a file stored again clears a storage problem",
     [log(0, []), {"t": "err", "code": "quota-full", "id": "x", "m": "full"}, {"putFile": {"hex": FILE.hex()}}],
     {"pendingFiles": 0, "storeProblem": None}, as_="member")
case("a project that's only relayed uploads no files (they go peer to peer)",
     [{**log(0, []), "mode": "relay"}, {"putFile": {"hex": FILE.hex()}}],
     {"uploaded": []}, as_="member")
case("a read-only link uploads nothing",
     [log(0, []), {"putFile": {"hex": FILE.hex()}}],
     {"uploaded": []})
case("a file is fetched from the server",
     [log(0, []), {"getFile": FID}],
     {"got": FILE.hex()}, uploads={FID: {"status": 200, "body": owner.encrypt(FILE).hex()}})
case("a file the server swapped for other content isn't taken: peers are asked instead",
     [log(0, []), {"getFile": FID}],
     {"got": "waiting", "sent": ["p2p:sv1", "p2p:breq"]}, uploads={FID: {"status": 200, "body": owner.encrypt(OTHER).hex()}})
case("a file the server doesn't have is asked of peers, and a peer's answer taken",
     [log(0, []), {"getFile": FID, "expect": {"got": "waiting", "sent": ["p2p:sv1", "p2p:breq"]}},
      p2p({"k": "blob", "id": FID, "b": b64e(FILE)})],
     {"got": FILE.hex()})
case("a peer's answer with other content than asked for is ignored",
     [log(0, []), {"getFile": FID}, p2p({"k": "blob", "id": FID, "b": b64e(OTHER)})],
     {"got": "waiting"})
case("a file kept here is given to a peer that asks for it",
     [log(0, []), {"putFile": {"hex": FILE.hex()}}, p2p({"k": "breq", "id": FID})],
     {"sent": ["p2p:sv1", "p2p:blob"]}, as_="member", put={"status": 503})
case("a peer asking for a file not kept here gets nothing",
     [log(0, []), p2p({"k": "breq", "id": OID})],
     {"sent": ["p2p:sv1"]}, as_="member")

out = {
    "about": "Made by scripts/make_replica_vectors.py; checked by scripts/check_replica.py and web/scripts/check-replica.mjs.",
    "links": {"owner": owner.fragment(), "member": member.fragment(), "reader": reader.fragment(False)},
    "cases": cases,
}
(ROOT / "scripts/replica-vectors.json").write_text(json.dumps(out, indent=1) + "\n")
print(f"{len(cases)} cases")
