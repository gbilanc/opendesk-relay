"""Standalone relay server for OpenDesk.

Provides TCP fallback connectivity when direct P2P (WebRTC) is unavailable.
Includes a web dashboard for monitoring, structured logging, and Prometheus metrics.

Usage:
    relay-server --port 8474
    relay-server --config /path/to/config.yaml

Or via Python:
    python -m relay_server --port 8474
"""

from __future__ import annotations

__version__ = "0.2.0"
