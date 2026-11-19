#!/usr/bin/env python3
"""Probe applicativo del relay: verifica che un client OpenDesk venga servito.

Esegue sull'host remoto la sequenza reale di handshake:
  PING -> attende PONG
  RELAY_REGISTER (device) -> attende conferma/sessione
  RELAY_DEVICE_LIST -> (il server non risponde se non accoppiato: non bloccante)

Uso: deploy/probe-relay.py [--tls] [--insecure] [host] [port]
     es. deploy/probe-relay.py relay.gibisoft.net 8474
         deploy/probe-relay.py --tls relay.gibisoft.net 8474

--tls       la porta parla TLS (server.tls_cert/tls_key valorizzati)
--insecure  non verificare il certificato (solo per self-signed)

Exit: 0 = handshake ok, 1 = il relay non risponde correttamente.
"""
import asyncio
import ssl
import struct
import sys
import time
import uuid

import msgpack

PING, PONG = 0x70, 0x71
RELAY_REGISTER, RELAY_DEVICE_LIST = 0x80, 0x83


def enc(t, p):
    body = msgpack.packb({"t": t, "p": p})
    return struct.pack("!I", len(body)) + body


async def read_msg(reader, timeout=6.0):
    hdr = await asyncio.wait_for(reader.readexactly(4), timeout)
    (n,) = struct.unpack("!I", hdr)
    if n > 10 * 1024 * 1024:
        raise ValueError(f"frame troppo grande: {n}")
    body = await asyncio.wait_for(reader.readexactly(n), timeout)
    m = msgpack.unpackb(body, raw=False)
    return m.get("t"), m.get("p")


async def main(host, port, use_tls=False, insecure=False):
    t0 = time.time()
    ssl_ctx = None
    if use_tls:
        ssl_ctx = ssl.create_default_context()
        if insecure:
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
    reader, writer = await asyncio.open_connection(host, port, ssl=ssl_ctx)
    peer = writer.get_extra_info("peername")
    proto = "TLS" if use_tls else "TCP"
    print(f"✓ {proto} connesso a {host}:{port} (peer visto dal server: {peer})")

    ok = True

    # 1) PING / PONG
    writer.write(enc(PING, {"seq": 7}))
    await writer.drain()
    try:
        t, p = await read_msg(reader)
        print(f"✓ PONG ricevuto: type=0x{t:02x} payload={p}" if t == PONG else f"✗ risposta inattesa a PING: 0x{t:02x} {p}")
        ok &= t == PONG
    except asyncio.TimeoutError:
        print("✗ nessuna risposta al PING (timeout)")
        ok = False

    # 2) registrazione device
    did = f"probe-{uuid.uuid4().hex[:8]}"
    writer.write(enc(RELAY_REGISTER, {"device_id": did, "device_name": "connectivity-probe"}))
    await writer.drain()
    resp = []
    try:
        for _ in range(3):
            t, p = await read_msg(reader, timeout=3.0)
            resp.append((t, p))
    except (asyncio.TimeoutError, asyncio.IncompleteReadError):
        pass
    print(f"✓ RELAY_REGISTER inviato (device_id={did}); risposte: " +
          (", ".join(f"0x{t:02x} {p}" for t, p in resp) if resp else "nessuna (ok: server resta in attesa)"))

    # 3) elenco device
    writer.write(enc(RELAY_DEVICE_LIST, {}))
    await writer.drain()
    try:
        t, p = await read_msg(reader, timeout=4.0)
        print(f"✓ RELAY_DEVICE_LIST → 0x{t:02x} {p}")
    except asyncio.TimeoutError:
        print("· RELAY_DEVICE_LIST senza risposta (non gestito lato server: non bloccante)")

    writer.write(enc(0x72, {"reason": "probe done"}))  # DISCONNECT
    await writer.drain()
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    print(f"✓ chiusura pulita — totale {time.time() - t0:.2f}s")
    return 0 if ok else 1


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    h = args[0] if len(args) > 0 else "relay.gibisoft.net"
    p = int(args[1]) if len(args) > 1 else 8474
    # --tls: il relay ha TLS abilitato (la porta parla TLS, non TCP in chiaro).
    # --insecure: non verificare il certificato (solo per test con self-signed).
    raise SystemExit(
        asyncio.run(
            main(h, p, use_tls="--tls" in flags, insecure="--insecure" in flags)
        )
    )
