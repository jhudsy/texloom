---------------------------- MODULE Failover ----------------------------
(* Automatic failover of the relay's database: a lease in the bucket, a standby, and an app.
   See SCALING.md ("Switching over"). Two servers, s1 the primary at the start and s2 its standby.
   The questions this model answers, before any failover code is written:

   - SingleWriter: are there ever two servers that may each write at once (by their own clocks)?
   - NoLostAck: when an app is connected to the server that holds the lease, and has nothing left
     to send, does that server have every change the app ever made? (An app that was told "stored"
     by a server that is then replaced must not lose the change.)

   The lease is a version number in the bucket, changed only by a conditional write ("if the version
   is still v"), which the store does atomically. The primary renews it, so its version moves.
   The standby never compares clocks with anyone: it watches the version and counts time on its own
   clock since it last saw the version change. After TakeoverWait without a change it claims the
   lease with a conditional write on the version it saw (so a renewal in between makes the claim
   fail) and becomes the primary. A primary that has not renewed for TTL on its own clock stops
   itself (fenced), and one whose renewal finds the version changed learns it lost.

   Time is real ticks (`now`). Each tick every server's own timer advances by 1..MaxRate, which is
   how clocks that run at different speeds are modelled. No clock is ever compared with another: a
   timer is only "how long on my own clock since my last renewal" (a primary) or "since I last saw
   the version change" (a standby), and it stops counting at the limit it is compared with.
   A server can be frozen (a suspended virtual machine, a long stall): it does nothing. If
   FreezeStopsClock, its timer stops too, which is the dangerous case: on waking it thinks little
   time has passed.

   The app keeps what it made until a server stores it. It also remembers the last change it was
   told about as (number, id). On connecting, the hello check compares that with the server's log:
   if the server's log has not got that same change at that number, the app sends everything again
   (HelloChecks; this is the check the real apps already make, scripts/check_restore.py).
   Replication is asynchronous: the standby's log is a prefix of the primary's. *)
EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS TTL, TakeoverWait, MaxRate, MaxTime, MaxChanges, FreezeStopsClock, HelloChecks

S1 == "s1"
S2 == "s2"
Servers == {S1, S2}
Other(s) == IF s = S1 THEN S2 ELSE S1
NoSeen == [seq |-> 0, id |-> 0]
Range(f) == {f[i] : i \in 1..Len(f)}
Cap == IF TTL > TakeoverWait THEN TTL ELSE TakeoverWait
Min(a, b) == IF a < b THEN a ELSE b

VARIABLES now, frozen,
          timer,        \* on its own clock: a primary's time since it claimed or renewed, a standby's
                        \* since it saw the version change (it counts no further than Cap)
          lease,        \* in the bucket: [ver, holder]
          role,         \* "primary", "standby" or "fenced" (stopped itself, or found it had lost)
          myver,        \* the version the primary last wrote
          seenVer,      \* the version the standby last saw
          log,          \* each server's log: a sequence of change ids
          following,    \* the standby is still replicating from the primary
          created,      \* every change the app has made
          outbox,       \* the ones no server has told it are stored
          conn,         \* the server the app is connected to, or "none"
          lastSeen      \* the last change a server told it about: [seq, id]

vars == <<now, frozen, timer, lease, role, myver, seenVer, log, following,
          created, outbox, conn, lastSeen>>

TypeOK == /\ now \in 0..MaxTime
          /\ lease.holder \in Servers
          /\ \A s \in Servers : role[s] \in {"primary", "standby", "fenced"}
          /\ outbox \subseteq created
          /\ conn \in Servers \cup {"none"}

Init == /\ now = 0
        /\ frozen = [s \in Servers |-> FALSE]
        /\ timer = [s \in Servers |-> 0]
        /\ lease = [ver |-> 1, holder |-> S1]
        /\ role = [s \in Servers |-> IF s = S1 THEN "primary" ELSE "standby"]
        /\ myver = [s \in Servers |-> IF s = S1 THEN 1 ELSE 0]
        /\ seenVer = [s \in Servers |-> 1]
        /\ log = [s \in Servers |-> <<>>]
        /\ following = [s \in Servers |-> s = S2]
        /\ created = {}
        /\ outbox = {}
        /\ conn = "none"
        /\ lastSeen = NoSeen

\* a primary may write while it last renewed less than TTL ago by its own clock
CanWrite(s) == role[s] = "primary" /\ timer[s] < TTL

Tick ==
    /\ now < MaxTime
    /\ now' = now + 1
    /\ \E rate \in [Servers -> 1..MaxRate] :
         timer' = [s \in Servers |->
                     IF frozen[s] /\ FreezeStopsClock THEN timer[s] ELSE Min(timer[s] + rate[s], Cap)]
    /\ UNCHANGED <<frozen, lease, role, myver, seenVer, log, following, created, outbox, conn, lastSeen>>

Freeze(s) ==
    /\ ~frozen[s]
    /\ frozen' = [frozen EXCEPT ![s] = TRUE]
    /\ UNCHANGED <<now, timer, lease, role, myver, seenVer, log, following, created, outbox, conn, lastSeen>>

Thaw(s) ==
    /\ frozen[s]
    /\ frozen' = [frozen EXCEPT ![s] = FALSE]
    /\ UNCHANGED <<now, timer, lease, role, myver, seenVer, log, following, created, outbox, conn, lastSeen>>

