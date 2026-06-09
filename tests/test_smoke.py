"""Smoke tests to verify the package and entry point import cleanly."""

from __future__ import annotations

import ambilight_hue_bridge
from ambilight_hue_bridge.__main__ import get_version, main, parse_args


def test_package_imports() -> None:
    """The top-level package imports without side effects."""
    assert ambilight_hue_bridge.__doc__


def test_version_is_a_string() -> None:
    """A version string is always returned, even when not installed."""
    assert isinstance(get_version(), str)


def test_argument_parsing_defaults() -> None:
    """The CLI parser exposes the expected defaults."""
    args = parse_args([])
    assert args.web_port > 0
    assert args.log_level == "INFO"


def test_main_is_callable() -> None:
    """The console-script entry point is callable."""
    assert callable(main)
