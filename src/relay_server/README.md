# Manuale del Relay Server di OpenDesk

## Indice

1. [Introduzione](#1-introduzione)
2. [Architettura e ruolo nella connettività](#2-architettura-e-ruolo-nella-connettività)
3. [Avvio e configurazione](#3-avvio-e-configurazione)
4. [Protocollo di comunicazione](#4-protocollo-di-comunicazione)
5. [Ciclo di vita di una sessione relay](#5-ciclo-di-vita-di-una-sessione-relay)
6. [Funzioni dettagliate del server](#6-funzioni-dettagliate-del-server)
7. [Codici e messaggi di errore](#7-codici-e-messaggi-di-errore)
8. [Sicurezza](#8-sicurezza)
9. [Installazione come servizio systemd](#9-installazione-come-servizio-systemd)
10. [Riferimenti codice sorgente](#10-riferimenti-codice-sorgente)

---

## 1. Introduzione

Il **Relay Server** di OpenDesk è un server TCP standalone che funge da **fallback di connettività** quando la connessione diretta P2P (peer-to-peer) tramite WebRTC non è possibile. Questa situazione si verifica tipicamente quando:

- Uno o entrambi i peer sono dietro **NAT simmetrici** (Symmetrical NAT)
- **Firewall aziendali** bloccano le porte necessarie per WebRTC (UDP 3478, range 49152–65535)
- La rete utilizza **proxy trasparenti** che interferiscono con i protocolli ICE/STUN
- Le connessioni via **TURN** non sono disponibili o configurate

Il relay server adotta il **modello AnyDesk/TeamViewer**: un host crea una sessione, ottiene un ID numerico (session ID), e un guest si connette usando quell'ID. Il relay forwarda i messaggi tra i due peer.

Implementazione: progetto standalone **`../opendesk-relay`** (file principale
`server.py`). Questo manuale descrive il comportamento del server e la sua
integrazione con il client OpenDesk; le costanti e il protocollo condivisi
sono definiti in `opendesk/network/protocol.py`.

---

## 2. Architettura e ruolo nella connettività

### 2.1 Stack di rete OpenDesk

```
┌──────────────────────────────────────────────────┐
│              OpenDesk Client                      │
│                                                    │
│   ┌──────────┐    ┌───────────┐    ┌───────────┐ │
│   │  P2P via  │    │  STUN     │    │   Relay   │ │
│   │  WebRTC   │───▶│ Discovery │───▶│  Client   │ │
│   │ (aiortc)  │    │           │    │           │ │
│   └──────────┘    └───────────┘    └─────┬─────┘ │
│                                          │       │
└──────────────────────────────────────────┼───────┘
                                           │
                                    ┌──────▼──────┐
                                    │  Relay       │
                                    │  Server      │
                                    │ (TCP :8474)  │
                                    └─────────────┘
```

### 2.2 Quando viene usato il relay

1. Il client tenta prima la connessione **P2P diretta** via WebRTC (ICE/STUN)
2. Se il P2P fallisce (ICE state = `failed` o timeout), il client attiva la **modalità relay**
3. Il relay server viene configurato nelle impostazioni di rete:
   - **Settings → Network → Enable relay fallback** (attivo per default)
   - **Relay Host**: indirizzo pubblico del server relay
   - **Relay Port**: porta TCP (default 8474)

### 2.3 Componenti coinvolte

| Componente | Ruolo |
|---|---|
| `opendesk-relay/server.py` | Server relay standalone (repo separato `opendesk-relay`) |
| `opendesk/network/protocol.py` | Definizione messaggi relay (`RELAY_REGISTER`, `RELAY_ROUTE`, `RELAY_PEER_LIST`) + serializzazione |
| `opendesk/network/p2p.py` | `SignallingClient` e integrazione client-side |
| `opendesk/ui/settings_dialog.py` | Impostazioni relay nell'interfaccia utente |
| `opendesk/crypto/auth.py` | Generazione session ID, hashing password (Argon2id) |

---

## 3. Avvio e configurazione

### 3.1 Avvio rapido

```bash
# Con uv (raccomandato)
uv run relay-server --port 8474

# Con pip
relay-server --port 8474
```

> **Nota:** Il relay server è ora un'app standalone in `../opendesk-relay/`.
> Documentazione completa: `../opendesk-relay/README.md`

### 3.2 Opzioni CLI

```
relay-server [--host HOST] [--port PORT] [--debug]
```

| Opzione | Default | Descrizione |
|---|---|---|
| `--host` | `0.0.0.0` | Indirizzo su cui il server ascolta. Usare `127.0.0.1` per solo locale |
| `--port` | `8474` | Porta TCP su cui ascoltare |
| `--debug` | `False` | Abilita logging di debug (livello DEBUG) |

### 3.3 Esempi di avvio

```bash
# Ascolto su tutte le interfacce (default)
uv run relay-server

# Porta personalizzata
uv run relay-server --port 9443

# Solo interfaccia locale (per test)
uv run relay-server --host 127.0.0.1

# Debug completo
uv run relay-server --debug
```

### 3.4 Output di avvio tipico

```
2025-01-15 10:30:00,123 [INFO] opendesk.crypto.auth: Loaded 0 credentials from /home/user/.opendesk/credentials.json
2025-01-15 10:30:00,124 [INFO] server: Relay server listening on 0.0.0.0:8474
```

---

## 4. Protocollo di comunicazione

### 4.1 Layer di trasporto

Il relay server utilizza **TCP** come layer di trasporto. Il formato dei messaggi è definito in `opendesk/network/protocol.py`.

### 4.2 Formato del frame

Ogni messaggio è incapsulato in un frame con header a lunghezza fissa:

```
┌─────────────────────────────────────────────────────┐
│ 4 byte (UInt32 BE)  │  1 byte  │  Payload variabile │
│ = lunghezza payload │ = tipo   │ = MessagePack       │
└─────────────────────────────────────────────────────┘
```

- **Header**: intero unsigned 32-bit big-endian che indica la lunghezza del body (escluso l'header stesso)
- **Tipo**: byte che identifica il tipo di messaggio (es. `0x80` = RELAY_REGISTER)
- **Payload**: serializzato in **MessagePack** (formato binario compatto simile a JSON)

### 4.3 Tipi di messaggio relay

Dalla classe `MessageType` in `protocol.py`:

| Codice | Costante | Descrizione |
|---|---|---|
| `0x80` | `RELAY_REGISTER` | Registrazione peer presso il relay (crea o si unisce a una sessione) |
| `0x81` | `RELAY_ROUTE` | Instradamento di un messaggio attraverso il relay al peer abbinato |
| `0x82` | `RELAY_PEER_LIST` | Lista dei peer connessi (inviata all'host quando un guest si unisce) |
| `0x70` | `PING` | Keep-alive / misura latenza |
| `0x71` | `PONG` | Risposta a PING |
| `0x72` | `DISCONNECT` | Richiesta di disconnessione |
| `0x73` | `ERROR` | Messaggio di errore |

### 4.4 Struttura dei payload

#### RELAY_REGISTER (0x80) — Richiesta di registrazione

```python
# Il peer si presenta al relay:
{
    "session_id": ""  # Se vuoto: crea nuova sessione
                      # Se presente: si unisce a sessione esistente
}

# Risposta del relay (nuova sessione):
{
    "session_id": "123 456 789"  # ID della sessione creata
}

# Risposta del relay (sessione esistente, pairing riuscito):
{
    "session_id": "123 456 789",
    "paired": True
}
```

#### RELAY_ROUTE (0x81) — Instradamento messaggio

```python
{
    "inner_type": <int>,        # Tipo del messaggio originale (es. 0x60 per CHAT)
    "inner_payload": { ... }    # Payload del messaggio originale
}
```

Il relay server estrae `inner_type` e `inner_payload` e crea un nuovo oggetto `Message` da inoltrare al peer destinatario.

#### RELAY_PEER_LIST (0x82) — Notifica nuovi peer

```python
{
    "peers": ["peer-abc123"]  # Lista di peer_id connessi
}
```

#### ERROR (0x73)

```python
{
    "code": 404,          # Codice errore numerico
    "message": "..."      # Descrizione
}
```

---

## 5. Ciclo di vita di una sessione relay

### 5.1 Diagramma di flusso

```
┌───────┐                    ┌───────────┐                    ┌───────┐
│ Host  │                    │   Relay   │                    │ Guest │
│       │                    │   Server  │                    │       │
│──RELAY_REGISTER({""})─────▶│           │                    │       │
│       │                    │           │                    │       │
│◀──────│──{"123 456 789"}───│           │                    │       │
│       │                    │           │                    │       │
│       │                    │           │──RELAY_REGISTER(   │       │
│       │                    │           │  {"123 456 789"})──│──────▶│
│       │                    │           │                    │       │
│       │                    │◀──────────│──{"paired":True}───│       │
│       │                    │           │                    │       │
│◀──────│──RELAY_PEER_LIST──│           │                    │       │
│       │                    │           │                    │       │
│       │                    │           │                    │       │
│──RELAY_ROUTE→  ...  →─────▶──────────▶│──RELAY_ROUTE→...──▶│──────▶│
│       │                    │           │                    │       │
│◀──────│← ... RELAY_ROUTE──│◀──────────│← ... RELAY_ROUTE──│◀──────│
│       │                    │           │                    │       │
│       │                    │           │                    │       │
│──PING─────────────────────▶│           │                    │       │
│◀──────│───PONG─────────────│           │                    │       │
│       │                    │           │                    │       │
│──DISCONNECT────────────────▶│           │                    │       │
│       │                    │           │                    │       │
│       │                    │──ERROR(410)→ al paired peer    │       │
└───────┘                    └───────────┘                    └───────┘
```

### 5.2 Fase 1: Creazione sessione (Host)

1. L'host si connette al relay server via TCP
2. Invia un messaggio `RELAY_REGISTER` con `session_id = ""`
3. Il relay server:
   - Genera un nuovo session ID (es. `"123 456 789"`)
   - Memorizza l'associazione `session_id → host_peer_id`
   - Risponde con il session ID creato
4. L'host mostra il session ID all'utente (da comunicare al guest)

### 5.3 Fase 2: Join sessione (Guest)

1. Il guest si connette al relay server
2. Invia un messaggio `RELAY_REGISTER` con il session ID ricevuto dall'host
3. Il relay server:
   - Cerca il session ID nella tabella delle sessioni
   - Verifica che l'host sia ancora connesso
   - **Abbina (pair)** i due peer: imposta `paired_peer_id` su entrambi
   - Invia conferma al guest con `paired: True`
   - Invia una `RELAY_PEER_LIST` all'host con l'ID del guest

### 5.4 Fase 3: Scambio messaggi

Una volta abbinati:
- Ogni messaggio inviato da un peer non riconosciuto come comando relay viene automaticamente **forwardato al peer abbinato**
- Per messaggi specifici, si usa `RELAY_ROUTE` per incapsulare il messaggio originale con il suo tipo e payload
- Il relay funge da **proxy trasparente**: non modifica il contenuto dei messaggi

### 5.5 Fase 4: Disconnessione

1. Un peer invia `DISCONNECT` o chiude la connessione TCP
2. Il relay server:
   - Rimuove il peer dalla tabella `_peers`
   - Invia un `ERROR` con codice `410 (Peer disconnected)` al peer ancora connesso
   - Resetta il `paired_peer_id` del peer superstite
   - Se il peer disconnesso era l'host, elimina anche la sessione
3. Il peer superstite può attendere una riconnessione o terminare

### 5.6 Keep-alive e timeout

- I peer inviano **PING** ogni 30 secondi (costante `_PING_INTERVAL`)
- Il relay risponde con **PONG**
- Se un peer non mostra attività per **120 secondi** (costante `_PEER_TIMEOUT`), viene considerato **stale** e disconnesso automaticamente
- Il controllo avviene in un loop asincrono periodico (`_cleanup_loop`)

---

## 6. Funzioni dettagliate del server

### 6.1 Classe `RelayServer`

**File:** `opendesk-relay/server.py` (repo separato `opendesk-relay`)

```python
class RelayServer:
    def __init__(self, host="0.0.0.0", port=8474, auth_manager=None)
```

#### 6.1.1 Costruttore

| Parametro | Default | Descrizione |
|---|---|---|
| `host` | `"0.0.0.0"` | Indirizzo di bind |
| `port` | `8474` | Porta TCP |
| `auth_manager` | `None` | Istanza di `AuthManager` opzionale per autenticazione |

#### 6.1.2 Metodi principali

| Metodo | Descrizione |
|---|---|
| `start()` | Avvia il server TCP asincrono e il loop di cleanup periodico |
| `stop()` | Ferma il server, disconnette tutti i peer e pulisce lo stato |
| `_handle_client(reader, writer)` | Callback per ogni nuova connessione TCP. Crea un oggetto `RelayPeer` e avvia il loop del peer |
| `_peer_loop(peer)` | Loop principale per un peer: legge messaggi in continuazione |
| `_handle_message(peer, msg)` | Router centrale: smista il messaggio in base al tipo |
| `_handle_register(peer, payload)` | Gestisce registrazione/join di sessione |
| `_handle_route(peer, payload)` | Gestisce instradamento messaggi al peer abbinato |
| `_send(peer, msg)` | Invia un messaggio serializzato a un peer |
| `_remove_peer(peer_id)` | Rimuove un peer e pulisce lo stato associato |
| `_cleanup_loop()` | Loop periodico che disconnette peer inattivi |

### 6.2 Classe `RelayPeer`

```python
@dataclass
class RelayPeer:
    peer_id: str                    # Identificativo unico del peer
    writer: asyncio.StreamWriter     # Scrittore per inviare dati
    reader: asyncio.StreamReader     # Lettore per ricevere dati
    session_id: str = ""             # ID sessione (vuoto se non registrato)
    last_activity: float = ...       # Timestamp ultima attività
    authenticated: bool = False      # Flag autenticazione
    paired_peer_id: str | None = None # ID del peer abbinato
```

Ogni peer connesso riceve un `peer_id` generato automaticamente come `"peer-" + hex(id(writer))`.

### 6.3 Tabella delle sessioni

```python
self._sessions: dict[str, str] = {}   # session_id → host_peer_id
self._peers: dict[str, RelayPeer] = {} # peer_id → RelayPeer
```

Esempio di stato interno con due peer connessi e abbinati:

```python
_peers = {
    "peer-abc": RelayPeer(peer_id="peer-abc", session_id="123 456 789",
                          paired_peer_id="peer-def", ...),
    "peer-def": RelayPeer(peer_id="peer-def", session_id="123 456 789",
                          paired_peer_id="peer-abc", ...),
}
_sessions = {
    "123 456 789": "peer-abc",  # host della sessione
}
```

### 6.4 Schema di routing dei messaggi

```
                    ┌───────────────────┐
                    │  _handle_message  │
                    └────────┬──────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
              ▼              ▼              ▼
     RELAY_REGISTER   RELAY_ROUTE     Altri messaggi
              │              │              │
              ▼              │              ▼
     _handle_register       │     Se paired_peer_id esiste:
              │             │     forwarda al peer abbinato
              ▼             ▼
     Crea/join     Estrae inner_type
     sessione      e inner_payload
                   Ricostruisce Message
                   Invia al peer abbinato
```

**Comportamento default:** Se un messaggio ricevuto non è `RELAY_REGISTER`, `RELAY_ROUTE`, `PING` o `DISCONNECT`, e il peer ha un `paired_peer_id`, il messaggio viene automaticamente forwardato al peer abbinato. Questo permette a qualsiasi tipo di messaggio del protocollo OpenDesk (video, input, clipboard, file, chat...) di transitare attraverso il relay senza modifiche.

---

## 7. Codici e messaggi di errore

Il relay utilizza i seguenti codici di errore (standardizzati in `MessageType.ERROR`):

| Codice | Significato | Causa |
|---|---|---|
| `404` | Session not found | Il session ID specificato non esiste (guest tenta di unirsi a sessione inesistente) |
| `410` | Host disconnected | L'host della sessione si è disconnesso prima che il guest si unisse |
| `410` | Peer disconnected | Il peer abbinato si è disconnesso (inviato al peer superstite) |

---

## 8. Sicurezza

### 8.1 Autenticazione Argon2id

Il relay server supporta autenticazione tramite `AuthManager` (da `opendesk/crypto/auth.py`):

- Le password sono hashate con **Argon2id** (memory-hard, resistente a GPU/ASIC)
- Parametri: 2 iterazioni, 19 MiB memoria, 1 thread — minimo raccomandato OWASP
- Supporto **One-Time Password (OTP)**: password monouso valide 5 minuti
- Credenziali persistenti su file JSON in `~/.opendesk/credentials.json`
- Gli hash creati con parametri precedenti vengono ri-generati automaticamente
  al primo login riuscito (`needs_rehash`)

### 8.2 Session ID

- Formato: `"XXX YYY ZZZ"` (9 cifre in blocchi da 3, stile AnyDesk)
- Generato con `secrets.choice()` (CSPRNG), così come gli OTP
- Unicità garantita: se il generatore produce un duplicato, continua a rigenerare

### 8.3 Cleanup automatico

- Peer inattivi per 120 secondi → disconnessione forzata
- Sessioni OTP scadute → rimosse periodicamente

### 8.4 Limitazione dimensione messaggi

- Massimo 100 MB per messaggio (costante `_MAX_MESSAGE_SIZE` in `protocol.py`)

### 8.5 Crittografia end-to-end (E2E)

Il relay server **non decifra** i messaggi. La crittografia E2E (Curve25519 + XSalsa20-Poly1305) è gestita a livello applicativo:

- Scambio chiavi via `KEY_EXCHANGE` / `KEY_EXCHANGE_ACK`
- Perfect Forward Secrecy (PFS) con chiavi effimere per sessione
- Il relay forwarda i messaggi crittografati **senza poterli leggere**
- File: `opendesk/crypto/e2ee.py`

> **Nota:** Anche se i messaggi transitano attraverso il relay confidenziali grazie all'E2E, il relay server ha comunque visibilità degli indirizzi IP dei peer e delle dimensioni dei messaggi. In contesti ad alta sicurezza, si raccomanda di usare anche TLS per il trasporto (attualmente non implementato, il relay usa TCP plain).

### 8.6 Note di sicurezza per il deploy

- **Eseguire il relay su una rete trusted** (meglio un VPS dedicato)
- **Configurare un firewall** per limitare l'accesso alla porta TCP 8474
- **Usare fail2ban** per bloccare tentativi di connessione abusivi
- Le credenziali sono memorizzate in chiaro sul filesystem (hash Argon2id, ma pur sempre file locale)
- Il relay non supporta TLS nativamente; si può usare **stunnel** o un reverse proxy (nginx, haproxy) per aggiungere crittografia TLS al canale

---

## 9. Installazione come servizio systemd

### 9.1 Script di installazione

```bash
# Dalla directory del progetto relay
cd ../opendesk-relay
sudo ./install-relay.sh [--port PORT]
```

Lo script:
1. Rileva la presenza di `uv` o `pip`
2. Installa le dipendenze
3. Crea la directory di configurazione `~/.opendesk`
4. Genera e installa il file service systemd in `/etc/systemd/system/opendesk-relay.service`
5. Abilita il servizio all'avvio

### 9.2 Comandi di gestione

```bash
sudo systemctl start opendesk-relay      # Avvio
sudo systemctl stop opendesk-relay       # Arresto
sudo systemctl restart opendesk-relay    # Riavvio
sudo systemctl status opendesk-relay     # Stato
sudo systemctl enable opendesk-relay     # Abilita all'avvio
sudo journalctl -u opendesk-relay -f     # Log in tempo reale
```

### 9.3 File service di esempio

```ini
[Unit]
Description=OpenDesk Relay Server
After=network.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/home/giampaolo/.local/bin/uv run --directory /home/giampaolo/Codium/opendesk-relay relay-server --port 8474
Restart=on-failure
RestartSec=5
User=giampaolo
Group=giampaolo

# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=/home/giampaolo/.opendesk

[Install]
WantedBy=multi-user.target
```

### 9.4 Hardening systemd

Il service include misure di sicurezza **systemd-hardening**:

| Direttiva | Effetto |
|---|---|
| `NoNewPrivileges=true` | Impedisce escalation di privilegi |
| `PrivateTmp=true` | Isola `/tmp` |
| `ProtectSystem=full` | Rende read-only `/usr` e `/etc` |
| `ProtectHome=true` | Rende read-only la home (eccetto `ReadWritePaths`) |
| `ReadWritePaths=~/.opendesk` | Permette sola scrittura configurazione |

---

## 10. Riferimenti codice sorgente

### 10.1 File principali

| File | Descrizione |
|---|---|
| `opendesk-relay/server.py` | Server TCP asincrono (classe `RelayServer`, `RelayPeer`, entry point CLI) — repo separato |
| `opendesk/network/protocol.py` | Definizione messaggi relay e serializzazione MessagePack |
| `opendesk/network/p2p.py` | Client-side P2P + `SignallingClient` per relay |
| `opendesk/network/nat_traversal.py` | STUN discovery e configurazione TURN |
| `opendesk/crypto/auth.py` | `AuthManager`, hashing Argon2id, session ID |
| `opendesk/crypto/e2ee.py` | Crittografia E2E (NaCl Box) |
| `opendesk/ui/settings_dialog.py` | UI impostazioni relay (Tab Network) |
| `install-relay.sh` | Script di installazione servizio (in `opendesk-relay/`) |
| `opendesk-relay.service` | Template servizio systemd (in `opendesk-relay/`) |

### 10.2 Costanti importanti

```python
# opendesk-relay/server.py
_DEFAULT_PORT = 8474        # Porta TCP default
_PING_INTERVAL = 30         # Secondi tra PING
_PEER_TIMEOUT = 120         # Secondi senza attività → disconnessione

# opendesk/network/protocol.py
_PROTOCOL_VERSION = 1       # Versione protocollo
_MAX_MESSAGE_SIZE = 100 * 1024 * 1024  # 100 MB
_HEADER_FORMAT = "!I"       # 4 byte unsigned big-endian

# opendesk/crypto/auth.py
_SESSION_ID_LENGTH = 9      # 9 cifre (es. "123 456 789")
_OTP_LENGTH = 8             # 8 caratteri OTP
_OTP_VALIDITY_SECONDS = 300 # 5 minuti validità OTP
```

### 10.3 Entry point

Il relay server è registrato come script CLI nel `pyproject.toml` del
progetto separato `opendesk-relay`:

```toml
[project.scripts]
relay-server = "server:main"   # vedi repo opendesk-relay
```

### 10.4 Test

I test pertinenti al relay si trovano in:

| File | Contenuto |
|---|---|
| `tests/test_network.py` | Test serializzazione messaggi (codici relay `0x80`–`0x82`) |
| `tests/test_integration.py` | Scenari end-to-end con handshake, E2E, ping/pong |
| `tests/test_crypto.py` | Test crittografia e autenticazione |
| `tests/test_protocol_edge.py` | Test casi limite del protocollo |
| `tests/test_relay_integration.py` | Test integrazione con il relay |

---

## Appendice A: Schema riassuntivo messaggi relay

```
┌────────────────────┬──────────┬────────────────────────────┬──────────────────┐
│ Messaggio          │ Codice   │ Direzione                 │ Effetto          │
├────────────────────┼──────────┼────────────────────────────┼──────────────────┤
│ RELAY_REGISTER     │ 0x80     │ Client → Server           │ Crea/join sessione│
│ RELAY_ROUTE        │ 0x81     │ Client → Server           │ Forwarda messaggio│
│ RELAY_PEER_LIST    │ 0x82     │ Server → Host             │ Notifica nuovo    │
│                    │          │                            │ peer connesso    │
│ PING               │ 0x70     │ Bidirezionale             │ Keep-alive       │
│ PONG               │ 0x71     │ Bidirezionale             │ Risposta PING    │
│ DISCONNECT         │ 0x72     │ Client → Server           │ Richiesta fine   │
│ ERROR              │ 0x73     │ Server → Client           │ Notifica errore  │
└────────────────────┴──────────┴────────────────────────────┴──────────────────┘
```

## Appendice B: Esempio di sessione completa (sequenza terminale)

```
[Host]     CONNECT  →  TCP connect (:8474)
[Host]     SEND     →  RELAY_REGISTER {session_id: ""}
[Server]   SEND     →  RELAY_REGISTER {session_id: "456 789 123"}
[Host]     visualizza "456 789 123"

[Guest]    CONNECT  →  TCP connect (:8474)
[Guest]    SEND     →  RELAY_REGISTER {session_id: "456 789 123"}
[Server]   verifica → session_id esiste, host connesso
[Server]   PAIR     → host ↔ guest
[Server]   SEND     →  RELAY_REGISTER {paired: true} al guest
[Server]   SEND     →  RELAY_PEER_LIST {peers: ["peer-guest"]} all'host

[Host←→Guest]       ↔  RELAY_ROUTE (messaggi applicativi)

[Guest]    SEND     →  DISCONNECT
[Server]   SEND     →  ERROR {code: 410, message: "Peer disconnected"} all'host
[Server]   REMOVE   → guest rimosso
```

---

*Versione documento: 1.0 — Ultimo aggiornamento: 2026-07-06*
*OpenDesk Relay Server — Documentazione tecnica*
