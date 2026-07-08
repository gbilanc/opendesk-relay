#!/bin/bash
#
# install-relay.sh — Install OpenDesk Relay Server on Linux (via uv)
#
# This script installs the relay server as a systemd service with:
#   - Dedicated system user (opendesk-relay)
#   - YAML config in /etc/opendesk-relay/
#   - Log rotation in /etc/logrotate.d/
#   - Default environment in /etc/default/
#
# Build & install is done via uv (no pip required).
#
# Usage:
#   sudo ./install-relay.sh                 # install from local source
#   sudo ./install-relay.sh --prefix /usr   # custom prefix
#   sudo ./install-relay.sh --port 9443     # custom port
#
# For a quick dev install you can also use:
#   sudo make install
#

# bash-only options (skip if running under sh/dash)
if [ -n "${BASH_VERSION:-}" ]; then
    set -euo pipefail
else
    set -eu
fi

# ═══════════════════════════════════════════════════════════════════════════
# Defaults
# ═══════════════════════════════════════════════════════════════════════════
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
NAME="opendesk-relay-server"
SERVICE_NAME="opendesk-relay"

PREFIX="${PREFIX:-/usr/local}"
BINDIR="${PREFIX}/bin"
SYSTEMD_DIR="/etc/systemd/system"
DEFAULT_DIR="/etc/default"
LOGROTATE_DIR="/etc/logrotate.d"
RELAY_CONF_DIR="/etc/opendesk-relay"
RELAY_LOG_DIR="/var/log/opendesk-relay"
RELAY_DATA_DIR="/var/lib/opendesk-relay"

UV="${UV:-uv}"
PORT="${PORT:-8474}"
USERNAME="opendesk-relay"
GROUPNAME="opendesk-relay"

# ═══════════════════════════════════════════════════════════════════════════
# Help & args
# ═══════════════════════════════════════════════════════════════════════════
usage() {
    cat <<EOF
Usage: $0 [OPTIONS]

Install OpenDesk Relay Server as a systemd service.

Options:
  --prefix DIR     Install prefix (default: ${PREFIX})
  --port PORT      Relay TCP port (default: ${PORT})
  --uv PATH        uv executable (default: ${UV})
  --user USER      System user for the service (default: ${USERNAME})
  --help           Show this message

Environment variables:
  PREFIX, PORT, UV — same as the corresponding flags.

Examples:
  sudo $0                          # standard install
  sudo $0 --port 9443              # custom port
  sudo $0 --prefix /opt/opendesk   # custom prefix
EOF
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) PREFIX="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --uv) UV="$2"; shift 2 ;;
        --user) USERNAME="$2"; GROUPNAME="$2"; shift 2 ;;
        --help|-h) usage ;;
        *) echo "❌ Unknown option: $1"; usage ;;
    esac
done

# ═══════════════════════════════════════════════════════════════════════════
# Sanity checks
# ═══════════════════════════════════════════════════════════════════════════
if [ "$(id -u)" -ne 0 ]; then
    echo "❌ This script must be run as root (use sudo)."
    exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
    echo "❌ systemd not found — this script is for Linux with systemd only."
    echo "   On other platforms run: relay-server --port ${PORT}"
    exit 1
fi

if ! command -v "$UV" >/dev/null 2>&1; then
    echo "❌ uv not found at '${UV}'."
    echo "   Install it with: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

echo ""
echo "═══════════════════════════════════════════"
echo " OpenDesk Relay Server — Installer (uv)"
echo "═══════════════════════════════════════════"
echo " Source dir  : ${SCRIPT_DIR}"
echo " uv          : ${UV}"
echo " Prefix      : ${PREFIX}"
echo " Port        : ${PORT}"
echo " User        : ${USERNAME}"
echo ""

# ═══════════════════════════════════════════════════════════════════════════
# 1. Create system user
# ═══════════════════════════════════════════════════════════════════════════
echo "── Step 1/7: Creating system user ──"

if getent group "$GROUPNAME" >/dev/null 2>&1; then
    echo "  ✓ Group '${GROUPNAME}' already exists"
