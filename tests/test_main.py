"""Tests for settings resolution (CLI > add-on options.json > env > defaults) and log levels."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import pytest

from ambilight_hue_bridge.__main__ import _resolve_settings, parse_args
from ambilight_hue_bridge.const import VERBOSE

if TYPE_CHECKING:
    from pathlib import Path

    from ambilight_hue_bridge.__main__ import _Settings

_ENV_VARS = ("DATA_DIR", "HTTP_PORT", "HTTPS_PORT", "LOG_LEVEL", "LOG_FILE")


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch) -> None:
    """Isolate each test from any of our env vars set in the real environment."""
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _settings(tmp_path: Path, argv: list[str]) -> _Settings:
    """Resolve settings for the given argv, rooted at an empty temp data dir."""
    return _resolve_settings(parse_args(["--data-dir", str(tmp_path), *argv]))


def _write_options(tmp_path: Path, options: dict) -> None:
    """Write a Home Assistant-style options.json into the data dir."""
    (tmp_path / "options.json").write_text(json.dumps(options), encoding="utf-8")


def test_verbose_level_registered() -> None:
    """The custom VERBOSE level is registered below DEBUG and resolves by name."""
    assert VERBOSE < logging.DEBUG
    assert logging.getLevelName("VERBOSE") == VERBOSE


def test_env_vars_apply(tmp_path: Path, monkeypatch) -> None:
    """Env vars fill in settings when no flag/option is given."""
    monkeypatch.setenv("HTTP_PORT", "9001")
    monkeypatch.setenv("LOG_LEVEL", "verbose")
    settings = _settings(tmp_path, [])
    assert settings.http_port == 9001
    assert settings.log_level == "VERBOSE"


def test_cli_beats_env(tmp_path: Path, monkeypatch) -> None:
    """An explicit flag wins over the env var."""
    monkeypatch.setenv("HTTP_PORT", "9001")
    settings = _settings(tmp_path, ["--http-port", "7000"])
    assert settings.http_port == 7000


def test_addon_options_beat_env_but_lose_to_cli(tmp_path: Path, monkeypatch) -> None:
    """options.json overrides env vars; an explicit flag still wins over options.json."""
    monkeypatch.setenv("HTTP_PORT", "9001")
    _write_options(tmp_path, {"http_port": 8088, "log_level": "debug"})
    assert _settings(tmp_path, []).http_port == 8088
    assert _settings(tmp_path, []).log_level == "DEBUG"
    assert _settings(tmp_path, ["--http-port", "7000"]).http_port == 7000


def test_addon_https_toggle(tmp_path: Path) -> None:
    """The add-on's boolean `https` option maps to the TLS port (443) or off (0)."""
    _write_options(tmp_path, {"https": True})
    assert _settings(tmp_path, []).https_port == 443
    _write_options(tmp_path, {"https": False})
    assert _settings(tmp_path, []).https_port == 0


def test_invalid_log_level_falls_back_to_info(tmp_path: Path, monkeypatch) -> None:
    """An unrecognized env log level degrades to INFO rather than crashing logging setup."""
    monkeypatch.setenv("LOG_LEVEL", "bogus")
    assert _settings(tmp_path, []).log_level == "INFO"


def test_invalid_option_log_level_falls_through_to_valid_env(tmp_path: Path, monkeypatch) -> None:
    """An invalid higher-precedence level falls through to a valid lower-precedence source."""
    monkeypatch.setenv("LOG_LEVEL", "debug")
    _write_options(tmp_path, {"log_level": "bogus"})
    assert _settings(tmp_path, []).log_level == "DEBUG"