\* the primary renews (not twice in one moment: some time has passed since the last): the
\* conditional write succeeds only if nobody changed the version
Renew(s) ==
    /\ ~frozen[s]
    /\ CanWrite(s)
    /\ timer[s] > 0
    /\ lease.ver = myver[s] /\ lease.holder = s
    /\ lease' = [ver |-> lease.ver + 1, holder |-> s]
    /\ myver' = [myver EXCEPT ![s] = lease.ver + 1]
    /\ timer' = [timer EXCEPT ![s] = 0]
    /\ UNCHANGED <<now, frozen, role, seenVer, log, following, created, outbox, conn, lastSeen>>

\* the conditional write failed: it has lost the lease
Lost(s) ==
    /\ ~frozen[s]
    /\ CanWrite(s)
    /\ ~(lease.ver = myver[s] /\ lease.holder = s)
    /\ role' = [role EXCEPT ![s] = "fenced"]
    /\ UNCHANGED <<now, frozen, timer, lease, myver, seenVer, log, following, created, outbox, conn, lastSeen>>

\* it has not renewed for TTL on its own clock: it stops itself
Fence(s) ==
    /\ ~frozen[s]
    /\ role[s] = "primary" /\ ~CanWrite(s)
    /\ role' = [role EXCEPT ![s] = "fenced"]
    /\ UNCHANGED <<now, frozen, timer, lease, myver, seenVer, log, following, created, outbox, conn, lastSeen>>

\* the standby reads the lease: a new version restarts its count
Observe(s) ==
    /\ ~frozen[s]
    /\ role[s] = "standby"
    /\ lease.ver /= seenVer[s]
    /\ seenVer' = [seenVer EXCEPT ![s] = lease.ver]
    /\ timer' = [timer EXCEPT ![s] = 0]
    /\ UNCHANGED <<now, frozen, lease, role, myver, log, following, created, outbox, conn, lastSeen>>

\* TakeoverWait on its own clock with no new version: claim it, if the version is still the one it saw
Promote(s) ==
    /\ ~frozen[s]
    /\ role[s] = "standby"
    /\ timer[s] >= TakeoverWait
    /\ lease.ver = seenVer[s]
    /\ lease' = [ver |-> lease.ver + 1, holder |-> s]
    /\ myver' = [myver EXCEPT ![s] = lease.ver + 1]
    /\ timer' = [timer EXCEPT ![s] = 0]
    /\ role' = [role EXCEPT ![s] = "primary"]
    /\ following' = [following EXCEPT ![s] = FALSE]
    /\ UNCHANGED <<now, frozen, seenVer, log, created, outbox, conn, lastSeen>>

\* streaming replication, until promoted
Replicate(s) ==
    /\ following[s]
    /\ Len(log[s]) < Len(log[Other(s)])
    /\ log' = [log EXCEPT ![s] = Append(log[s], log[Other(s)][Len(log[s]) + 1])]
    /\ UNCHANGED <<now, frozen, timer, lease, role, myver, seenVer, following, created, outbox, conn, lastSeen>>

Create ==
    /\ Cardinality(created) < MaxChanges
    /\ created' = created \cup {Cardinality(created) + 1}
    /\ outbox' = outbox \cup {Cardinality(created) + 1}
    /\ UNCHANGED <<now, frozen, timer, lease, role, myver, seenVer, log, following, conn, lastSeen>>

\* the hello: if the server's log hasn't the change the app last heard of, at that number, the
\* app will send everything again
Connect(s) ==
    /\ ~frozen[s]
    /\ role[s] = "primary"
    /\ conn' = s
    /\ IF HelloChecks /\ ~(lastSeen.seq = 0 \/ (lastSeen.seq <= Len(log[s]) /\ log[s][lastSeen.seq] = lastSeen.id))
         THEN /\ outbox' = created /\ lastSeen' = NoSeen
         ELSE UNCHANGED <<outbox, lastSeen>>
    /\ UNCHANGED <<now, frozen, timer, lease, role, myver, seenVer, log, following, created>>

Disconnect ==
    /\ conn /= "none"
    /\ conn' = "none"
    /\ UNCHANGED <<now, frozen, timer, lease, role, myver, seenVer, log, following, created, outbox, lastSeen>>

\* The app's record of the last change it has only ever moves forward (texloom/replica.py _note_last)
Note(seq, id) == IF seq > lastSeen.seq THEN [seq |-> seq, id |-> id] ELSE lastSeen

\* a server that may write stores a change (or recognises one it has) and tells the app
Write(s) ==
    /\ ~frozen[s]
    /\ CanWrite(s)
    /\ conn = s
    /\ \E id \in outbox :
         /\ outbox' = outbox \ {id}
         /\ IF id \in Range(log[s])
              THEN /\ lastSeen' = Note(CHOOSE i \in 1..Len(log[s]) : log[s][i] = id, id)
                   /\ UNCHANGED log
              ELSE /\ log' = [log EXCEPT ![s] = Append(log[s], id)]
                   /\ lastSeen' = Note(Len(log[s]) + 1, id)
    /\ UNCHANGED <<now, frozen, timer, lease, role, myver, seenVer, following, created, conn>>

Next ==
    \/ Tick \/ Create \/ Disconnect
    \/ \E s \in Servers : Freeze(s) \/ Thaw(s) \/ Renew(s) \/ Lost(s) \/ Fence(s) \/ Observe(s)
                          \/ Promote(s) \/ Replicate(s) \/ Connect(s) \/ Write(s)

Spec == Init /\ [][Next]_vars

\* never two servers that may each write
SingleWriter == ~(CanWrite(S1) /\ CanWrite(S2))

\* an app connected to the lease holder, with nothing left to send, has stored everything it made
NoLostAck ==
    (conn /= "none" /\ conn = lease.holder /\ role[conn] = "primary" /\ outbox = {})
        => created \subseteq Range(log[conn])
=============================================================================
