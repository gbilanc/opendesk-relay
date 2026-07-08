#!/usr/bin/env bash
#
# restart-relay.sh — Riavvia opendesk-relay
#
# Il relay server è ora standalone in ../opendesk-relay/
#
# Supporta due modalità:
#   1) Systemd: se il servizio è installato, usa sudo systemctl restart
#   2) Manuale: trova e kill il processo in esecuzione, poi lo riavvia
#
# Usage:
#   ./scripts/restart-relay.sh              # porta default 8474
#   ./scripts/restart-relay.sh --port 9443  # porta personalizzata
#   ./scripts/restart-relay.sh --help       # questo messaggio
#

if [ -n "${BASH_VERSION:-}" ]; then
    set -euo pipefail
else
    set -eu
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Relay project is now standalone
RELAY_DIR="$(cd "$PROJECT_DIR/../opendesk-relay" 2>/dev/null && pwd)" || {
    RELAY_DIR="$PROJECT_DIR/../opendesk-relay"
}

SERVICE_NAME="opendesk-relay"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
PORT="${PORT:-8474}"
LOG_DIR="${LOG_DIR:-$HOME/.opendesk}"

# Parsing argomenti
while [ $# -gt 0 ]; do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: $0 [--port PORT]"
            echo ""
            echo "Riavvia opendesk-relay (systemd o processo manuale)."
            echo "Il relay server è in: $RELAY_DIR"
            exit 0
            ;;
        *) echo "Opzione sconosciuta: $1"; exit 1 ;;
    esac
done

# ── helper: kill processo esistente ─────────────────────────────────
kill_existing_relay() {
    local pids
    pids="$(pgrep -f "opendesk-relay" 2>/dev/null || true)"
    pids="$pids $(pgrep -f "relay-server" 2>/dev/null || true)"
    pids="$(echo "$pids" | tr ' ' '\n' | sort -u | xargs)"

    if [ -n "$pids" ]; then
        echo "→ Processo(i) relay in esecuzione: $(echo "$pids" | tr '\n' ' ')"
        echo "→ Invio SIGTERM..."
        for pid in $pids; do
            if [ "$pid" -eq "$$" ]; then continue; fi
            kill "$pid" 2>/dev/null || true
        done
        sleep 2
        for pid in $pids; do
            if [ "$pid" -eq "$$" ]; then continue; fi
            if kill -0 "$pid" 2>/dev/null; then
                echo "→ Forzatura kill -9 per pid $pid..."
                kill -9 "$pid" 2>/dev/null || true
            fi
        done
        echo "✅ Processo(i) relay terminati."
    else
        echo "→ Nessun processo relay in esecuzione."
    fi
}

# ── helper: avvio manuale ──────────────────────────────────────────
start_manual_relay() {
    mkdir -p "$LOG_DIR"
    local log_file="$LOG_DIR/relay-server.log"
    local pid_file="$LOG_DIR/relay-server.pid"

    echo "→ Avvio relay in background su porta $PORT..."
    echo "   Log: $log_file"
    echo "   PID: $pid_file"
    echo "   Dir: $RELAY_DIR"

    cd "$RELAY_DIR"

    if command -v uv >/dev/null 2>&1; then
        nohup uv run relay-server --port "$PORT" >> "$log_file" 2>&1 &
    else
        nohup python3 -m relay_server --port "$PORT" >> "$log_file" 2>&1 &
    fi

    local new_pid=$!
    echo "$new_pid" > "$pid_file"

    sleep 1
    if kill -0 "$new_pid" 2>/dev/null; then
        echo "✅ Relay avviato (PID $new_pid)."
    else
        echo "❌ Errore: il relay non si è avviato. Controlla $log_file."
        exit 1
    fi
}

# ════════════════════════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════════════════════════

echo "═══════════════════════════════════════════"
echo " OpenDesk Relay — Restart"
echo "═══════════════════════════════════════════"
echo "Relay dir : $RELAY_DIR"
echo "Port      : $PORT"
echo ""

if [ -f "$SERVICE_FILE" ]; then
    echo "→ Servizio systemd trovato: $SERVICE_FILE"
    echo "→ Esecuzione: sudo systemctl restart $SERVICE_NAME"
    sudo systemctl restart "$SERVICE_NAME"
    echo ""
    echo "✅ Servizio systemd '$SERVICE_NAME' riavviato."
    echo ""
    echo "   Status:  systemctl status $SERVICE_NAME"
    echo "   Logs:    journalctl -u $SERVICE_NAME -f"
else
    echo "→ Servizio systemd non trovato, modalità manuale."
    echo ""
    kill_existing_relay
    echo ""
    sleep 1
    start_manual_relay
    echo ""
    echo "   Log:  tail -f $LOG_DIR/relay-server.log"
    echo "   Kill: kill \$(cat $LOG_DIR/relay-server.pid)"
fi
