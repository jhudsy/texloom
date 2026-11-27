# Relays model

A TLA+ model of stateless relays (FUTURE_ARCHITECTURE.md): two relays sharing one database and a bus,
two apps connected to either, one stored project. Relays die between any two steps of storing a change,
breaks in a relay's link to the bus lose what was on its way, and the database fails over to a standby
that may lack its last changes, after which change numbers are used again.

    uv run python scripts/check_model.py relays        # about a minute; needs Java

`Relays.tla` is the model. It checks two things:

- **NoLostAck**: an app that is connected, has heard everything its relay sent it and has nothing left to
  send has every change it made in the database. An app told "stored" by a database that then failed over
  must find out and send the change again.
- **Converged**: once everything is quiet (every app connected, nothing in flight, every relay caught up
  with the database), every app has every change. No relay's gap tracking may leave one out.

Each `.cfg` is one question. Its first line says whether it is expected to hold or to be violated: a
violated one shows that a part of the design is needed.

| configuration | what it checks | result |
|---|---|---|
| `failover` | the design, with a database failover | holds (simulated) |
| `crash` | the design, with a relay dying between any two steps | holds |
| `break` | the design, with a break in a relay's link to the bus | holds |
| `together` | the design, with a relay dying and a failover in one run | holds (simulated) |
| `ack-first` | the old order (acknowledge, then pass on), with a relay dying | holds: see below |
| `no-recheck` | without "recheck" after a failover | violated: NoLostAck |
| `no-store-again` | without storing again a change stored before a failover | violated: NoLostAck |
| `no-reset` | a relay keeping its `upto` from before a failover | violated: Converged |
| `no-timeline-on-bus` | changes on the bus without the timeline that numbered them | violated: Converged |

`failover` and `together` have hundreds of millions of states (tens of GB of the checker's state files), so
they are checked by random simulation: 100,000 runs of up to 40 steps each. The others are searched
completely within the model's bounds (two apps, two relays, two changes, at most one of each failure, and
at most three messages waiting for an app and two for a relay). Simulation finds the hole each `no-…`
configuration opens within seconds.

## What it says about the design

The model found two holes in the plan as first written (FUTURE_ARCHITECTURE.md, "Decided"), both
around a database failover, where change numbers are used again:

1. **A change stored before a failover is stored again before it is acknowledged.** The relay stores a
   change, the database fails over and loses it, the relay notices and sends its apps "recheck", and only
   then acknowledges the change. The app rechecked before it knew of the change, so found nothing wrong,
   and the acknowledgement makes it forget the change. A second "recheck" after the acknowledgement isn't
   enough: by then the change's number may belong to another app's change, so the app's check finds that
   one and is satisfied. So a relay that has noticed a failover runs the transaction again for every change
   it stored before it (found, or added) and acknowledges it with its new number (`no-store-again`).
2. **Changes on the bus carry the timeline that numbered them.** A relay ignores, for its `upto`, numbers
   from an older timeline, which now belong to other changes (`no-timeline-on-bus`). And a relay that hears
   of a newer timeline that way deals with the failover (recheck, storing again) before it passes anything
   on: otherwise it can pass on a change numbered by the new timeline and then acknowledge a lost one
   under the same number.

Two results confirm the plan:

- **"recheck" is needed** (`no-recheck`), and so is starting `upto` afresh from the new head after a
  failover (`no-reset`).
- **The old order (acknowledge, then pass on) loses nothing in a stored project** (`ack-first` holds): a
  change a relay acknowledged but died before passing on reaches the others through the head check. What
  the new order buys is time, which this model doesn't measure: without it, others wait for the next head
  check (a minute) rather than a resend (seconds). In a sync-only project there is no database to check,
  and the order is what guarantees delivery. That isn't modelled here.

## What it assumes (check these on the real thing)

- The database's transactions are atomic and number changes in order. A relay notices a failover (the
  timeline going up) before it next uses the database.
- An app sends each change once a connection: again only on a new connection, or after a catch-up that
  finds the server's log isn't the one it saw.
- A relay's head check happens eventually: periodically, and after every break in its link to the bus.
- Not modelled: sync-only projects, peer-to-peer messages, presence, and soft state in the bus (connection
  lists, relay numbers). Those may be lost by design.
