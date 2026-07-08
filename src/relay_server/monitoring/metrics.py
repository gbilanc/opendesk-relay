"""
Prometheus metrics and health checks for the relay server.

Exposes:
- ``/metrics`` endpoint for Prometheus scraping
- ``/health``, ``/health/ready``, ``/health/live`` endpoints
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from prometheus_client import Counter, Gauge, Histogram, generate_latest

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

# Connection counters
CONNECTIONS_TOTAL = Counter(
    "relay_connections_total",
    "Total number of TCP connections accepted",
    ["protocol"],  # "tcp" or "tls"
)

CONNECTIONS_ACTIVE = Gauge(
    "relay_connections_active",
    "Number of currently active connections",
)

# Session counters
SESSIONS_TOTAL = Counter(
    "relay_sessions_total",
    "Total number of sessions created",
)

SESSIONS_ACTIVE = Gauge(
    "relay_sessions_active",
    "Number of currently active sessions",
)

# Message counters
MESSAGES_FORWARDED = Counter(
    "relay_messages_forwarded_total",
    "Total number of messages forwarded between peers",
    ["message_type"],
)

MESSAGES_RECEIVED = Counter(
    "relay_messages_received_total",
    "Total number of messages received by the relay",
    ["message_type"],
)

# Error counters
ERRORS_TOTAL = Counter(
    "relay_errors_total",
    "Total number of protocol errors",
    ["code"],
)

PEER_TIMEOUTS = Counter(
    "relay_peer_timeout_total",
    "Total number of peer timeouts",
)

# Data transfer
BYTES_RECEIVED = Counter(
    "relay_bytes_received_total",
    "Total bytes received from peers",
)

BYTES_SENT = Counter(
    "relay_bytes_sent_total",
    "Total bytes sent to peers",
)

# Latency histogram (for PING/PONG)
LATENCY = Histogram(
    "relay_latency_seconds",
    "Round-trip latency between peers through the relay",
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)

# Device counters
DEVICES_REGISTERED = Gauge(
    "relay_devices_registered",
    "Number of known devices (online + offline)",
)

DEVICES_ONLINE = Gauge(
    "relay_devices_online",
    "Number of currently online devices",
)


# ---------------------------------------------------------------------------
# Metrics registry
# ---------------------------------------------------------------------------


@dataclass
class MetricsCollector:
    """Collects and exposes relay server metrics."""

    start_time: float = field(default_factory=time.time)
    _active_connections: int = 0
    _active_sessions: int = 0
    _devices_online: int = 0
    _errors_count: int = 0

    @property
    def uptime_seconds(self) -> float:
        """Return server uptime in seconds."""
        return time.time() - self.start_time

    def _get_gauge_value(self, gauge) -> int:
        """Safely get the value of a Gauge metric."""
        try:
            # Gauges without labels have _value
            if hasattr(gauge, '_value'):
                return int(gauge._value.get())
            # Gauges with labels store values per-label in _metrics
            if hasattr(gauge, '_metrics') and gauge._metrics:
                # Sum all label combinations
                total = 0
                for metric in gauge._metrics.values():
                    if hasattr(metric, '_value'):
                        total += int(metric._value.get())
                return total
        except Exception:
            pass
        return 0

    def on_connection(self, protocol: str = "tcp") -> None:
        """Called when a new connection is accepted."""
        CONNECTIONS_TOTAL.labels(protocol=protocol).inc()
        CONNECTIONS_ACTIVE.inc()
        self._active_connections += 1

    def on_disconnection(self) -> None:
        """Called when a connection is closed."""
        CONNECTIONS_ACTIVE.dec()
        self._active_connections -= 1

    def on_session_created(self) -> None:
        """Called when a new session is created."""
        SESSIONS_TOTAL.inc()
        SESSIONS_ACTIVE.inc()
        self._active_sessions += 1

    def on_session_closed(self) -> None:
        """Called when a session is removed."""
        SESSIONS_ACTIVE.dec()
        self._active_sessions -= 1

    def on_message_received(self, msg_type: str) -> None:
        """Called when a message is received."""
        MESSAGES_RECEIVED.labels(message_type=msg_type).inc()

    def on_message_forwarded(self, msg_type: str) -> None:
        """Called when a message is forwarded to a peer."""
        MESSAGES_FORWARDED.labels(message_type=msg_type).inc()

    def on_error(self, code: int) -> None:
        """Called when an error occurs."""
        ERRORS_TOTAL.labels(code=str(code)).inc()
        self._errors_count += 1

    def on_peer_timeout(self) -> None:
        """Called when a peer times out."""
        PEER_TIMEOUTS.inc()

    def on_bytes_received(self, n: int) -> None:
        """Called when bytes are received from a peer."""
        BYTES_RECEIVED.inc(n)

    def on_bytes_sent(self, n: int) -> None:
        """Called when bytes are sent to a peer."""
        BYTES_SENT.inc(n)

    def on_device_registered(self) -> None:
        """Called when a device registers."""
        DEVICES_REGISTERED.inc()
        DEVICES_ONLINE.inc()
        self._devices_online += 1

    def on_device_offline(self) -> None:
        """Called when a device goes offline."""
        DEVICES_ONLINE.dec()
        self._devices_online -= 1

    def gather_health(self) -> dict:
        """Gather health check data."""
        return {
            "status": "healthy",
            "uptime_seconds": self.uptime_seconds,
            "connections_active": self._active_connections,
            "sessions_active": self._active_sessions,
            "devices_online": self._devices_online,
            "errors_total": self._errors_count,
            "timestamp": time.time(),
        }


# ---------------------------------------------------------------------------
# Convenience: generate metrics endpoint output
# ---------------------------------------------------------------------------


def render_metrics() -> str:
    """Render Prometheus metrics as text."""
    return generate_latest().decode("utf-8")
