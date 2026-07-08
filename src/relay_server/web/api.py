"""
REST API endpoints for the relay server dashboard.

Authentication
--------------
- Dashboard web UI uses HTTP Basic Auth (admin credentials from config)
- Programmatic API uses Bearer token (api_token from config)
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBasic, HTTPBasicCredentials, HTTPBearer

from relay_server.monitoring.metrics import MetricsCollector, render_metrics
from relay_server.server import RelayServer

# ---------------------------------------------------------------------------
# Auth schemes
# ---------------------------------------------------------------------------

_basic = HTTPBasic(auto_error=False)
_bearer = HTTPBearer(auto_error=False)

router = APIRouter()


def _check_auth(
    request: Request,
    basic: HTTPBasicCredentials | None = Depends(_basic),
    bearer: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    """Check authentication for API requests.

    Accepts either:
    - Basic auth with admin credentials (for web dashboard)
    - Bearer token with configured API token (for programmatic access)
    """
    server: RelayServer = request.app.state.server
    config = server.config

    # If auth is disabled, allow all
    if not config.auth.enabled:
        return

    # Try bearer token first
    if bearer is not None:
        token = bearer.credentials
        if server.auth.verify_api_token(token):
            return
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API token")

    # Try basic auth
    if basic is not None:
        username = basic.username
        password = basic.password
        if username == config.admin.username and config.admin.password_hash:
            from relay_server.auth import verify_password
            if verify_password(password, config.admin.password_hash):
                return
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    # No auth provided
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": "Basic realm=\"relay-server\", Bearer"},
    )


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _get_server(request: Request) -> RelayServer:
    return request.app.state.server


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/status", summary="Server status")
async def get_status(
    request: Request,
    _auth: None = Depends(_check_auth),
) -> dict[str, Any]:
    """Get server status information."""
    server = _get_server(request)
    collector: MetricsCollector = server.metrics

    return {
        "version": __import__("relay_server", fromlist=["__version__"]).__version__,
        "uptime_seconds": collector.uptime_seconds,
        "connections_active": len(server._peers),
        "sessions_active": len(server._sessions),
        "devices_online": len(server._devices),
        "config": {
            "relay_host": server.config.server.host,
            "relay_port": server.config.server.port,
            "admin_enabled": server.config.admin.enabled,
            "auth_enabled": server.config.auth.enabled,
        },
        "timestamp": time.time(),
    }


@router.get("/peers", summary="List connected peers")
async def get_peers(
    request: Request,
    _auth: None = Depends(_check_auth),
) -> list[dict[str, Any]]:
    """Get list of currently connected peers."""
    server = _get_server(request)
    now = time.time()
    peers = []
    for pid, peer in server._peers.items():
        peers.append({
            "peer_id": pid,
            "device_id": peer.device_id or "",
            "device_name": peer.device_name or "",
            "session_id": peer.session_id,
            "paired_with": peer.paired_peer_id or "",
            "authenticated": peer.authenticated,
            "connected_seconds": round(now - peer.last_activity),
            "address": str(peer.writer.get_extra_info("peername", ("?", 0))),
        })
    return peers


@router.get("/sessions", summary="List active sessions")
async def get_sessions(
    request: Request,
    _auth: None = Depends(_check_auth),
) -> list[dict[str, Any]]:
    """Get list of active sessions."""
    server = _get_server(request)
    sessions = []
    for sid, host_id in server._sessions.items():
        host_peer = server._peers.get(host_id)
        sessions.append({
            "session_id": sid,
            "host_peer_id": host_id,
            "host_device_name": host_peer.device_name if host_peer else "?",
            "host_online": host_peer is not None,
        })
    return sessions


@router.get("/devices", summary="List registered devices")
async def get_devices(
    request: Request,
    _auth: None = Depends(_check_auth),
) -> list[dict[str, Any]]:
    """Get list of known devices (online + offline)."""
    server = _get_server(request)
    now = time.time()
    devices = []
    for did, peer in server._devices.items():
        online = peer.writer is not None and not peer.writer.is_closing()
        devices.append({
            "device_id": did,
            "device_name": peer.device_name or did[:8],
            "session_id": peer.session_id,
            "online": online,
            "last_seen_seconds": round(now - peer.last_activity) if online else -1,
        })
    return devices


@router.get("/metrics", summary="Detailed metrics")
async def get_metrics(
    request: Request,
    _auth: None = Depends(_check_auth),
) -> dict[str, Any]:
    """Get detailed server metrics."""
    server = _get_server(request)
    collector: MetricsCollector = server.metrics
    health = collector.gather_health()

    # Add per-peer details
    peer_count = len(server._peers)
    session_count = len(server._sessions)

    return {
        **health,
        "total_peers": peer_count,
        "total_sessions": session_count,
        "prometheus_endpoint": "/metrics",
    }


@router.delete("/peers/{peer_id}", summary="Disconnect a peer")
async def disconnect_peer(
    peer_id: str,
    request: Request,
    _auth: None = Depends(_check_auth),
) -> dict[str, Any]:
    """Disconnect a specific peer by ID."""
    server = _get_server(request)

    peer = server._peers.get(peer_id)
    if peer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Peer {peer_id} not found")

    try:
        peer.writer.close()
    except Exception:
        pass
    server._remove_peer(peer_id)

    return {"status": "ok", "message": f"Peer {peer_id} disconnected"}


@router.post("/config/reload", summary="Reload configuration")
async def reload_config(
    request: Request,
    _auth: None = Depends(_check_auth),
) -> dict[str, Any]:
    """Reload server configuration from file (not yet implemented)."""
    # TODO: implement hot-reload
    return {"status": "ok", "message": "Configuration reload triggered"}