else
    groupadd --system "$GROUPNAME"
    echo "  ✓ Group '${GROUPNAME}' created"
fi

if getent passwd "$USERNAME" >/dev/null 2>&1; then
    echo "  ✓ User '${USERNAME}' already exists"
else
    useradd --system \
        --gid "$GROUPNAME" \
        --no-create-home \
        --home-dir "$RELAY_DATA_DIR" \
        --shell /usr/sbin/nologin \
        --comment "OpenDesk Relay Server" \
        "$USERNAME"
    echo "  ✓ User '${USERNAME}' created"
fi

echo ""

# ═══════════════════════════════════════════════════════════════════════════
# 2. Build & install Python package with uv
# ═══════════════════════════════════════════════════════════════════════════
echo "── Step 2/7: Building & installing Python package ──"

cd "$SCRIPT_DIR"
echo "  → Building wheel..."
"$UV" build --wheel

# Create a dedicated virtual environment for the relay server
# (avoids PEP 668 externally-managed Python errors on modern distros)
VENV_DIR="${RELAY_DATA_DIR}/venv"

echo "  → Creating virtual environment at ${VENV_DIR}..."
install -d -m 0755 "$(dirname "$VENV_DIR")" 2>/dev/null || true
"$UV" venv "$VENV_DIR"

