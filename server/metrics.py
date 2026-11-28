"""The relay's numbers for Prometheus, at /metrics (its text format, written by hand: a few counters need no library).

Counters count from the relay's start; gauges are read when asked. Grafana adds relays together. Nothing here names a
project, a code or an address: they'd say who works on what, and each would be a series of its own (the admin page has
that detail, behind its password). nginx doesn't pass /metrics on: it is reached on the relay's own address (on one
machine, localhost; on several, the private network), as /api/health is.
"""
from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from aiohttp import web

from texloom import __version__
from .policy import UNLIMITED
from .wire import APPS, version_tuple

if TYPE_CHECKING:
    from .relay import Relay

STORE_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)
HELP = {
    "texloom_connections_total": ("counter", "WebSocket connections accepted"),
    "texloom_connections_refused_total": ("counter", "WebSocket connections refused, by why"),
    "texloom_messages_total": ("counter", "messages from apps handled, by type"),
    "texloom_stores_total": ("counter", "changes stored"),
    "texloom_stores_refused_total": ("counter", "changes not stored (an allowance, the storage cap, a full disk)"),
    "texloom_store_seconds": ("histogram", "time to store a change (its transaction)"),
    "texloom_asked_total": ("counter", "connections asked to resync (after a break in the bus) or recheck (after the database failed over)"),
    "texloom_catchup_bytes_total": ("counter", "stored bytes sent to apps catching up"),
    "texloom_cut_off_total": ("counter", "connections cut off for not reading"),
    "texloom_database_failovers_total": ("counter", "database failovers this relay noticed"),
    "texloom_relay_info": ("gauge", "this relay: its number on the bus, and its version"),
    "texloom_healthy": ("gauge", "1 if this relay takes new connections (as /api/health)"),
    "texloom_bus_away": ("gauge", "1 while the bus is away"),
    "texloom_database_unavailable": ("gauge", "1 while the database is unavailable"),
    "texloom_connections": ("gauge", "WebSocket connections open"),
    "texloom_projects_open": ("gauge", "projects with a connection here"),
    "texloom_apps": ("gauge", "connections open, by app and version"),
    "texloom_memory_held_bytes": ("gauge", "bytes received and not yet finished with"),
    "texloom_memory_busy_bytes": ("gauge", "memory taken by big work at once (catch-ups, snapshots)"),
    "texloom_memory_limit_bytes": ("gauge", "the most big work may take at once (the memory setting)"),
    "texloom_out_waiting_bytes": ("gauge", "bytes waiting to go out to connections"),
    "texloom_storage_used_bytes": ("gauge", "bytes stored, every project together"),
    "texloom_storage_cap_bytes": ("gauge", "the storage cap (max_bytes; absent if unlimited)"),
    "texloom_disk_free_bytes": ("gauge", "free space on the data folder's disk"),
}
LABELLED = {"texloom_connections_refused_total", "texloom_messages_total", "texloom_asked_total"}  # (no series until counted)


def label(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class Metrics:
    def __init__(self) -> None:
        self.counts: dict[tuple[str, tuple], float] = defaultdict(float)
        self.store_buckets = [0] * len(STORE_BUCKETS)
        self.store_sum = 0.0
        self.store_count = 0

    def inc(self, name: str, n: float = 1, **labels: str) -> None:
        self.counts[(name, tuple(sorted(labels.items())))] += n

    def stored(self, seconds: float) -> None:
        self.store_sum += seconds
        self.store_count += 1
        for i, b in enumerate(STORE_BUCKETS):
            if seconds <= b:
                self.store_buckets[i] += 1

    def text(self, relay: Relay) -> str:
        why = "stopping" if relay.stopping else relay.unhealthy()
        apps: dict[tuple, int] = defaultdict(int)
        for conn in relay.conns.values():
            apps[(("app", conn.app), ("version", conn.version))] += 1
        store = relay.policy.store
        limits = relay.policy.limits
        gauges: dict[tuple[str, tuple], float] = {
            ("texloom_relay_info", (("relay", str(relay.number)), ("version", __version__))): 1,
            ("texloom_healthy", ()): why is None,
            ("texloom_bus_away", ()): relay._bus_away_since is not None,
            ("texloom_database_unavailable", ()): relay._db_trouble,
            ("texloom_connections", ()): len(relay.conns),
            ("texloom_projects_open", ()): sum(1 for r in relay.rooms.values() if r),
            ("texloom_memory_held_bytes", ()): relay.held,
            ("texloom_memory_busy_bytes", ()): relay.memory.used,
            ("texloom_memory_limit_bytes", ()): relay.memory.limit,
            ("texloom_out_waiting_bytes", ()): relay.out_waiting(),
            ("texloom_storage_used_bytes", ()): store.total,
            ("texloom_disk_free_bytes", ()): store.free_disk(),
            **{("texloom_apps", k): n for k, n in apps.items()},
        }
        if limits.max_bytes < UNLIMITED:
            gauges[("texloom_storage_cap_bytes", ())] = limits.max_bytes
        series: dict[str, list[str]] = defaultdict(list)
        for (name, labels), v in [*self.counts.items(), *gauges.items()]:
            tags = ",".join(f'{k}="{label(x)}"' for k, x in labels)
            n = f"{int(v)}" if float(v).is_integer() else f"{float(v):.6g}"
            series[name].append(f"{name}{{{tags}}} {n}" if tags else f"{name} {n}")
        h = "texloom_store_seconds"
        series[h] = [f'{h}_bucket{{le="{b:g}"}} {n}' for b, n in zip(STORE_BUCKETS, self.store_buckets)]
        series[h] += [f'{h}_bucket{{le="+Inf"}} {self.store_count}', f"{h}_sum {self.store_sum:g}", f"{h}_count {self.store_count}"]
        out = []
        for name in HELP:
            kind, about = HELP[name]
            if name in series or (kind == "counter" and name not in LABELLED):
                out += [f"# HELP {name} {about}", f"# TYPE {name} {kind}", *(series.get(name) or [f"{name} 0"])]
        return "\n".join(out) + "\n"

    async def page(self, relay: Relay, _request: web.Request) -> web.Response:
        return web.Response(text=self.text(relay), content_type="text/plain", charset="utf-8",
                            headers={"Cache-Control": "no-store"})


METRICS = Metrics()


def app_label(app: object) -> str:
    return app if isinstance(app, str) and app in APPS else "other"


def version_label(version: object) -> str:
    """A version as it is if it is one released so far (no later than this relay's), else "other": connections can't
    add series without end."""
    t = version_tuple(version)
    return version if t and t <= (version_tuple(__version__) or ()) else "other"
