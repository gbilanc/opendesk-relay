#!/bin/bash
# deploy/setup-production.sh — installazione completa OpenDesk Relay su relay.gibisoft.net
# Eseguire con: sudo ./deploy/setup-production.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "── 1. Installazione servizio ──"
./install-relay.sh

echo "── 2. Configurazione produzione ──"
cp deploy/relay-config-prod.yaml /etc/opendesk-relay/relay-config.yaml
chown opendesk-relay:opendesk-relay /etc/opendesk-relay/relay-config.yaml

echo "── 3. Avvio servizio ──"
systemctl enable --now opendesk-relay

echo "── 4. Apache reverse proxy ──"
cp deploy/apache-relay-gibisoft.net.conf /etc/apache2/sites-available/relay-gibisoft.net.conf
a2ensite relay-gibisoft.net
a2enmod proxy proxy_http proxy_wstunnel rewrite
apachectl configtest
systemctl reload apache2

echo "── 5. Firewall ──"
ufw allow 8474/tcp
ufw allow 443/tcp

echo ""
echo "✅ Installazione completata."
systemctl status opendesk-relay --no-pager | head -10
