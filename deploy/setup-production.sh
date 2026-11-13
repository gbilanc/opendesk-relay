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

echo "── 5. Firewall locale (UFW) ──"
# La 8474 deve essere raggiungibile dai client remoti: la regola deve valere per
# «Anywhere» o per la CIDR dei client, NON solo per la LAN 192.168.x.0/24.
ufw allow 8474/tcp comment 'opendesk-relay WAN'
ufw allow 443/tcp
ufw reload
ufw status | grep -E '8474|443' || true

# Se esiste una regola restrittiva per la 8474 (es. solo LAN) va rimossa:
#   sudo ufw status numbered
#   sudo ufw delete <n. della regola 8474/tcp limitata alla LAN>

echo "── 6. Verifica esposizione (NAT + firewall) ──"
if [ -n "$(ip route | awk '/^default/ {print $3; exit}')" ] && \
   ! ip -4 -brief addr | grep -q " $(ip route | awk '/^default/ {print $3; exit}')/"; then
  echo "  ⚠ La macchina è dietro NAT."
  echo "    Serve una condivisione porta TCP 8474 sul router verso questo host."
  echo "    Su FRITZ!Box: Internet ▸ Permettere accesso ▸ Condivisioni porta ▸"
  echo "    aggiungi dispositivo → porta 8474/TCP → (443 resta com'è)."
fi
./deploy/check-connectivity.sh || echo "  ⚠ La porta 8474 non risulta raggiungibile da Internet: vedi i suggerimenti sopra."

echo ""
echo "✅ Installazione completata."
systemctl status opendesk-relay --no-pager | head -10
