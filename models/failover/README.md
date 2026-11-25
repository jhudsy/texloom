# Failover model

A TLA+ model of automatic failover (SCALING.md, "Switching over"): a lease in the bucket, a standby
that takes over, and an app that was told "stored" by a server that was then replaced. It settled the design
that `server/failover.py` implements (issue 8).

    uv run python scripts/check_model.py        # about half a minute (a few if the machine is slow); needs Java

`Failover.tla` is the model. Each `.cfg` is one question, and says in its first lines whether it is
expected to hold or to be violated (a violated one shows the model notices a wrong design):

| configuration | what it checks | result |
|---|---|---|
| `safe` | clocks of different speeds (a factor of 2), a stalled server whose clock keeps running, timers set safely: never two servers that may write; an app told "stored" loses nothing | holds |
| `freeze-heals` | a frozen server whose clock **stops** (a suspended virtual machine) wakes after the standby took over: an app told "stored" by it still loses nothing | holds |
| `freeze-writers` | the same, but asks for never two writers | violated: for a moment there can be two |
| `short-wait` | the standby waits no longer than the primary's lease | violated: two writers |
| `no-hello` | without the app's hello check (resend everything when the server's log lacks the change you last heard of) | violated: a change is lost |

## What it says about the design

1. **Timers**: the standby must wait at least `MaxRate × TTL` of its own clock after it last saw the
   lease change (`MaxRate` is how much faster one clock may run than another; 2 is far more than real
   clocks drift), and the primary stops itself `TTL` after its last renewal on its own clock. Nothing
   compares two machines' clocks, so they need not agree, only run at roughly the same speed.
2. **The takeover is a conditional write** on the version the standby saw, so a renewal that
   squeezes in makes it fail and the standby looks again.
3. **A suspended machine can overlap** for a moment: a clock that stops while suspended makes it wake
   believing it still holds the lease. It stops itself at its first renewal, which fails. The overlap
   can't be removed by timers, but it costs nothing: the changes it accepted in that moment are in the
   apps, whose hello check finds the new primary's log lacks them and sends them again. **That check
   is therefore what failover depends on** (`no-hello`), and it must stay as it is: in particular an
   app's record of the last change it has (`last`, texloom/replica.py `_note_last`) must only move
   forward (the model finds a way to lose a change if it can move back).
4. A clock that counts suspended time (`CLOCK_BOOTTIME` on Linux) removes most such overlaps and is
   worth using where there is one.

One rule the model only allows and the code adds: **a standby that couldn't read the lease counts its wait
afresh once it can** (its timer is only ever lengthened, so the model's results stand). Without it, a
bucket that was away from both servers for longer than the wait is followed by a needless switch, because
the version "hasn't changed" for as long as the outage lasted (`scripts/rehearse_failover.py outage`).

## What it assumes (check these on the real thing)

- The store's conditional write is atomic and strongly consistent. Most S3-compatible stores have
  this now; the server tests its own store at start and refuses to fail over on one that fails
  (`texloom-server --failover-check`; STANDBY.md lists what is known).
- One app and two servers; the app never loses its own copy; change ids are unique. (Several apps
  and a third server don't add a different kind of case: each app checks for itself.)
- The app may connect to any server that thinks it is the primary, including an old one: more than
  DNS would allow, so this is the harder case.
- Not modelled: a server that is slow but not frozen mid-request (same as frozen with a running
  clock, which `safe` covers, apart from a write already in progress when the lease ran out), the
  network between app and server beyond "connected or not", and the lease's real timing values.
