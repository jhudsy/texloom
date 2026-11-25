#!/usr/bin/env bash
# Runs a command of yours while this machine's TeXLoom server is the primary, so that something
# outside it (a DNS record, a firewall rule, a message to you) follows the primary. See INSTALL.md,
# "Moving traffic to the primary". It does nothing itself about DNS or anything else: that is the
# command, which is yours and stays in your private settings.
#
# It asks this server's /api/health, which answers 200 only on the primary (a standby, and a
# server that has stopped itself, answer 503), every INTERVAL seconds. After CONFIRM answers in a
# row it runs ON_PRIMARY (with sh -c), at once when the server has just become the primary; again
# every REFRESH seconds while it stays so (so a command that sets a DNS record keeps it right); and
# again every RETRY seconds while the command fails. So ON_PRIMARY must be safe to repeat.
#
# Settings, as environment variables (the systemd unit reads /etc/texloom/follow-primary.env):
#   ON_PRIMARY   the command (required); its environment is this one, so keys can go in the file
#   HEALTH_URL   default http://127.0.0.1:8787/api/health
#   INTERVAL     seconds between looks, default 5
#   CONFIRM      answers of 200 in a row before acting, default 2
#   REFRESH      seconds between repeats while all is well, default 600
#   RETRY        seconds between tries while the command fails, default 30
set -u

: "${ON_PRIMARY:?set ON_PRIMARY to the command to run while this server is the primary}"
HEALTH_URL=${HEALTH_URL:-http://127.0.0.1:8787/api/health}
INTERVAL=${INTERVAL:-5}
CONFIRM=${CONFIRM:-2}
REFRESH=${REFRESH:-600}
RETRY=${RETRY:-30}

say() { echo "follow-primary: $*"; }

streak=0      # answers of 200 in a row
acting=0      # this server has been the primary, and the command has been tried since
last_try=0    # when, and whether it worked
last_ok=0

while true; do
  code=$(curl -s -o /dev/null -m 3 -w '%{http_code}' "$HEALTH_URL" 2>/dev/null) || code=0
  if [ "$code" = 200 ]; then
    streak=$((streak + 1))
  else
    if [ "$acting" = 1 ]; then say "this server is not the primary any more (health says $code)"; fi
    streak=0
    acting=0
  fi
  if [ "$streak" -ge "$CONFIRM" ]; then
    now=$(date +%s)
    due=0
    if [ "$acting" = 0 ]; then
      say "this server is the primary"
      due=1
    elif [ "$last_ok" = 1 ] && [ $((now - last_try)) -ge "$REFRESH" ]; then
      due=1
    elif [ "$last_ok" = 0 ] && [ $((now - last_try)) -ge "$RETRY" ]; then
      due=1
    fi
    if [ "$due" = 1 ]; then
      acting=1
      last_try=$now
      sh -c "$ON_PRIMARY"
      status=$?
      if [ "$status" = 0 ]; then
        last_ok=1
        say "ran the command: ok"
      else
        last_ok=0
        say "ran the command: it failed (status $status), trying again in $RETRY s"
      fi
    fi
  fi
  sleep "$INTERVAL"
done
