"""Command-line entry point for the Ambilight+Hue Pro Bridge service."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from contextlib import suppress
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .app import BridgeApp
from .const import DEFAULT_DATA_DIR, DISPLAY_NAME, PACKAGE_NAME

LOGGER = logging.getLogger(PACKAGE_NAME)


def get_version() -> str:
    """Return the installed package version, or a dev placeholder."""
    try:
        return version("ambilight-hue-pro-bridge")
    except PackageNotFoundError:
        return "0.0.0.dev0"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """
    Parse command-line arguments.

    :param argv: Optional argument list (defaults to ``sys.argv``).
    """
    parser = argparse.ArgumentParser(prog="ambilight-hue-bridge", description=DISPLAY_NAME)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(DEFAULT_DATA_DIR),
        help="Directory for persistent configuration and state.",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=None,
        help="Override the virtual bridge HTTP port (default: from config, 80).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {get_version()}",
    )
    return parser.parse_args(argv)


async def _serve(args: argparse.Namespace) -> None:
    """Run the bridge service until a stop signal is received."""
    app = BridgeApp(args.data_dir, http_port=args.http_port)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, app.request_stop)
    await app.run()


def main(argv: list[str] | None = None) -> None:
    """Run the console-script entry point."""
    args = parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    LOGGER.info("%s %s starting", DISPLAY_NAME, get_version())
    try:
        asyncio.run(_serve(args))
    except KeyboardInterrupt:
        LOGGER.info("Shutting down")


if __name__ == "__main__":
    main()
