#!/usr/bin/env bash
# B144 scraper launcher.
#   ./run.sh          start the web UI (restarts it if it's already running) and open the browser
#   ./run.sh stop     stop the web UI
#   ./run.sh status   show whether it's running
set -euo pipefail

cd "$(dirname "$0")"
PORT=8501
URL="http://localhost:${PORT}"
PID_FILE="data/app.pid"
LOG_FILE="data/app.log"
mkdir -p data

export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

is_alive() { [[ -n "${1:-}" ]] && kill -0 "$1" 2>/dev/null; }
current_pid() { [[ -f "$PID_FILE" ]] && cat "$PID_FILE" 2>/dev/null || true; }
port_pids() { lsof -ti "tcp:${PORT}" -sTCP:LISTEN 2>/dev/null || true; }

stop_pid() {
  local pid="$1"
  is_alive "$pid" || return 0
  # uv spawns streamlit as a child; signal both so nothing is left holding the port.
  local kids; kids="$(pgrep -P "$pid" 2>/dev/null || true)"
  kill -TERM "$pid" $kids 2>/dev/null || true
  for _ in $(seq 1 50); do
    is_alive "$pid" || break
    sleep 0.1
  done
  if is_alive "$pid"; then
    echo "  process $pid did not exit after 5 s — sending KILL"
    kill -KILL "$pid" $kids 2>/dev/null || true
  fi
}

stop_app() {
  local pid; pid="$(current_pid)"
  if is_alive "$pid"; then
    echo "Stopping B144 scraper (PID $pid)…"
    stop_pid "$pid"
  fi
  rm -f "$PID_FILE"
  # Free the port if something from this app is still holding it.
  local holders; holders="$(port_pids)"
  for p in $holders; do
    if ps -o command= -p "$p" 2>/dev/null | grep -q "streamlit"; then
      echo "Freeing port ${PORT} (PID $p)…"
      stop_pid "$p"
    else
      echo "Port ${PORT} is used by another program (PID $p: $(ps -o comm= -p "$p")). Not touching it." >&2
    fi
  done
}

status_app() {
  local pid; pid="$(current_pid)"
  if is_alive "$pid"; then
    echo "running  PID $pid  ${URL}"
    port_pids | grep -q . && echo "port ${PORT}: listening" || echo "port ${PORT}: not listening yet"
  else
    echo "stopped"
    [[ -n "$(port_pids)" ]] && echo "note: port ${PORT} is held by PID(s) $(port_pids | tr '\n' ' ')"
  fi
  return 0
}

ensure_uv() {
  if ! command -v uv >/dev/null 2>&1; then
    echo "Installing uv (Python package manager)…"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
  fi
}

start_app() {
  ensure_uv
  echo "Syncing dependencies (uv sync)…"
  uv sync --quiet
  stop_app
  echo "Starting B144 scraper on ${URL} (log: ${LOG_FILE})…"
  nohup uv run streamlit run app.py --server.port "$PORT" --server.headless true \
    --browser.gatherUsageStats false >>"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"
  for _ in $(seq 1 60); do
    if curl -fsS -o /dev/null "${URL}/_stcore/health" 2>/dev/null; then
      echo "Ready: ${URL}  (PID $(current_pid))"
      command -v open >/dev/null && open "$URL" || true
      return 0
    fi
    if ! is_alive "$(current_pid)"; then
      echo "The app exited during startup. Last log lines:" >&2
      tail -n 30 "$LOG_FILE" >&2
      exit 1
    fi
    sleep 0.5
  done
  echo "The app didn't answer on ${URL} within 30 s; see ${LOG_FILE}" >&2
  exit 1
}

case "${1:-start}" in
  start | restart) start_app ;;
  stop) stop_app; echo "Stopped." ;;
  status) status_app ;;
  *) echo "usage: $0 [start|stop|status]" >&2; exit 2 ;;
esac
