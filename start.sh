#!/usr/bin/env bash
# Start the whole agent with one command and keep it running:
#   ChromaDB (:8000) -> bridge (:5001) -> Reply Desk (:5002) -> WhatsApp client
#
# - Each service is restarted if it crashes (waiting 5 s, up to 60 s if it keeps failing).
# - The Mac is kept awake (caffeinate) for as long as this script runs.
# - Output goes to logs/services/<name>.log. Ctrl+C stops everything this script started.
# - Refuses to start if any of the ports is already in use: stop those servers first.
#
# Run from anywhere:  ./start.sh
# Pair WhatsApp once first (QR code):  node whatsapp/baileys_client.js

set -u

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$PROJECT_ROOT/logs/services"
MIN_DELAY=5
MAX_DELAY=60
HEALTHY_AFTER=60  # a run this long resets the restart delay

say() { printf '[start] %s\n' "$*"; }

port_in_use() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }

wait_for_port() {  # wait_for_port <port> <seconds>
  local port=$1 limit=$2 waited=0
  until port_in_use "$port"; do  # any listener: ChromaDB may bind IPv6 localhost only
    [ "$waited" -ge "$limit" ] && return 1
    sleep 1
    waited=$((waited + 1))
  done
}

# supervise <name> <command...>: run the command, restart it when it exits, until told to stop.
supervise() {
  local name=$1 child="" delay=$MIN_DELAY started status
  shift
  trap 'if [ -n "$child" ]; then kill "$child" 2>/dev/null; wait "$child" 2>/dev/null; fi; exit 0' TERM INT
  while true; do
    started=$SECONDS
    printf '\n=== %s: starting %s ===\n' "$(date '+%F %T')" "$name" >>"$LOG_DIR/$name.log"
    "$@" >>"$LOG_DIR/$name.log" 2>&1 &
    child=$!
    wait "$child"
    status=$?
    child=""
    if [ $((SECONDS - started)) -ge "$HEALTHY_AFTER" ]; then delay=$MIN_DELAY; fi
    say "$name stopped (exit $status); restarting in ${delay}s. See logs/services/$name.log"
    sleep "$delay" &
    child=$!
    wait "$child"
    child=""
    delay=$((delay * 2))
    if [ "$delay" -gt "$MAX_DELAY" ]; then delay=$MAX_DELAY; fi
  done
}

PIDS=""
SLEEPER=""

stop_all() {
  trap - INT TERM
  say "stopping all services..."
  local pid
  for pid in $PIDS $SLEEPER; do kill "$pid" 2>/dev/null; done
  for pid in $PIDS; do wait "$pid" 2>/dev/null; done
  say "stopped."
  exit 0
}

start_service() {  # start_service <name> <command...>
  supervise "$@" &
  PIDS="$PIDS $!"
}

main() {
  cd "$PROJECT_ROOT" || exit 1
  mkdir -p "$LOG_DIR"
  # shellcheck disable=SC1091
  [ -f .venv/bin/activate ] && . .venv/bin/activate

  local missing=0 port
  for port in 8000 5001 5002; do
    if port_in_use "$port"; then
      say "port $port is already in use: $(lsof -nP -iTCP:"$port" -sTCP:LISTEN | awk 'NR==2 {print $1" (pid "$2")"}')"
      missing=1
    fi
  done
  if pgrep -f "node whatsapp/baileys_client.js" >/dev/null 2>&1; then
    say "the WhatsApp client is already running (pid $(pgrep -f 'node whatsapp/baileys_client.js' | head -1))"
    missing=1
  fi
  [ "$missing" -eq 1 ] && { say "stop those first (Ctrl+C in their terminals), then run ./start.sh again."; exit 1; }

  command -v chroma >/dev/null 2>&1 || { say "chroma not found: activate .venv or run pip install -r requirements.txt"; exit 1; }
  command -v node >/dev/null 2>&1 || { say "node not found: install Node.js 20+"; exit 1; }
  [ -d node_modules ] || { say "node_modules missing: run npm install"; exit 1; }
  [ -f .env ] || { say ".env missing: add GEMINI_API_KEY (see README 3.1)"; exit 1; }
  if [ ! -f auth_info_baileys/creds.json ]; then
    say "WhatsApp isn't paired yet. Run 'node whatsapp/baileys_client.js' once, scan the QR code, stop it, then run ./start.sh."
    exit 1
  fi

  trap stop_all INT TERM

  # Keep the Mac awake (no idle or system sleep on power) while this script runs.
  caffeinate -ims -w $$ &
  PIDS="$PIDS $!"

  start_service chroma chroma run --path ./chroma_data --port 8000
  wait_for_port 8000 60 || say "ChromaDB isn't answering yet; check logs/services/chroma.log"
  start_service bridge python3 agent/bridge.py
  wait_for_port 5001 180 || say "the bridge isn't answering yet; check logs/services/bridge.log"
  start_service reply-desk python3 console/app.py
  wait_for_port 5002 60 || say "Reply Desk isn't answering yet; check logs/services/reply-desk.log"
  start_service whatsapp node whatsapp/baileys_client.js

  say "all services started. Reply Desk: http://127.0.0.1:5002"
  say "logs: tail -f logs/services/*.log   |   stop: Ctrl+C"
  while true; do
    sleep 3600 &
    SLEEPER=$!
    wait "$SLEEPER"
  done
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
