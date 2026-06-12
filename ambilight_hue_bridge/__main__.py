"""Command-line entry point for the Ambilight+Hue Pro Bridge service."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
from contextlib import suppress
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .app import BridgeApp
from .config.store import ConfigStore
from .const import (
    ADDON_OPTIONS_FILENAME,
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

# Log levels offered on the CLI / add-on options, most to least detailed. The custom VERBOSE
# level name is registered in const.py (alongside its definition).
_LOG_LEVELS = ("VERBOSE", "DEBUG", "INFO", "WARNING", "ERROR")
# Port the optional TLS listener uses when the add-on's boolean `https` option is turned on.
_HTTPS_ON_PORT = 443


@dataclass
class _Settings:
    """Resolved runtime settings (CLI > add-on options.json > env var > default)."""

    data_dir: Path
    http_port: int
    ui_port: int | None
    https_port: int
    log_level: str
    log_file: Path


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
    # Flags default to None so an explicit value can be told apart from "fall back to the
    # add-on option / env var / built-in default" (resolved in _resolve_settings).
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help=(
            f"Directory for persistent config and state (env DATA_DIR; default {DEFAULT_DATA_DIR})."
        ),
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=None,
        help=(
            f"TCP port for the Hue API (env HTTP_PORT; default {DEFAULT_HTTP_PORT}). Use 80 for "
            "older Ambilight+Hue TVs, which assume the bridge is on port 80. Also serves the web "
            "UI unless --ui-port is set."
        ),
    )
    parser.add_argument(
        "--ui-port",
        type=int,
        default=None,
        help=(
            "Optional separate TCP port for the config web UI (env UI_PORT). Defaults to sharing "
            "--http-port. As a Home Assistant add-on the UI is also served over ingress."
        ),
    )
    parser.add_argument(
        "--https-port",
        type=int,
        default=None,
        help=(
            "Optional TCP port for a TLS listener with a Hue-style cert (env HTTPS_PORT; default "
            f"{DEFAULT_HTTPS_PORT} = off). The Ambilight+Hue TVs tested so far connect over plain "
            "HTTP; set a port (e.g. 443) only if a client requires TLS."
        ),
    )
    parser.add_argument(
        "--log-level",
        type=str.upper,
        default=None,
        choices=_LOG_LEVELS,
        help="Logging verbosity (env LOG_LEVEL; default INFO). VERBOSE adds SSDP + web-UI traces.",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help=(
            "Path to a rotating log file written in addition to the console "
            f"(env LOG_FILE; default <data-dir>/{LOG_FILENAME})."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {get_version()}")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("serve", help="Run the bridge service (the default).")
    pair = subparsers.add_parser("pair", help="Pair with a real Hue bridge.")
    pair.add_argument("host", help="IP address or hostname of the real Hue bridge.")
    subparsers.add_parser("areas", help="List entertainment areas on the configured bridge.")
    return parser.parse_args(argv)


def _env(name: str) -> str | None:
    """Return an environment variable's value, treating empty/unset as None."""
    return os.environ.get(name) or None


def _coerce_int(value: object) -> int | None:
    """Coerce an int (from options.json) or numeric string (from env) to int, else None."""
    if isinstance(value, int):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _path(value: str | None) -> Path | None:
    """Wrap a non-empty string as a Path, else None."""
    return Path(value) if value else None


def _load_addon_options(data_dir: Path) -> dict[str, object]:
    """Read the Home Assistant add-on's options.json from the data dir, or {} when absent."""
    try:
        data = json.loads((data_dir / ADDON_OPTIONS_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _resolve_port(cli: int | None, option: object, env_name: str) -> int | None:
    """Resolve a port from CLI flag, then add-on option, then env var (else None)."""
    if cli is not None:
        return cli
    from_option = _coerce_int(option)
    if from_option is not None:
        return from_option
    return _coerce_int(_env(env_name))


def _resolve_https_port(args: argparse.Namespace, options: dict[str, object]) -> int:
    """Resolve the HTTPS port; the add-on exposes a boolean `https` toggle instead of a port."""
    if args.https_port is not None:
        return int(args.https_port)
    if "https" in options:
        return _HTTPS_ON_PORT if options.get("https") else 0
    env = _coerce_int(_env("HTTPS_PORT"))
    return env if env is not None else DEFAULT_HTTPS_PORT


def _valid_level(value: object) -> str | None:
    """Normalize a log-level value to a known level name, or None if absent/unrecognized."""
    if value is None:
        return None
    level = str(value).upper()
    return level if level in _LOG_LEVELS else None


def _resolve_log_level(args: argparse.Namespace, options: dict[str, object]) -> str:
    """Resolve the log level (CLI > add-on option > env > INFO), each source validated alone."""
    # Validate per source so an invalid higher-precedence value falls through to the next one
    # (matching _resolve_port), rather than snapping the whole chain to INFO.
    return (
        _valid_level(args.log_level)
        or _valid_level(options.get("log_level"))
        or _valid_level(_env("LOG_LEVEL"))
        or "INFO"
    )


def _resolve_settings(args: argparse.Namespace) -> _Settings:
    """
    Resolve runtime settings, layering CLI flags over add-on options, env vars, then defaults.

    :param args: The parsed command-line namespace (unset flags are None).
    """
    data_dir = args.data_dir or _path(_env("DATA_DIR")) or Path(DEFAULT_DATA_DIR)
    options = _load_addon_options(data_dir)
    http_port = _resolve_port(args.http_port, options.get("http_port"), "HTTP_PORT")
    return _Settings(
        data_dir=data_dir,
        http_port=DEFAULT_HTTP_PORT if http_port is None else http_port,
        ui_port=_resolve_port(args.ui_port, options.get("ui_port"), "UI_PORT"),
        https_port=_resolve_https_port(args, options),
        log_level=_resolve_log_level(args, options),
        log_file=args.log_file or _path(_env("LOG_FILE")) or (data_dir / LOG_FILENAME),
    )


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
    settings = _resolve_settings(args)
    _configure_logging(settings.log_level, settings.log_file)
    LOGGER.info("Logging to %s", settings.log_file)
    command = args.command or "serve"
    if command == "serve":
        LOGGER.info("%s %s starting", DISPLAY_NAME, get_version())
        with suppress(KeyboardInterrupt):
            asyncio.run(_serve(settings))
    elif command == "pair":
        asyncio.run(_pair(settings.data_dir, args.host))
    elif command == "areas":
        asyncio.run(_areas(settings.data_dir))


async def _serve(settings: _Settings) -> None:
    """Run the bridge service until a stop signal is received."""
    app = BridgeApp(
        settings.data_dir,
        http_port=settings.http_port,
        ui_port=settings.ui_port,
        https_port=settings.https_port,
    )
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
