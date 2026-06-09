"""Command-line entry point for the Ambilight+Hue Pro Bridge service."""

from __future__ import annotations

import argparse
import logging
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .const import DEFAULT_DATA_DIR, DEFAULT_WEB_PORT, DISPLAY_NAME, PACKAGE_NAME

LOGGER = logging.getLogger(PACKAGE_NAME)


def get_version() -> str:
    """Return the installed package version, or a dev placeholder."""
    try:
        return version("ambilight-hue-pro-bridge")
    except PackageNotFoundError:
        return "0.0.0.dev0"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(prog="ambilight-hue-bridge", description=DISPLAY_NAME)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(DEFAULT_DATA_DIR),
        help="Directory for persistent configuration and state.",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=DEFAULT_WEB_PORT,
        help="TCP port for the web configuration interface.",
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


def main(argv: list[str] | None = None) -> None:
    """Run the console-script entry point."""
    args = parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    args.data_dir.mkdir(parents=True, exist_ok=True)
    LOGGER.info("%s %s starting", DISPLAY_NAME, get_version())
    LOGGER.info("Data directory: %s | web port: %d", args.data_dir.resolve(), args.web_port)
    # TODO: wire up the service core once the architecture is finalized:
    #   - virtual Hue bridge (SSDP/UPnP responder + legacy v1 REST emulator)
    #   - web configuration UI
    #   - Hue Entertainment streaming client (DTLS/PSK) to the real bridge
    LOGGER.warning(
        "Service core not implemented yet — this is a project skeleton. "
        "Nothing is listening; the bridge components will be wired up next.",
    )


if __name__ == "__main__":
    main()
