"""Command-line entry point for the Ambilight+Hue Pro Bridge service."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from contextlib import suppress
from importlib.metadata import PackageNotFoundError, version
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .app import BridgeApp
from .config.store import ConfigStore
from .const import (
    CONFIG_FILENAME,
    DEFAULT_DATA_DIR,
    DEFAULT_HTTP_PORT,
    DEFAULT_HTTPS_PORT,
    DISPLAY_NAME,
    LOG_FILENAME,
    PACKAGE_NAME,
)
from .outbound.controller import active_bridge, list_areas, pair_and_store

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
        default=DEFAULT_HTTP_PORT,
        help=(
            f"TCP port for the Hue API + web UI (default: {DEFAULT_HTTP_PORT}). "
            "Use 80 for older Ambilight+Hue TVs, which assume the bridge is on port 80."
        ),
    )
    parser.add_argument(
        "--https-port",
        type=int,
        default=DEFAULT_HTTPS_PORT,
        help=(
            "Optional TCP port for a TLS listener with a Hue-style cert "
            f"(default: {DEFAULT_HTTPS_PORT} = off). The Ambilight+Hue TVs tested so far connect "
            "over plain HTTP; set a port (e.g. 443) only if a client requires TLS."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help=(
            "Path to a rotating log file written in addition to the console "
            f"(default: <data-dir>/{LOG_FILENAME})."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {get_version()}")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("serve", help="Run the bridge service (the default).")
    pair = subparsers.add_parser("pair", help="Pair with a real Hue bridge.")
    pair.add_argument("host", help="IP address or hostname of the real Hue bridge.")
    subparsers.add_parser("areas", help="List entertainment areas on the configured bridge.")
    return parser.parse_args(argv)


def _configure_logging(level: str, log_path: Path) -> None:
    """
    Send logs to the console and a rotating file.

    :param level: Logging level name (e.g. ``INFO``).
    :param log_path: Path to the rotating log file (its directory is created if needed).
    """
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=3)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


def main(argv: list[str] | None = None) -> None:
    """Run the console-script entry point."""
    args = parse_args(argv)
    log_path = args.log_file or (args.data_dir / LOG_FILENAME)
    _configure_logging(args.log_level, log_path)
    LOGGER.info("Logging to %s", log_path)
    command = args.command or "serve"
    if command == "serve":
        LOGGER.info("%s %s starting", DISPLAY_NAME, get_version())
        with suppress(KeyboardInterrupt):
            asyncio.run(_serve(args.data_dir, args.http_port, args.https_port))
    elif command == "pair":
        asyncio.run(_pair(args.data_dir, args.host))
    elif command == "areas":
        asyncio.run(_areas(args.data_dir))


async def _serve(data_dir: Path, http_port: int, https_port: int) -> None:
    """Run the bridge service until a stop signal is received."""
    app = BridgeApp(data_dir, http_port=http_port, https_port=https_port)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, app.request_stop)
    await app.run()


async def _pair(data_dir: Path, host: str) -> None:
    """Pair with a real Hue bridge and store the credentials in the config."""
    print(f"Press the link button on the Hue bridge at {host} (waiting up to 30s)...")
    store = ConfigStore(data_dir / CONFIG_FILENAME)
    store.load()
    bridge = await pair_and_store(store, host)
    print(f"Paired bridge '{bridge.id}' at {host}.")
    print("Next: open the web UI and assign each paired TV an entertainment area.")


async def _areas(data_dir: Path) -> None:
    """List the entertainment areas (and their channels) on the configured bridge."""
    store = ConfigStore(data_dir / CONFIG_FILENAME)
    store.load()
    bridge = active_bridge(store)
    if bridge is None:
        print("No paired bridge configured. Run 'pair <host>' first.")
        return
    areas = await list_areas(bridge.host, bridge.app_key)
    if not areas:
        print("No entertainment areas found on the bridge.")
        return
    for area in areas:
        print(f"Area {area.id}  '{area.name}'  ({len(area.channels)} channels)")
        for channel in area.channels:
            print(f"    channel {channel.channel_id}: {channel.service_id}  pos={channel.position}")


if __name__ == "__main__":
    main()
