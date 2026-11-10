#!/bin/bash
# The ONLY sanctioned way for pawns to quit / replace / relaunch the user's AMV Matcher.
# Honours the in-app "Dev: do not disturb" toggle (<app data>/dnd.json {on,since,by}): while it is on, WAIT, never quit.
#
# Usage: tools/relaunch_app.sh [TIMEOUT_SECONDS] [--app PATH] [--data-dir DIR] [--poll SECONDS] [--dry-run]
#   TIMEOUT_SECONDS  how long to wait for DND to turn off (default 3600). Exit code 3 on timeout (app untouched).
#   --app PATH       app bundle (default /Applications/AMV Matcher.app)
#   --data-dir DIR   app data dir holding dnd.json (default ~/Library/Application Support/AMV Matcher); tests use a temp dir
#   --poll SECONDS   DND poll interval (default 30)
#   --dry-run        do the DND logic but only print the quit/open steps
# Never run this against the user's real app from tests: use --dry-run or a throwaway app + --data-dir.
set -u
APP="/Applications/AMV Matcher.app"
DATA="$HOME/Library/Application Support/AMV Matcher"
TIMEOUT=3600; POLL=30; DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --app) APP="$2"; shift 2;;
    --data-dir) DATA="$2"; shift 2;;
    --poll) POLL="$2"; shift 2;;
    --dry-run) DRY=1; shift;;
    -h|--help) sed -n 2,12p "$0"; exit 0;;
    ''|*[!0-9]*) echo "relaunch_app: unknown argument: $1" >&2; exit 2;;
    *) TIMEOUT="$1"; shift;;
  esac
done

dnd_on() {   # true when dnd.json says {"on": true}; missing/unreadable file = off
  [ -f "$DATA/dnd.json" ] || return 1
  /usr/bin/python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get("on") is True else 1)' "$DATA/dnd.json" 2>/dev/null
}

waited=0
if dnd_on; then echo "relaunch_app: Do-not-disturb is ON; waiting (poll ${POLL}s, timeout ${TIMEOUT}s)"; fi
while dnd_on; do
  if [ "$waited" -ge "$TIMEOUT" ]; then echo "relaunch_app: timed out after ${waited}s with Do-not-disturb still ON; app left untouched" >&2; exit 3; fi
  sleep "$POLL"; waited=$((waited + POLL))
done
echo "relaunch_app: Do-not-disturb is off; proceeding"

BIN="$APP/Contents/MacOS"
alive() { /usr/bin/pgrep -f "$BIN/" >/dev/null 2>&1; }
NAME="$(basename "$APP" .app)"
if [ "$DRY" = 1 ]; then
  echo "relaunch_app: [dry-run] would quit '$NAME' (osascript, wait ~10s, one SIGTERM if alive), then open '$APP'"
  exit 0
fi
if alive; then
  /usr/bin/osascript -e "tell application \"$NAME\" to quit" >/dev/null 2>&1
  for _ in 1 2 3 4 5 6 7 8 9 10; do alive || break; sleep 1; done
  if alive; then /usr/bin/pkill -TERM -f "$BIN/" ; sleep 2; fi
fi
/usr/bin/open "$APP"
echo "relaunch_app: relaunched $APP"
