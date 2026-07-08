"""
FastAPI web server for the relay dashboard.

Provides:
- Static file serving for the dashboard UI
- REST API endpoints (via ``api.py``)
- Prometheus metrics endpoint
- Health check endpoints
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from relay_server.config import Config
from relay_server.monitoring.metrics import MetricsCollector, render_metrics
from relay_server.server import RelayServer
from relay_server.web.api import router as api_router

logger = logging.getLogger(__name__)

_HERE = Path(__file__).parent
_STATIC_DIR = _HERE / "static"


def create_web_app(server: RelayServer, config: Config) -> FastAPI:
    """Create and configure the FastAPI web application.

    Parameters
    ----------
    server
        The relay server instance to monitor.
    config
        The server configuration.

    Returns
    -------
    FastAPI
    """
    app = FastAPI(
        title="OpenDesk Relay Server Dashboard",
        version=__import__("relay_server", fromlist=["__version__"]).__version__,
        description="Web dashboard and REST API for monitoring the OpenDesk relay server",
    )

    # Store references in app.state
    app.state.server = server
    app.state.config = config

    # ── Mount static files ──────────────────────────────────────────
    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # ── REST API ────────────────────────────────────────────────────
    app.include_router(api_router, prefix="/api")

    # ── Prometheus metrics ──────────────────────────────────────────
    @app.get("/metrics", include_in_schema=False)
    async def metrics_endpoint() -> PlainTextResponse:
        """Prometheus metrics endpoint."""
        return PlainTextResponse(render_metrics())

    # ── Health checks ───────────────────────────────────────────────
    @app.get("/health", include_in_schema=False)
    async def health_basic() -> dict:
        """Basic health check — always returns 200 if the server is running."""
        return {"status": "healthy"}

    @app.get("/health/ready", include_in_schema=False)
    async def health_ready() -> dict:
        """Readiness probe — checks if the relay is accepting connections."""
        collector: MetricsCollector = server.metrics
        return collector.gather_health()

    @app.get("/health/live", include_in_schema=False)
    async def health_live() -> dict:
        """Liveness probe — returns 200 if the server is alive."""
        return {"status": "alive", "uptime_seconds": server.metrics.uptime_seconds}

    # ── Dashboard HTML ──────────────────────────────────────────────
    dashboard_html = _STATIC_DIR / "index.html"
    if dashboard_html.exists():
        @app.get("/", include_in_schema=False)
        async def dashboard() -> HTMLResponse:
            """Serve the dashboard UI."""
            content = dashboard_html.read_text()
            return HTMLResponse(content)

    return app


async def run_web_server(server: RelayServer, config: Config) -> uvicorn.Server:
    """Start the web dashboard server as an asyncio task.

    Parameters
    ----------
    server
        The relay server instance.
    config
        The server configuration.

    Returns
    -------
    uvicorn.Server
        The uvicorn server instance (already started).
    """
    app = create_web_app(server, config)

    cfg = uvicorn.Config(
        app,
        host=config.admin.web_host,
        port=config.admin.web_port,
        log_level="warning",
        access_log=False,
    )
    uvicorn_server = uvicorn.Server(cfg)

    logger.info(
        "Web dashboard starting on http://%s:%d",
        config.admin.web_host,
        config.admin.web_port,
    )

    # Start uvicorn in a separate task
    asyncio.create_task(uvicorn_server.serve())

    return uvicorn_server
