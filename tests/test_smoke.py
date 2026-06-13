"""Smoke tests to verify the package and entry point import cleanly."""

from __future__ import annotations

import ambilight_hue_bridge
from ambilight_hue_bridge.__main__ import _resolve_settings, get_version, main, parse_args
from ambilight_hue_bridge.const import DEFAULT_HTTP_PORT, DEFAULT_HTTPS_PORT


def test_package_imports() -> None:
    """The top-level package imports without side effects."""
    assert ambilight_hue_bridge.__doc__


def test_version_is_a_string() -> None:
    """A version string is always returned, even when not installed."""
    assert isinstance(get_version(), str)


def test_unset_flags_default_to_none() -> None:
    """Flags default to None sentinels so _resolve_settings can layer the other sources."""
    args = parse_args([])
    assert args.http_port is None
    assert args.https_port is None
    assert args.log_level is None


def test_resolved_defaults(tmp_path, monkeypatch) -> None:
    """With no flags, env, or options.json, settings fall back to the built-in defaults."""
    for var in ("DATA_DIR", "HTTP_PORT", "HTTPS_PORT", "LOG_LEVEL", "LOG_FILE"):
        monkeypatch.delenv(var, raising=False)
    settings = _resolve_settings(parse_args(["--data-dir", str(tmp_path)]))
    assert settings.http_port == DEFAULT_HTTP_PORT
    assert settings.https_port == DEFAULT_HTTPS_PORT
    assert settings.log_level == "INFO"


def test_main_is_callable() -> None:
    """The console-script entry point is callable."""
    assert callable(main)
