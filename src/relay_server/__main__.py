"""Entry point for ``python -m relay_server`` and the ``relay-server`` CLI script."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import logging
import signal
import sys

from relay_server.config import Config, load_config
from relay_server.server import RelayServer
from relay_server.web.app import create_web_app


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="OpenDesk Relay Server — Standalone fallback connectivity",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  relay-server                              # default :8474\n"
            "  relay-server --port 9443                   # custom port\n"
            "  relay-server --config ./my-config.yaml     # config file\n"
            "  relay-server --admin-port 8484 --debug     # with dashboard\n"
            "  relay-server --no-admin                    # disable dashboard\n"
        ),
    )
    parser.add_argument(
        "--config", "-c",
        default=None,
        help="Path to YAML config file (default: relay-config.yaml, then ~/.opendesk/relay-config.yaml)",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Bind address for relay (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=None,
        help="TCP port for relay (default: 8474)",
    )
    parser.add_argument(
        "--admin-host",
        default=None,
        help="Bind address for web dashboard (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--admin-port",
        type=int,
        default=None,
        help="Port for web dashboard (default: 8484)",
    )
    parser.add_argument(
        "--no-admin",
        action="store_true",
        default=None,
        help="Disable the web dashboard",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=None,
        help="Enable debug logging",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Start the relay server."""
    args = _parse_args(argv)

    # Load configuration (CLI > env > file)
    config = load_config(args.config)

    # CLI overrides
    overrides = {}
    if args.host is not None:
        overrides["server"] = {**dataclasses.asdict(config.server), "host": args.host}
    if args.port is not None:
        overrides["server"] = {**dataclasses.asdict(config.server), "port": args.port}
    if args.admin_host is not None:
        overrides["admin"] = {**dataclasses.asdict(config.admin), "web_host": args.admin_host}
    if args.admin_port is not None:
        overrides["admin"] = {**dataclasses.asdict(config.admin), "web_port": args.admin_port}
    if args.no_admin is True:
        overrides["admin"] = {**dataclasses.asdict(config.admin), "enabled": False}
    if args.debug is True:
        overrides["logging"] = {**dataclasses.asdict(config.logging), "level": "DEBUG"}

    if overrides:
        config = config.merge(overrides)

    # Configure logging
    config.configure_logging()

    logger = logging.getLogger(__name__)
    logger.info(
        "Starting OpenDesk Relay Server v%s",
        __import__("relay_server", fromlist=["__version__"]).__version__,
    )

    async def _run() -> None:
        server = RelayServer(config=config)

        # Register the API token from config so Bearer auth works
        # (RelayAuth only loads tokens from credentials.json otherwise).
        if config.admin.api_token:
            server.auth.add_api_token(config.admin.api_token)

        # Start web dashboard if enabled
        if config.admin.enabled:
            web_app = create_web_app(server, config)
            server.web_app = web_app

        # ── Signal handling ──
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()
        stop_requested = False

        def _request_stop(signame: str) -> None:
            nonlocal stop_requested
            if stop_requested:
                # uvicorn installa i propri handler sui medesimi segnali:
                # senza questa guardia lo stop verrebbe loggato/eseguito
                # due volte.
                return
            stop_requested = True
            logger.info("Received %s, shutting down", signame)
            stop_event.set()

        def _reload_tls() -> None:
            logger.info("Received SIGHUP, reloading TLS certificate")
            asyncio.create_task(server.reload_tls())

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _request_stop, sig.name)
            except NotImplementedError:  # pragma: no cover - non-POSIX
                pass
        try:
            # systemctl reload / kill -HUP → swap certificate without restart
            loop.add_signal_handler(signal.SIGHUP, _reload_tls)
        except (AttributeError, NotImplementedError):  # pragma: no cover
            pass

        server_task = asyncio.create_task(server.start(), name="relay-server")
        stop_task = asyncio.create_task(stop_event.wait(), name="relay-stop")
        try:
            await asyncio.wait(
                {server_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            stop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await server.stop()
            server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await server_task

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("Shutdown requested")
    except Exception as e:
        logger.exception("Fatal error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
