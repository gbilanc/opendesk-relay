#!/bin/bash
# deploy/check-connectivity.sh — verifica end-to-end dell'esposizione del relay
#
# Controlla, in ordine:
#   1. listener locale (ss)
#   2. handshake TCP locale sulla 8474
#   3. regole UFW per la 8474 (solo se eseguito come root)
#   4. raggiungibilità reale da Internet (check-host.net, 3 nodi)
#   5. eventuale NAT a monte (IP pubblico != IP del gateway)
#
# Uso:
#   ./deploy/check-connectivity.sh                 # default: porta 8474, relay.gibisoft.net
#   sudo PORT=8474 HOST=relay.example.com ./deploy/check-connectivity.sh
#
# Exit: 0 = tutto ok, 1 = locale ko, 2 = non raggiungibile da Internet
set -uo pipefail

PORT="${PORT:-8474}"
HOST="${HOST:-relay.gibisoft.net}"
LOCAL_OK=0
EXTERNAL_OK=0

hr() { printf '\n── %s ──\n' "$*"; }

# ---------------------------------------------------------------- 1. listener
hr "1. Listener locale"
if ss -tln | grep -qE ":$PORT "; then
  ss -tln | grep -E ":$PORT "
else
  echo "  ✗ nessun listener su :$PORT — servizio non in esecuzione?"
fi

# ------------------------------------------------- 2. handshake TCP locale
hr "2. Handshake TCP locale (127.0.0.1:$PORT)"
curl -sS -m 5 -o /dev/null "http://127.0.0.1:$PORT/" >/dev/null 2>&1
rc=$?
# 52/56/18 = TCP accettato ma risposta non-HTTP: è il caso atteso (protocollo binario relay)
case $rc in
  0|18|52|56) echo "  ✓ TCP accettato (curl rc=$rc, risposta non-HTTP: atteso)"; LOCAL_OK=1 ;;
  7)          echo "  ✗ connessione rifiutata (rc=7): servizio giù o bind su altro indirizzo" ;;
  28)         echo "  ✗ timeout (rc=28): nessuno in ascolto / filtro locale" ;;
  *)          echo "  ? curl rc=$rc" ;;
esac

# ------------------------------------------------------------ 3. regole UFW
hr "3. Regole firewall (UFW) per $PORT"
if [ "$(id -u)" -eq 0 ] && command -v ufw >/dev/null; then
  if ufw status | grep -qE "(^| )$PORT(/tcp)? "; then
    ufw status | grep -E "(^| )$PORT(/tcp)? "
    echo "  ⚠ verifica che la regola valga per «Anywhere» o per la CIDR del client:"
    echo "    una regola solo per 192.168.178.0/24 NON apre la porta a Internet."
  else
    echo "  ✗ nessuna regola UFW per $PORT (con DEFAULT_INPUT_POLICY=DROP la porta è chiusa)"
  fi
else
  echo "  (serve root: sudo ufw status | grep $PORT)"
fi

# ------------------------------------------------- 4. test da Internet
hr "4. Raggiungibilità da Internet (check-host.net)"
req=$(curl -sS -m 15 "https://check-host.net/check-tcp?host=$HOST:$PORT&max_nodes=3" \
        -H 'Accept: application/json' 2>/dev/null)
req_id=$(printf '%s' "$req" | sed -n 's/.*"request_id":"\([^"]*\)".*/\1/p')
if [ -z "$req_id" ]; then
  echo "  ! impossibile avviare il check esterno (rete/DNS?)"
else
  sleep 12
  res=$(curl -sS -m 20 "https://check-host.net/check-result/$req_id" \
          -H 'Accept: application/json' 2>/dev/null)
  printf '%s\n' "$res" | sed 's/},{/}\n{/g' | sed 's/^  */  /'
  if printf '%s' "$res" | grep -q '"time"'; then
    EXTERNAL_OK=1
    echo "  ✓ porta $PORT raggiungibile da Internet"
  else
    echo "  ✗ porta $PORT NON raggiungibile da Internet (timeout su tutti i nodi)"
  fi
  echo "  report: https://check-host.net/check-report/$req_id"
fi

# --------------------------------------------------------- 5. NAT a monte
hr "5. Indirizzamento / NAT"
gw=$(ip route | awk '/^default/ {print $3; exit}')
pub=$(curl -sS -m 8 https://api.ipify.org 2>/dev/null)
echo "  gateway: ${gw:-n/d}   ip-pubblico: ${pub:-n/d}"
if [ -n "$gw" ] && [ -n "$pub" ]; then
  if ip -4 -brief addr | grep -q " $gw/"; then
    echo "  → la macchina ha il gateway su una propria interfaccia: probabile IP pubblico diretto"
  elif [ "$EXTERNAL_OK" -eq 1 ]; then
    echo "  → NAT presente, ma il port forward TCP $PORT è attivo (porta raggiungibile da Internet)"
  else
    echo "  → NAT: serve una condivisione porta (port forward) TCP $PORT sul router verso questo host"
  fi
fi

# ------------------------------------------------------------- verdetto
hr "Verdetto"
[ "$LOCAL_OK" -eq 1 ]    && echo "  locale:      OK"      || echo "  locale:      KO"
[ "$EXTERNAL_OK" -eq 1 ] && echo "  internet:    OK"      || echo "  internet:    KO"
if [ "$LOCAL_OK" -eq 1 ] && [ "$EXTERNAL_OK" -eq 0 ]; then
  echo
  echo "  Se il servizio è locale OK ma Internet KO, in ordine di probabilità:"
  echo "    a) UFW: regola mancante o limitata alla LAN → sudo ufw allow $PORT/tcp"
  echo "    b) router/NAT: condivisione porta TCP $PORT mancante o verso IP sbagliato"
  echo "    c) firewall del provider / security group dell'hoster"
fi
echo

if [ "$LOCAL_OK" -ne 1 ]; then exit 1; fi
if [ "$EXTERNAL_OK" -ne 1 ]; then exit 2; fi
exit 0
