---------------------------- MODULE Relays ----------------------------
(* Stateless relays (DESIGN.md, "Several relays"): several relays sharing one database and a bus, one stored
   project, and apps connected to any relay. The questions:

   - NoLostAck: when an app is connected, has heard everything its relay sent it, and has nothing left
     to send, does the database have every change the app made? (An app told "stored" by a database
     that then failed over, losing that change, must find out and send it again.)
   - Converged: once everything is quiet (every app connected, nothing in flight, every relay caught up
     with the database), does every app have every change? (No relay's gap tracking may leave one out.)

   The design under test, with switches to take parts of it away:
   - A relay stores a change (numbered by the database), passes it on (to its own connections and to the
     other relays through the bus), and then acknowledges it (AckAfterPublish; FALSE: the old order).
     These are separate steps, and a relay can die between any two.
   - A change sent again that is stored already is passed on again with its number.
   - Each relay keeps `upto`: the number up to which its connections have had every change. A change
     from the bus numbered upto+1 moves it on; one at or below is dropped (already had); one above is
     passed on and leaves a gap. A head check (periodic, and after a break in the bus) reads the database
     past `upto` and passes on what it finds.
   - The database fails over to a standby that may lack the last changes (asynchronous replication), and
     its numbering goes on from there, so numbers are used again. A relay notices (the timeline went up)
     before it next uses the database, and then: tells its apps to recheck (Recheck), and starts its
     `upto` again from the new head (ResetOnTimeline).
   - Found by this model: a change stored before the failover but not yet acknowledged when the relay
     noticed it is stored again (found, or appended) in the new timeline before it is acknowledged
     (StoreAgain), with its new number. Otherwise its acknowledgement can arrive after the app's recheck,
     and the change is lost: the app rechecked before it knew of the change, and a second recheck doesn't
     help, since the number may by then belong to another app's change. And a change passed on through the
     bus says which timeline numbered it (TimelineOnBus): one numbered by an older timeline than the relay's
     doesn't move its `upto`, since that number now belongs to another change; and a relay that hears of a
     newer timeline that way notices the failover before it passes anything on, else it could acknowledge a
     lost change under a number its app has just heard belongs to another.
   - An app's hello (on connecting, and on "recheck") says which change it last had ("last"); if the
     database hasn't got that change at that number, the app sends everything it has again. *)
EXTENDS Naturals, Sequences, FiniteSets, TLC

CONSTANTS Apps, Relays, MaxChanges, MaxCrashes, MaxFailovers, MaxBreaks,
          AckAfterPublish, Recheck, ResetOnTimeline, StoreAgain, TimelineOnBus

None == "none"
NoSeen == [seq |-> 0, id |-> 0]
Range(f) == {f[i] : i \in 1..Len(f)}
Max(a, b) == IF a > b THEN a ELSE b
Min2(a, b) == IF a < b THEN a ELSE b

VARIABLES
    db, standby, tl,      \* the database: its log (change ids, numbered by position), the standby's copy (a prefix), its timeline
    up, rtl, upto, pend, busq, broken,
                          \* each relay: running; the timeline it last saw; `upto`; changes it has stored but not yet passed
                          \* on and acknowledged ([c, q, a, st]); its messages from the bus; a break not yet followed by a head check
    conn, toApp,          \* each app: the relay it is connected to, and the messages on their way to it
    have, outbox, sent, last, since,
                          \* each app: the changes it has; those not yet acknowledged; those sent on this connection (an
                          \* app sends each once a connection, and again only on a new one or after a "lost" catch-up); the
                          \* last change it had from the server ([seq, id]); the number up to which it has had every change
    author, next, crashes, failovers, breaks

vars == <<db, standby, tl, up, rtl, upto, pend, busq, broken, conn, toApp, have, outbox, sent, last, since,
          author, next, crashes, failovers, breaks>>

Created == {c \in 1..(next - 1) : TRUE}
At(r) == {a \in Apps : conn[a] = r}
DbHead == Len(db)
LastOf(log) == IF Len(log) = 0 THEN NoSeen ELSE [seq |-> Len(log), id |-> log[Len(log)]]
Up(c, q) == [t |-> "up", c |-> c, q |-> q]
Recheck1 == [t |-> "recheck", c |-> 0, q |-> 0]
Ack(c, q) == [t |-> "ack", c |-> c, q |-> q]

TypeOK == /\ \A a \in Apps : conn[a] \in Relays \cup {None} /\ outbox[a] \subseteq have[a]
          /\ \A r \in Relays : upto[r] \in Nat

Init == /\ db = <<>> /\ standby = <<>> /\ tl = 1
        /\ up = [r \in Relays |-> TRUE] /\ rtl = [r \in Relays |-> 1] /\ upto = [r \in Relays |-> 0]
        /\ pend = [r \in Relays |-> {}] /\ busq = [r \in Relays |-> <<>>] /\ broken = [r \in Relays |-> FALSE]
        /\ conn = [a \in Apps |-> None] /\ toApp = [a \in Apps |-> <<>>]
        /\ have = [a \in Apps |-> {}] /\ outbox = [a \in Apps |-> {}] /\ sent = [a \in Apps |-> {}]
        /\ last = [a \in Apps |-> NoSeen] /\ since = [a \in Apps |-> 0]
        /\ author = [c \in 1..MaxChanges |-> None] /\ next = 1
        /\ crashes = 0 /\ failovers = 0 /\ breaks = 0

\* Messages to every app connected to relay r but `except`, appended.
SendAll(r, except, m) == [a \in Apps |-> IF conn[a] = r /\ a # except THEN Append(toApp[a], m) ELSE toApp[a]]

\* The app's hello and the catch-up that answers it, in one step (the relay subscribes before it reads).
CatchUpOn(a, fresh) ==   \* fresh: a new connection, on which nothing has been sent yet
    LET lost == \/ since[a] > DbHead
                \/ (last[a].seq > 0 /\ (last[a].seq > DbHead \/ db[last[a].seq] # last[a].id))
    IN /\ have' = [have EXCEPT ![a] = @ \cup Range(db)]
       /\ outbox' = [outbox EXCEPT ![a] = IF lost THEN have[a] ELSE @]   \* lost: send everything again
       /\ sent' = [sent EXCEPT ![a] = IF lost \/ fresh THEN {} ELSE @]
       /\ last' = [last EXCEPT ![a] = IF lost \/ DbHead >= last[a].seq THEN LastOf(db) ELSE @]
       /\ since' = [since EXCEPT ![a] = DbHead]
CatchUp(a) == CatchUpOn(a, FALSE)

\* --- apps ----------------------------------------------------------------------------------------
Create(a) ==
    /\ next <= MaxChanges
    /\ author' = [author EXCEPT ![next] = a]
    /\ have' = [have EXCEPT ![a] = @ \cup {next}]
    /\ outbox' = [outbox EXCEPT ![a] = @ \cup {next}]
    /\ next' = next + 1
    /\ UNCHANGED <<db, standby, tl, up, rtl, upto, pend, busq, broken, conn, toApp, sent, last, since, crashes, failovers, breaks>>

Connect(a, r) ==
    /\ conn[a] = None /\ up[r] /\ rtl[r] = tl
    /\ conn' = [conn EXCEPT ![a] = r]
    /\ toApp' = [toApp EXCEPT ![a] = <<>>]
    /\ CatchUpOn(a, TRUE)
    /\ UNCHANGED <<db, standby, tl, up, rtl, upto, pend, busq, broken, author, next, crashes, failovers, breaks>>

Receive(a) ==
    /\ toApp[a] # <<>>
    /\ LET m == Head(toApp[a]) IN
       IF m.t = "recheck" THEN
            /\ toApp' = [toApp EXCEPT ![a] = Tail(@)]
            /\ CatchUp(a)
       ELSE
            /\ toApp' = [toApp EXCEPT ![a] = Tail(@)]
            /\ have' = [have EXCEPT ![a] = @ \cup {m.c}]
            /\ outbox' = [outbox EXCEPT ![a] = IF m.t = "ack" THEN @ \ {m.c} ELSE @]
            /\ last' = [last EXCEPT ![a] = IF m.q > @.seq THEN [seq |-> m.q, id |-> m.c] ELSE @]
            /\ since' = [since EXCEPT ![a] = IF m.q = @ + 1 THEN m.q ELSE @]
            /\ UNCHANGED sent
    /\ UNCHANGED <<db, standby, tl, up, rtl, upto, pend, busq, broken, conn, author, next, crashes, failovers, breaks>>

\* --- relays --------------------------------------------------------------------------------------
\* A change from app a, stored (or found stored already) in one transaction.
StoreIn(r, a, c, others) ==   \* the transaction: found, or appended; `others` are the relay's other pending changes
    LET stored == \E i \in 1..DbHead : db[i] = c
        q == IF stored THEN CHOOSE i \in 1..DbHead : db[i] = c ELSE DbHead + 1
    IN /\ db' = IF stored THEN db ELSE Append(db, c)
       /\ pend' = [pend EXCEPT ![r] = others \cup {[c |-> c, q |-> q, a |-> a, st |-> "stored", tl |-> tl]}]
       /\ upto' = [upto EXCEPT ![r] = IF q = @ + 1 THEN q ELSE @]

Store(r, a, c) ==
    /\ up[r] /\ conn[a] = r /\ rtl[r] = tl /\ c \in outbox[a] /\ c \notin sent[a]
    /\ sent' = [sent EXCEPT ![a] = @ \cup {c}]
    /\ StoreIn(r, a, c, pend[r])
    /\ UNCHANGED <<standby, tl, up, rtl, busq, broken, conn, toApp, have, outbox, last, since, author, next, crashes, failovers, breaks>>

\* Pass it on: to this relay's other connections, and to the other relays through the bus.
Publish(r, p) ==
    /\ up[r] /\ p \in pend[r] /\ p.st = (IF AckAfterPublish THEN "stored" ELSE "acked")
    /\ toApp' = SendAll(r, p.a, Up(p.c, p.q))
    /\ busq' = [s \in Relays |-> IF s # r /\ up[s] /\ ~broken[s] THEN Append(busq[s], <<p.q, p.c, p.tl>>) ELSE busq[s]]
    /\ pend' = [pend EXCEPT ![r] = IF AckAfterPublish THEN (@ \ {p}) \cup {[p EXCEPT !.st = "published"]} ELSE @ \ {p}]
    /\ UNCHANGED <<db, standby, tl, up, rtl, upto, broken, conn, have, outbox, sent, last, since, author, next, crashes, failovers, breaks>>

AckIt(r, p) ==
    /\ up[r] /\ p \in pend[r] /\ p.st = (IF AckAfterPublish THEN "published" ELSE "stored")
    /\ IF StoreAgain /\ p.tl # rtl[r] THEN   \* stored before a failover this relay has since noticed: store it again first
          /\ rtl[r] = tl
          /\ StoreIn(r, p.a, p.c, pend[r] \ {p})
          /\ UNCHANGED <<toApp, standby, tl, up, rtl, busq, broken, conn, have, outbox, sent, last, since, author, next, crashes, failovers, breaks>>
       ELSE
          /\ toApp' = IF conn[p.a] = r THEN [toApp EXCEPT ![p.a] = Append(@, Ack(p.c, p.q))] ELSE toApp
          /\ pend' = [pend EXCEPT ![r] = IF AckAfterPublish THEN @ \ {p} ELSE (@ \ {p}) \cup {[p EXCEPT !.st = "acked"]}]
          /\ UNCHANGED <<db, standby, tl, up, rtl, upto, busq, broken, conn, have, outbox, sent, last, since, author, next, crashes, failovers, breaks>>

\* A change from another relay, through the bus.
FromBus(r) ==
    /\ up[r] /\ busq[r] # <<>>
    /\ TimelineOnBus => busq[r][1][3] <= rtl[r]   \* numbered by a newer timeline: the relay notices the failover first
    /\ LET q == busq[r][1][1]
           c == busq[r][1][2]
           ours == ~TimelineOnBus \/ busq[r][1][3] = rtl[r]   \* numbered by the timeline this relay knows
       IN /\ busq' = [busq EXCEPT ![r] = Tail(@)]
          /\ IF ours /\ q <= upto[r] THEN UNCHANGED <<toApp, upto>>     \* had already
             ELSE /\ toApp' = SendAll(r, None, Up(c, q))
                  /\ upto' = [upto EXCEPT ![r] = IF ours /\ q = @ + 1 THEN q ELSE @]  \* above: a gap, for the head check
    /\ UNCHANGED <<db, standby, tl, up, rtl, pend, broken, conn, have, outbox, sent, last, since, author, next, crashes, failovers, breaks>>

\* Read the database past `upto` and pass on what's there (periodically, and after a break in the bus).
HeadCheck(r) ==
    /\ up[r] /\ rtl[r] = tl /\ (upto[r] < DbHead \/ broken[r])
    /\ toApp' = [a \in Apps |-> IF conn[a] = r
                                THEN toApp[a] \o [i \in 1..(DbHead - Min2(upto[r], DbHead)) |-> Up(db[Min2(upto[r], DbHead) + i], Min2(upto[r], DbHead) + i)]
                                ELSE toApp[a]]
    /\ upto' = [upto EXCEPT ![r] = Max(@, DbHead)]
    /\ broken' = [broken EXCEPT ![r] = FALSE]
    /\ UNCHANGED <<db, standby, tl, up, rtl, pend, busq, conn, have, outbox, sent, last, since, author, next, crashes, failovers, breaks>>


\* The relay notices the database failed over (its timeline went up), before it uses it again.
NoticeFailover(r) ==
    /\ up[r] /\ rtl[r] # tl
    /\ rtl' = [rtl EXCEPT ![r] = tl]
    /\ toApp' = IF Recheck THEN SendAll(r, None, Recheck1) ELSE toApp
    /\ upto' = IF ResetOnTimeline THEN [upto EXCEPT ![r] = DbHead] ELSE upto
    /\ UNCHANGED <<db, standby, tl, up, pend, busq, broken, conn, have, outbox, sent, last, since, author, next, crashes, failovers, breaks>>

\* --- failures ------------------------------------------------------------------------------------
Crash(r) ==
    /\ up[r] /\ crashes < MaxCrashes
    /\ up' = [up EXCEPT ![r] = FALSE]
    /\ pend' = [pend EXCEPT ![r] = {}]
    /\ busq' = [busq EXCEPT ![r] = <<>>]
    /\ conn' = [a \in Apps |-> IF conn[a] = r THEN None ELSE conn[a]]
    /\ toApp' = [a \in Apps |-> IF conn[a] = r THEN <<>> ELSE toApp[a]]   \* what was on its way is lost with it
    /\ crashes' = crashes + 1
    /\ UNCHANGED <<db, standby, tl, rtl, upto, broken, have, outbox, sent, last, since, author, next, failovers, breaks>>

Restart(r) ==
    /\ ~up[r]
    /\ up' = [up EXCEPT ![r] = TRUE]
    /\ rtl' = [rtl EXCEPT ![r] = tl]
    /\ upto' = [upto EXCEPT ![r] = DbHead]   \* a relay joins a project at the database's head
    /\ broken' = [broken EXCEPT ![r] = FALSE]
    /\ UNCHANGED <<db, standby, tl, pend, busq, conn, toApp, have, outbox, sent, last, since, author, next, crashes, failovers, breaks>>

\* A break in a relay's link to the bus: what was on its way to it is lost.
BusBreak(r) ==
    /\ up[r] /\ breaks < MaxBreaks
    /\ busq' = [busq EXCEPT ![r] = <<>>]
    /\ broken' = [broken EXCEPT ![r] = TRUE]
    /\ breaks' = breaks + 1
    /\ UNCHANGED <<db, standby, tl, up, rtl, upto, pend, conn, toApp, have, outbox, sent, last, since, author, next, crashes, failovers>>

Replicate ==
    /\ Len(standby) < DbHead
    /\ standby' = Append(standby, db[Len(standby) + 1])
    /\ UNCHANGED <<db, tl, up, rtl, upto, pend, busq, broken, conn, toApp, have, outbox, sent, last, since, author, next, crashes, failovers, breaks>>

\* The primary is lost; the standby, with what it had copied, takes over (and a new standby copies it).
Failover ==
    /\ failovers < MaxFailovers
    /\ db' = standby
    /\ tl' = tl + 1
    /\ failovers' = failovers + 1
    /\ UNCHANGED <<standby, up, rtl, upto, pend, busq, broken, conn, toApp, have, outbox, sent, last, since, author, next, crashes, breaks>>

Next ==
    \/ \E a \in Apps : Create(a) \/ Receive(a) \/ \E r \in Relays : Connect(a, r)
    \/ \E r \in Relays : \/ FromBus(r) \/ HeadCheck(r) \/ NoticeFailover(r) \/ Crash(r) \/ Restart(r) \/ BusBreak(r)
                         \/ \E a \in Apps, c \in 1..MaxChanges : Store(r, a, c)
                         \/ \E p \in pend[r] : Publish(r, p) \/ AckIt(r, p)
    \/ Replicate \/ Failover

Spec == Init /\ [][Next]_vars

\* --- what must hold ------------------------------------------------------------------------------
Settled(a) == /\ conn[a] # None /\ up[conn[a]] /\ rtl[conn[a]] = tl
              /\ toApp[a] = <<>> /\ outbox[a] = {}
              /\ ~\E p \in pend[conn[a]] : p.a = a

NoLostAck == \A a \in Apps : Settled(a) => \A c \in Created : author[c] = a => c \in Range(db)

Quiet == /\ \A a \in Apps : Settled(a)
         /\ \A r \in Relays : up[r] => (pend[r] = {} /\ busq[r] = <<>> /\ ~broken[r] /\ rtl[r] = tl /\ upto[r] >= DbHead)

Converged == Quiet => \A a \in Apps : have[a] = Created

\* Apps are interchangeable, and so are relays: states that differ only by naming are checked once.
Symmetry == Permutations(Apps) \cup Permutations(Relays)

\* To keep the search finite and small: no more than this many messages waiting for one app or one relay.
Bounded == /\ \A a \in Apps : Len(toApp[a]) <= 3
           /\ \A r \in Relays : Len(busq[r]) <= 2
=============================================================================
