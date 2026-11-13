# OpenDesk Relay Server

Standalone TCP relay server for [OpenDesk](https://github.com/opendesk/opendesk) — provides fallback connectivity when direct P2P (WebRTC) is unavailable.

## Features

- 🔁 **TCP relay** — forwards messages between paired peers
- 🔐 **Authentication** — Argon2id password hashing, session IDs (AnyDesk-style), API tokens
- 🖥️ **Web dashboard** — monitor connections, sessions, devices in real-time
- 📊 **Prometheus metrics** — `/metrics` endpoint for monitoring
- ⚙️ **Configurable** — YAML config file, environment variables, CLI args
- 🐳 **Docker support** — multi-stage Dockerfile + docker-compose
- 📝 **Structured logging** — JSON or text format, file rotation
- 🔒 **IP access control** — whitelist/blacklist by IP or CIDR
- ✅ **Health checks** — `/health`, `/health/ready`, `/health/live`

## Quick start

```bash
# Install with pip
pip install opendesk-relay-server

# Run directly
relay-server --port 8474

# Or with uv (from source)
uv run relay-server --port 8474

# Or via Python module
python -m relay_server --port 8474
```

## Linux Installation (systemd)

For a production Linux server, use the provided installer or Makefile.

### Option 1 — Makefile (recommended)

```bash
# Build wheel and install as root
sudo make install

# Custom prefix
sudo make install PREFIX=/opt/opendesk
```

### Option 2 — Install script

```bash
# Run as root
sudo ./install-relay.sh

# Custom port
sudo ./install-relay.sh --port 9443

# Custom prefix
sudo ./install-relay.sh --prefix /opt/opendesk
```

### What gets installed

| Path | Purpose |
|---|---|
| `relay-server` (in `$PREFIX/bin`) | CLI binary (pip entry point) |
| `/etc/opendesk-relay/relay-config.yaml` | YAML configuration |
| `/etc/default/opendesk-relay` | Environment variable overrides |
| `/etc/systemd/system/opendesk-relay.service` | Systemd unit |
| `/etc/logrotate.d/opendesk-relay` | Log rotation |
| `/var/log/opendesk-relay/` | Log files |
| `/var/lib/opendesk-relay/` | Data directory |

The service runs under a dedicated `opendesk-relay` system user.

### Manage the service

```bash
sudo systemctl enable --now opendesk-relay   # Start on boot
sudo systemctl status opendesk-relay          # Check status
sudo journalctl -u opendesk-relay -f          # Follow logs
sudo systemctl restart opendesk-relay         # Restart
```

### Esporre la porta 8474 su Internet

Il relay ascolta su `0.0.0.0:8474` (config `server.host`/`server.port`), ma perché i client
remoti possano connettersi servono **tre** cose in fila:

```text
client Internet → [router/NAT: port forward TCP 8474] → [ufw: allow 8474] → relay-server
```

1. **Firewall locale (UFW)** — la regola deve valere per «Anywhere» o per la CIDR dei client:

   ```bash
   sudo ufw allow 8474/tcp comment 'opendesk-relay WAN'
   sudo ufw reload
   sudo ufw status | grep 8474
   ```

   ⚠ Una regola limitata alla LAN (`ufw allow from 192.168.178.0/24 to any port 8474`)
   **non** apre la porta a Internet: il servizio risponde in LAN ma i client remoti vanno in timeout.

2. **Port forward sul router** — TCP 8474 verso l'IP LAN del server (es. `192.168.178.142`).
   Su FRITZ!Box: *Internet ▸ Permettere accesso ▸ Condivisioni porta ▸ Aggiungi dispositivo*.
   La 8484 (dashboard) **non** va inoltrata: resta su `127.0.0.1` dietro il reverse proxy Apache su 443.

3. **Firewall del provider / security group** — se il server è in cloud, la porta non standard
   va aperta anche lì.

Verifica end-to-end (listener, handshake, UFW, test reale da 3 nodi esterni, rilevamento NAT):

```bash
./deploy/check-connectivity.sh          # exit 0 = ok, 1 = locale ko, 2 = non raggiungibile da Internet
sudo ./deploy/check-connectivity.sh     # include anche le regole UFW
```

Nota: `curl http://<host>:8474/` restituisce *Empty reply from server* con exit 52 — è il
comportamento atteso, la porta parla un protocollo binario e non HTTP.

#### Accesso dalla LAN (NAT loopback)

Se il server è dietro NAT (es. `192.168.178.142` dietro un FRITZ!Box), un client nella
stessa LAN che usa il nome pubblico dipende dal *NAT loopback* del router e da un DNS che
risolva l'IP pubblico corrente: se il nome punta a un IP vecchio, o il resolver del client
è irraggiungibile, la pagina non si apre mentre da Internet funziona.

Soluzione più affidabile — mappare il nome sull'IP LAN nel file hosts del client:

```
# /etc/hosts (Linux/macOS) oppure C:\Windows\System32\drivers\etc\hosts (Windows)
192.168.178.142 relay.gibisoft.net
```

Il certificato resta valido (è emesso per il nome, non per l'IP). In alternativa si può
usare direttamente `https://192.168.178.142` (con warning del browser sul nome).
Attenzione: il NAT loopback non funziona dalla **WLAN ospiti** del router, che è isolata
dalla LAN.

### Uninstall

```bash
sudo make uninstall
```

### Makefile targets

| Target | Description |
|---|---|
| `make build` | Build pip wheel in `dist/` |
| `make install` | Install wheel + system files (as root) |
| `make uninstall` | Remove all installed files |
| `make reinstall` | Uninstall + install |
| `make clean` | Remove build artifacts |
| `make dist` | Build wheel + distribution tarball |
| `make test` | Run tests |
| `make lint` | Run ruff + mypy |

## Docker

```bash
# Build and run
docker compose up -d

# Or build manually
docker build -t opendesk-relay .
docker run -d -p 8474:8474 -p 8484:8484 opendesk-relay
```

## Configuration

The relay server uses a three-layer configuration cascade (each level overrides the previous):

1. **Defaults** → built-in sensible defaults
2. **YAML file** → `relay-config.yaml` or `~/.opendesk/relay-config.yaml`
3. **Environment variables** → `RELAY_*` prefixed
4. **CLI arguments** → `--port`, `--host`, `--debug`, etc.

### Example config

```yaml
server:
  host: "0.0.0.0"
  port: 8474

admin:
  enabled: true
  web_host: "127.0.0.1"
  web_port: 8484
  username: "admin"
  password_hash: ""    # Set via: python3 -c "from relay_server.auth import hash_password; print(hash_password('my-password'))"

auth:
  enabled: true
  whitelist_ips: []
  blacklist_ips: []

relay:
  ping_interval: 30
  peer_timeout: 120

logging:
  level: "INFO"
  format: "text"       # or "json"
  file: "/var/log/opendesk-relay/relay.log"
```

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `RELAY_HOST` | `0.0.0.0` | Relay bind address |
| `RELAY_PORT` | `8474` | Relay TCP port |
| `RELAY_ADMIN_ENABLED` | `true` | Enable web dashboard |
| `RELAY_ADMIN_HOST` | `127.0.0.1` | Dashboard bind address |
| `RELAY_ADMIN_PORT` | `8484` | Dashboard port |
| `RELAY_ADMIN_USERNAME` | `admin` | Dashboard username |
| `RELAY_ADMIN_PASSWORD_HASH` | — | Argon2id password hash |
| `RELAY_API_TOKEN` | — | API token for programmatic access |
| `RELAY_AUTH_ENABLED` | `true` | Enable authentication |
| `RELAY_WHITELIST_IPS` | — | Comma-separated CIDR whitelist |
| `RELAY_BLACKLIST_IPS` | — | Comma-separated CIDR blacklist |
| `RELAY_PING_INTERVAL` | `30` | PING interval (seconds) |
| `RELAY_PEER_TIMEOUT` | `120` | Peer timeout (seconds) |
| `RELAY_LOG_LEVEL` | `INFO` | Log level |
| `RELAY_LOG_FILE` | — | Log file path |
| `RELAY_LOG_FORMAT` | `text` | Log format (`text` or `json`) |

### CLI

```
relay-server [OPTIONS]

Options:
  --config, -c FILE     Config file path
  --host HOST           Bind address (default: 0.0.0.0)
  --port, -p PORT       TCP port (default: 8474)
  --admin-host HOST     Dashboard address (default: 127.0.0.1)
  --admin-port PORT     Dashboard port (default: 8484)
  --no-admin            Disable web dashboard
  --debug               Enable debug logging
```

## Web Dashboard

When enabled (default), the web dashboard is available at `http://127.0.0.1:8484/`.

### REST API

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/status` | Server status, version, uptime |
| `GET` | `/api/peers` | List connected peers |
| `GET` | `/api/sessions` | List active sessions |
| `GET` | `/api/devices` | List registered devices |
| `GET` | `/api/metrics` | Detailed metrics |
| `DELETE` | `/api/peers/{id}` | Disconnect a peer |
| `POST` | `/api/config/reload` | Reload configuration |

### Auth for API

- **Basic auth** with admin credentials (for the web UI)
- **Bearer token** with `api_token` (for programmatic access)

### Prometheus

Metrics available at `GET /metrics` (Prometheus text format).

### Health checks

| Endpoint | Purpose |
|---|---|
| `GET /health` | Basic health (always 200) |
| `GET /health/ready` | Readiness probe |
| `GET /health/live` | Liveness probe |

## Development

```bash
# Install dependencies
uv sync

# Run tests
make test

# Format code
make lint

# Build wheel
make build

# Build distribution tarball
make dist
```

## Project structure

```
opendesk-relay/
├── pyproject.toml              # Package metadata
├── Makefile                    # Build & install targets
├── README.md
├── MANIFEST.in
├── relay-config.yaml           # Example configuration
├── install-relay.sh            # Linux systemd installer
├── restart-relay.sh            # Restart helper
├── Dockerfile                  # Multi-stage Docker build
├── docker-compose.yml          # Docker Compose
├── deploy/
│   ├── opendesk-relay.service  # Systemd unit template
│   ├── opendesk-relay.default  # Default environment vars
│   └── opendesk-relay.logrotate  # Logrotate config
├── src/
│   └── relay_server/           # Python package
│       ├── __init__.py
│       ├── __main__.py         # Entry point
│       ├── server.py           # Core relay server
│       ├── protocol.py         # Message protocol
│       ├── auth.py             # Authentication
│       ├── config.py           # Configuration management
│       ├── web/
│       │   ├── app.py          # FastAPI web server
│       │   ├── api.py          # REST API endpoints
│       │   └── static/         # Dashboard UI
│       └── monitoring/
│           └── metrics.py      # Prometheus metrics
└── tests/                      # Test suite
    ├── test_protocol.py
    ├── test_auth.py
    ├── test_config.py
    └── test_server.py
```
