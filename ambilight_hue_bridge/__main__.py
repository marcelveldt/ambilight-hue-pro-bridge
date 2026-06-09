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
from .config.models import RealBridge
from .config.store import ConfigStore
from .const import CONFIG_FILENAME, DEFAULT_DATA_DIR, DISPLAY_NAME, PACKAGE_NAME
from .outbound.controller import list_areas, pair_bridge

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
    parser.add_argument("--version", action="version", version=f"%(prog)s {get_version()}")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("serve", help="Run the bridge service (the default).")
    pair = subparsers.add_parser("pair", help="Pair with a real Hue bridge.")
    pair.add_argument("host", help="IP address or hostname of the real Hue bridge.")
    subparsers.add_parser("areas", help="List entertainment areas on the configured bridge.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Run the console-script entry point."""
    args = parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    command = args.command or "serve"
    if command == "serve":
        LOGGER.info("%s %s starting", DISPLAY_NAME, get_version())
        with suppress(KeyboardInterrupt):
            asyncio.run(_serve(args.data_dir, args.http_port))
    elif command == "pair":
        asyncio.run(_pair(args.data_dir, args.host))
    elif command == "areas":
        asyncio.run(_areas(args.data_dir))


async def _serve(data_dir: Path, http_port: int | None) -> None:
    """Run the bridge service until a stop signal is received."""
    app = BridgeApp(data_dir, http_port=http_port)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, app.request_stop)
    await app.run()


async def _pair(data_dir: Path, host: str) -> None:
    """Pair with a real Hue bridge and store the credentials in the config."""
    print(f"Press the link button on the Hue bridge at {host} (waiting up to 30s)...")
    creds = await pair_bridge(host)
    store = ConfigStore(data_dir / CONFIG_FILENAME)
    store.load()
    bridge = _upsert_bridge(store, host, creds)
    store.config.active_real_bridge = bridge.id
    store.save()
    config_path = data_dir / CONFIG_FILENAME
    print(f"Paired bridge '{bridge.id}' at {host}.")
    print(f"Next: run 'areas', then edit 'entertainment_area' and 'channels' in {config_path}.")


async def _areas(data_dir: Path) -> None:
    """List the entertainment areas (and their channels) on the configured bridge."""
    store = ConfigStore(data_dir / CONFIG_FILENAME)
    store.load()
    bridge = _active_bridge(store)
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


def _upsert_bridge(store: ConfigStore, host: str, creds: dict[str, str]) -> RealBridge:
    """Add or update a real bridge entry with freshly paired credentials."""
    for bridge in store.config.real_bridges:
        if bridge.host == host:
            bridge.app_key = creds["username"]
            bridge.client_key = creds["clientkey"]
            return bridge
    bridge = RealBridge(
        id=host.replace(".", "-"),
        host=host,
        app_key=creds["username"],
        client_key=creds["clientkey"],
    )
    store.config.real_bridges.append(bridge)
    return bridge


def _active_bridge(store: ConfigStore) -> RealBridge | None:
    """Return the configured active real bridge (or the first one)."""
    bridges = store.config.real_bridges
    active = store.config.active_real_bridge
    if active:
        for bridge in bridges:
            if bridge.id == active:
                return bridge
    return bridges[0] if bridges else None


if __name__ == "__main__":
    main()
