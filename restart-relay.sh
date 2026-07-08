#!/bin/bash
#
# restart-relay.sh — Restart the OpenDesk Relay Server
#
# Supports two modes:
#   1) systemd — if the service is installed, use systemctl
#   2) manual  — find & kill the running process, then restart
#
# Usage:
#   ./restart-relay.sh              # port 8474 (default)
#   ./restart-relay.sh --port 9443  # custom port
#   ./restart-relay.sh --help       # show help
#

# bash-only options (skip if running under sh/dash)
if [ -n "${BASH_VERSION:-}" ]; then
    set -euo pipefail
else
    set -eu
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="opendesk-relay"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
PORT="${PORT:-8474}"
LOG_DIR="${LOG_DIR:-/var/log/opendesk-relay}"

# Parse args
while [ $# -gt 0 ]; do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: $0 [--port PORT]"
            echo ""
            echo "Restart the OpenDesk Relay Server (systemd or manual)."
            exit 0
            ;;
        *) echo "❌ Unknown: $1"; exit 1 ;;
    esac
done

# ─────────────────────────────────────────────────────────────────────────────
# Helper: kill existing relay process
# ─────────────────────────────────────────────────────────────────────────────
kill_existing_relay() {
    local pids
    pids="$(pgrep -f "relay-server" 2>/dev/null || true)"
    pids="$pids $(pgrep -f "opendesk-relay" 2>/dev/null || true)"
    pids="$(echo "$pids" | tr ' ' '\n' | sort -u | grep -v "^$" || true)"

    if [ -n "$pids" ]; then
        echo "→ Process(s) running: $(echo "$pids" | tr '\n' ' ')"
        echo "→ Sending SIGTERM..."
        for pid in $pids; do
            [[ "$pid" -eq $$ ]] && continue
            kill "$pid" 2>/dev/null || true
        done
        sleep 2
        for pid in $pids; do
            [[ "$pid" -eq $$ ]] && continue
            if kill -0 "$pid" 2>/dev/null; then
                echo "→ Force kill -9 for pid $pid..."
                kill -9 "$pid" 2>/dev/null || true
            fi
        done
        echo "✅ Relay process(s) terminated."
    else
        echo "→ No relay process running."
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Helper: start relay manually
# ─────────────────────────────────────────────────────────────────────────────
start_manual_relay() {
    mkdir -p "$LOG_DIR"
    local log_file="$LOG_DIR/relay-server.log"
    local pid_file="/tmp/opendesk-relay.pid"

    echo "→ Starting relay in background on port $PORT..."
    echo "   Log: $log_file"
    echo "   PID: $pid_file"

    nohup relay-server --port "$PORT" >> "$log_file" 2>&1 &
    local new_pid=$!
    echo "$new_pid" > "$pid_file"

    sleep 1
    if kill -0 "$new_pid" 2>/dev/null; then
        echo "✅ Relay started (PID $new_pid)."
    else
        echo "❌ Relay failed to start. Check: $log_file"
        exit 1
    fi
}

# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════
echo "═══════════════════════════════════════════"
echo " OpenDesk Relay — Restart"
echo "═══════════════════════════════════════════"
echo "Port: $PORT"
echo ""

if [ -f "$SERVICE_FILE" ]; then
    echo "→ systemd service found: $SERVICE_FILE"
    sudo systemctl restart "$SERVICE_NAME"
    echo ""
    echo "✅ Service '$SERVICE_NAME' restarted."
    echo ""
    echo "   Status: systemctl status $SERVICE_NAME"
    echo "   Logs:   journalctl -u $SERVICE_NAME -f"
else
    echo "→ No systemd service — manual mode."
    echo ""
    kill_existing_relay
    echo ""
    sleep 1
    start_manual_relay
    echo ""
    echo "   Log: tail -f $LOG_DIR/relay-server.log"
    echo "   Kill: kill \$(cat /tmp/opendesk-relay.pid)"
fi
