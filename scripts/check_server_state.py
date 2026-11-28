#!/usr/bin/env python3
"""What each relay keeps in its own memory, and which addresses it serves: every piece is listed
here with what it would mean for several relays at once (issue 12), so that a new one is a
decision, not an accident. See "Keeping several servers possible" in SCALING.md.

It reads server/*.py and fails on:
- an attribute set on `self` in a server class (or a mutable value at module level, other than a
  constant in capitals) not listed in STATE below, or listed but gone;
- a route added in make_app (server/app.py) not listed in ROUTES, or listed but gone; and a
  route listed as "project" without {project} in its path.

The kinds of state:
  setting      the same on every relay: settings, handles to the database or file store
  connection   belongs to connections on this relay
  per-server   meant to be each relay's own (its memory, its caches of public files, rate limits
               that then add up across relays)
  project      about one project: right while every connection to a project reaches the same
               relay (routing by project id). Anything another relay changes about the project
               (maintenance, a purge) must tell this one to drop its copy
  shared       totals across projects (an owner's usage, the server's total): with several relays
               they move into the database
  maintenance  background work: with several relays, only one runs it

    uv run python scripts/check_server_state.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
KINDS = {"setting", "connection", "per-server", "project", "shared", "maintenance"}

# "Class.attribute" (or "Class.*" for every attribute of a class, "module:NAME" for module level)
STATE = {
    # settings and handles
    "Relay.policy": "setting", "Relay.bus": "setting", "Relay.number": "per-server", "Endpoints.*": "setting",
    "Policy.*": "setting",  # the limits, the codes file (every relay needs the same one), handles to the store
    "Store.db": "setting", "Store.files": "setting", "Store.dir": "setting",
    "Lists.*": "setting", "Admin.*": "setting", "CodeBook.*": "setting",
    "Database.*": "setting", "PostgreSQL.*": "setting", "SQLite.*": "setting", "_PgCalls.*": "setting",
    "_SqliteCalls.*": "setting", "FolderFiles.*": "setting", "S3Files.*": "setting",
    # the bus (bus.py): connections to it, its subscriptions (this relay's), and the in-process one's state,
    # which with several relays is the Redis server's
    "Bus.*": "setting", "RespBus.*": "per-server", "_Conn.*": "per-server", "MemoryBus.*": "per-server",
    "Hub.*": "per-server", "module:bus._hubs": "per-server",  # (shared buses in one process: for checks)
    # passing messages on from the bus, in order per project; its subscriptions; its heartbeat, and the relays it found gone
    "Relay._inbox": "connection", "Relay._draining": "connection", "Relay._subscribed": "connection",
    "Relay._beating": "per-server", "Relay._gone": "per-server", "Relay._trouble_at": "per-server",
    # the changes passed on to this relay's connections, per stored project open here (gap tracking), and the head check
    "Relay.tracks": "connection", "Track.*": "connection", "Relay._checking": "per-server",
    # the database's timeline as this relay last saw it, and whether it was unavailable lately (a failover); the store's
    # callback to the relay when its connection to the database is back
    "Relay.timeline": "per-server", "Relay._db_trouble": "per-server", "Store.on_back": "setting",
    "Relay._bus_away_since": "per-server", "Relay._pinging": "per-server",  # (its health; pinging its connections)
    # connections on this relay
    "Relay.rooms": "connection", "Relay.conns": "connection", "Relay._next_id": "connection",
    "Conn.*": "connection", "Metered.*": "connection",
    "Relay.per_address": "connection", "Relay.per_project": "connection",  # connections open here, for their limits
    "Relay.room_pace": "connection", "Pace.*": "connection",  # the pace of what's relayed unsigned, per connection and room
    "Capped.*": "connection",  # one arXiv import's unpacking
    "Policy.busy": "connection",  # whether a project is open: asks this relay's connections only
    # each relay's own
    "Relay.memory": "per-server", "Relay.address_memory": "per-server", "Relay.held": "per-server", "Relay.stopping": "per-server", "Budget.*": "per-server",
    "Store.node": "per-server", "Store._refreshing": "per-server",  # its name in what it tells the others; looking again at what they changed
    "Store.meta_tokens": "per-server", "Store.meta_at": "per-server",  # the budget for rows that store nothing
    "ArxivFetcher.*": "per-server",  # arXiv's rate limits, per relay
    "TexLive.*": "per-server",  # a cache of public TeX Live files
    # per project
    "Store.usage": "project", "Store.owners": "project", "Store.seen": "project", "Store.evicted": "project", "Store.pending": "project",
    "Store._facts": "project", "Store._revoked": "project", "Recent.*": "project",
    # this relay's tasks queue for a project here; the transaction's lock in the database (Store.locked) does the rest
    "Store._locks": "per-server",
    "Tx.*": "setting",  # a transaction's calls, and what to do once it commits
    # across projects: shown from memory; allowances are checked against the database (Store.owner_total),
    # and the server's total is this relay's count, read again from the database (TOTALS_EVERY)
    "Store.total": "per-server", "Store.owner_bytes": "per-server", "Store.list_bytes": "per-server",
    "Store._totals": "per-server",
    # background work
    "Store._reaper": "maintenance",
}

# Every address make_app serves: "project" ones carry the project in the path, so a front end can
# send them to the project's relay; "any" ones work on any relay (the database, or public files).
ROUTES = {
    "GET /ws/{project}": "project", "PUT /blob/{project}/{id}": "project", "GET /blob/{project}/{id}": "project",
    "DELETE /blob/{project}/{id}": "project",
    "GET /api/info": "any", "GET /api/health": "any", "POST /api/code": "any", "GET /api/arxiv/{id}": "any", "GET /admin": "any",
    "GET /api/list/{id}": "any", "PUT /api/list/{id}": "any", "DELETE /api/list/{id}": "any",  # compare-and-swap in the database
    "GET /texlive/{year}/{fmt}/{name}": "any", "STATIC /core": "any", "GET /": "any", "STATIC /": "any",
}

MUTABLE_CALLS = {"dict", "list", "set", "defaultdict", "OrderedDict", "deque"}


def found_state() -> set[str]:
    out = set()
    for f in sorted(SERVER.glob("*.py")):
        tree = ast.parse(f.read_text())
        for node in tree.body:  # module level: mutable containers
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                mutable = isinstance(value, (ast.Dict, ast.List, ast.Set)) or (
                    isinstance(value, ast.Call) and getattr(value.func, "id", getattr(value.func, "attr", "")) in MUTABLE_CALLS)
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if mutable and isinstance(t, ast.Name) and not t.id.isupper():  # (capitals: a constant)
                        out.add(f"module:{f.stem}.{t.id}")
        for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            for n in ast.walk(cls):
                targets = n.targets if isinstance(n, ast.Assign) else [n.target] if isinstance(n, (ast.AnnAssign, ast.AugAssign)) else []
                for t in targets:
                    for x in [t, *(t.elts if isinstance(t, ast.Tuple) else [])]:
                        if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "self":
                            out.add(f"{cls.name}.{x.attr}")
    return out


def found_routes() -> set[str]:
    tree = ast.parse((SERVER / "app.py").read_text())
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr.startswith("add_") \
                and n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):
            verb = n.func.attr.removeprefix("add_").upper()
            out.add(f"{verb} {n.args[0].value}")
    return out


def main() -> int:
    problems = []
    for key, kind in STATE.items():
        if kind not in KINDS:
            problems.append(f"{key}: unknown kind {kind!r}")
    state = found_state()
    covered = set()
    for name in state:
        cls = name.split(".")[0]
        if name in STATE:
            covered.add(name)
        elif f"{cls}.*" in STATE:
            covered.add(f"{cls}.*")
        else:
            problems.append(f"{name}: new state kept in a relay's memory. Add it to STATE in this script with its kind "
                            "(see SCALING.md, \"Keeping several servers possible\")")
    problems += [f"{k}: listed in STATE but not in the code any more" for k in STATE if k not in covered]
    routes = found_routes()
    problems += [f"{r}: a new address. Add it to ROUTES in this script: \"project\" (with {{project}} in its path) or \"any\""
                 for r in sorted(routes - ROUTES.keys())]
    problems += [f"{r}: listed in ROUTES but not served any more" for r in sorted(ROUTES.keys() - routes)]
    problems += [f"{r}: a project's address needs {{project}} in its path, so a front end can route it"
                 for r, kind in ROUTES.items() if kind == "project" and "{project}" not in r]
    if problems:
        print("server state:", *problems, sep="\n  ")
        return 1
    by_kind = {k: sum(1 for v in STATE.values() if v == k) for k in sorted(KINDS)}
    print(f"server state: {len(state)} attributes, {len(routes)} addresses, all accounted for "
          f"({', '.join(f'{n} {k}' for k, n in by_kind.items())})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
