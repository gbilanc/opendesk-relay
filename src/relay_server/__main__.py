"""Entry point for ``python -m relay_server`` and the ``relay-server`` CLI script."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
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

        # Start web dashboard if enabled
        if config.admin.enabled:
            web_app = create_web_app(server, config)
            server.web_app = web_app

        await server.start()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("Shutdown requested")
    except Exception as e:
        logger.exception("Fatal error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
