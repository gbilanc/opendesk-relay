#!/usr/bin/env bash
#
# Install OpenDesk Relay Server as a systemd service (Linux).
#
# Usage:
#   sudo ./scripts/install-relay.sh              # install from ../opendesk-relay
#   sudo ./scripts/install-relay.sh --port 9443   # custom port
#
# Prerequisites:
#   - Python 3.12+ with uv or pip
#   - systemd (Linux only)
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# The relay server is now standalone at ../opendesk-relay
RELAY_DIR="$(cd "$PROJECT_DIR/../opendesk-relay" 2>/dev/null && pwd)" || {
    # Fallback: look for it relative to the script
    RELAY_DIR="$(cd "$SCRIPT_DIR/../opendesk-relay" 2>/dev/null && pwd)" || {
        echo "❌ opendesk-relay directory not found at ../opendesk-relay"
        echo "   Please clone it: git clone https://github.com/opendesk/opendesk-relay"
        exit 1
    }
}

SERVICE_NAME="opendesk-relay"
SERVICE_FILE="$RELAY_DIR/opendesk-relay.service"
SYSTEMD_DIR="/etc/systemd/system"
PORT="${PORT:-8474}"

# Parse --port argument
while [[ $# -gt 0 ]]; do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        --help) echo "Usage: $0 [--port PORT]"; exit 0 ;;
        *) echo "Unknown: $1"; exit 1 ;;
    esac
done

echo "============================================"
echo " OpenDesk Relay Server Installer"
echo "============================================"
echo "Relay project : $RELAY_DIR"
echo "Port          : $PORT"
echo ""

# ── Check systemd ──
if ! command -v systemctl &>/dev/null; then
    echo "❌ systemd not found — this script is for Linux with systemd only."
    echo "   On other platforms, run: cd $RELAY_DIR && uv run relay-server --port $PORT"
    exit 1
fi

# ── Check uv or pip ──
if command -v uv &>/dev/null; then
    PYTHON_CMD="uv run --directory $RELAY_DIR relay-server"
    echo "✅ Using uv"
elif command -v python3 &>/dev/null; then
    PYTHON_CMD="python3 -m relay_server"
    echo "✅ Using python3 (assumes relay_server is installed)"
else
    echo "❌ Neither uv nor python3 found."
    exit 1
fi

# ── Install dependencies ──
if command -v uv &>/dev/null; then
    echo "→ Installing dependencies with uv..."
    cd "$RELAY_DIR"
    uv sync
else
    echo "→ Installing dependencies with pip..."
    pip install -e "$RELAY_DIR"
fi

# ── Create config directory ──
mkdir -p "$HOME/.opendesk"
echo "✅ Config dir: $HOME/.opendesk"

# ── Create systemd service ──
if [ ! -f "$SERVICE_FILE" ]; then
    # Copy the template from the new location if it doesn't exist
    if [ -f "$RELAY_DIR/opendesk-relay.service" ]; then
        SERVICE_FILE="$RELAY_DIR/opendesk-relay.service"
    else
        echo "❌ Service file not found. Generating from template..."
        cat > /tmp/opendesk-relay.service << 'SERVICEEOF'
[Unit]
Description=OpenDesk Relay Server
Documentation=https://github.com/opendesk/opendesk-relay
After=network.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=PLACEHOLDER --port PLACEHOLDER_PORT
Restart=on-failure
RestartSec=5
User=PLACEHOLDER_USER
Group=PLACEHOLDER_GROUP

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=%h/.opendesk

[Install]
WantedBy=multi-user.target
SERVICEEOF
        SERVICE_FILE="/tmp/opendesk-relay.service"
    fi
fi

# Read the service template and update paths
sed -e "s|ExecStart=.*$|ExecStart=$PYTHON_CMD --port $PORT|" \
    -e "s|User=.*|User=$(whoami)|" \
    -e "s|Group=.*|Group=$(id -gn)|" \
    "$SERVICE_FILE" > "/tmp/$SERVICE_NAME.service"

sudo cp "/tmp/$SERVICE_NAME.service" "$SYSTEMD_DIR/$SERVICE_NAME.service"
rm "/tmp/$SERVICE_NAME.service"

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

echo ""
echo "✅ Service installed: $SERVICE_NAME"
echo ""
echo "   Start:  sudo systemctl start $SERVICE_NAME"
echo "   Stop:   sudo systemctl stop $SERVICE_NAME"
echo "   Status: sudo systemctl status $SERVICE_NAME"
echo "   Logs:   sudo journalctl -u $SERVICE_NAME -f"
echo ""
echo "   Relay will listen on port $PORT"
echo ""
echo "   To update after code changes:"
echo "     cd $RELAY_DIR && git pull && sudo systemctl restart $SERVICE_NAME"