echo "  → Installing wheel into ${VENV_DIR}..."
"$UV" pip install --python "${VENV_DIR}/bin/python" dist/*.whl

# Symlink binary into PATH
install -d -m 0755 "${BINDIR}"
ln -sf "${VENV_DIR}/bin/relay-server" "${BINDIR}/relay-server"

echo "  ✓ Package installed into dedicated venv"
echo "  ✓ Symlink: ${BINDIR}/relay-server → ${VENV_DIR}/bin/relay-server"
echo ""

# ═══════════════════════════════════════════════════════════════════════════
# 3. Create directories
# ═══════════════════════════════════════════════════════════════════════════
echo "── Step 3/7: Creating directories ──"

install -d -m 0755 "$RELAY_CONF_DIR"
install -d -m 0755 "$RELAY_LOG_DIR"
install -d -m 0750 "$RELAY_DATA_DIR"
install -d -m 0755 "$SYSTEMD_DIR"
install -d -m 0755 "$DEFAULT_DIR"
install -d -m 0755 "$LOGROTATE_DIR"

echo "  ✓ Directories created"
echo ""

# ═══════════════════════════════════════════════════════════════════════════
# 4. Install config
# ═══════════════════════════════════════════════════════════════════════════
echo "── Step 4/7: Installing configuration ──"

if [ ! -f "$RELAY_CONF_DIR/relay-config.yaml" ]; then
    # Set log file path for production
    sed -e 's|^  file: ""|  file: "/var/log/opendesk-relay/relay.log"|' \
        "$SCRIPT_DIR/relay-config.yaml" > "$RELAY_CONF_DIR/relay-config.yaml"
    echo "  ✓ Config installed: ${RELAY_CONF_DIR}/relay-config.yaml"
else
    cp "$SCRIPT_DIR/relay-config.yaml" "$RELAY_CONF_DIR/relay-config.yaml.example"
    echo "  ⚠ Config exists, installed as example: ${RELAY_CONF_DIR}/relay-config.yaml.example"
fi

chown -R "$USERNAME:$GROUPNAME" "$RELAY_CONF_DIR"
chown -R "$USERNAME:$GROUPNAME" "$RELAY_DATA_DIR"
chown -R "$USERNAME:$GROUPNAME" "$RELAY_LOG_DIR"
echo ""

# ═══════════════════════════════════════════════════════════════════════════
# 5. Install systemd service
# ═══════════════════════════════════════════════════════════════════════════
echo "── Step 5/7: Installing systemd service ──"

# Determine the absolute path of relay-server (from venv or system PATH)
VENV_DIR="${RELAY_DATA_DIR}/venv"
if [ -x "${VENV_DIR}/bin/relay-server" ]; then
    RELAY_BIN="${VENV_DIR}/bin/relay-server"
elif command -v relay-server >/dev/null 2>&1; then
    RELAY_BIN="$(command -v relay-server)"
else
    RELAY_BIN="${BINDIR}/relay-server"
fi

# Build the service file with correct paths
sed -e "s|^ExecStart=.*$|ExecStart=${RELAY_BIN} --config ${RELAY_CONF_DIR}/relay-config.yaml|" \
    -e "s|User=opendesk-relay|User=${USERNAME}|" \
    -e "s|Group=opendesk-relay|Group=${GROUPNAME}|" \
    "$SCRIPT_DIR/deploy/opendesk-relay.service" > "/tmp/${SERVICE_NAME}.service"

install -m 0644 "/tmp/${SERVICE_NAME}.service" "${SYSTEMD_DIR}/${SERVICE_NAME}.service"
rm -f "/tmp/${SERVICE_NAME}.service"

systemctl daemon-reload
echo "  ✓ Service unit installed: ${SYSTEMD_DIR}/${SERVICE_NAME}.service"
echo ""

# ═══════════════════════════════════════════════════════════════════════════
# 6. Install default environment & logrotate
# ═══════════════════════════════════════════════════════════════════════════
echo "── Step 6/7: Installing defaults & logrotate ──"

# Defaults — uncomment the log file path for production
sed -e 's|^# RELAY_LOG_FILE=|RELAY_LOG_FILE=|' \
    "$SCRIPT_DIR/deploy/opendesk-relay.default" > "/tmp/${SERVICE_NAME}.default"
install -m 0644 "/tmp/${SERVICE_NAME}.default" "${DEFAULT_DIR}/${SERVICE_NAME}"
rm -f "/tmp/${SERVICE_NAME}.default"

# Logrotate
install -m 0644 "$SCRIPT_DIR/deploy/opendesk-relay.logrotate" "${LOGROTATE_DIR}/${SERVICE_NAME}"

echo "  ✓ Defaults: ${DEFAULT_DIR}/${SERVICE_NAME}"
echo "  ✓ Logrotate: ${LOGROTATE_DIR}/${SERVICE_NAME}"
echo ""

# ═══════════════════════════════════════════════════════════════════════════
# 7. Enable & start
# ═══════════════════════════════════════════════════════════════════════════
echo "── Step 7/7: Enabling service ──"

systemctl enable "$SERVICE_NAME"
systemctl start "$SERVICE_NAME" || {
    echo "  ⚠ Service didn't start. Check: journalctl -u ${SERVICE_NAME} -n 50 --no-pager"
}

echo ""

# ═══════════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════════
echo "═══════════════════════════════════════════"
echo " ✅ Installation complete!"
echo "═══════════════════════════════════════════"
echo ""
echo "   Config : ${RELAY_CONF_DIR}/relay-config.yaml"
echo "   Binary : ${RELAY_BIN}"
echo "   Logs   : ${RELAY_LOG_DIR}/relay.log"
echo "   Data   : ${RELAY_DATA_DIR}"
echo ""
echo "   Status : systemctl status ${SERVICE_NAME}"
echo "   Logs   : journalctl -u ${SERVICE_NAME} -f"
echo "   Restart: systemctl restart ${SERVICE_NAME}"
echo ""
echo "   To uninstall:"
echo "     sudo systemctl stop ${SERVICE_NAME}"
echo "     sudo systemctl disable ${SERVICE_NAME}"
echo "     sudo rm -f ${SYSTEMD_DIR}/${SERVICE_NAME}.service"
echo "     sudo rm -f ${DEFAULT_DIR}/${SERVICE_NAME}"
echo "     sudo rm -f ${LOGROTATE_DIR}/${SERVICE_NAME}"
echo "     sudo rm -f ${BINDIR}/relay-server"
echo "     sudo rm -rf ${RELAY_DATA_DIR}/venv"
echo "     sudo userdel ${USERNAME}"
echo "     sudo groupdel ${GROUPNAME}"
echo ""
